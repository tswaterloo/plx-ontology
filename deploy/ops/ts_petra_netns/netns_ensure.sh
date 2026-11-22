#!/bin/bash
# Idempotent: netns tsp + veth + NAT + BBR inside the namespace (boot unit plx-ts-petra-netns).
set -e
ip netns list | grep -qw tsp || ip netns add tsp
if ! ip link show tsp0 >/dev/null 2>&1; then
  ip link add tsp0 type veth peer name tsp1
  ip link set tsp1 netns tsp
  ip addr add 10.201.0.1/30 dev tsp0
fi
ip link set tsp0 up
ip netns exec tsp ip addr show tsp1 | grep -q 10.201.0.2 || ip netns exec tsp ip addr add 10.201.0.2/30 dev tsp1
ip netns exec tsp ip link set tsp1 up; ip netns exec tsp ip link set lo up
ip netns exec tsp ip route replace default via 10.201.0.1
mkdir -p /etc/netns/tsp; grep -qs nameserver /etc/netns/tsp/resolv.conf || echo "nameserver 172.31.0.2" > /etc/netns/tsp/resolv.conf
modprobe tcp_bbr; ip netns exec tsp sysctl -q -w net.ipv4.tcp_congestion_control=bbr
sysctl -q -w net.ipv4.ip_forward=1
iptables -C FORWARD -i tsp0 -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -i tsp0 -j ACCEPT
iptables -C FORWARD -o tsp0 -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -o tsp0 -j ACCEPT
iptables -t nat -C POSTROUTING -s 10.201.0.0/30 -o enp39s0 -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -s 10.201.0.0/30 -o enp39s0 -j MASQUERADE
