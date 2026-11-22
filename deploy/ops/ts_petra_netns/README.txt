The Petra tailnet node on the box, kernel mode inside network namespace "tsp" with BBR (11-21).
Why: box -> Spark ran 22-28 Mbit/s per TCP flow in userspace mode (and 28-32 in kernel mode with
cubic: path loss held the window at ~60 packets); kernel mode + BBR in the namespace: 307-315
Mbit/s; the standby's WAL 2.95 -> 36.8 MB/s. Only the namespace uses BBR; the host keeps cubic.
Layout: veth tsp0 10.201.0.1/30 (host) <-> tsp1 10.201.0.2 (ns), NAT out enp39s0, ip_forward=1;
root unit ts-petra-k runs tailscaled in the namespace with the same state (same node); user
units tsp-socks-proxy/tsp-http-proxy carry host 127.0.0.1:1055/1056 into it, so ssh_config and
the Portal replica tunnel are unchanged.
After a reboot:  tsp_netns_up.sh && tsp_kernel_switch.sh, then restart the plx-spark-*-tunnel units.
Roll back:       tsp_netns_down.sh && tsp_switch.sh ""   (the old userspace user unit ts-petra)
Boot (11-22): system units plx-ts-petra-netns (netns_ensure.sh, idempotent), plx-ts-petra (tailscaled in
the namespace), plx-ts-petra-socks-proxy / plx-ts-petra-http-proxy; installed under
/usr/local/lib/plx-ops/ts_petra_netns and enabled. The live node still runs as the transient units
ts-petra-k / tsp-socks-proxy / tsp-http-proxy until the next reboot or a planned switch
(stop those three, start the four). Roll back for good: systemctl disable --now plx-ts-petra-socks-proxy
plx-ts-petra-http-proxy plx-ts-petra plx-ts-petra-netns; tsp_netns_down.sh; tsp_switch.sh "".
The two proxy units carry @PLX_USER@ (as deploy/systemd does): substitute the service login when installing.
