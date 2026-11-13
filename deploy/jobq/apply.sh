#!/bin/bash
# Install, switch on or revert the PLX host job queue.
#
#  apply.sh --install   create the queue database (plx_jobq) and schema, render and install
#                        plx-jobq-worker.service + plx-jobq-enqueue@.service and the polkit
#                        rule, start the worker. Timers still start their services directly.
#   apply.sh --switch    timers/path units of delta, proactive-whole, proactive-inc and
#                        search-follow queue a job instead (drop-ins from switch/), and
#                        PLX_JOBQ=1 is written to ~/.config/plx/jobq.env for testslot and
#                        land.sh callers that source it.
#   apply.sh --revert    remove the switch drop-ins (timers start services directly again)
#                        and PLX_JOBQ; the worker keeps running harmlessly (stop it by hand).
#   apply.sh --check     print what is installed and the queue status.
#
#Env: REPO (default: this file's checkout), PLX_USER (default: the caller), DB (plx_jobq).
set -euo pipefail
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
repo=${REPO:-$(git -C "$here" rev-parse --show-toplevel)}
user=${PLX_USER:-$(id -un)}
db=${DB:-plx_jobq}
py=/data/venv/bin/python3
etc=/etc/systemd/system
units=(plx-delta.timer plx-proactive-whole.timer plx-proactive-inc.timer plx-proactive-inc.path
       plx-search-follow.timer plx-search-follow.path)
envfile=$HOME/.config/plx/jobq.env

render() {  # deploy/systemd/<unit> -> $etc/<unit> with the same placeholders as deploy/render.py
  local out; out=$(mktemp -d)
  "$py" "$repo/deploy/render.py" --out "$out" >/dev/null
  sudo install -m 0644 "$out/$1" "$etc/$1"
  rm -rf "$out"
}

case "${1:-}" in
  --install)
    psql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1 \
      || createdb "$db"
    (cd "$repo" && PLX_JOBQ_DSN="dbname=$db" "$py" -m operating.jobq init)
    render plx-jobq-worker.service
    render plx-jobq-enqueue@.service
    sed "s/@PLX_USER@/$user/g" "$here/50-plx-jobq.rules" \
      | sudo tee /etc/polkit-1/rules.d/50-plx-jobq.rules >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --now plx-jobq-worker.service
    echo "installed; timers unchanged until --switch"
    ;;
  --switch)
    systemctl is-active -q plx-jobq-worker.service || { echo "worker not running: --install first" >&2; exit 1; }
    for unit in "${units[@]}"; do
      sudo install -d "$etc/$unit.d"
      sudo install -m 0644 "$here/switch/$unit.d/50-jobq.conf" "$etc/$unit.d/50-jobq.conf"
    done
    sudo systemctl daemon-reload
    for unit in "${units[@]}"; do sudo systemctl restart "$unit"; done
    mkdir -p "$(dirname "$envfile")"; echo "PLX_JOBQ=1" > "$envfile"
    echo "switched: timers queue jobs; export PLX_JOBQ=1 (or source $envfile) for testslot/land.sh"
    ;;
  --revert)
    for unit in "${units[@]}"; do sudo rm -f "$etc/$unit.d/50-jobq.conf"; done
    sudo systemctl daemon-reload
    for unit in "${units[@]}"; do sudo systemctl restart "$unit"; done
    rm -f "$envfile"
    echo "reverted: timers start their services directly"
    ;;
  --check)
    for unit in "${units[@]}"; do
      printf '%-28s %s\n' "$unit" "$(systemctl show -p Triggers --value "$unit")"
    done
    systemctl is-active plx-jobq-worker.service || true
    (cd "$repo" && PLX_JOBQ_DSN="dbname=$db" "$py" -m operating.jobq status) || true
    ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
