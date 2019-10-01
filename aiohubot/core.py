from random import randint
from asyncio import Future, get_event_loop, iscoroutine, Event

from pyee import AsyncIOEventEmitter


class DataStoreUnavailable(Exception):
    pass


class User(dict):
    """ Represents a participating user in the chat. """

    def __init__(self, id, robot, **options):
        super().__init__()
        self['id'] = id
        self.update(**options)
        self.__dict__["_robot"] = robot
        if not self.name:
            self.name = str(self.id)

    def __setattr__(self, attr, val):
        self[attr] = val

    def __getattr__(self, attr):
        return self.__dict__.get(attr) or super().get(attr, None)

    def __repr__(self):
        robot = self.__dict__['_robot']
        remains = {k: v for k, v in self.items() if k != 'id'}
        return f"User({self.id}, {robot}, **{remains})"

    def set(self, key, value):
        self._check_datastore_available()
        return self._get_datastore()._set(self._construct(key), value, "users")

    def get(self, key):
        self._check_datastore_available()
        return self._get_datastore()._get(self._construct(key), 'users')

    def _construct(self, key):
        return f"{self.id}+{key}"

    def _get_datastore(self):
        if self._robot:
            return self._robot.datastore

    def _check_datastore_available(self):
        if not self._get_datastore():
            raise DataStoreUnavailable("datastore is not initialized")


class Brain(AsyncIOEventEmitter):
    """ Represents somewhat persistent storage for the robot. """

    def __init__(self, robot, auto_save=True, loop=None):
        super().__init__(loop or get_event_loop())
        #: Enable or disable the automatic saving
        self.auto_save = auto_save

        self._robot = robot
        self.timer = None
        self.data = dict(users=dict(), _private=dict())
        robot.on("running", lambda: self.reset_save_interval(5))

    def set(self, key, value):
        """ Store key-value pair under the private namespace and extend existing
        @data before emitting the 'loaded' event.

        Returns the instance for chaining.
        """

        if isinstance(key, dict):
            pair = key
        else:
            pair = {key: value}
        self.data['_private'].update(pair)

        self.emit('loaded', self.data)
        return self

    def get(self, key):
        """ Get value by key from private namespace in @data or return None if
        not found.
        """
        return self.data['_private'].get(key)

    def remove(self, key):
        """ Remove value by key from the private namespace in @data if it exists
        """

        self.data['_private'].pop(key, None)
        return self

    def save(self):
        """ Emits the 'save' event so that 'brain' scripts can handle persisting
        """
        self.emit("save", self.data)

    def close(self):
        """ Emits the 'close' event so that 'brain' scripts can handle closing.
        """
        if self.timer:
            self.timer.cancel()
        self.save()
        self.emit("close")

    def reset_save_interval(self, seconds):
        """ Reset the interval between save function calls.

        :param seconds: An Integer of seconds between saves.
        """

        def tock():
            if self.auto_save:
                self.save()

        if self.timer:
            self.timer.cancel()
        self.timer = self._loop.call_later(seconds, tock)

    def merge_data(self, data):
        """ Merge keys loaded from a DB against the in memory representation.

        .. caution :: Deeply nested structures don't merge well.
        """

        def renew_ifneed(user):
            if not isinstance(user, User):
                id_ = getattr(user, 'id', "undefined:%s" % id(user))
                return User(id_, self._robot, **getattr(user, 'options', dict()))
            return user

        if isinstance(data, dict):
            self.data.update(data)
            users = self.data['users']
            self.data['users'] = {k: renew_ifneed(u) for k, u in users.items()}

        self.emit("loaded", self.data)

    def users(self):
        """ Get an list of User objects stored in the brain """
        return self.data["users"].values()

    def user_for_id(self, id, **options):
        """ Get a User object given a unique identifier """
        users = self.data['users']
        user = users.get(id, User(id, self._robot, **options))
        users[id] = user

        room = options.get('room')
        if room and (not user.room or user.room != room):
            user = User(id, self._robot, **options)
            users[id] = user

        return user

    def user_for_name(self, name):
        """ Get a User object given a name """
        for user in self.users():
            if str(user.name).lower() == name.lower():
                return user

    def users_for_raw_fuzzy_name(self, fuzzy_name):
        """ Get all users whose names startswith the fuzzy_name. """
        return [u for u in self.users()
                if str(u.name).lower().startwith(fuzzy_name.lower())]

    def users_for_fuzzy_name(self, fuzzy_name):
        """ If fuzzy_name is an exact match for a user, returns a list with just
        that user.  Otherwise, returns a list of all users for which fuzzy_name
        is a raw fuzzy matched in `users_for_raw_fuzzy_name`.
        """
        users = self.users_for_raw_fuzzy_name(fuzzy_name)
        matches = [u for u in users if u.name.lower() == fuzzy_name.lower()]
        return matches if matches else users


