# xmpp-alerts

Send short alerts to XMPP multi-user chat rooms. Built for cron jobs. Each run
connects, joins the room, posts one message, waits for the room to confirm it,
leaves, and disconnects. Messages are OMEMO-encrypted by default. It prints
nothing on success, so cron only mails you when something goes wrong.

## Install

```sh
uv tool install .          # puts `xmpp-alert` on your PATH
```

## Configure

```sh
mkdir -p ~/.config/xmpp-alerts
cp config.example.toml ~/.config/xmpp-alerts/config.toml
export XMPP_ALERTS_PASSWORD=...   # the bot account's password
```

The bot account must already be a member of any members-only room it posts to.

## Use

```sh
xmpp-alert "Disk 95% on db1"                  # to default_room
xmpp-alert -r backups "nightly backup failed" # to a room alias
xmpp-alert -r dev@conference.example.org hi   # to a room JID
some-check 2>&1 | xmpp-alert -r ops           # message from stdin
```

In a crontab:

```cron
XMPP_ALERTS_PASSWORD=...
0 3 * * * /usr/local/bin/backup.sh || xmpp-alert -r backups "backup failed on $(hostname)"
```

If you put the password in a crontab, make sure only you can read it
(`crontab -e` does this already). You can also export it from a `chmod 600` file in a wrapper script.

| Exit code | Meaning |
|---|---|
| 0 | sent |
| 1 | config or usage error (missing config, no password, unknown room alias, empty message) |
| 2 | could not connect, TLS verification failed, or authentication failed |
| 3 | could not join the room (not a member, banned, room doesn't exist, ...), or the room rejected the message (e.g. no voice in a moderated room) |
| 4 | timed out |
| 5 | OMEMO encryption not possible (anonymous room, nobody with OMEMO, server without PEP) |

Run with `-v` for progress, or `-vv` for the full XMPP stream.

TLS certificates are verified. Credentials are never sent over an unencrypted
connection. `--insecure` turns off certificate verification and is only meant for test servers.

If the room doesn't exist and the server would create it on join, `xmpp-alert`
leaves at once, which destroys the new room, and exits with 3. A typo in a room name
therefore fails loudly instead of posting into a new, empty room.

## OMEMO

With `encryption = "omemo"` (the default), each message is encrypted to every
OMEMO device of every room member, including owners and admins, and members who
are offline. Clients that load room history can then decrypt it later. Trust is blind: any device a
member publishes is used. Members with no OMEMO devices are skipped (`-v` shows
who). The run fails with exit code 5 only if nobody at all can be encrypted for.

Requirements:

- The room must be **non-anonymous** (real JIDs visible to members), so the bot
  can look up members' devices. Set `encryption = "none"` for rooms that aren't.
- The server must support PEP (XEP-0163), which the bot uses to publish its keys.
  All common servers do.
- The bot reads the room's member list. If the server doesn't allow that,
  `xmpp-alert` warns and encrypts only for members currently in the room. Making
  the bot a room admin fixes this.

The bot's OMEMO identity and session state live in a keystore file, by
default `~/.local/state/xmpp-alerts/omemo.json`. You can change it with
`omemo_store` in the config or `--omemo-store PATH`. Keep this file:

- It holds the bot's **private key**. Protect it like the password. It's created
  with mode 600.
- Every run must use the same file. A new or deleted keystore shows up to
  members as a new device for the bot.
- Concurrent runs share it safely: each run holds a lock on it while it works.

## From Python

```python
from xmpp_alerts import load, send_alert

cfg = load()
send_alert(cfg, cfg.resolve_room("ops"), "Disk 95% on db1")  # raises AlertError on failure
```

## Development

```sh
uv run pytest                                    # unit tests

docker compose -f dev/docker-compose.yml up -d   # local Prosody
uv run python dev/integration_test.py            # end-to-end checks against it, incl. OMEMO decryption
docker compose -f dev/docker-compose.yml down
```
