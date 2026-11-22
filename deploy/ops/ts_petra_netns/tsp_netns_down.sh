#!/bin/bash
# undo the netns plumbing for the kernel-mode Petra node (safe to run twice)
sudo systemctl stop ts-petra-k 2>/dev/null; sudo systemctl reset-failed ts-petra-k 2>/dev/null
systemctl --user stop tsp-socks-proxy tsp-http-proxy 2>/dev/null
sudo iptables -t nat -D POSTROUTING -s 10.201.0.0/30 -o enp39s0 -j MASQUERADE 2>/dev/null
sudo iptables -D FORWARD -i tsp0 -j ACCEPT 2>/dev/null; sudo iptables -D FORWARD -o tsp0 -j ACCEPT 2>/dev/null
sudo ip link del tsp0 2>/dev/null; sudo ip netns del tsp 2>/dev/null
sudo sysctl -q -w net.ipv4.ip_forward=0
sudo chown -R "$(stat -c %U:%G /data/plx-state)" /data/plx-state/ts-petra
echo down