class Response:
    ''' Responses are sent to matching listeners.  Messages know about the
    content and user that made the original message, and how to reply back to
    them.

    :param robot: A Robot instance.
    :param message: A Message instance.
    :param match: A Match object from the successful Regex match.
    '''

    def __init__(self, robot, message, match=None):
        self.robot = robot
        self.message = message
        self.match = match
        self.envelope = {
            "room": message.room,
            "user": message.user,
            "message": message
        }

    def send(self, *strings):
        """ Posts a message back to the chat source

        :param strings: One or more strings to be posted.  The order of these
            strings should be kept intact.
        """
        return self._run_middleware('send', *strings, plaintext=True)

    def emote(self, *strings):
        """ Posts an emote back to the chat source

        :param strings: One or more strings to be posted.  The order of these
            strings should be kept intact.
        """
        return self._run_middleware('emote', *strings, plaintext=True)

    def reply(self, *strings):
        """ Posts a message mentioning the current user.

        :param strings: One or more strings to be posted.  The order of these
            strings should be kept intact.
        """
        return self._run_middleware('reply', *strings, plaintext=True)

    def topic(self, *strings):
        """ Posts a topic changing message

        :param strings: One or more strings to set as the topic of the room the
            bot is in.
        """
        return self._run_middleware('topic', *strings, plaintext=True)

    def play(self, *strings):
        """ Posts a sound in the chat source

        :param strings: One or more strings to be posted as sounds to play.
            The order of these strings should be kept intact.
        """
        return self._run_middleware('play', *strings)

    def locked(self, *string):
        """ Posts a message in an unlogged room

        :param strings: One or more strings to be posted.  The order of these
            strings should be kept intact.
        """
        return self._run_middleware('locked', *strings, plaintext=True)

    async def _run_middleware(self, method_name, *strings, plaintext=False):
        ctx = dict(response=self, method=method_name,
                   plaintext=plaintext, strings=list(strings)[:])
        ctx = await self.robot.response_middleware.execute(ctx)
        # XXX: `locked` is not the basic adapter method, only for campfire.
        handle = getattr(self.robot.adapter, method_name)
        coro = handle(self.envelope, *ctx.get('strings', list()))
        if iscoroutine(coro):
            return await coro
        return coro

    def random(self, items):
        """ Picks a random item from the given items.

        :param items: An Sequence of items.
        """
        return items[randint(0, len(items) - 1)]

    def finish(self):
        """ Tell the message to stop dispatching to listeners. """
        self.message.finish()


class Middleware:
    def __init__(self, robot):
        self.robot = robot
        self.stack = list()

    def register(self, middleware):
        ''' A generic pipeline component function that can either continue the
        pipeline or interrupt it.

        :param middleware: A callable (async) function that will receive two
            arguments: (context: dict, finish: callable).
        '''

        # we will not check signature and just make sure it is callable.
        if not callable(middleware):
            raise ValueError("middleware should be a callable with 2 arguments.")
        self.stack.append(middleware)

    async def execute(self, context):
        ''' Execute all middleware in order, if the `finish` is not called.

        :async:
        :param context: A dict represents as context that is passed through the
            middleware stack.  When handling errors, this is assumed to have a
            `response` key inside.
        '''
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
                self.robot.logger.exception(f"emit {e!r} to `error` event.")
                self.robot.emit("error", e, context.response)
                break
        return context


class Listener:
    ''' Listeners receive every message from the chat source and decide if they
    want to act on it.
    An identifier should be provided in the options parameter to uniquely
    identify the listener (`id` in options).

    :param robot: A Robot instance
    :param matcher: A `callable` that determines if this listener should trigger
        the handler.
    :param handler: A callable (async) function that is triggered if the incoming
        message matches.
    :param options: Additional parameters keyed on extension name. (optional)
    '''

    regex = None

    def __init__(self, robot, matcher, handler, **options):
        self.robot = robot
        self.matcher = matcher
        self.options = options
        self.handler = handler
        self.options['id'] = self.options.get('id', None)
        if not (callable(matcher) and callable(handler)):
            raise ValueError("Callback function should be callable.")

    async def call(self, message, middleware=None):
        """ Determines if the listener likes the content of the messages.
        If so, a Response built from the given Message is passed through all
        registered middleware and potentially the Listener handler.  Note that
        middleware can intercept the message and prevent the handler from ever
        being executed.

        :async:
        :param message: A Message instance
        :param middleware: Optional Middleware object to execute before the
            listener handled.
        """
        middleware = middleware or Middleware(self.robot)
        match = self.matcher(message)

        if match:
            if self.regex:
                if hasattr(self.regex, 'pattern'):
                    pattern = self.regex.pattern
                else:
                    pattern = self.regex
                    self.robot.logger.info("Unknown expected `re` object.")
                msg = (f"Message '{message}' matched regex `{pattern}`;"
                       f" listener.options = {self.options}")
                self.robot.logger.debug(msg)

            response = Response(self.robot, message, match)
            context = dict(listener=self, response=response)
            await middleware.execute(context)

            try:
                self.robot.logger.debug("Executing listener handler for"
                                        f" Message {message}")
                coro = self.handler(context['response'])
                if iscoroutine(coro):
                    await coro
            except Exception as e:
                self.robot.logger.exception(f"emit {e!r} to `error` event.")
                self.robot.emit("error", e, context['response'])
            return True

        return False


class Message:
    ''' Represents an incoming message from the chat.

    :param user: A User instance that sent the message.
    '''

    def __init__(self, user, done=False):
        self.user = user
        self.done = done
        self.room = user.room

    def finish(self):
        """ Indicates that no other Listener should be called on this object. """
        self.done = True
