"""Connect, join a MUC, post one message, leave."""

import asyncio
import logging
import ssl

import slixmpp
from slixmpp import Presence
from slixmpp.exceptions import PresenceError

from .config import Config, Room

log = logging.getLogger(__name__)

MAX_NICK_ATTEMPTS = 5
DISCONNECT_WAIT = 2.0

# MUC status code: the room did not exist and our join just created it.
ROOM_CREATED = 201

JOIN_ERRORS = {
    "registration-required": "bot is not a member of this members-only room",
    "forbidden": "bot is banned from this room",
    "not-authorized": "room requires a password (set 'password' under [rooms.<alias>])",
    "item-not-found": "room does not exist",
    "not-allowed": "room does not exist and the bot may not create it",
    "service-unavailable": "room is full or the MUC service is unavailable",
}


class AlertError(Exception):
    exit_code = 1


class ConnectError(AlertError):
    exit_code = 2


class JoinError(AlertError):
    exit_code = 3


class AlertTimeout(AlertError):
    exit_code = 4


def send_alert(config: Config, room: Room, message: str, *, insecure: bool = False) -> None:
    """Blocking wrapper around send_alert_async(). Raises AlertError on failure."""
    asyncio.run(send_alert_async(config, room, message, insecure=insecure))


async def send_alert_async(
    config: Config, room: Room, message: str, *, insecure: bool = False
) -> None:
    xmpp = slixmpp.ClientXMPP(config.jid, config.password)
    xmpp.register_plugin("xep_0045")
    if insecure:
        xmpp.ssl_context.check_hostname = False
        xmpp.ssl_context.verify_mode = ssl.CERT_NONE

    session: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    connect_errors: list[object] = []

    def on_session_start(_event):
        if not session.done():
            session.set_result(None)

    def fail_session(message):
        if not session.done():
            session.set_exception(ConnectError(message))

    xmpp.add_event_handler("session_start", on_session_start)
    xmpp.add_event_handler(
        "failed_all_auth", lambda _: fail_session(f"authentication failed for {config.jid}")
    )
    xmpp.add_event_handler(
        "ssl_invalid_chain",
        lambda e: fail_session(f"TLS certificate verification failed: {e}"),
    )
    xmpp.add_event_handler("connection_failed", connect_errors.append)

    stage = "connecting"
    try:
        async with asyncio.timeout(config.timeout):
            xmpp.connect(config.host, config.port)
            await session

            stage = f"joining {room.jid}"
            nick = await _join(xmpp, room, config.nick)

            stage = "sending"
            xmpp.send_message(mto=room.jid, mbody=message, mtype="groupchat")
            xmpp.plugin["xep_0045"].leave_muc(room.jid, nick)
    except TimeoutError:
        # Can't use session.done() here: the timeout cancels the pending future.
        if stage == "connecting" and connect_errors:
            raise ConnectError(f"could not connect: {connect_errors[-1]}") from None
        raise AlertTimeout(f"timed out after {config.timeout:g}s while {stage}") from None
    finally:
        xmpp.cancel_connection_attempt()
        # disconnect() flushes the send queue (message, leave) before closing the stream.
        await xmpp.disconnect(wait=DISCONNECT_WAIT)


async def _join(xmpp: slixmpp.ClientXMPP, room: Room, base_nick: str) -> str:
    """Join the room, retrying with a suffixed nick on conflict. Returns the nick used."""
    muc = xmpp.plugin["xep_0045"]
    for attempt in range(1, MAX_NICK_ATTEMPTS + 1):
        nick = base_nick if attempt == 1 else f"{base_nick}-{attempt}"
        try:
            pres = await _join_once(xmpp, room, nick)
        except PresenceError as e:
            condition = e.condition
            if condition == "conflict":
                log.info("nick %r taken in %s, retrying", nick, room.jid)
                continue
            reason = JOIN_ERRORS.get(condition, f"server returned {condition}")
            raise JoinError(f"could not join {room.jid}: {reason}") from None

        if ROOM_CREATED in pres["muc"]["status_codes"]:
            # A typo in the room JID would otherwise silently create a new, empty room.
            # Leaving a freshly created (still locked) room destroys it on compliant servers.
            muc.leave_muc(room.jid, nick)
            raise JoinError(f"could not join {room.jid}: room does not exist")
        return nick

    raise JoinError(f"could not join {room.jid}: nicks {base_nick!r}..-{MAX_NICK_ATTEMPTS} all taken")


async def _join_once(xmpp: slixmpp.ClientXMPP, room: Room, nick: str) -> Presence:
    """join_muc_wait(), but also fail on error presences that lack the MUC payload.

    slixmpp only routes a join error to the waiter if the server echoes the
    <x xmlns='http://jabber.org/protocol/muc'/> element, which RFC 6120 makes
    optional (Prosody omits it). Without this, join errors surface as timeouts.
    """
    room_jid = slixmpp.JID(room.jid)
    error: asyncio.Future[Presence] = asyncio.get_running_loop().create_future()

    def on_presence_error(pres: Presence):
        if pres["from"].bare == room_jid.bare and not error.done():
            error.set_result(pres)

    join = asyncio.ensure_future(
        xmpp.plugin["xep_0045"].join_muc_wait(
            room_jid,
            nick,
            password=room.password,
            maxstanzas=0,
            timeout=None,  # bounded by the overall timeout
        )
    )
    xmpp.add_event_handler("presence_error", on_presence_error)
    try:
        await asyncio.wait([join, error], return_when=asyncio.FIRST_COMPLETED)
    finally:
        xmpp.del_event_handler("presence_error", on_presence_error)
        if not join.done():
            join.cancel()
    if error.done():
        raise PresenceError(error.result())
    pres, *_ = join.result()
    return pres
