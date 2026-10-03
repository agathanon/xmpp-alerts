import io

import pytest

from xmpp_alerts import cli
from xmpp_alerts.client import AlertTimeout, ConnectError, JoinError
from xmpp_alerts.config import PASSWORD_ENV


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, "secret")
    path = tmp_path / "config.toml"
    path.write_text(
        """
        jid = "bot@example.org"
        default_room = "ops"
        timeout = 15
        [rooms.ops]
        jid = "ops@conference.example.org"
        """
    )
    return path


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli, "send_alert", lambda cfg, room, msg, insecure: calls.append((cfg, room, msg, insecure))
    )
    return calls


class FakeStdin(io.StringIO):
    def __init__(self, text, tty=False):
        super().__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


def test_message_from_args(config_path, sent):
    assert cli.main(["-c", str(config_path), "disk", "full"]) == 0
    [(cfg, room, msg, insecure)] = sent
    assert room.jid == "ops@conference.example.org"
    assert msg == "disk full"
    assert insecure is False


def test_message_from_stdin(config_path, sent, monkeypatch):
    monkeypatch.setattr("sys.stdin", FakeStdin("line one\nline two\n"))
    assert cli.main(["-c", str(config_path)]) == 0
    assert sent[0][2] == "line one\nline two"


def test_dash_reads_stdin(config_path, sent, monkeypatch):
    monkeypatch.setattr("sys.stdin", FakeStdin("from stdin"))
    assert cli.main(["-c", str(config_path), "-"]) == 0
    assert sent[0][2] == "from stdin"


def test_no_message_on_tty(config_path, sent, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", FakeStdin("", tty=True))
    assert cli.main(["-c", str(config_path)]) == 1
    assert "no message" in capsys.readouterr().err
    assert not sent


def test_empty_message(config_path, sent, monkeypatch):
    monkeypatch.setattr("sys.stdin", FakeStdin("  \n"))
    assert cli.main(["-c", str(config_path)]) == 1
    assert not sent


def test_overrides(config_path, sent):
    args = ["-c", str(config_path), "--nick", "pager", "--timeout", "3", "--insecure"]
    args += ["-r", "dev@conference.example.org", "hi"]
    assert cli.main(args) == 0
    cfg, room, _, insecure = sent[0]
    assert cfg.nick == "pager"
    assert cfg.timeout == 3.0
    assert room.jid == "dev@conference.example.org"
    assert insecure is True


def test_config_error(tmp_path, sent, capsys):
    assert cli.main(["-c", str(tmp_path / "missing.toml"), "hi"]) == 1
    assert "not found" in capsys.readouterr().err


@pytest.mark.parametrize(
    "exc, code",
    [(ConnectError("auth"), 2), (JoinError("members-only"), 3), (AlertTimeout("slow"), 4)],
)
def test_alert_errors_map_to_exit_codes(config_path, monkeypatch, capsys, exc, code):
    def fail(*args, **kwargs):
        raise exc

    monkeypatch.setattr(cli, "send_alert", fail)
    assert cli.main(["-c", str(config_path), "hi"]) == code
    assert capsys.readouterr().err == f"xmpp-alert: {exc}\n"
