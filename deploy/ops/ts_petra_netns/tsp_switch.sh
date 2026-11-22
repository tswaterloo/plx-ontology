#!/bin/bash
# INFRA-ARCH 11-21: restart the Petra tailnet node (user unit ts-petra, userspace netstack) with extra env
# (e.g. TS_NETSTACK_ENABLE_RACK=true), verify, roll back to the plain command on any failure.
#   tsp_switch.sh "VAR=val ..."     (empty: the plain command = rollback)
L=${TSP_LOG:-/data/scratch/infra-arch-1121/tsp_switch.log}
ARGS=(--tun=userspace-networking --state=/data/plx-state/ts-petra/tailscaled.state --socket=/data/plx-state/ts-petra/tailscaled.sock --statedir=/data/plx-state/ts-petra --port=41642 --socks5-server=127.0.0.1:1055 --outbound-http-proxy-listen=127.0.0.1:1056)
TS="tailscale --socket=/data/plx-state/ts-petra/tailscaled.sock"
start() {  # $1: env words
  local envs=(); for kv in $1; do envs+=(-E "$kv"); done
  systemctl --user stop ts-petra 2>/dev/null; sleep 1; systemctl --user reset-failed ts-petra 2>/dev/null
  systemd-run --user --unit ts-petra -p CPUWeight=1000 "${envs[@]}" /usr/sbin/tailscaled "${ARGS[@]}" >/dev/null
}
check() {
  # wait for the first peer through the tailnet; the hosts are the ssh_config aliases below
  for i in $(seq 1 30); do $TS status 2>/dev/null | grep -q "active" && break; sleep 1; done
  ssh -F /data/plx-state/ts-petra/ssh_config -o ConnectTimeout=15 s7d3d true || { echo "ssh s7d3d failed"; return 1; }
  ssh -F /data/plx-state/ts-petra/ssh_config -o ConnectTimeout=15 sb4ec true || { echo "ssh sb4ec failed"; return 1; }
  python3 - <<'PY' || { echo "portal replica unreachable"; return 1; }
import socket, struct, sys
s = socket.create_connection(("127.0.0.1", 15433), timeout=15); s.settimeout(15)
s.sendall(struct.pack(">II", 8, 80877103)); r = s.recv(1)
sys.exit(0 if r in (b"S", b"N") else 1)
PY
  return 0
}
echo "$(date '+%F %T') switch env='$1'" >> $L
start "$1"
if check >> $L 2>&1; then echo "$(date '+%F %T') OK env='$1'" >> $L; echo OK
else
  echo "$(date '+%F %T') FAILED env='$1' -> rollback to plain" >> $L; start ""; check >> $L 2>&1 && echo "$(date '+%F %T') rollback OK" >> $L; echo ROLLED_BACK; exit 1
fi
