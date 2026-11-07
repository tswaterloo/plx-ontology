#!/bin/bash
# Every freshness target (operating/ops_health.py) over the
# live state dir, the API's page p95 and that it answers. Prints one line per check; exit 1
# on any breach, 2 when the state dir is unreadable. Sends nothing anywhere.
# Usage: ops_check.sh [--json]
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
cd "$PLX_CURRENT" 2>/dev/null || cd "$(dirname "$(readlink -f "$0")")/../.."
exec "$PLX_VENV/bin/python3" -m operating.ops_health check --state-dir "$PLX_STATE" \
  --api "$PLX_API_URL" --pg-dsn "${PLX_OPS_PG_DSN:-dbname=plx}" "$@"
