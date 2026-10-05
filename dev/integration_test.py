"""End-to-end checks against the Prosody in dev/docker-compose.yml.

    docker compose -f dev/docker-compose.yml up -d
    uv run python dev/integration_test.py

An admin client (itself an OMEMO device) creates and configures the test rooms,
stays in them, and decrypts whatever xmpp-alert posts. Safe to re-run.
"""

import asyncio
import os
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import slixmpp
from slixmpp import JID

from xmpp_alerts.omemo import JSONFileStorage

PROJ = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
CFG = TMP / "config.toml"
STATE = TMP / "state"  # XDG_STATE_HOME for the CLI, so the real keystore is never touched
DEFAULT_STORE = STATE / "xmpp-alerts" / "omemo.json"

OPS = "ops@conference.localhost"      # members-only, non-anonymous: OMEMO works
ANON = "anon@conference.localhost"    # members-only, anonymous: OMEMO impossible
QUIET = "quiet@conference.localhost"  # open but moderated: the bot joins without voice
BOT = "alertbot@localhost"
GHOST = "ghost@localhost"             # a member with no account and so no OMEMO devices
OBSERVER_NICKS = {"admin", "admin-phone"}  # the test's own sessions, ignored by observers

CFG.write_text(f'''
jid = "{BOT}"
nick = "alertbot"
default_room = "ops"
timeout = 10
host = "127.0.0.1"
port = 5222

[rooms.ops]
jid = "{OPS}"

[rooms.plain]
jid = "{OPS}"
encryption = "none"

[rooms.anon]
jid = "{ANON}"

[rooms.quiet]
jid = "{QUIET}"
encryption = "none"
''')

received = []  # (nick, body, was_encrypted)
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, detail)


