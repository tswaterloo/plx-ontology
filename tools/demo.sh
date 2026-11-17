#!/bin/bash
# The standing mock demo (blueprint C5): the API on demo data and the app over it, one command.
#
#   tools/demo.sh [--alarms] [--check]
#
# Starts tools/demo_api.py (the mock backend as the demo owner: fixture data only, every
# credential variable removed, a scripted drafting model, no live-send switch: a confirm logs
# a dry run) and `next dev` of app/ over it, waits until the sign-in page answers, prints the
# URL and keeps both running until Ctrl-C or the terminal closes; then stops both process
# groups and removes its state. --alarms keeps two ops alarms open (the Health page);
# --check stops after the URL answers (exit 0) instead of waiting.
# Environment: DEMO_API_PORT (8823), DEMO_APP_PORT (3323), DEMO_HOST (127.0.0.1; reach it
# from a laptop with `ssh -L 3323:127.0.0.1:3323 <host>`), DEMO_PYTHON
# (/data/venv/bin/python3), DEMO_NODE (/data/tools/node-v22.23.4-linux-x64/bin),
# DEMO_NODE_MODULES (an existing app/node_modules, hard-linked when app/ has none).
set -euo pipefail

check=0
alarms=0
for arg in "$@"; do
  case "$arg" in
    --check) check=1 ;;
    --alarms) alarms=1 ;;
    *) echo "usage: $0 [--alarms] [--check]" >&2; exit 2 ;;
  esac
done

tree=$(git -C "$(dirname "$(readlink -f "$0")")/.." rev-parse --show-toplevel)
api_port=${DEMO_API_PORT:-8823}
app_port=${DEMO_APP_PORT:-3323}
host=${DEMO_HOST:-127.0.0.1}
py=${DEMO_PYTHON:-/data/venv/bin/python3}
[ -x "$py" ] || py=$(command -v python3)
export PATH="${DEMO_NODE:-/data/tools/node-v22.23.4-linux-x64/bin}:$PATH" NEXT_TELEMETRY_DISABLED=1
# Fixture data only: no key, secret, token or DSN reaches either process.
while read -r var; do unset "$var"; done < <(compgen -e | grep -E '(KEY|SECRET|TOKEN|PASSWORD|PASSWD|DSN|CREDENTIAL)|^PLX_ACTIONS_|^AZURE_|^PG' || true)

for port in "$api_port" "$app_port"; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "port $port is in use (set DEMO_API_PORT / DEMO_APP_PORT)" >&2; exit 1
  fi
done

state=$(mktemp -d)
linked=""
api="" app=""
stop() {
  for group in $api $app; do kill -TERM -- "-$group" 2>/dev/null || true; done
  sleep 1
  for group in $api $app; do kill -KILL -- "-$group" 2>/dev/null || true; done
  [ -z "$linked" ] || rm -rf "$linked"
  rm -rf "$state"
}
trap stop EXIT
trap 'exit 130' INT TERM HUP

if [ ! -d "$tree/app/node_modules" ]; then
  from=${DEMO_NODE_MODULES:-}
  if [ -z "$from" ]; then
    for candidate in "$tree"/../team-dashboard/app/node_modules "$tree"/wt/*/app/node_modules "$tree"/../*/app/node_modules; do
      [ -d "$candidate" ] && [ ! -L "$candidate" ] && { from=$candidate; break; }
    done
  fi
  [ -d "$from" ] || { echo "no app/node_modules to link from (set DEMO_NODE_MODULES)" >&2; exit 1; }
  cp -al "$from" "$tree/app/node_modules"
  linked="$tree/app/node_modules"
fi

# Each server in its own process group, so stopping reaches next dev's children too.
(cd "$tree" && DEMO_ALARMS=$alarms exec setsid "$py" tools/demo_api.py "$api_port" "$state") \
  > "$state/api.log" 2>&1 &
api=$!
(cd "$tree/app" && PLX_API_ORIGIN="http://127.0.0.1:$api_port" exec setsid npx next dev -p "$app_port" -H "$host") \
  > "$state/app.log" 2>&1 &
app=$!

url="http://$host:$app_port"
for _ in $(seq 120); do
  if ! kill -0 "$api" 2>/dev/null || ! kill -0 "$app" 2>/dev/null; then
    echo "a server stopped while starting:" >&2; tail -n 20 "$state/api.log" "$state/app.log" >&2; exit 1
  fi
  if curl -sf -o /dev/null "http://127.0.0.1:$api_port/api/health" && curl -sf -o /dev/null "$url/signin"; then
    echo "Demo ready: $url  (demo data, mock API on :$api_port; Ctrl-C stops both)"
    [ "$check" = 1 ] && exit 0
    wait "$app"
    exit 0
  fi
  sleep 2
done
echo "the demo did not answer within 240 s:" >&2
tail -n 20 "$state/api.log" "$state/app.log" >&2
exit 1
