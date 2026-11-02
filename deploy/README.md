# Deploying the PLX Workbench app on this server

One HTTPS link, two local processes behind Caddy:

| Piece | Listens on | Unit |
|---|---|---|
| Caddy (TLS, reverse proxy) | :443 and :80 on 127.0.0.1 + this machine's Tailscale addresses only | `deploy/systemd/plx-caddy.service` |
| Web app (`app/`, Next.js) | 127.0.0.1:3000 | `deploy/systemd/plx-app.service` |
| API (`operator_api`) | 127.0.0.1:8780 | `deploy/systemd/plx-api.service` |

`/api/*` goes to the API (the answer stream is passed through unbuffered);
everything else goes to the web app. Both are same-origin, which the API's
session cookie (`Path=/api`, `SameSite=Strict`) and its Origin check require.

**Which origins may POST** (sign-in, Ask, feedback): the page's own origin only, i.e. an
`Origin` whose host equals the host the browser addressed. That host is
`X-Forwarded-Host` when the proxy in front set one (Caddy, which keeps `Host` and sets the
same value; or the app's own `/api` rewrite under `next start`, which sends the API's
loopback address as `Host` and the browser's as `X-Forwarded-Host`), else `Host`. A
cross-site page cannot set that header (no CORS preflight is granted), so nothing else is
trusted; a header naming several hosts is ignored. `--allowed-origin` adds one exact origin
and is not needed in either topology. The session cookie is `Secure`: serve the app over
HTTPS (Caddy); plain-http access (e.g. `http://<tailscale-ip>:3000` from a phone) needs
`--insecure-cookie` and is for demos on a trusted network only.

The units are **not enabled** by the build. Nothing here writes to a PLX system.

## One-time setup (owner, with sudo)

1. Node 20+ (this server: `/data/tools/node`, Node 22 LTS) and Caddy 2.7+
   (`apt install caddy` from the Caddy repository, or the release binary in
   `/usr/bin/caddy`). The API's Python needs `fastapi` and `uvicorn`.
2. A dedicated unprivileged user for the app and the API (no shell, no home
   to write), with read access to the checkout and Node:
   ```sh
   sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin plx-workbench
   # read-only access by ACL; ownership of the checkout is unchanged
   sudo setfacl -R -m g:plx-workbench:rX -m d:g:plx-workbench:rX /data/OntologyProject /data/tools/node
   # Caddy's user, if the Caddy package did not create it:
   getent passwd caddy || sudo useradd --system --user-group --home-dir /var/lib/caddy --shell /usr/sbin/nologin caddy
   sudo install -d -o caddy -g caddy -m 0750 /var/lib/caddy
   ```
   The API reads Postgres over the local socket as its OS user, so it needs a
   role of its own (`sudo -u postgres createuser plx-workbench`) with the
   least grants the API needs; the database's permission model is owned by
   the Postgres platform work, so take the grants from there.
3. Build the app, and give the service its one writable directory:
   ```sh
   cd /data/OntologyProject/app
   PATH=/data/tools/node/bin:$PATH npm ci && npm run build
   sudo install -d -o plx-workbench -g plx-workbench -m 0750 .next/cache
   ```
4. Tailscale certificate access for Caddy (for `*.ts.net` names):
   add `TS_PERMIT_CERT_UID=caddy` to `/etc/default/tailscaled`, then
   `sudo systemctl restart tailscaled`. Turn on HTTPS certificates for the
   tailnet in the Tailscale admin console (DNS → HTTPS Certificates) if it is off.
5. Configuration files (no secrets in the repository):
   ```sh
   sudo mkdir -p /etc/plx
   # Caddy: the host name to serve, and the ONLY addresses to listen on besides
   # 127.0.0.1: this machine's Tailscale addresses. Never a public interface.
   {
     echo "PLX_APP_HOST=$(tailscale status --self --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
     echo "PLX_BIND_ADDRS=\"$(tailscale ip -4) $(tailscale ip -6)\""   # quoted: also sourced by sh
   } | sudo tee /etc/plx/workbench-caddy.env
   # API: its serve flags (see `python3 -m operator_api serve --help`).
   echo 'PLX_API_ARGS=--mock' | sudo tee /etc/plx/workbench-api.env      # demo over fixtures
   ```
   For Microsoft sign-in, `PLX_API_ARGS` names the Entra resource file and
   state directory (`operator_api/server.py`), and the Entra app
   registration needs the redirect URI `https://<host>/signin` (SPA platform).
6. Install the units:
   ```sh
   sudo cp deploy/systemd/plx-*.service /etc/systemd/system/
   sudo systemctl daemon-reload
   ```
   If the distro's `caddy.service` is already running, either stop and disable
   it, or point it at `deploy/Caddyfile` instead of using `plx-caddy.service`.

## Start, stop, status

```sh
sudo systemctl start plx-api plx-app plx-caddy      # start
sudo systemctl stop plx-caddy plx-app plx-api       # stop
sudo systemctl restart plx-app                      # after a new build
sudo systemctl reload plx-caddy                     # after a Caddyfile change
systemctl status plx-api plx-app plx-caddy
journalctl -u plx-app -u plx-api -u plx-caddy -f    # logs
```

To start them at boot: `sudo systemctl enable plx-api plx-app plx-caddy`
(a decision; the build does not do it).

## What Caddy listens on

`default_bind 127.0.0.1 {$PLX_BIND_ADDRS}` in the Caddyfile applies to every
listener, and the HTTP→HTTPS redirect is an explicit site block, so
`caddy adapt` shows all of them. With this machine's values
(2026-10-29: `tailscale ip -4` <tailscale-ipv4>, `-6` <tailscale-ipv6>):

```text
srv0 (https) 127.0.0.1:443, <tailscale-ipv4>:443, [<tailscale-ipv6>]:443
srv1 (redirect) 127.0.0.1:80, <tailscale-ipv4>:80, [<tailscale-ipv6>]:80
admin 127.0.0.1:2019
```

With `PLX_BIND_ADDRS` unset Caddy binds 127.0.0.1 only. Re-check after any
change:

```sh
set -a; . /etc/plx/workbench-caddy.env; set +a
caddy adapt --config deploy/Caddyfile --adapter caddyfile \
  | python3 -c 'import json,sys;[print(n,s["listen"]) for n,s in json.load(sys.stdin)["apps"]["http"]["servers"].items()]'
sudo ss -ltnp | grep caddy        # after start: the same addresses, nothing on 0.0.0.0 or [::]
```

TLS for the `*.ts.net` name comes from tailscaled (Caddy's built-in Tailscale
certificate support); nothing is requested from a public CA.

## Check it

```sh
curl -sI https://<host>/signin          # 200 from the app
curl -s  https://<host>/api/health      # {"status":"ok"} from the API
caddy validate --config deploy/Caddyfile --adapter caddyfile
```

Then open `https://<host>/` on a laptop or phone signed in to the tailnet.
In `--mock` or `--dev` mode every page shows the amber **DEV mode** banner
with the data label (e.g. FIXTURE); staff should only ever use Entra mode.

## Notes

- Caddy keeps no access log by default here: request lines carry questions
  and record ids. Turn one on deliberately if needed, and treat it as business data.
- Content-Security-Policy: the app sets a per-request nonce policy
  (`app/src/proxy.ts`, `app/src/lib/csp.ts`); Caddy adds a deny-all policy on `/api/*`.
- Backups and security scans are tracked outside this module.

## Search

The app's agents and tools search with hybrid search (`operating/search`: BM25, trigram,
bge-m3 dense, exact identifiers and record links, then the authority order of
`operating/search/weights.json`) when the API serves a live host (`--host-config`). The API
sets `PLX_SEARCH=hybrid` (and `PLX_PG_DSN` from `--pg-dsn`) in its own environment unless
`PLX_SEARCH` is already set, and every agent host it starts inherits them
(`operator_api.backend.default_search`, `operating.agent_session.SEARCH_ENV`).

