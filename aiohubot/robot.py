import re
import sys
from os import environ
from logging import getLogger, StreamHandler, Formatter
from pathlib import Path
from asyncio import get_event_loop, iscoroutine
from importlib import import_module, util as import_util

from pyee import AsyncIOEventEmitter
from aiohttp import web

from .core import Response, Middleware, Brain, Listener
from .plugins import (EnterMessage, LeaveMessage, TopicMessage, CatchAllMessage,
                      TextListener)

DEFAULT_ADAPTERS = ["shell"]
HUBOT_DOCUMENTATION_SECTIONS = ('description', 'dependencies', 'configuration',
                                'commands', 'notes', 'author', 'authors',
                                'examples', 'tags', 'urls')


def get_logger(level="INFO"):
    fmt = Formatter("[%(asctime)s] [%(module)s.%(funcName)s] [%(levelname)s]::"
                    " %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S %z")
    ch = StreamHandler()
    ch.setLevel("DEBUG")
    ch.setFormatter(fmt)

    log = getLogger("aiohubot")
    log.addHandler(ch)
    try:
        log.setLevel(level.upper())
    except ValueError:
        log.setLevel("INFO")
        log.error(f"Fail to set log level with errors", exc_info=True)

    return log


class Robot:
    ''' Robots receive messages from a chat source, and dispatch them to
    matching listeners.

    :param adapter: A string that will be passed to `load_adapter` to use.
    :param httpd: Whether to enable the HTTP daemon, disabled by default.
    :param name: A string of the robot name, defaults to Hubot.
    :param alias: A string of the alias of the robot name.
    '''

    def __init__(self, adapter, httpd=False, name="Hubot", alias="", loop=None):
        self.datastore = self.adapter = None
        self.commands, self.listeners, self.error_handlers = [], [], []
        self.listener_middleware = Middleware(self)
        self.response_middleware = Middleware(self)
        self.receive_middleware = Middleware(self)
        self.logger = get_logger(environ.get("HUBOT_LOG_LEVEL", "info"))

        self.name = name
        self.alias = alias
        self._loop = loop or get_event_loop()

        # used in httprouter
        self.server = self.router = self.ping_interval_id = None
        if httpd:
            self.setup_httprouter()  # was setupExpress()
        else:
            self.setup_nullrouter()

        self.adapter_name = self.load_adapter(adapter)
        #: delegate to adapter.send
        self.send = self.adapter.send
        #: delegate to adapter.reply
        self.reply = self.adapter.reply

        self.events = AsyncIOEventEmitter()
        #: delegate to events.on
        self.on = self.events.on
        #: delegate to events.emit
        self.emit = self.events.emit
        self.brain = Brain(self)
        self.on("error", self.invoke_error_handlers)

    def __repr__(self):
        name = self.__class__.__name__
        return (f"<{self.__module__}.{name}"
                f" adapter={self.adapter_name!r}"
                f" name={self.name!r} alias={self.alias!r}"
                f" at 0x{id(self):08X}>")

    def listen(self, matcher, handler, **options):
        """ Adds a custom Listener with the provided matcher, handler and other
        options.

        :param matcher: A function returns a truthy value to determines whether
            to call the handler.
        :param handler: A (async) function is called with a Response object if
            the matcher function returns true.
        :param options: additional parameters keyed on extension name.
        """
        self.listeners.append(Listener(self, matcher, handler, **options))

    def hear(self, regex, handler, *, flags=0, **options):
        """ Adds a Listener that attempts to match incoming messages based on a
        regular expression.

        :param regex: A Regex that determines if the handler should be called.
        :param handler: A (async) function is called with a Response object.
        :param options: additional parameters keyed on extension name.
        """
        regex = re.compile(regex)
        pattern, oflags = regex.pattern, regex.flags
        regexp = re.compile(pattern, oflags | flags)
        listener = TextListener(self, regexp, handler, **options)
        self.listeners.append(listener)

    def respond(self, regex, handler, *, flags=0, **options):
        """ Adds a Listener that attempts to match incoming messages directed
        at the robot based on a Regex.  All regexes treat patterns like they
        begin with a '^'

        :param regex: A Regex that determines if the handler should be called.
        :param handler: A (async) function is called with a Response object.
        :param flags: The constants passed to `re.compile`.
        :param options: additional parameters keyed on extension name.
        """
        self.hear(self.respond_pattern(regex, flags), handler, **options)

    def respond_pattern(self, pattern, flags=0):
        """ Build a regular expression that matches messages addressed directly
        to the robot.

        :param pattern: A string of regex for the message part that follows the
            robot's name/alias.
        :param flags: The constants passed to `re.compile`.
        """
        regex = re.compile(pattern)
        pattern, oflags = regex.pattern, regex.flags
        escape = re.compile(r"[-[\]{}()*+?.,\\^$|#\s]")
        name = escape.sub("\\$&", self.name)
        if pattern.startswith("^"):
            self.logger.warning("Anchors don't work well with respond, "
                                "perhaps you want to use `hear`?")
            self.logger.warning(f"the regex is {pattern}")

        if not self.alias:
            return re.compile(fr"^\s*[@]?{name}[:,]?\s*(?:{pattern})", flags)

        alias = escape.sub("\\$&", self.alias)
        # XXX: it seems not need to return in different order in python
        if len(name) > len(alias):
            x, y = name, alias
            _pattern = fr"^\s*[@]?(?:{x}[:,]?|{y}[:,]?)\s*(?:{pattern})"
        else:
            _pattern = fr"^\s*[@]?(?:{y}[:,]?|{x}[:,]?)\s*(?:{pattern})"
        return re.compile(_pattern, oflags | flags)

    def enter(self, handler, **options):
        """ Adds a Listener that triggers when anyone enters the room.

        :param handler: A (async) function is called with a Response object.
        :param options: additional parameters keyed on extension name.
        """
        self.listen(lambda m: isinstance(m, EnterMessage), handler, **options)

    def leave(self, handler, **options):
        """ Adds a Listener that triggers when anyone leaves the room.

        :param handler: A (async) function is called with a Response object.
        :param options: additional parameters keyed on extension name.
        """
        self.listen(lambda m: isinstance(m, LeaveMessage), handler, **options)

    def topic(self, handler, **options):
        """ Adds a Listener that triggers when anyone changes the topic.

        :param handler: A (async) function is called with a Response object.
        :param options: additional parameters keyed on extension name.
        """
        self.listen(lambda m: isinstance(m, TopicMessage), handler, **options)

    def error(self, handler):
        """ Adds an error handler when an uncaught exception or user emitted
        error event occurs.

        :param handler: A (async) function is called with the error object.
        """
        self.error_handlers.append(handler)

    async def invoke_error_handlers(self, err, res=None):
        """ Calls and passes any registered error handlers for unhandled
        exceptions or user emitted error events.

        :param err: An Exception object.
        :param res: An optional Response object that generated the error.
        """
        for hdlr in self.error_handlers:
            try:
                coro = hdlr(err, res)
                if iscoroutine(coro):
                    await coro
            except Exception:
                msg = "exception when invoking error handler:"
                self.logger.error(msg, exc_info=True)

    def catch_all(self, handler, **options):
        """ Adds a Listener that triggers when no other text matchers match.

        :param handler: A (async) function is called with a Response object.
        :param options: additional parameters keyed on extension name.
        """
        def _check(m):
            return isinstance(m, CatchAllMessage)

        def hdlr(msg):
            msg.message = msg.message.message
            return handler(msg)

        self.listen(_check, hdlr, **options)

    def middleware_listener(self, middleware):
        """ Registers new middleware for execution after matching but before
        listener handled.

        :param middleware: A callable (async) function that will receive two
            arguments: (context: dict, finish: callable).
        """
        self.listener_middleware.register(middleware)

    def middleware_response(self, middleware):
        """ Registers new middleware for execution as a response to any message
        is being sent.

        :param middleware: A callable (async) function that will receive two
            arguments: (context: dict, finish: callable).
        """
        self.response_middleware.register(middleware)

    def middleware_receive(self, middleware):
        """ Registers new middleware for execution before matching.

        :param middleware: A callable (async) function that will receive two
            arguments: (context: dict, finish: callable).
        """
        self.receive_middleware.register(middleware)

    async def receive(self, message):
        """ Passes the given message to any interested Listeners after running
        receive middleware.

        :param message: A Message instance.  Listeners can flag this message as
            'done' to prevent further execution.
        """
        context = dict(response=Response(self, message))
        await self.receive_middleware.execute(context)
        executed = False
        for listener in self.listeners:
            try:
                called = await listener.call(context['response'].message,
                                             self.listener_middleware)
                executed = executed or called
                if message.done:
                    break
            except Exception as e:
                self.logger.exception(f"emit {e!r} to `error` event.")
                self.emit("error", e, Response(self, context['response'].message))
        else:
            msg = context['response'].message
            if not isinstance(msg, CatchAllMessage) and not executed:
                self.logger.debug("No listeners executed; failling back to catch-all")
                executed = await self.receive(CatchAllMessage(msg))
        return executed

    def load_file(self, filepath):
        """ Loads a file in path.

        :param filepath: A string of full filepath in the filesystem.
        """
        fpath = Path(filepath).absolute()
        try:
            spec = import_util.spec_from_file_location(fpath.stem, fpath)
            module = import_util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, "use"):
                if callable(module.use):
                    module.use(self)
                    self.parse_help(module.__doc__ or "")
                else:
                    self.logger.warning(f"Expected `use` in {fpath} isn't callable.")
            else:
                self.logger.warning(f"{fpath} not expose expected `use` function.")
        except Exception:
            self.logger.error(f"Unable to load {fpath}:", exc_info=True)
            sys.exit(1)

    def load(self, path):
        """ Loads every python script in the given path.

        :param path: A string path on the filesystem.
        """
        self.logger.debug(f"Loading scripts from {path}")
        p = Path(path)
        if p.is_dir():
            for f in p.iterdir():
                if f.is_file() and f.name.endswith(".py"):
                    self.load_file(f)
        elif p.is_file() and p.name.endswith(".py"):
            self.load_file(p)

    def load_external_scripts(self, packages):
        """ Load scripts from packages specified in the `external-scripts.json`
        file.

        :param packages: An list of packages containing hubot scripts to load.
            Or A dict that key is the module name, and value is the additional
            parameter to be passed into `use` function as second argument.
        """
        self.logger.debug("Loading external-scripts")
        try:
            if isinstance(packages, list):
                return [import_module(p).use(self) for p in packages]
            elif isinstance(packages, dict):
                return [import_module(p).use(self, v) for p, v in packages.items()]
        except Exception:
            self.logger.error(f"Error loading scripts", exc_info=True)
            sys.exit(1)

    def setup_httprouter(self):
        @web.middleware
        async def set_header(request, handler):
            response = await handler(request)
            response.headers["X-Powered-By"] = f"hubot/{self.name}"
            return response

        user, pwd = environ.get("EXPRESS_USER"), environ.get("EXPRESS_PASSWORD")
        stat = environ.get("EXPRESS_STATIC")
        addr = environ.get("EXPRESS_BIND_ADDRESS", "0.0.0.0")
        port = int(environ.get("EXPRESS_PORT", 8080))

        mws = [set_header]

        if user and pwd:
            try:
                from aiohttp_basicauth import BasicAuthMiddleware
            except ImportError:
                self.logger.warn(f"`aiohttp_basicauth` is required",
                                 exc_info=True)
            else:
                mws.append(BasicAuthMiddleware(username=user, password=pwd))

        self.server = _WebAppBuilder(addr, port, middlewares=mws)
        self.router = self.server.router

        if stat and Path(stat).is_dir():
            self.router.add_static(stat, stat)

    def setup_nullrouter(self):
        """ Setup an empty router object. """
        def _warn():
            return self.logger.warning(msg)
        msg = ("A script has tried registering a HTTP route"
               " while the HTTP server is disabled with --disabled-httpd.")

        class NoneDispatcher(web.UrlDispatcher):
            def __getattribute__(self, attr):
                _warn()
                return super().__getattribute__(attr)

        self.router = NoneDispatcher()

    def load_adapter(self, adapter):
        """ Load the adapter Hubot is going to use.

        :param adapter: A String of the adapter name to use.  It supports the
            'pkg.adapters.module:initial_function' syntax.  The section after
            `:` is on demend.  If not set, `module.use` will be called.
        :return: The last module name if the `initial_function` is not set,
            otherwise return the initial_function name.
        """
        self.logger.debug(f"loading adapter {adapter}")
        try:
            if adapter in DEFAULT_ADAPTERS:
                mod_name = "aiohubot.adapters.%s" % adapter
                has_attrs = attrs = ""
            else:
                mod_name, has_attrs, attrs = adapter.partition(":")

            module = import_module(mod_name)
            if has_attrs:
                init_adap = module
                parts = attrs.split(".")
                for part in parts:
                    init_adap = getattr(init_adap, part)
                name = init_adap.__name__
            else:
                init_adap = getattr(module, 'use')
                name = mod_name.rsplit(".", 1)[-1]
            self.adapter = init_adap(self)
        except Exception as e:
            self.logger.exception(f"Cannot load adapter {adapter} - {e!r}")
            sys.exit(1)

        return name

    def help_commands(self):
        """ Help commands for Running Scripts. """
        return sorted(self.commands)

    def parse_help(self, document):
        docs, section = dict(), None
        for raw_line in document.strip().splitlines():
            line = raw_line.lower().strip(":")
            if line in HUBOT_DOCUMENTATION_SECTIONS:
                section = line
                continue
            else:
                docs.setdefault(section, list()).append(raw_line)

            if section == 'commands':
                self.commands.append(line.strip())
        return docs

    def message_room(self, room, *strings):
        """ A helper send function to message a room that the robot is in.

        :param room: String designating the room to message.
        :param strings: One or more Strings for each message to send.
        """
        envelope = dict(room=room)
        self.adapter.send(envelope, *strings)

    def run(self):
        """ Kick off the event loop for the adapter. """

        if self.server is not None:
            logger, server = self.logger, self.server

            @self.events.once("scripts-loaded")
            async def _start():
                logger.debug("HTTP server Starting ...")
                await server.start()
                logger.debug("HTTP Server Started .")

        coro = self.adapter.run()
        if iscoroutine(coro):
            self._loop.create_task(coro)
        try:
            self.emit("running")
            self._loop.run_forever()
        except KeyboardInterrupt:
            self.logger.info("robot stoping by ctrl+C")
        finally:
            self.shutdown()

    def shutdown(self):
        """ Gracefully shutdown the robot process. """
        self._loop.stop()
        if self.server:
            self._loop.run_until_complete(self.server.shutdown())
        if self.ping_interval_id:
            self.ping_interval_id.cancel()
        self.adapter.close()
        self.brain.close()


