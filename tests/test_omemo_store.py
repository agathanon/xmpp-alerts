import asyncio
import json

import pytest

from xmpp_alerts.omemo import JSONFileStorage, KeystoreError, default_store_path


def run(coro):
    return asyncio.run(coro)


async def opened(path):
    store = JSONFileStorage(path)
    await store.open()
    return store


def test_default_path_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_store_path() == tmp_path / "xmpp-alerts" / "omemo.json"


def test_roundtrip_persists_across_opens(tmp_path):
    path = tmp_path / "sub" / "omemo.json"

    async def write():
        store = await opened(path)
        await store._store("/own_device_id", 1234)
        await store._store("/keys", {"a": [1, 2]})
        await store._delete("/keys")
        store.close()

    async def read():
        store = await opened(path)
        try:
            return (await store._load("/own_device_id")).from_just(), (await store._load("/keys")).is_nothing
        finally:
            store.close()

    run(write())
    assert run(read()) == (1234, True)
    assert json.loads(path.read_text()) == {"/own_device_id": 1234}


def test_file_and_dir_permissions(tmp_path):
    path = tmp_path / "state" / "omemo.json"

    async def go():
        store = await opened(path)
        await store._store("k", "v")
        store.close()

    run(go())
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_corrupt_file(tmp_path):
    path = tmp_path / "omemo.json"
    path.write_text("{not json")
    with pytest.raises(KeystoreError, match="cannot read"):
        run(opened(path))


def test_lock_excludes_second_opener(tmp_path):
    path = tmp_path / "omemo.json"

    async def go():
        first = await opened(path)
        second = JSONFileStorage(path)
        waiter = asyncio.ensure_future(second.open())
        await asyncio.sleep(0.3)
        blocked = not waiter.done()
        first.close()
        await asyncio.wait_for(waiter, 2)
        second.close()
        return blocked

    assert run(go()) is True
