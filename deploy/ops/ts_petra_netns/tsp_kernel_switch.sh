#!/bin/bash
# Switch the Petra node from the user userspace tailscaled to a root kernel-mode one inside netns tsp
# (same state = same node), with host 127.0.0.1:1055/1056 proxied to it; verify, roll back on failure.
D=$(dirname "$(readlink -f "$0")"); L=${TSP_LOG:-/data/scratch/infra-arch-1121/tsp_switch.log}
ST=/data/plx-state/ts-petra
rollback() {
  echo "$(date '+%F %T') ROLLBACK: $1" >> $L
  sudo systemctl stop ts-petra-k 2>/dev/null; sudo systemctl reset-failed ts-petra-k 2>/dev/null
  systemctl --user stop tsp-socks-proxy tsp-http-proxy 2>/dev/null
  systemctl --user reset-failed tsp-socks-proxy tsp-http-proxy 2>/dev/null
  sudo chown -R "$(stat -c %U:%G "$(dirname "$ST")")" $ST
  $D/tsp_switch.sh "" >> $L 2>&1; echo ROLLED_BACK; exit 1
}
echo "$(date '+%F %T') kernel switch start" >> $L
systemctl --user stop ts-petra; sleep 1; systemctl --user reset-failed ts-petra 2>/dev/null
sudo systemd-run --unit ts-petra-k -p CPUWeight=1000 -p Restart=on-failure /usr/sbin/ip netns exec tsp /usr/sbin/tailscaled \
  --state=$ST/tailscaled.state --socket=$ST/tailscaled.sock --statedir=$ST --port=41642 --tun=tspts \
  --socks5-server=10.201.0.2:1055 --outbound-http-proxy-listen=10.201.0.2:1056 >/dev/null 2>&1 || rollback "start failed"
for i in $(seq 1 40); do sudo ip netns exec tsp ss -ltn 2>/dev/null | grep -q "10.201.0.2:1055" && break; sleep 1; done
systemd-run --user --unit tsp-socks-proxy -p CPUWeight=1000 /usr/bin/systemd-socket-activate -l 127.0.0.1:1055 /lib/systemd/systemd-socket-proxyd 10.201.0.2:1055 >/dev/null 2>&1 || rollback "socks proxy"
systemd-run --user --unit tsp-http-proxy /usr/bin/systemd-socket-activate -l 127.0.0.1:1056 /lib/systemd/systemd-socket-proxyd 10.201.0.2:1056 >/dev/null 2>&1 || rollback "http proxy"
sleep 3
for i in $(seq 1 30); do sudo ip netns exec tsp tailscale --socket=$ST/tailscaled.sock status 2>/dev/null | grep -q "active" && break; sleep 1; done
ok=1
timeout 30 ssh -F $ST/ssh_config -o ConnectTimeout=20 s7d3d true || { ok=0; echo "ssh s7d3d failed" >> $L; }
[ $ok = 1 ] && { timeout 40 ssh -F $ST/ssh_config -o ConnectTimeout=20 sb4ec true || { ok=0; echo "ssh sb4ec failed" >> $L; }; }
[ $ok = 1 ] && { python3 - <<'PY' || { ok=0; echo "portal replica failed" >> $L; }
import socket, struct, sys
s = socket.create_connection(("127.0.0.1", 15433), timeout=20); s.settimeout(20)
s.sendall(struct.pack(">II", 8, 80877103)); sys.exit(0 if s.recv(1) in (b"S", b"N") else 1)
PY
}
[ $ok = 1 ] || rollback "verify failed"
echo "$(date '+%F %T') kernel switch OK" >> $L; echo OK