class Blueprint:
    def __init__(self):
        self.holds = dict()
        self.router = web.RouteTableDef()
        self.robot = None

    def __call__(self, robot):
        self.robot = robot
        for delegatee, handlers in self.holds.items():
            for kws in handlers:
                getattr(robot, delegatee)(**kws)
        if robot.server is not None:
            robot.router.add_routes(self.router)
        elif self.router:
            robot.logger.info("HTTP server is disabled."
                              f"{len(self.router)} routes will be dropped.")

    def listen(self, matcher, **options):
        def decorator(handler):
            kws.update(handler=handler)
            self.holds.setdefault("listen", list()).append(kws)
            return handler

        kws = options.copy()
        kws.update(matcher=matcher)
        return decorator

    def hear(self, regex, **options):
        def decorator(handler):
            kws.update(handler=handler)
            self.holds.setdefault("hear", list()).append(kws)
            return handler

        kws = options.copy()
        kws.update(regex=regex)
        return decorator

    def respond(self, regex, *, flags=0, **options):
        def decorator(handler):
            kws.update(handler=handler)
            self.holds.setdefault("respond", list()).append(kws)
            return handler

        kws = options.copy()
        kws.update(regex=regex, flags=flags)
        return decorator

    def enter(self, **options):
        def decorator(handler):
            kws.update(handler=handler)
            self.holds.setdefault("enter", list()).append(kws)
            return handler

        kws = options.copy()
        return decorator

    def leave(self, **options):
        def decorator(handler):
            kws.update(handler=handler)
            self.holds.setdefault("leave", list()).append(kws)
            return handler

        kws = options.copy()
        return decorator

    def topic(self, **options):
        def decorator(handler):
            kws.update(handler=handler)
            self.holds.setdefault("topic", list()).append(kws)
            return handler

        kws = options.copy()
        return decorator

    def catch_all(self, **options):
        def decorator(handler):
            kws.update(handler=handler)
            self.holds.setdefault("catch_all", list()).append(kws)
            return handler

        kws = options.copy()
        return decorator

    def error(self, handler):
        self.holds.setdefault("error", list()).append(dict(handler=handler))
        return handler

    def middleware_listener(self, middleware):
        kws = dict(middleware=middleware)
        self.holds.setdefault("middleware_listener", list()).append(kws)
        return middleware

    def middleware_response(self, middleware):
        kws = dict(middleware=middleware)
        self.holds.setdefault("middleware_response", list()).append(kws)
        return middleware

    def middleware_receive(self, middleware):
        kws = dict(middleware=middleware)
        self.holds.setdefault("middleware_receive", list()).append(kws)
        return middleware


class _WebAppBuilder:
    def __init__(self, address, port, *, middlewares=(), **kws):
        self.addr, self.port = address, port
        self.middlewares = list(middlewares)
        self.init_kws = kws
        self.router = web.UrlDispatcher()
        self.app = self.runner = None

    async def build(self):
        self.app = web.Application(router=self.router,
                                   middlewares=self.middlewares, **self.init_kws)
        return self.app

    async def start(self):
        if self.app is None:
            await self.build()

        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.addr, self.port)
        await site.start()

    async def cleanup(self):
        if self.runner:
            await self.runner.cleanup()
            self.app = self.runner = None
    
    shutdown = cleanup
