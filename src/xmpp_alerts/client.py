"""Connect, join a MUC, post one message, leave."""

import asyncio
import logging
import ssl

import slixmpp
from omemo.session_manager import NoEligibleDevices
from slixmpp import JID, Message, Presence
from slixmpp.exceptions import IqError, PresenceError

from .config import Config, Room
from .omemo import JSONFileStorage, KeystoreError, default_store_path

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


class SendRejected(AlertError):
    exit_code = 3


class AlertTimeout(AlertError):
    exit_code = 4


class EncryptionFailed(AlertError):
    exit_code = 5


def send_alert(config: Config, room: Room, message: str, *, insecure: bool = False) -> None:
    """Blocking wrapper around send_alert_async(). Raises AlertError on failure."""
    asyncio.run(send_alert_async(config, room, message, insecure=insecure))


async def send_alert_async(
    config: Config, room: Room, message: str, *, insecure: bool = False
) -> None:
    xmpp = slixmpp.ClientXMPP(config.jid, config.password)
    xmpp.register_plugin("xep_0045")
    keystore = None
    if room.encryption == "omemo":
        keystore = JSONFileStorage(config.omemo_store or default_store_path())
        xmpp.register_plugin("xep_0384", {"keystore": keystore})
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
            if keystore:
                stage = f"waiting for the OMEMO keystore lock on {keystore.path}"
                await keystore.open()
                stage = "connecting"
            xmpp.connect(config.host, config.port)
            await session

            if keystore:
                stage = "setting up OMEMO"
                await _setup_omemo(xmpp)

            stage = f"joining {room.jid}"
            nick = await _join(xmpp, room, config.nick)

            msg = xmpp.make_message(mto=room.jid, mbody=message, mtype="groupchat")
            if keystore:
                stage = "encrypting"
                msg = await _encrypt(xmpp, room, msg)

            stage = "waiting for the room to confirm the message"
            await _send_confirmed(xmpp, room, msg)
            xmpp.plugin["xep_0045"].leave_muc(room.jid, nick)
    except TimeoutError:
        # Can't use session.done() here: the timeout cancels the pending future.
        if stage == "connecting" and connect_errors:
            raise ConnectError(f"could not connect: {connect_errors[-1]}") from None
        raise AlertTimeout(f"timed out after {config.timeout:g}s while {stage}") from None
    except KeystoreError as e:
        raise AlertError(str(e)) from None
    finally:
        xmpp.cancel_connection_attempt()
        # disconnect() flushes the send queue (message, leave) before closing the stream.
        await xmpp.disconnect(wait=DISCONNECT_WAIT)
        if keystore:
            keystore.close()


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


async def _send_confirmed(xmpp: slixmpp.ClientXMPP, room: Room, msg: Message) -> None:
    """Send, then wait for the room to reflect the message back (or reject it).

    The reflection means the room accepted and broadcast it, which is a stronger
    guarantee than the stanza leaving our socket.
    """
    room_bare = JID(room.jid).bare
    outcome: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def on_reflection(reply: Message):
        if reply["id"] == msg["id"] and reply["from"].bare == room_bare and not outcome.done():
            outcome.set_result(None)

    def on_error(reply: Message):
        if reply["id"] == msg["id"] and reply["from"].bare == room_bare and not outcome.done():
            condition = reply["error"]["condition"]
            text = reply["error"]["text"]
            detail = f"{condition}: {text}" if text else condition
            outcome.set_exception(SendRejected(f"{room.jid} rejected the message ({detail})"))

    xmpp.add_event_handler("groupchat_message", on_reflection)
    xmpp.add_event_handler("groupchat_message_error", on_error)
    try:
        msg.send()
        await outcome
    finally:
        xmpp.del_event_handler("groupchat_message", on_reflection)
        xmpp.del_event_handler("groupchat_message_error", on_error)


async def _setup_omemo(xmpp: slixmpp.ClientXMPP) -> None:
    """Load or create our OMEMO identity and make sure our device and keys are published."""
    try:
        await xmpp.plugin["xep_0384"].get_session_manager()
    except Exception as e:
        raise EncryptionFailed(
            f"OMEMO setup failed ({e!r}); the server must support PEP (XEP-0163)"
        ) from None


async def _encrypt(xmpp: slixmpp.ClientXMPP, room: Room, msg: Message) -> Message:
    """Encrypt for every room member we can; members without usable devices are skipped."""
    recipients = await _recipients(xmpp, room)
    omemo = xmpp.plugin["xep_0384"]
    # Re-download every member's device list. The plugin otherwise subscribes once and relies
    # on PEP pushes to keep its cache current, but this bot is offline when members add
    # devices, so it never receives those pushes and new devices would never be encrypted for.
    try:
        await omemo.refresh_device_lists(recipients, force_download=True)
    except Exception as e:
        raise EncryptionFailed(f"could not fetch OMEMO device lists: {e!r}") from None
    while recipients:
        try:
            encrypted, errors = await omemo.encrypt_message(msg, recipients)
        except NoEligibleDevices as e:
            log.info("no OMEMO device to encrypt for: %s", ", ".join(sorted(e.bare_jids)))
            recipients = {jid for jid in recipients if jid.bare not in e.bare_jids}
            continue
        except Exception as e:
            raise EncryptionFailed(f"OMEMO encryption failed: {e!r}") from None
        for err in errors:
            log.info("could not encrypt for %s/%d: %r", err.bare_jid, err.device_id, err.exception)
        return encrypted
    raise EncryptionFailed(f"no member of {room.jid} has an OMEMO device to encrypt for")


async def _recipients(xmpp: slixmpp.ClientXMPP, room: Room) -> set[JID]:
    """Real JIDs of the room's affiliated members plus anyone currently in it, minus ourselves."""
    room_jid = JID(room.jid)
    muc = xmpp.plugin["xep_0045"]

    info = await xmpp.plugin["xep_0030"].get_info(jid=room_jid)
    if "muc_nonanonymous" not in info["disco_info"]["features"]:
        raise EncryptionFailed(
            f"{room.jid} is anonymous, so members' devices can't be looked up for OMEMO; "
            "make the room non-anonymous or set encryption = \"none\" for it"
        )

    bare_jids: set[str] = set()
    try:
        for affiliation in ("owner", "admin", "member"):
            members = await muc.get_affiliation_list(room_jid, affiliation)
            bare_jids.update(JID(j).bare for j in members)  # returns str despite the hint
    except IqError as e:
        log.warning(
            "cannot fetch the member list of %s (%s); encrypting only for members currently "
            "in the room. Make the bot a room admin to fix this.",
            room.jid,
            e.condition,
        )
    for occupant in muc.get_roster(room_jid):
        real_jid = muc.get_jid_property(room_jid, occupant, "jid")
        if real_jid:
            bare_jids.add(JID(real_jid).bare)

    bare_jids.discard(xmpp.boundjid.bare)
    if not bare_jids:
        raise EncryptionFailed(f"found no members of {room.jid} to encrypt for")
    return {JID(j) for j in bare_jids}
