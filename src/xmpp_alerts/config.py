"""Load settings from a TOML file, with the account password from the environment."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PASSWORD_ENV = "XMPP_ALERTS_PASSWORD"
ENCRYPTION_MODES = ("omemo", "none")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Room:
    jid: str
    password: str | None = None
    encryption: str = "omemo"


@dataclass(frozen=True)
class Config:
    jid: str
    password: str
    nick: str = "alertbot"
    default_room: str | None = None
    timeout: float = 15.0
    host: str | None = None
    port: int | None = None
    encryption: str = "omemo"
    omemo_store: Path | None = None
    rooms: dict[str, Room] = field(default_factory=dict)

    def resolve_room(self, name: str | None) -> Room:
        """Turn a CLI --room value (alias or JID) into a Room, falling back to default_room."""
        name = name or self.default_room
        if not name:
            raise ConfigError("no room given and no default_room in config")
        if name in self.rooms:
            return self.rooms[name]
        if "@" not in name:
            raise ConfigError(f"unknown room alias {name!r} (not a JID and not in [rooms])")
        return Room(jid=name, encryption=self.encryption)


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "xmpp-alerts" / "config.toml"


def load(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    path = path or default_path()
    env = os.environ if env is None else env

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from None

    jid = data.get("jid")
    if not jid or "@" not in jid:
        raise ConfigError(f"{path}: 'jid' must be set to the bot's account JID")

    password = env.get(PASSWORD_ENV)
    if not password:
        raise ConfigError(f"{PASSWORD_ENV} is not set")

    encryption = _encryption(data, path, "encryption", "omemo")

    rooms = {}
    for alias, room in data.get("rooms", {}).items():
        if not isinstance(room, dict) or "jid" not in room:
            raise ConfigError(f"{path}: [rooms.{alias}] needs a 'jid'")
        rooms[alias] = Room(
            jid=room["jid"],
            password=room.get("password"),
            encryption=_encryption(room, path, f"rooms.{alias}.encryption", encryption),
        )

    host, port = data.get("host"), data.get("port")
    if (host is None) != (port is None):
        raise ConfigError(f"{path}: 'host' and 'port' must be set together")

    return Config(
        jid=jid,
        password=password,
        nick=data.get("nick", Config.nick),
        default_room=data.get("default_room"),
        timeout=float(data.get("timeout", Config.timeout)),
        host=host,
        port=port,
        encryption=encryption,
        omemo_store=Path(data["omemo_store"]).expanduser() if "omemo_store" in data else None,
        rooms=rooms,
    )


def _encryption(table: dict, path: Path, key: str, default: str) -> str:
    value = table.get("encryption", default)
    if value not in ENCRYPTION_MODES:
        raise ConfigError(f"{path}: '{key}' must be one of {', '.join(ENCRYPTION_MODES)}")
    return value
