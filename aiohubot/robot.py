import re
import sys
from os import environ
from logging import getLogger, StreamHandler, Formatter
from pathlib import Path
from asyncio import get_event_loop, iscoroutine
from importlib import import_module, util as import_util

from pyee import AsyncIOEventEmitter

from .core import Response, Middleware, Brain, Listener, TextListener
from .plugins import EnterMessage, LeaveMessage, TopicMessage, CatchAllMessage

DEFAULT_ADAPTERS = ["shell"]


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
        self.server = self.ping_interval_id = None
        self.router = dict()
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

    def hear(self, regex, handler, **options):
        """ Adds a Listener that attempts to match incoming messages based on a
        regular expression.

        :param regex: A Regex that determines if the handler should be called.
        :param handler: A (async) function is called with a Response object.
        :param options: additional parameters keyed on extension name.
        """
        listener = TextListener(self, re.comiple(regex), handler, **options)
        self.listeners.append(listener)

    def response(self, regex, handler, *, flags=0, **options):
        """ Adds a Listener that attempts to match incoming messages directed
        at the robot based on a Regex.  All regexes treat patterns like they
        begin with a '^'

        :param regex: A Regex that determines if the handler should be called.
        :param handler: A (async) function is called with a Response object.
        :param flags: The constants passed to `re.compile`.
        :param options: additional parameters keyed on extension name.
        """
        self.hear(self.response_pattern(regex, flags), handler, **options)

    def response_pattern(self, pattern, flags=0):
        """ Build a regular expression that matches messages addressed directly
        to the robot.

        :param pattern: A string of regex for the message part that follows the
            robot's name/alias.
        :param flags: The constants passed to `re.compile`.
        """
        escape = re.compile(r"[-[\]{}()*+?.,\\^$|#\s]")
        name = escape.sub("\\$&", self.name)
        if pattern.startswith("^"):
            self.logger.warning("Anchors don't work well with response, "
                                "perhaps you want to use `hear`?")
            self.logger.warning(f"the regex is {pattern}")

        if not self.alias:
            return re.compile(fr"^\\s*[@]?{name}[:,]?\\s*(?:{pattern})", flags)

        alias = escape("\\$&", self.alias)
        # XXX: it seems not need to return in different order in python
        if len(name) > len(alias):
            x, y = name, alias
            _pattern = fr"^\\s*[@]?(?:{x}[:,]?|{y}[:,]?)\\s*(?:{pattern})"
        else:
            _pattern = fr"^\\s*[@]?(?:{y}[:,]?|{x}[:,]?)\\s*(?:{pattern})"
        return re.compile(_pattern, flags)

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

        :async:
        :param err: An Exception object.
        :param res: An optional Response object that generated the error.
        """
        self.logger.exception(err)
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
                executed = await listener.call(context['response'].message,
                                               self.listener_middleware)
                if message.done:
                    break
            except Exception as e:
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
                    self.parse_help(module.__doc__)
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
        raise NotImplementedError("Not support yet.")

    def setup_nullrouter(self):
        """ Setup an empty router object. """
        def _warn():
            return self.logger.warning(msg)
        msg = ("A script has tried registering a HTTP route"
               " while the HTTP server is disabled with --disabled-httpd.")
        self.router = dict(get=_warn, post=_warn, put=_warn, delete=_warn)

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
            self.logger.error(f"Cannot load adapter {adapter} - {e}")
            self.logger.exception(e)
            sys.exit(1)

        return name

    def help_commands(self):
        """ Help commands for Running Scripts. """
        return sorted(self.commands)

    def parse_help(self, document):
        pass

    def message_room(self, room, *strings):
        """ A helper send function to message a room that the robot is in.

        :param room: String designating the room to message.
        :param strings: One or more Strings for each message to send.
        """
        envelope = dict(room=room)
        self.adapter.send(envelope, *strings)

    def run(self):
        """ Kick off the event loop for the adapter. """
        self.emit("running")
        coro = self.adapter.run()
        if iscoroutine(coro):
            self._loop.create_task(coro)
        try:
            self._loop.run_forever()
        except KeyboardInterrupt:
            self.shutdown()

    def shutdown(self):
        """ Gracefully shutdown the robot process. """

        if self.ping_interval_id:
            self.ping_interval_id.cancel()
        self.adapter.close()
        if self.server:
            self.server.close()
        self.brain.close()
