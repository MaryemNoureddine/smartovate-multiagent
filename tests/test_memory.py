import json
import time

from memory.cosmos_logger import CosmosLogger
from memory.redis_memory import SessionMemory


class FakeRedis:
    def __init__(self, fail=False):
        self.store, self.lists, self.fail = {}, {}, fail

    def _check(self):
        if self.fail:
            raise ConnectionError("down")

    def ping(self):
        self._check()
        return True

    def set(self, k, v, ex=None):
        self._check()
        self.store[k] = v

    def get(self, k):
        self._check()
        return self.store.get(k)

    def delete(self, k):
        self.store.pop(k, None)

    def lrange(self, k, a, b):
        self._check()
        return self.lists.get(k, [])[a: b + 1]

    def pipeline(self):
        parent = self

        class P:
            def lpush(self, k, v):
                parent.lists.setdefault(k, []).insert(0, v)

            def ltrim(self, k, a, b):
                parent.lists[k] = parent.lists[k][a: b + 1]

            def execute(self):
                parent._check()

        return P()


def test_local_memory_roundtrip_and_ttl(settings):
    mem = SessionMemory(settings)
    assert mem.backend == "local"
    mem.set_json("k", {"a": 1})
    assert mem.get_json("k") == {"a": 1}
    assert mem.get_json("absent") is None
    mem.ttl = -1
    mem.set_json("old", 1)
    assert mem.get_json("old") is None
    mem.delete("k")
    assert mem.get_json("k") is None
    for i in range(3):
        mem.add_to_index(f"t{i}", limit=2)
    assert mem.list_index() == ["t2", "t1"]
    assert mem.health()["status"] == "ok"


def test_redis_backend_and_fallback(settings):
    fake = FakeRedis()
    mem = SessionMemory(settings, client=fake)
    assert mem.backend == "redis"
    mem.set_json("k", {"b": 2})
    assert json.loads(fake.store["k"]) == {"b": 2}
    assert mem.get_json("k") == {"b": 2}
    mem.add_to_index("t1")
    assert mem.list_index() == ["t1"]
    assert mem.health()["status"] == "ok"
    mem.delete("k")
    fake.fail = True
    mem.set_json("k2", 3)              # repli local transparent
    assert mem.get_json("k2") == 3
    mem.add_to_index("t2")
    assert mem.list_index() == ["t2"]
    assert mem.health()["status"] == "down"


def test_redis_unreachable_url_degrades(settings):
    settings.redis_url = "redis://127.0.0.1:1/0"
    mem = SessionMemory(settings)
    assert mem.backend == "local"
    assert mem.health()["status"] == "degraded"


def test_cosmos_local_fallback(settings):
    log = CosmosLogger(settings)
    assert log.backend == "local"
    assert log.get("x", "x") is None
    log.log({"id": "1", "session_id": "1", "v": 1})
    log.log({"id": "1", "session_id": "1", "v": 2})
    assert log.get("1", "1")["v"] == 2
    assert log.local_path.exists()
    assert log.health()["status"] == "ok"


class FakeContainer:
    def __init__(self, fail=False):
        self.items, self.fail = {}, fail

    def upsert_item(self, doc):
        if self.fail:
            raise RuntimeError("cosmos down")
        self.items[doc["id"]] = doc

    def read_item(self, item, partition_key):
        return self.items[item]

    def read(self):
        if self.fail:
            raise RuntimeError("down")
        return {}


def test_cosmos_backend_and_failover(settings):
    cont = FakeContainer()
    log = CosmosLogger(settings, container=cont)
    assert log.backend == "cosmos"
    log.log({"id": "a", "session_id": "a", "ts": time.time()})
    assert log.get("a", "a")["id"] == "a"
    assert log.get("zz", "zz") is None
    assert log.health()["status"] == "ok"
    cont.fail = True
    log.log({"id": "b", "session_id": "b"})
    assert log.local_path.exists()
    assert log.health()["status"] == "down"


def test_cosmos_bad_credentials_degrades(settings):
    from pydantic import SecretStr

    settings.cosmos_endpoint = "https://127.0.0.1:1/"
    settings.cosmos_key = SecretStr("ZmFrZQ==")
    log = CosmosLogger(settings)
    assert log.backend == "local"
    assert log.health()["status"] == "degraded"