- Turn it off: `PLX_SEARCH=keyword` in `/etc/plx/workbench-api.env`, then
  `sudo systemctl restart plx-api`. Any value other than `hybrid` is the word index alone.
- `--mock` keeps the word index (its fixtures have no search tables).
- A search that cannot run hybrid (no database, a timeout) falls back to the word index and
  the answer says so (`search_fallback`).
- The models (about 3.4 GB) load once per host user in `operating.search.model_server`,
  started by the first search and stopped after 30 idle minutes; every API, agent and
  scoreboard process asks it through a private spool directory instead of loading its own
  copy.
  `PLX_SEARCH_MODELS=local` loads them per process (avoid: 3.4 GB each).
- `PLX_APP_HYBRID_SEARCH` is no longer read; remove it from the env file.

## Demo host

The running demo's host-specific setup (checkout, user, keys, access link) is in
[`DEMO-HOST.md`](DEMO-HOST.md).

## Adding the staff domain later (with Vince)

The default is deliberately Tailscale-only. A staff domain is a separate,
reviewed change:

1. Decide where staff reach it from (office network, VPN, or the internet) and
   the address it should listen on. Add that address, and only that one, to
   `PLX_BIND_ADDRS` (or give the new site block its own `bind <address>`).
2. Point the domain's DNS at that address.
3. In the Caddyfile, uncomment the staff site block, set the name, and add an
   `http://<domain>` redirect block like the Tailscale one. Caddy then gets a
   public certificate for it (ACME HTTP-01 or TLS-ALPN on that address), so
   ports 80/443 must be reachable there.
