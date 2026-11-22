#!/bin/bash
set -e
sudo ip netns add tsp
sudo ip link add tsp0 type veth peer name tsp1
sudo ip link set tsp1 netns tsp
sudo ip addr add 10.201.0.1/30 dev tsp0; sudo ip link set tsp0 up
sudo ip netns exec tsp ip addr add 10.201.0.2/30 dev tsp1
sudo ip netns exec tsp ip link set tsp1 up; sudo ip netns exec tsp ip link set lo up
sudo ip netns exec tsp ip route add default via 10.201.0.1
sudo mkdir -p /etc/netns/tsp; echo "nameserver 172.31.0.2" | sudo tee /etc/netns/tsp/resolv.conf >/dev/null
sudo modprobe tcp_bbr; sudo ip netns exec tsp sysctl -q -w net.ipv4.tcp_congestion_control=bbr
sudo sysctl -q -w net.ipv4.ip_forward=1
sudo iptables -I FORWARD 1 -i tsp0 -j ACCEPT; sudo iptables -I FORWARD 1 -o tsp0 -j ACCEPT
sudo iptables -t nat -A POSTROUTING -s 10.201.0.0/30 -o enp39s0 -j MASQUERADE
echo up
