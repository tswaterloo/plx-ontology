#!/bin/bash
# Every 5 min (oldmail-watch.timer): a build that records no unit for 15 min, or an embed that stores
# no vector for 20 min, is restarted (it resumes: recorded units are skipped, the embed follows its
# cursor); every check is one line in ~/oldmail-store/watch.log.
set -uo pipefail
export LD_LIBRARY_PATH=$HOME/plx-replica/pg17/lib
Q="$HOME/plx-replica/pg17/bin/psql -X -h $HOME/oldmail-store/run -p 55710 -d plx -At"
L=$HOME/oldmail-store/watch.log
now=$(date -u +%FT%TZ)
if ! $Q -c "SELECT 1" >/dev/null 2>&1; then echo "$now store_unreachable $(systemctl --user is-active oldmail-store)" >> $L; exit 0; fi
units=$($Q -c "SELECT count(*) || ' ' || coalesce(extract(epoch FROM now() - max(done_at))::int, -1) FROM search.oldmail_units")
left=$($Q -c "SELECT count(DISTINCT p.unit) FROM search.oldmail_plan p WHERE NOT EXISTS (SELECT 1 FROM search.oldmail_units u WHERE u.unit = p.unit)")
build=$(systemctl --user is-active oldmail-build); embed=$(systemctl --user is-active oldmail-embed)
age=${units#* }; act=none
if [ "$build" = active ] && [ "$age" -gt 900 ] && [ "$(systemctl --user show -p ActiveEnterTimestampMonotonic --value oldmail-build)" -lt $(( $(awk '{print int($1)}' /proc/uptime) * 1000000 - 900000000 )) ]; then
  systemctl --user restart oldmail-build; act=build_restarted_no_unit_15min
fi
elog=$HOME/oldmail-store/embed.log
if [ "$embed" = active ] && [ -f $elog ] && [ $(( $(date +%s) - $(stat -c %Y $elog) )) -gt 1200 ] && ! tail -1 $elog | grep -q waiting; then
  systemctl --user restart oldmail-embed; act=embed_restarted_no_report_20min
fi
echo "$now units_done=${units% *} last_unit_age_s=$age units_left=$left build=$build embed=$embed avail_gb=$(awk '/MemAvailable/ {print int($2/1048576)}' /proc/meminfo) free_gb=$(df -BG --output=avail $HOME/oldmail-store | tail -1 | tr -dc 0-9) action=$act" >> $L
