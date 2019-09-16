from asyncio import Future

from aiohubot.plugins import DataStore


class InMemoryDataStore(DataStore):
    def __init__(self, robot):
        super().__init__(robot)
        self.data = {
            "global": {},
            "users": {}
        }

    def _get(self, key, table):
        f = Future()
        f.set_result(self.data.get(table, {}).get(key))
        return f

    def _set(self, key, value, table):
        f = Future()
        self.get(table, {})[key] = value
        f.set_result(None)
        return f

use = InMemoryDataStore
