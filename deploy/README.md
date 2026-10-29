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
     echo "PLX_BIND_ADDRS=$(tailscale ip -4) $(tailscale ip -6)"
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
