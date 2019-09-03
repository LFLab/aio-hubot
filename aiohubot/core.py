from random import randint
from asyncio import Future, get_event_loop, iscoroutine, Event

from pyee import AsyncIOEventEmitter


class User:
    def __init__(self, id, robot=None, **options):
        self.id = id
        self.options = options
        self._robot = robot

    def __getattr__(self, attr):
        rv = self.options.get(attr, getattr(self, attr, None))
        if attr == "name" and rv is None:
            rv = str(self.id)
        return rv

    def set(self, key, value):
        self._check_datastore_available()
        return self._get_datastore()._set(self._construct_key(key), value, "users")

    def get(self, key):
        self._check_datastore_available()
        return self._get_datastore()._get(self._construct_key(key), 'users')

    def _construct_key(self, key):
        return f"{self.id}+{key}"

    def _get_datastore(self):
        if self._robot:
            return self._robot.datastore

    def _check_datastore_available(self):
        if not self._get_datastore():
            raise DataStoreUnavailable("datastore is not initialized")


class Brain(AsyncIOEventEmitter):
    def __init__(self, robot, auto_save=True, loop=None):
        super().__init__(loop or get_event_loop())
        self._robot = robot
        self.auto_save = auto_save
        self.timer = None
        self.data = dict(users=dict(), _private=dict())
        robot.on("running", lambda: self.reset_save_interval(5))

    def set(self, key, value):
        if isinstance(key, dict):
            pair = key
        else:
            pair = {key: value}
        self.data['_private'].update(pair)

        self.emit('loaded', self.data)
        return self

    def get(self, key):
        return self.data['_private'].get(key)

    def remove(self, key):
        self.data['_private'].pop(key, None)
        return self

    def save(self):
        self.emit("save", self.data)

    def close(self):
        if self.timer:
            self.timer.cancel()
        self.save()
        self.emit("close")

    def reset_save_interval(self, seconds):
        def tock():
            if self.auto_save:
                self.save()

        if self.timer:
            self.timer.cancel()
        self.timer = self._loop.call_later(seconds, tock)

    def merge_data(self, data):
        def renew_ifneed(user):
            if not isinstance(user, User):
                id_ = getattr(user, 'id', "undefined:%s" % id(user))
                return User(id_, self._robot, getattr(user, 'options', dict()))
            return user
        if isinstance(data, dict):
            self.data.update(data)
            users = self.users()
            self.data['users'] = {k:renew_ifneed(u) for k, u in users.items()}

        self.emit("loaded", self.data)

    def users(self):
        return self.data.get("users", dict())

    def user_for_id(self, id, **options):
        users = self.users()
        user = users.get(id, User(id, self._robot, **options))
        users[id] = user

        room = options.get('room')
        if room and (not user.room or user.room != room):
            user = User(id, self._robot, **options)
            users[id] = user

        return user

    def user_for_name(self, name):
        for user in self.users().values():
            if str(user.name).lower() == name.lower():
                return user

    def users_for_raw_fuzzy_name(self, fuzzy_name):
        return [u for u in self.users().values()
                if str(u.name).lower().startwith(fuzzy_name.lower())]

    def users_for_fuzzy_name(self, fuzzy_name):
        users = self.users_for_raw_fuzzy_name(fuzzy_name)
        matches = [u for u in users if u.name.lower() == fuzzy_name.lower()]
        return matches if matches else users


class Message:

    def __init__(self, user, done=False):
        self.user = user
        self.done = done
        self.room = user.room

    def finish(self):
        self.done = True


class TextMessage(Message):

    def __init__(self, user, text, id):
        super().__init__(user)
        self.text = text
        self.id = id

    def __str__(self):
        return str(self.text)

    def match(self, regex):
        return regex.match(self.text)


class EnterMessage(Message):
    pass


class LeaveMessage(Message):
    pass


class TopicMessage(TextMessage):
    pass


class CatchAllMessage(Message):

    def __init__(self, message):
        super().__init__(message.user)
        self.message = message


class Response:
    def __init__(self, robot, message, match=None):
        self.robot = robot
        self.message = message
        self.match = match
        self.envelope = dict(room=message.room, user=message.user, message=message)
        self.http = self.robot.http

    def send(self, *strings):
        return self.run_with_middleware('send', *strings, plaintext=True)

    def emote(self, *strings):
        return self.run_with_middleware('emote', *strings, plaintext=True)

    def reply(self, *strings):
        return self.run_with_middleware('reply', *strings, plaintext=True)

    def topic(self, *strings):
        return self.run_with_middleware('topic', *strings, plaintext=True)

    def play(self, *strings):
        return self.run_with_middleware('play', *strings)

    def locked(self, *string):
        return self.run_with_middleware('locked', *strings, plaintext=True)

    async def run_with_middleware(self, method_name, *strings, plaintext=False):
        ctx = dict(response=self, method=method_name,
                   plaintext=plaintext, strings=strings.copy())
        ctx = await self.robot.response_middleware.execute(ctx)
        # XXX: `locked` is not the basic adapter method, only for campfire.
        handle = getattr(self.robot.adapter, method_name)
        return handle(self.envelope, context.get('strings', list()))

    def random(self, items):
        return items[randint(0, len(items) -1)]

    def finish(self):
        self.message.finish()
