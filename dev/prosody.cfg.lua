-- Minimal Prosody for local testing of xmpp-alert. Not for production.
admins = { "admin@localhost" }
modules_enabled = { "disco", "roster", "saslauth", "tls", "ping", "register" }
allow_registration = false
c2s_require_encryption = true
authentication = "internal_hashed"
storage = "internal"
log = { info = "*console" }
pidfile = "/var/run/prosody/prosody.pid"

VirtualHost "localhost"

Component "conference.localhost" "muc"
    restrict_room_creation = true  -- only admins create rooms
