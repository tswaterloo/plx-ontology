"""Credential-shaped environment names: the one pattern every keyless context applies.

The write twin's test-mode refusal (``twin/mode.py``) imports it from here, so product
code does not depend on tooling and a tooling change does not select
every test that reaches the twin. Stdlib only, no other first-party import.
"""
from __future__ import annotations

import re

# Credential-shaped names:
# no test run or full suite sees them. A name pattern, not a list of known
# names, so a new key (BRAIN_API_KEY, a session-signing secret, a --key-env name) is
# stripped without anyone remembering to add it. Git's injected config (GIT_CONFIG_COUNT,
# GIT_CONFIG_KEY_<n>, GIT_CONFIG_VALUE_<n>) goes as a whole: a VALUE can carry an
# http.extraheader, and stripping only the KEYs makes every git call fail. A database URL
# carries its password under a name no key word matches (DATABASE_URL, POSTGRES_URL,
# POSTGRES_URL_NON_POOLING, DIRECT_URL, DB_URL, REPORTING_DATABASE_URL, PGURL), and
# PGPASSFILE names a password file: a test run never reaches a live database.
KEYLESS = re.compile(r"^(AWS_.*|AZURE_.*|GOOGLE_.*|CLOUDSDK_.*|GH_.*|GITHUB_TOKEN|ANTHROPIC_.*"
                     r"|CLAUDE_CODE_OAUTH.*|MICROSOFT_GRAPH_.*|BC_.*|VMC_.*"
                     r"|MC_.*|BRAIN_.*|PLX_WORKBENCH_.*|GIT_CONFIG_COUNT|GIT_CONFIG_VALUE_.*"
                     r"|.*(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|COOKIE).*"
                     r"|(.*_)?(DATABASE|POSTGRES(QL)?|PG|DB|DIRECT)(_\w+)?_URL(_\w+)?|PGURL|PGPASSFILE"
                     r")$", re.IGNORECASE)
# What a development SHELL keeps although KEYLESS names it: git's
# injected config and the GitHub token, which are needed to commit, push and read CI.
# Test runs strip them too.
SHELL_KEEP = re.compile(r"^(GIT_CONFIG_COUNT|GIT_CONFIG_KEY_.*|GIT_CONFIG_VALUE_.*|GH_TOKEN"
                        r"|GITHUB_TOKEN)$")
