import pytest

from xmpp_alerts.config import PASSWORD_ENV, ConfigError, Room, load

ENV = {PASSWORD_ENV: "secret"}


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_minimal(tmp_path):
    cfg = load(write(tmp_path, 'jid = "bot@example.org"\n'), ENV)
    assert cfg.jid == "bot@example.org"
    assert cfg.password == "secret"
    assert cfg.nick == "alertbot"
    assert cfg.timeout == 15.0
    assert cfg.host is None and cfg.port is None


def test_full(tmp_path):
    path = write(
        tmp_path,
        """
        jid = "bot@example.org"
        nick = "pager"
        default_room = "ops"
        timeout = 5
        host = "xmpp.example.org"
        port = 5223

        [rooms.ops]
        jid = "ops@conference.example.org"
        password = "roompw"
        """,
    )
    cfg = load(path, ENV)
    assert cfg.nick == "pager"
    assert cfg.timeout == 5.0
    assert (cfg.host, cfg.port) == ("xmpp.example.org", 5223)
    assert cfg.rooms["ops"] == Room("ops@conference.example.org", "roompw")


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "nope.toml", ENV)


def test_bad_toml(tmp_path):
    with pytest.raises(ConfigError, match="invalid TOML"):
        load(write(tmp_path, "jid = "), ENV)


def test_missing_jid(tmp_path):
    with pytest.raises(ConfigError, match="'jid'"):
        load(write(tmp_path, 'nick = "x"\n'), ENV)


def test_missing_password(tmp_path):
    with pytest.raises(ConfigError, match=PASSWORD_ENV):
        load(write(tmp_path, 'jid = "bot@example.org"\n'), {})


def test_host_without_port(tmp_path):
    with pytest.raises(ConfigError, match="together"):
        load(write(tmp_path, 'jid = "bot@example.org"\nhost = "h"\n'), ENV)


def test_room_without_jid(tmp_path):
    with pytest.raises(ConfigError, match=r"\[rooms.ops\]"):
        load(write(tmp_path, 'jid = "bot@example.org"\n[rooms.ops]\npassword = "x"\n'), ENV)


@pytest.fixture
def cfg(tmp_path):
    return load(
        write(
            tmp_path,
            """
            jid = "bot@example.org"
            default_room = "ops"
            [rooms.ops]
            jid = "ops@conference.example.org"
            """,
        ),
        ENV,
    )


def test_resolve_alias(cfg):
    assert cfg.resolve_room("ops").jid == "ops@conference.example.org"


def test_resolve_default(cfg):
    assert cfg.resolve_room(None).jid == "ops@conference.example.org"


def test_resolve_literal_jid(cfg):
    assert cfg.resolve_room("dev@conference.example.org") == Room("dev@conference.example.org")


def test_resolve_unknown_alias(cfg):
    with pytest.raises(ConfigError, match="unknown room alias"):
        cfg.resolve_room("dev")


def test_resolve_no_room(tmp_path):
    cfg = load(write(tmp_path, 'jid = "bot@example.org"\n'), ENV)
    with pytest.raises(ConfigError, match="no room given"):
        cfg.resolve_room(None)


def test_encryption_defaults_to_omemo(cfg):
    assert cfg.encryption == "omemo"
    assert cfg.resolve_room("ops").encryption == "omemo"
    assert cfg.resolve_room("dev@conference.example.org").encryption == "omemo"
    assert cfg.omemo_store is None


def test_encryption_global_and_per_room(tmp_path):
    path = write(
        tmp_path,
        """
        jid = "bot@example.org"
        encryption = "none"
        omemo_store = "~/keys/omemo.json"
        [rooms.secure]
        jid = "secure@conference.example.org"
        encryption = "omemo"
        [rooms.open]
        jid = "open@conference.example.org"
        """,
    )
    cfg = load(path, ENV)
    assert cfg.rooms["secure"].encryption == "omemo"
    assert cfg.rooms["open"].encryption == "none"
    assert cfg.resolve_room("x@conference.example.org").encryption == "none"
    assert cfg.omemo_store.is_absolute() and cfg.omemo_store.name == "omemo.json"


@pytest.mark.parametrize("text", ['encryption = "pgp"\n', '[rooms.x]\njid = "x@c.example.org"\nencryption = 1\n'])
def test_bad_encryption(tmp_path, text):
    with pytest.raises(ConfigError, match="encryption"):
        load(write(tmp_path, 'jid = "bot@example.org"\n' + text), ENV)
