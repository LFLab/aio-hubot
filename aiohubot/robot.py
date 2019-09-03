import re
import sys
from os import environ
from pathlib import Path
from logging import getLogger
from asyncio import get_event_loop, iscoroutine
from importlib import import_module

from pyee import AsyncIOEventEmitter

import Listener
import Message
from .core import Response, Middleware, Brain


def get_logger(level):
    # TODO:
    return getLogger("aiohubot")


class Robot:
    def __init__(self, adapter, httpd=None, name="Hubot", alias=False):
        self.name = name
        self.events = AsyncIOEventEmitter()
        self.brain = Brain(self)
        self.alias = alias
        self.datastore = self.adapter = None
        self.commands = []
        self.listeners = []
        self.error_handlers = []
        self.listener_middleware = Middleware(self)
        self.response_middleware = Middleware(self)
        self.receive_middleware = Middleware(self)
        self.logger = get_logger(environ.get("HUBOT_LOG_LEVEL", "info"))
        self.ping_interval_id = None
        self.global_http_options = {}
        self.version = ""  # was parseVersion

        self.router = {}
        if httpd:
            self.setup_httprouter()  # was setupExpress()
        else:
            self.setup_nullrouter()

        self.load_adapter(adapter)
        self.adapter_name = adapter

        self.on = self.events.on
        self.emit = self.events.emit

        self.on("error", self.invoke_error_handlers)

    def response_pattern(self, regex):
        escape = re.compile(r"[-[\]{}()*+?.,\\^$|#\s]")
        pattern, flags = regex
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
            return re.compile(fr"^\\s*[@]?(?:{x}[:,]?|{y}[:,]?)\\s*(?:{pattern})",
                              flags)
        return re.compile(fr"^\\s*[@]?(?:{y}[:,]?|{x}[:,]?)\\s*(?:{pattern})", flags)

    def listen(self, matcher, cb, **options):
        self.listeners.append(Listener.Listener(self, matcher, cb, **options))

    def hear(self, regex, cb, **options):
        listener = Listener.TextListener(self, re.comiple(regex), cb, **options)
        self.listeners.append(listener)

    def response(self, regex, cb, **options):
        listener = Listener.TextListener(self, self.response_pattern(regex),
                                         cb, **options)
        self.listeners.append(listener)

    def enter(self, cb, **options):
        self.listen(lambda m: isinstance(m, Message.EnterMessage), cb, **options)

    def leave(self, cb, **options):
        self.listen(lambda m: isinstance(m, Message.LeaveMessage), cb, **options)

    def topic(self, cb, **options):
        self.listen(lambda m: isinstance(m, Message.TopicMessage), cb, **options)

    def catch_all(self, cb, **options):
        def _check(m):
            return isinstance(m, Message.CatchAllMessage)

        def hdlr(msg):
            msg.message = msg.message.message
            return cb(msg)

        self.listen(_check, hdlr, **options)

    def error(self, handler):
        self.error_handlers.append(handler)

    async def invoke_error_handlers(self, err, res=None):
        self.logger.exception(err)
        for hdlr in self.error_handlers:
            try:
                coro = hdlr(err, res)
                if iscoroutine(coro):
                    await coro
            except Exception as e:
                self.logger.error("exception when invoking error handler:")
                self.logger.exception(e)

    def listener_middleware(self, middleware):
        self.listener_middleware.register(middleware)

    def response_middleware(self, middleware):
        self.response_middleware.register(middleware)

    def receive_middleware(self, middleware):
        self.receive_middleware.register(middleware)

    async def receive(self, message):
        context = dict(response=Response(self, message))
        await self.receive_middleware.execute(context)
        for listener in self.listeners:
            try:
                await listener.call(context['response'].message,
                                    self.listener_middleware)
                if message.done:
                    break
            except Exception as e:
                self.emit("error", e, Response(self, context['response'].message))
        else:
            msg = context['response'].message
            if isinstance(msg, Message.CatchAllMessage):
                self.logger.debug("No listeners executed; failling back to catch-all")
                return self.receive(Message.CatchAllMessage(msg))

    def load_file(self, filepath):
        pass

    def load(self, path):
        self.logger.debug(f"Loading scripts from {path}")
        p = Path(path)
        if p.isdir():
            for f in p.iterdir():
                self.load_file(f)

    def load_hubot_scripts(self, path, scripts):
        self.logger.debug(f"Loading hubot-scripts from {path}")
        p = Path(path)
        if p.exists() and p.is_dir():
            for fname in scripts:
                self.load_file(p.joinpath(fname))

    def load_external_scripts(self, packages):
        pass

    def setup_httprouter(self):
        raise NotImplementedError("Not support yet.")

    def setup_nullrouter(self):
        def _warn():
            return self.logger.warning(msg)
        msg = ("A script has tried registering a HTTP route"
               " while the HTTP server is disabled with --disabled-httpd.")
        self.router = dict(get=_warn, post=_warn, put=_warn, delete=_warn)

    def load_adapter(self, adapter):
        self.logger.debug(f"loading adapter {adapter}")
        try:
            # TODO: need to handle naming and default adapter
            module = import_module(adapter)
            self.adapter = module.use(this)
        except Exception as e:
            self.logger.error(f"Cannot load adapter {adapter} - {e}")
            self.logger.exception(e)
            sys.exit(1)

    def help_commands(self):
        return sorted(self.commands)

    def parse_help(self, path):
        pass

    def send(self, envelope, *strings):
        # delegates to adapter's send
        return self.adapter.send(envelope, *strings)

    def reply(self, envelope, *strings):
        # delegates to adapter's reply
        return self.adapter.reply(envelope, *strings)

    def message_room(self, room, *strings):
        envelope = dict(room=room)
        self.adapter.send(envelope, *strings)

    async def run(self):
        self.emit("running")
        coro = self.adapter.run()
        if iscoroutine(coro):
            await coro

    def shutdown(self):
        if self.ping_interval_id:
            self.ping_interval_id.cancel()
        # process.removeListener("uncaughtException", onUncaughtException)
        self.adapter.close()
        if self.server:
            self.server.close()
        self.brain.close()

    def http(self):
        pass