4. Add `https://<domain>/signin` as a redirect URI in the Entra app
   registration, and `--allowed-origin https://<domain>` to `PLX_API_ARGS`
   if the API is told about origins explicitly.
5. Re-run the `caddy adapt` check above and `sudo ss -ltnp | grep caddy`, and
   record the listeners in this README.

## Continuous ingest (`deploy/systemd/plx-ingest.service`)

Every capture source in its own loop, all at once, one writer per store (each source is
one capture process writing its own store; the mail capture reads eight mailboxes at once
through its one store connection), and the projection built from store snapshots on its
own thread, so mail never waits for SharePoint or for a build. Replaces the serial
`operating.host run` cycle (`cold_cycle.sh`); never run both on one state directory (the
host's cycle lock refuses the second).

1. Wait for the running cycle to end (or stop it); nothing else may hold
   `<state>/host/cycle.lock`.
2. Write the continuous config from the cycle config (outside Git, mode 0600):
   ```sh
   python3 scripts/ingest_continuous_config.py --from /data/scratch/cfg/ingest_cold.local.json \
       --out /data/scratch/cfg/ingest_continuous.local.json --priority-mailbox <first mailbox> ...
   ```
   It keeps every scope, audience and budget, adds each source's cadence
   (`min_interval_seconds`: mail and SharePoint back to back, Teams every 30 min) and
   `ingest.build_inputs: "snapshot"`; with `--priority-mailbox` it writes the mail scope
   beside it with that `priority` order.
3. `/etc/plx/ingest.env`: `PLX_INGEST_STATE=/data/plx-state` and
   `PLX_INGEST_CONFIG=/data/scratch/cfg/ingest_continuous.local.json`; then
   `sudo cp deploy/systemd/plx-ingest.service /etc/systemd/system/ && sudo systemctl
   daemon-reload && sudo systemctl start plx-ingest`.
4. Watch: `python3 -m operating.host status --state-dir /data/plx-state` (per source its
   generation, last capture and blocked parts) and `journalctl -u plx-ingest` (one JSON line
   per refresh: counts and codes only). Stop cleanly with `touch
   /data/plx-state/host/ingest.stop` (remove it before the next start).
