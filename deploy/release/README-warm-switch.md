# Warm-then-switch API restarts (ARCH-T6)

`plx-release restart` (and `deploy_compat.sh`, `plx-release deploy`) never routes readers to a
cold API, and never reloads Caddy to do it.

Caddy sends `/api/*` to 127.0.0.1:8780 and, when that takes no connection, to the standby
plx-api@standby on `unix//run/plx-api-standby/api.sock` (`lb_policy first`); a refused
connection goes to the other (nothing was sent, so any request may go again) and the
refusing one is passed over for 2 s (deploy/Caddyfile). The standby serves only on that
socket, in the unit's RuntimeDirectory (owned by the API's user, 0711, removed when it stops),
so no dev or mock API anyone starts on a TCP port or elsewhere can ever be sent a reader; and
`serve --unix-socket` refuses `--mock`, `--dev-employee-oid` and `--dev-fixture-principal`.
At rest only plx-api.service listens, on 8780. A restart:

1. mirrors plx-api.service's drop-ins into `/etc/systemd/system/plx-api@.service.d/` (symlinks,
   plus `zzzz-slot.conf`: plx-api.service's own ExecStart with `--port %i`) and checks that every
   setting deciding what the API may read, write and run is the same for plx-api and
   plx-api@standby (user, environment, sandbox, capabilities, weights, ExecStart but the port);
2. checks the host has memory for a second API (MemAvailable minus the serving API's
   anonymous memory >= 2 GiB), that role `plx_api` has room for a second API's Postgres
   sessions (in use + the API's own + 8 <= its connection limit) and that the running Caddy
   sends to the standby's socket (its admin socket, read as the caddy user);
3. writes `/run/plx-api/hold-plx-api-standby` and starts plx-api@standby on current: an API
   started while its hold file (under 15 min old) exists starts its warm-up at once and opens
   its socket or port only once ready, at most 600 s (every
   known reader's Today and main screens read and kept on disk under the new code version;
   operator_api/server.py). plx-release waits for its `/api/health` and `/api/ready` (at most
   660 s, past the 600 s hold; below 1 GiB available it stops it);
4. restarts plx-api.service held the same way (`/run/plx-api/hold-8780`). SIGTERM makes the
   API close 8780, end the requests under way (at most 60 s; an Ask still streaming then is
   cut) and exit, so Caddy sends the next request to the standby; the new plx-api opens 8780
   once warm and Caddy sends to it again. Both units have TimeoutStopSec=100 (60 s drain +
   20 s worker joins);
5. waits 3 s (Caddy's 2 s pass-over) and stops plx-api@standby the same graceful way.

Hold files are removed after each start, on an interrupted run (a trap), and at the start of
the next switch. Two APIs share the state directory for the switch: the work log, feedback,
replay guard and action journals are cross-process safe, and the known readers' file is
written under a file lock.

Edge cases left: a POST that Caddy sends on a pooled keep-alive connection at the instant the
old API closes it gets a 502 (never sent twice: Caddy retries only GETs after a connection was
made); one transport error on 8780 (a 120 s response timeout) makes Caddy pass it over for 2 s,
so requests in those 2 s wait for it.

plx-api.service is the serving unit again at the end, so every script that starts, stops or
reads plx-api and 127.0.0.1:8780 works as before. A standby that is not ready is stopped and
the old API keeps serving (exit 1: deploy_compat.sh rolls back). If step 1 or 2 fails, the API
restarts in place as before and the reason is printed ("warm switch off: ...") and logged in
history.log. `plx-release slots` shows both units and whether the switch is on, writing
nothing; `plx-release slots sync` writes the mirror.

A restart reloads Caddy only when the release's Caddyfile differs from the previous one's: a
reload drops requests under way (measured on this host: 6-12 failed requests in 30 reloads of
an unchanged config at ~350 requests/s). `/api/ready` is answered 404 by Caddy: the probe is
for loopback only.

## Host install (once, with sudo)

Before the release with this change is deployed (the standby upstream is new in the
Caddyfile; nothing listens there until a deploy starts it, so the reload changes nothing
for readers):

```
R=<a checkout or release of this change>
python3 $R/deploy/render.py --out /tmp/plx-units
sudo install -m 644 /tmp/plx-units/plx-api@.service /etc/systemd/system/plx-api@.service
sudo install -m 644 /tmp/plx-units/plx-api.service.d/zz-stop.conf /etc/systemd/system/plx-api.service.d/
sudo systemctl daemon-reload
$R/deploy/release/plx-release slots sync     # writes plx-api@.service.d, prints the state
```

Then deploy the release as usual: its first `plx-release restart` reloads Caddy (the Caddyfile
changed), and from the next restart on the API switches warm. To switch warm on the first one
too, deploy with `PLX_DEPLOY_NO_RESTART=1`, `sudo systemctl reload plx-caddy`, then
`plx-release restart`.

Undo: `sudo rm -r /etc/systemd/system/plx-api@.service /etc/systemd/system/plx-api@.service.d
/etc/systemd/system/plx-api.service.d/zz-stop.conf /run/plx-api && sudo systemctl daemon-reload`
(the next restart is in place again: "plx-api@.service is not installed"). The Caddyfile's
standby upstream is harmless with nothing on its socket.
