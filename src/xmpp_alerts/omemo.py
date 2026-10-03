"""OMEMO support: a file-backed keystore and a blind-trust XEP-0384 plugin.

The keystore holds the bot's private identity key and session state. It must
persist between runs, or every cron run would show up to recipients as a new
device. It is locked for the duration of a run because concurrent runs would
otherwise both advance (and corrupt) the ratchet state.
"""

import asyncio
import fcntl
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, FrozenSet, Optional

from omemo.storage import Just, Maybe, Nothing, Storage
from omemo.types import DeviceInformation
from slixmpp.plugins.base import register_plugin
from slixmpp_omemo import TrustLevel, XEP_0384

log = logging.getLogger(__name__)

LOCK_POLL_INTERVAL = 0.1


class KeystoreError(Exception):
    pass


def default_store_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "xmpp-alerts" / "omemo.json"


class JSONFileStorage(Storage):
    """Key/value store in a single JSON file, rewritten atomically on every change."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self._lock_file: Optional[Any] = None
        self._data: dict[str, Any] = {}

    async def open(self) -> None:
        """Take the exclusive lock (waiting for other runs to finish) and load the file."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = self.path.with_name(self.path.name + ".lock")
            self._lock_file = open(lock_path, "a")
        except OSError as e:
            raise KeystoreError(f"cannot open OMEMO keystore lock {e.filename}: {e.strerror}") from None

        while True:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(LOCK_POLL_INTERVAL)

        try:
            with open(self.path) as f:
                self._data = json.load(f)
        except FileNotFoundError:
            log.info("creating new OMEMO keystore at %s", self.path)
            self._data = {}
        except (OSError, json.JSONDecodeError) as e:
            self.close()
            raise KeystoreError(f"cannot read OMEMO keystore {self.path}: {e}") from None

    def close(self) -> None:
        if self._lock_file is not None:
            self._lock_file.close()  # releases the flock
            self._lock_file = None

    def _flush(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:  # mkstemp creates the file with mode 0600
                json.dump(self._data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise

    async def _load(self, key: str) -> Maybe[Any]:
        if key in self._data:
            return Just(self._data[key])
        return Nothing()

    async def _store(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._flush()

    async def _delete(self, key: str) -> None:
        if self._data.pop(key, None) is not None:
            self._flush()


class BlindTrustOMEMO(XEP_0384):
    """XEP-0384 with every recipient device trusted. Fine for a send-only alert bot."""

    default_config = {**XEP_0384.default_config, "keystore": None}

    @property
    def storage(self) -> Storage:
        return self.keystore

    def session_bind(self, jid) -> None:
        # The base class starts OMEMO setup as an unawaited background task here, so
        # failures (e.g. no PEP on the server) can't be handled. The caller awaits
        # get_session_manager() explicitly instead.
        pass

    @property
    def _btbv_enabled(self) -> bool:
        return True

    async def _devices_blindly_trusted(
        self, blindly_trusted: FrozenSet[DeviceInformation], identifier: Optional[str]
    ) -> None:
        for device in blindly_trusted:
            log.info("trusting new OMEMO device %s/%d", device.bare_jid, device.device_id)

    async def _prompt_manual_trust(
        self, manually_trusted: FrozenSet[DeviceInformation], identifier: Optional[str]
    ) -> None:
        # Only reached if some device of a contact was already decided non-blindly,
        # which this bot never does; trust anyway rather than fail the alert.
        session_manager = await self.get_session_manager()
        for device in manually_trusted:
            await session_manager.set_trust(
                device.bare_jid, device.identity_key, TrustLevel.BLINDLY_TRUSTED.value
            )


register_plugin(BlindTrustOMEMO)