async def run_cli(*args, password="alertbot", stdin=None, config=CFG):
    env = {**os.environ, "XMPP_ALERTS_PASSWORD": password, "XDG_STATE_HOME": str(STATE)}
    if password is None:
        env.pop("XMPP_ALERTS_PASSWORD")
    p = await asyncio.create_subprocess_exec(
        "uv", "run", "--project", str(PROJ), "xmpp-alert", "-c", str(config), *args,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out, err = await p.communicate(stdin.encode() if stdin else None)
    return p.returncode, out.decode(), err.decode()


async def arrived(body, *, encrypted=True, nick="alertbot", wait=5.0, sink=received):
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if (nick, body, encrypted) in sink:
            sink.remove((nick, body, encrypted))
            return True
        await asyncio.sleep(0.1)
    return False


async def configure_room(admin, room, *, anonymous):
    muc = admin.plugin["xep_0045"]
    await muc.join_muc_wait(slixmpp.JID(room), "admin", maxstanzas=0, timeout=10)
    form = admin.plugin["xep_0004"].make_form(ftype="submit")
    form.add_field(var="FORM_TYPE", value="http://jabber.org/protocol/muc#roomconfig")
    form.add_field(var="muc#roomconfig_membersonly", value=True)
    form.add_field(var="muc#roomconfig_persistentroom", value=True)
    form.add_field(var="muc#roomconfig_whois", value="moderators" if anonymous else "anyone")
    await muc.set_room_config(room, form)
    for jid in (BOT, GHOST):
        await muc.set_affiliation(room, "member", jid=slixmpp.JID(jid))


async def bot_device_count(admin):
    sm = await admin.plugin["xep_0384"].get_session_manager()
    await sm.refresh_device_lists(BOT)
    return len(await sm.get_device_information(BOT))


async def observer(store_path, nick, sink):
    """Connect an admin@localhost session that is its own OMEMO device.

    Encrypted and plaintext messages from others land in `sink` as (nick, body, encrypted).
    """
    store = JSONFileStorage(store_path)
    await store.open()
    client = slixmpp.ClientXMPP("admin@localhost", "admin")
    client.ssl_context.check_hostname = False
    client.ssl_context.verify_mode = ssl.CERT_NONE
    for plugin in ("xep_0045", "xep_0004"):
        client.register_plugin(plugin)
    client.register_plugin("xep_0384", {"keystore": store})
    omemo = client.plugin["xep_0384"]
    muc = client.plugin["xep_0045"]

    # decrypt_message() looks up the sender's real JID in the room roster, but it runs as
    # a task, and by then the bot's leave presence (sent right after the message) may have
    # removed it. Remember every nick -> JID mapping we've seen so lookups still work.
    seen_jids = {}
    lookup = muc.get_jid_property

    def remember(pres):
        if pres["muc"]["jid"]:
            seen_jids[(pres["from"].bare, pres["from"].resource)] = pres["muc"]["jid"]

    def get_jid_property(room, nick, prop, *args, **kwargs):
        value = lookup(room, nick, prop, *args, **kwargs)
        if value is None and prop == "jid":
            value = seen_jids.get((JID(room).bare, nick))
        return value

    muc.get_jid_property = get_jid_property
    client.add_event_handler("groupchat_presence", remember)

    async def on_groupchat(msg):
        if msg["subject"] or msg["mucnick"] in OBSERVER_NICKS:
            return
        if omemo.is_encrypted(msg):
            try:
                decrypted, _device = await omemo.decrypt_message(msg)
            except Exception as e:
                print(f"{nick} could not decrypt:", repr(e))
                return
            sink.append((msg["mucnick"], decrypted["body"], True))
        elif msg["body"]:
            sink.append((msg["mucnick"], msg["body"], False))

    started = asyncio.get_running_loop().create_future()
    client.add_event_handler("session_start", lambda _: started.set_result(None))
    client.add_event_handler("groupchat_message", on_groupchat)
    client.connect("127.0.0.1", 5222)
    await asyncio.wait_for(started, 10)
    await asyncio.wait_for(omemo.get_session_manager(), 20)
    return client, store


async def main():
    admin, admin_store = await observer(TMP / "admin-omemo.json", "admin", received)
    muc = admin.plugin["xep_0045"]
    await configure_room(admin, OPS, anonymous=False)
    await configure_room(admin, ANON, anonymous=True)
    await muc.join_muc_wait(JID(QUIET), "admin", maxstanzas=0, timeout=10)
    form = admin.plugin["xep_0004"].make_form(ftype="submit")
    form.add_field(var="FORM_TYPE", value="http://jabber.org/protocol/muc#roomconfig")
    form.add_field(var="muc#roomconfig_moderatedroom", value=True)
    form.add_field(var="muc#roomconfig_persistentroom", value=True)
    await muc.set_room_config(QUIET, form)
    devices_before = await bot_device_count(admin)

    # --- connection and config errors (no OMEMO involved yet) ---
    rc, out, err = await run_cli("hi")
    check("no --insecure -> cert failure", rc == 2, f"rc={rc} err={err.strip()!r}")

    rc, out, err = await run_cli("--insecure", "x", password="wrong")
    check("bad password -> 2", rc == 2, f"rc={rc} err={err.strip()!r}")

    rc, out, err = await run_cli("--insecure", "x", password=None)
    check("missing password -> 1", rc == 1, f"rc={rc} err={err.strip()!r}")

    bad = TMP / "unreachable.toml"
    bad.write_text(CFG.read_text().replace("5222", "5299").replace("timeout = 10", "timeout = 3"))
    t = time.monotonic()
    rc, out, err = await run_cli("--insecure", "x", config=bad)
    check("unreachable -> 2 within timeout", rc == 2 and time.monotonic() - t < 6,
          f"rc={rc} {time.monotonic() - t:.1f}s err={err.strip()!r}")

    # --- OMEMO sends ---
    rc, out, err = await run_cli("--insecure", "Disk 95% on db1")
    check("encrypted send via args", rc == 0 and not out and not err and await arrived("Disk 95% on db1"),
          f"rc={rc} out={out!r} err={err!r}")
    check("keystore created with mode 600",
          DEFAULT_STORE.exists() and DEFAULT_STORE.stat().st_mode & 0o777 == 0o600,
          oct(DEFAULT_STORE.stat().st_mode) if DEFAULT_STORE.exists() else "missing")

    rc, out, err = await run_cli("--insecure", stdin="backup failed\nline two\n")
    check("encrypted send via stdin", rc == 0 and await arrived("backup failed\nline two"),
          f"rc={rc} err={err!r}")

    rc, out, err = await run_cli("--insecure", "-v", "ghost check")
    check("member without OMEMO skipped (-v says so)",
          rc == 0 and GHOST in err and await arrived("ghost check"), f"rc={rc} err={err[-300:]!r}")

    rc, out, err = await run_cli("--insecure", "--nick", "admin", "conflict test")
    check("nick conflict retried", rc == 0 and await arrived("conflict test", nick="admin-2"),
          f"rc={rc} err={err!r}")

    results_conc = await asyncio.gather(*(run_cli("--insecure", f"concurrent {i}") for i in range(3)))
    arrivals = [await arrived(f"concurrent {i}") for i in range(3)]
    check("3 concurrent runs all delivered", all(r[0] == 0 for r in results_conc) and all(arrivals),
          f"rcs={[r[0] for r in results_conc]} arrivals={arrivals} errs={[r[2] for r in results_conc if r[2]]}")

    count = await bot_device_count(admin)
    # This test run's fresh keystore should add exactly one device, however many sends it made.
    check("bot reuses one OMEMO device across runs", count == devices_before + 1,
          f"before={devices_before} after={count}")

    # --- a member adding a device after the bot has already sent to them ---
    # The bot is offline when the device list changes, so it never sees the PEP push and
    # must re-fetch member device lists itself.
    phone_received = []
    phone, phone_store = await observer(TMP / "admin-phone-omemo.json", "admin-phone", phone_received)
    await phone.plugin["xep_0045"].join_muc_wait(JID(OPS), "admin-phone", maxstanzas=0, timeout=10)
    rc, out, err = await run_cli("--insecure", "after new device")
    old_ok = await arrived("after new device")
    phone_ok = await arrived("after new device", sink=phone_received)
    check("member's new device can decrypt", rc == 0 and old_ok and phone_ok,
          f"rc={rc} err={err!r} old device={old_ok} new device={phone_ok}")
    await phone.disconnect()
    phone_store.close()

    # --- plaintext and unsuitable rooms ---
    rc, out, err = await run_cli("--insecure", "-r", "plain", "plain text")
    check("encryption = none sends plaintext", rc == 0 and await arrived("plain text", encrypted=False),
          f"rc={rc} err={err!r}")

    rc, out, err = await run_cli("--insecure", "-r", "anon", "x")
    check("anonymous room -> 5", rc == 5 and "anonymous" in err, f"rc={rc} err={err.strip()!r}")

    rc, out, err = await run_cli("--insecure", "-r", "quiet", "x")
    check("message rejected by room -> 3", rc == 3 and "rejected" in err, f"rc={rc} err={err.strip()!r}")

    rc, out, err = await run_cli("--insecure", "-r", "nope@conference.localhost", "x")
    check("nonexistent room -> 3", rc == 3, f"rc={rc} err={err.strip()!r}")

    await muc.set_affiliation(OPS, "none", jid=slixmpp.JID(BOT))
    rc, out, err = await run_cli("--insecure", "should not arrive")
    check("not a member -> 3", rc == 3, f"rc={rc} err={err.strip()!r}")
    await muc.set_affiliation(OPS, "member", jid=slixmpp.JID(BOT))

    # --- --omemo-store overrides the default location ---
    other = TMP / "elsewhere" / "keys.json"
    rc, out, err = await run_cli("--insecure", "--omemo-store", str(other), "other store")
    check("--omemo-store uses the given file",
          rc == 0 and other.exists() and await arrived("other store"), f"rc={rc} err={err!r}")
    check("...as a separate device", await bot_device_count(admin) == count + 1)

    await asyncio.sleep(0.5)
    check("no unexpected messages", received == [], repr(received))
    await admin.disconnect()
    admin_store.close()
    sys.exit(0 if all(results) else 1)


asyncio.run(main())
