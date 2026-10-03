"""xmpp-alert: post a one-line alert to an XMPP MUC. Built for cron."""

import argparse
import dataclasses
import logging
import sys
from pathlib import Path

from . import config as config_mod
from .client import AlertError, send_alert
from .config import ConfigError

EXIT_OK = 0
EXIT_USAGE = 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xmpp-alert",
        description="Send a short alert to an XMPP multi-user chat room.",
        epilog=(
            "The account password is read from $XMPP_ALERTS_PASSWORD. "
            "Exit codes: 0 sent, 1 config/usage error, 2 connect/auth failed, "
            "3 room join failed, 4 timeout."
        ),
    )
    p.add_argument(
        "message",
        nargs="*",
        help="message text (joined with spaces); read from stdin if omitted or '-'",
    )
    p.add_argument("-r", "--room", help="room JID or alias from [rooms] (default: default_room)")
    p.add_argument(
        "-c",
        "--config",
        type=Path,
        help=f"config file (default: {config_mod.default_path()})",
    )
    p.add_argument("--nick", help="nickname to use in the room (overrides config)")
    p.add_argument("--timeout", type=float, help="overall timeout in seconds (overrides config)")
    p.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS certificate verification (for test servers only)",
    )
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v for info, -vv for XMPP debug")
    return p


def read_message(parts: list[str]) -> str:
    if not parts or parts == ["-"]:
        if sys.stdin.isatty():
            raise ConfigError("no message given (pass it as an argument or on stdin)")
        text = sys.stdin.read()
    else:
        text = " ".join(parts)
    text = text.strip()
    if not text:
        raise ConfigError("message is empty")
    return text


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Stay silent on success so cron doesn't send mail; errors go to stderr below.
    level = {0: logging.ERROR, 1: logging.INFO}.get(args.verbose, logging.DEBUG)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    # slixmpp logs its own ERRORs for failures we already report on stderr.
    slixmpp_level = {0: logging.CRITICAL, 1: logging.WARNING}.get(args.verbose, logging.DEBUG)
    logging.getLogger("slixmpp").setLevel(slixmpp_level)

    try:
        cfg = config_mod.load(args.config)
        overrides = {}
        if args.nick:
            overrides["nick"] = args.nick
        if args.timeout is not None:
            overrides["timeout"] = args.timeout
        cfg = dataclasses.replace(cfg, **overrides)
        room = cfg.resolve_room(args.room)
        message = read_message(args.message)
    except ConfigError as e:
        print(f"xmpp-alert: {e}", file=sys.stderr)
        return EXIT_USAGE

    try:
        send_alert(cfg, room, message, insecure=args.insecure)
    except AlertError as e:
        print(f"xmpp-alert: {e}", file=sys.stderr)
        return e.exit_code
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
