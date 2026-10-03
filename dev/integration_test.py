"""End-to-end checks against the Prosody in dev/docker-compose.yml.

    docker compose -f dev/docker-compose.yml up -d
    uv run python dev/integration_test.py

Run against a fresh container: it creates and configures ops@conference.localhost.
"""

import asyncio, os, ssl, subprocess, sys, tempfile, time
import slixmpp

ROOM = "ops@conference.localhost"
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(tempfile.mkdtemp(), "config.toml")

open(CFG, "w").write(f'''
jid = "alertbot@localhost"
nick = "alertbot"
default_room = "ops"
timeout = 10
host = "127.0.0.1"
port = 5222
[rooms.ops]
jid = "{ROOM}"
''')

received = []


async def run_cli(*args, password="alertbot", stdin=None):
    env = {**os.environ, "XMPP_ALERTS_PASSWORD": password}
    if password is None:
        env.pop("XMPP_ALERTS_PASSWORD")
    p = await asyncio.create_subprocess_exec(
        "uv", "run", "--project", PROJ, "xmpp-alert", "-c", CFG, *args,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out, err = await p.communicate(stdin.encode() if stdin else None)
    return p.returncode, out.decode(), err.decode()


async def main():
    admin = slixmpp.ClientXMPP("admin@localhost", "admin")
    admin.ssl_context.check_hostname = False
    admin.ssl_context.verify_mode = ssl.CERT_NONE
    admin.register_plugin("xep_0045")
    admin.register_plugin("xep_0004")
    started = asyncio.get_running_loop().create_future()
    admin.add_event_handler("session_start", lambda _: started.set_result(None))
    admin.add_event_handler("groupchat_message",
        lambda m: received.append((m["mucnick"], m["body"])) if not m["subject"] and m["body"] else None)
    admin.connect("127.0.0.1", 5222)
    await asyncio.wait_for(started, 10)
    muc = admin.plugin["xep_0045"]
    await muc.join_muc_wait(slixmpp.JID(ROOM), "admin", maxstanzas=0, timeout=10)
    form = await muc.get_room_config(ROOM)
    new = admin.plugin["xep_0004"].make_form(ftype="submit")
    new.add_field(var="FORM_TYPE", value="http://jabber.org/protocol/muc#roomconfig")
    new.add_field(var="muc#roomconfig_membersonly", value=True)
    new.add_field(var="muc#roomconfig_persistentroom", value=True)
    await muc.set_room_config(ROOM, new)
    await muc.set_affiliation(ROOM, "member", jid=slixmpp.JID("alertbot@localhost"))

    results = []
    def check(name, ok, detail=""):
        results.append(ok)
        print(("PASS" if ok else "FAIL"), name, detail)

    rc, out, err = await run_cli("Disk 95% on db1")
    check("no --insecure -> cert failure", rc == 2, f"rc={rc} err={err.strip()!r}")

    rc, out, err = await run_cli("--insecure", "Disk 95% on db1")
    check("send via args", rc == 0 and not out and not err, f"rc={rc} out={out!r} err={err!r}")

    rc, out, err = await run_cli("--insecure", stdin="backup failed\nline two\n")
    check("send via stdin", rc == 0, f"rc={rc} err={err!r}")

    rc, out, err = await run_cli("--insecure", "x", password="wrong")
    check("bad password -> 2", rc == 2, f"rc={rc} err={err.strip()!r}")

    rc, out, err = await run_cli("--insecure", "x", password=None)
    check("missing password -> 1", rc == 1, f"rc={rc} err={err.strip()!r}")

    rc, out, err = await run_cli("--insecure", "-r", "nope@conference.localhost", "x")
    check("nonexistent room -> 3", rc == 3, f"rc={rc} err={err.strip()!r}")

    # The admin observer already holds nick 'admin'.
    rc, out, err = await run_cli("--insecure", "--nick", "admin", "conflict test")
    check("nick conflict retried", rc == 0, f"rc={rc} err={err!r}")

    await muc.set_affiliation(ROOM, "none", jid=slixmpp.JID("alertbot@localhost"))
    rc, out, err = await run_cli("--insecure", "should not arrive")
    check("not a member -> 3", rc == 3, f"rc={rc} err={err.strip()!r}")
    await muc.set_affiliation(ROOM, "member", jid=slixmpp.JID("alertbot@localhost"))

    # unreachable port: should fail within timeout, as connect error
    alt = CFG + ".bad"
    open(alt, "w").write(open(CFG).read().replace("5222", "5299").replace("timeout = 10", "timeout = 3"))
    t = time.monotonic()
    env = {**os.environ, "XMPP_ALERTS_PASSWORD": "alertbot"}
    pr = subprocess.run(["uv", "run", "--project", PROJ, "xmpp-alert", "-c", alt, "--insecure", "x"],
                        capture_output=True, text=True, env=env)
    check("unreachable -> 2 within timeout", pr.returncode == 2 and time.monotonic() - t < 6,
          f"rc={pr.returncode} {time.monotonic()-t:.1f}s err={pr.stderr.strip()!r}")

    await asyncio.sleep(1)
    print("received:", received)
    check("messages arrived", received == [
        ("alertbot", "Disk 95% on db1"),
        ("alertbot", "backup failed\nline two"),
        ("admin-2", "conflict test"),
    ])
    await admin.disconnect()
    sys.exit(0 if all(results) else 1)

asyncio.run(main())
