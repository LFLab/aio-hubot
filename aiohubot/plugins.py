import re
from asyncio import get_event_loop, Future, iscoroutine, Event

from pyee import AsyncIOEventEmitter

from .core import Listener, Message, Response, DataStoreUnavailable


class Adapter(AsyncIOEventEmitter):
    """ An adapter is a specific interface to a chat source for robots. """

    def __init__(self, robot):
        super().__init__(robot._loop)
        self.robot = robot
        self.receive = self.robot.receive  # dispatch

    def send(self, envelope, *strings):
        """ Raw method for sending data back to the chat source.

        :param envelope: An `dict` with message, room and user details.
        :param strings: One or more Strings for each message to send.
        """

    def emote(self, envelope, *strings):
        """ Raw method for sending emote data back to the chat source.
        Defaults as an alias for send.

        :param envelope: An `dict` with message, room and user details.
        :param strings: One or more Strings for each message to send.
        """
        return self.send(envelope, *strings)

    def reply(self, envelope, *strings):
        """ Raw method for building a reply and sending it back to the chat.

        :param envelope: An `dict` with message, room and user details.
        :param strings: One or more Strings for each reply to send.
        """

    def topic(self, envelope, *strings):
        """ Raw method for setting a topic on the chat source.

        :param envelope: An `dict` with message, room and user details.
        :param strings: One or more Strings to set as the topic.
        """

    def play(self, envelope, *strings):
        """ Raw method for playing a sound in the chat source.

        :param envelope: An `dict` with message, room and user details.
        :param strings: One or more Strings for each play message to send.
        """

    def run(self):
        """ Raw method for invoking the bot to run.  Could be a `coroutine`. """

    def close(self):
        """ Raw method for shutting the bot down. """


class TextMessage(Message):
    ''' Represents an incoming message from the chat.

    :param user: A User instance that sent the message.
    :param text: A message that should be `str`.
    :param id: String of the message ID.
    '''

    def __init__(self, user, text, id):
        super().__init__(user)
        self.text = text
        self.id = id

    def __repr__(self):
        name = self.__class__.__name__
        return f"{name}({self.user}, {self.text!r}, {self.id!r})"

    def __str__(self):
        return str(self.text)

    def match(self, regex):
        """ Determines if the message matches the given regex. 

        :param regex: A regular expression to check.
        """
        return regex.match(self.text)


class EnterMessage(Message):
    ''' Represents an incoming user entrance notification.

    :param user: A User instance for the user who entered.
    :param text: always None, meaningless.
    :param id: String of the message ID.
    '''


class LeaveMessage(Message):
    ''' Represents an incoming user exit notification.

    :param user: A User instance for the user who left.
    :param text: always None, meaningless.
    :param id: String of the message ID.
    '''


class TopicMessage(TextMessage):
    ''' Represents an incoming topic change notification. 

    :param user: A User instance for the user who changed the topic.
    :param text: String of the new topic.
    :param id: String of the message ID.
    '''


class CatchAllMessage(Message):
    ''' Represents a message that no matchers matched.

    :param message: The original message.
    '''

    def __init__(self, message):
        super().__init__(message.user)
        self.message = message


class TextListener(Listener):
    ''' TextListeners receive every message from the chat source and decide if
    they want to act on it.

    :param robot: A Robot instance
    :param regex: A regular expression that determines if this listener should
        trigger the handler.
    :param handler: A callable (async) function that is triggered if the incoming
        message matches.
    :param options: Additional parameters keyed on extension name. (optional)
    '''

    def __init__(self, robot, regex, handler, **options):
        super().__init__(robot, self._matcher, handler, **options)
        self.regex = re.compile(regex)

    def _matcher(self, message):
        if isinstance(message, TextMessage):
            return message.match(self.regex)


class DataStore:
    """ Represents a persistent, database-backed storage for the robot. """

    def __init__(self, robot):
        self._robot = robot

    def set(self, key, value):
        """ Set value for key in the database.  Overwrites existing values
        if present.  Value can be any JSON-serializable type.

        :rtype: asyncio.Future
        """

        return self._set(key, value, 'global')

    async def set_object(self, key, object_key, value):
        """ Assuming `key` represents an dict in the databse, sets its
        `object_key` to `value`.  If `key` isn't already present, it's
        instantiated as an empty dict.

        :async:
        """

        target = await self.get(key) or dict()
        target[object_key] = value
        return await self.set(key, target)

    async def set_array(self, key, value):
        """ Adds the supplied value(s) to the end of the existing array in the
        database marked by `key`.  If `key` isn't already present, it's
        instantiated as an empty list.

        :async:
        """
        target = await self.get(key) or []
        items = target + (value if isinstance(value, list) else [value])
        return await self.set(key, items)

    def get(self, key):
        """ Get value by key if in the database or return `None` if not found.

        :rtype: asyncio.Future
        """
        return self._get(key, 'global')

    async def get_object(self, key, object_key):
        """ Digs inside the object at `key` for a key named `object_key`.
        If `key` isn't already present, or if it doesn't contain an
        `object_key`, returns `None`.

        :async:
        """

        target = await self.get(key) or dict()
        return target.get(object_key)

    def _set(self, key, value, table):
        f = Future()
        f.set_exception(DataStoreUnavailable("Setter called on the abstract class."))
        return f

    def _get(self, key, table):
        f = Future()
        f.set_exception(DataStoreUnavailable("Getter called on the abstract class."))
        return f
