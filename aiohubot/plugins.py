from asyncio import get_event_loop, Future, iscoroutine, Event

from pyee import AsyncIOEventEmitter

from .core import TextMessage, Response


class DataStoreUnavailable(Exception):
    pass


class DataStore:
    def __init__(self, robot):
        self._robot = robot

    def set(self, key, value):
        return self._set(key, value, 'global')

    def set_object(self, key, object_key, value):
        target = self.get(key) or dict()
        target[object_key] = value
        return self.set(key, target)

    def set_array(self, key, value):
        target = self.get(key) or []
        items = target + (value if isinstance(value, list) else [value])
        return self.set(key, items)

    def get(self, key):
        return self._get(key, 'global')

    def get_object(self, key, object_key):
        target = self.get(key) or dict()
        return target.get(object_key)

    def _set(self, key, value, table):
        f = Future()
        f.set_exception(DataStoreUnavailable("Setter called on the abstract class."))
        return f

    def _get(self, key, table):
        f = Future()
        f.set_exception(DataStoreUnavailable("Getter called on the abstract class."))
        return f


class Adapter(AsyncIOEventEmitter):
    def __init__(self, robot, loop=None):
        super().__init__(loop or get_event_loop())
        self.robot = robot
        # deprecated methods:
        # users, userforid, userforname, userforfuzzyname, userforrawfuzzyname, http

    def send(self, envelope, *strings):
        pass

    def reply(self, envelope, *strings):
        pass

    def topic(self, envelope, *strings):
        pass

    def play(self, envelope, *strings):
        pass

    def run(self):
        pass

    def close(self):
        pass

    def receive(self, message):
        return self.robot.receive(message)

    emote = send


class Middleware:
    def __init__(self, robot):
        self.robot = robot
        self.stack = list()

    def register(self, middleware):
        # we will not check signature and just make sure it is callable.
        if not callable(middleware):
            raise ValueError("middleware should be a callable with 3 arguments.")
        self.stack.append(middleware)

    async def execute(self, context):
        class Finished(Exception):
            pass

        def finish(*args):
            raise Finished(*args)

        for func in self.stack:
            try:
                coro = func(context, finish)
                if iscoroutine(coro):
                    await coro
            except Finished:
                break
            except Exception as e:
                self.robot.emit("robot", e, context.response)
                break
        return context


class Listener:
    def __init__(self, robot, matcher, callback, **options):
        self.robot = robot
        self.matcher = matcher
        self.options = options
        self.callback = callback
        self.regex = None
        self.options['id'] = self.options.get('id', None)
        if not callable(callback):
            raise ValueError("Callback function should be callable.")

    async def call(self, message, middleware=None):
        middleware = middleware or Middleware(self.robot)
        match = self.matcher(message)

        if match:
            if self.regex:
                msg = (f"Message '{message}' matched regex `{self.regex.pattern}`;"
                       f" listener.options = {self.options}")
                self.robot.logger.debug(msg)
            response = Response(self.robot, message, match)
            context = dict(listener=self, response=response)
            try:
                await middleware.execute(context)
                coro = self.callback(context['response'])
                if iscoroutine(coro):
                    await coro
            except Exception as e:
                self.robot.emit("error", e, context['response'])
            return True
        return False


class TextListener(Listener):
    def __init__(self, robot, regex, callback, **options):
        super().__init__(robot, self._matcher, callback, **options)
        self.regex = regex

    def _matcher(self, message):
        if isinstance(message, TextMessage):
            return message.match(self.regex)
