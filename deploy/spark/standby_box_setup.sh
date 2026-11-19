#!/bin/bash
# One-time box setup for the Spark hot standby, run on the box by the login that runs
# plx-spark-standby-tunnel@ (its ssh client is what connects to the box socket):
#   - role plx_standby (LOGIN REPLICATION, no password);
#   - pg_hba: 'local replication plx_standby peer map=plx_standby', inserted BEFORE the
#     first 'local replication all peer' line (the first matching line wins, so an appended
#     line would never be reached and the standby would be refused);
#   - pg_ident: 'plx_standby <login> plx_standby'.
# Both files are backed up first; the edited files are checked (pg_hba_file_rules,
# pg_ident_file_mappings read the files on disk) before the reload, and put back on any
# error. Idempotent. Prints the undo.
#
#   deploy/spark/standby_box_setup.sh            (dry run: shows what it would change)
#   deploy/spark/standby_box_setup.sh --go
set -euo pipefail
go=${1:-}
login=$(id -un)
pg() { sudo -n -u postgres psql -d postgres -XAtv ON_ERROR_STOP=1 "$@"; }
hba=$(pg -c 'SHOW hba_file')
ident=$(pg -c 'SHOW ident_file')
rule="local   replication     plx_standby                             peer map=plx_standby"
map="plx_standby     $login     plx_standby"
anchor=$(sudo -n -u postgres grep -nE '^[[:space:]]*local[[:space:]]+replication[[:space:]]+all[[:space:]]+peer([[:space:]]|$)' "$hba" | head -1 | cut -d: -f1 || true)
has_rule=$(sudo -n -u postgres grep -cE '^[[:space:]]*local[[:space:]]+replication[[:space:]]+plx_standby[[:space:]]' "$hba" || true)
has_map=$(sudo -n -u postgres grep -cE "^[[:space:]]*plx_standby[[:space:]]+$login[[:space:]]+plx_standby[[:space:]]*$" "$ident" || true)
has_role=$(pg -c "SELECT count(*) FROM pg_roles WHERE rolname = 'plx_standby'")
echo "hba $hba: anchor line ${anchor:-none}, rule present $has_rule; ident $ident: map present $has_map; role present $has_role"
if [[ $go != --go ]]; then echo "dry run (pass --go to apply)"; exit 0; fi

[[ $has_role == 1 ]] || pg -c 'CREATE ROLE plx_standby LOGIN REPLICATION'
stamp=$(date -u +%Y%m%d%H%M%S)
changed=
if [[ $has_rule == 0 ]]; then
  sudo -n -u postgres cp -p "$hba" "$hba.bak-$stamp"
  if [[ -n $anchor ]]; then
    sudo -n -u postgres sed -i "${anchor}i ${rule}" "$hba"
  else
    printf '%s\n' "$rule" | sudo -n -u postgres tee -a "$hba" >/dev/null
  fi
  changed+=" hba"
fi
if [[ $has_map == 0 ]]; then
  sudo -n -u postgres cp -p "$ident" "$ident.bak-$stamp"
  printf '%s\n' "$map" | sudo -n -u postgres tee -a "$ident" >/dev/null
  changed+=" ident"
fi
undo() {
  [[ $changed == *hba* ]] && sudo -n -u postgres cp -p "$hba.bak-$stamp" "$hba"
  [[ $changed == *ident* ]] && sudo -n -u postgres cp -p "$ident.bak-$stamp" "$ident"
  return 0
}
errs=$(pg -c "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL")
errs=$((errs + $(pg -c "SELECT count(*) FROM pg_ident_file_mappings WHERE error IS NOT NULL")))
mine=$(pg -c "SELECT min(line_number) FROM pg_hba_file_rules WHERE type = 'local' AND 'replication' = ANY(database) AND 'plx_standby' = ANY(user_name)")
all=$(pg -c "SELECT min(line_number) FROM pg_hba_file_rules WHERE type = 'local' AND 'replication' = ANY(database) AND 'all' = ANY(user_name)")
if (( errs > 0 )) || [[ -z $mine ]] || { [[ -n $all ]] && (( mine > all )); }; then
  undo
  echo "standby_box_setup: edited files rejected (errors $errs, plx_standby line ${mine:-none}, all line ${all:-none}); files put back, nothing reloaded" >&2
  exit 1
fi
pg -c 'SELECT pg_reload_conf()' >/dev/null
echo "standby_box_setup: role plx_standby ok; changed:${changed:- nothing}; plx_standby replication rule at line $mine (before ${all:-no} 'all'); reloaded"
echo "undo:"
[[ $changed == *hba* ]] && echo "  sudo -u postgres cp -p $hba.bak-$stamp $hba"
[[ $changed == *ident* ]] && echo "  sudo -u postgres cp -p $ident.bak-$stamp $ident"
echo "  sudo -u postgres psql -d postgres -c 'SELECT pg_reload_conf()'   # then, once no slot or standby needs it:"
echo "  sudo -u postgres psql -d postgres -c 'DROP ROLE plx_standby'"
