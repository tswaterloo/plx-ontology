#!/usr/bin/env python3
"""Value-based secret scan: refuse text that carries a live secret value.

A regex only catches the formats it knows (an Azure client secret, a storage key or a hex
HMAC secret match none of the usual ones), so this reads the live values themselves from the
host's env files and looks for each one, as a fixed string, in the text on stdin (a diff, a
file list's contents). It reads only names that look like credentials (SECRET, KEY, TOKEN,
PASSWORD, PASSWD, CREDENTIAL, a DSN carrying a password) plus the people-exclusion ID list,
and bare one-value files (``--value-file``). It never prints a value: a hit names the variable
and the added line's number.

Usage: git diff --cached -U0 | secret_value_scan.py --env-file F [--env-file F] [--value-file F]
Exit 0 clean, 1 a value was found, 2 the scan could not run (a named file unreadable, or no
file held a value): a scan that could not see every value must not pass as clean.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SECRET_NAME = re.compile(r"SECRET|KEY|TOKEN|PASSWORD|PASSWD|CREDENTIAL", re.IGNORECASE)
LIST_NAMES = {"PLX_EXCLUDED_PEOPLE_OIDS"}      # each listed ID is private on its own
DSN_PASSWORD = re.compile(r"://[^/:@\s]+:([^@\s]+)@")
MIN_LENGTH = 12                                 # shorter values match ordinary words


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def env_values(text: str) -> dict[str, str]:
    """Secret values by a label (``NAME`` or ``NAME[i]``) from ``KEY=value`` lines."""
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), _unquote(value)
        if name in LIST_NAMES:
            for index, item in enumerate(re.split(r"[\s,;]+", value)):
                if item:
                    found[f"{name}[{index}]"] = item
        elif "DSN" in name.upper() or "URL" in name.upper():
            match = DSN_PASSWORD.search(value)
            if match:
                found[f"{name}(password)"] = match.group(1)
        elif SECRET_NAME.search(name):
            found[name] = value
    return {label: value for label, value in found.items() if len(value) >= MIN_LENGTH}


def load_values(env_files: list[str], value_files: list[str]) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    unreadable: list[str] = []
    for path in env_files:
        try:
            values.update(env_values(Path(path).read_text(encoding="utf-8", errors="replace")))
        except OSError:
            unreadable.append(path)
    for path in value_files:
        try:
            value = Path(path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            unreadable.append(path)
            continue
        if len(value) >= MIN_LENGTH:
            values[f"file:{Path(path).name}"] = value
    return values, unreadable


def scan(text: str, values: dict[str, str]) -> list[tuple[str, int]]:
    """(label, 1-based line number) for every line that carries a value."""
    hits: list[tuple[str, int]] = []
    for number, line in enumerate(text.splitlines(), 1):
        for label, value in values.items():
            if value in line:
                hits.append((label, number))
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", action="append", default=[])
    parser.add_argument("--value-file", action="append", default=[])
    args = parser.parse_args(argv)
    values, unreadable = load_values(args.env_file, args.value_file)
    for path in unreadable:
        print(f"secret scan: cannot read {path}", file=sys.stderr)
    if unreadable:
        return 2
    if not values:
        print("secret scan: no secret values loaded; refusing to call the text clean", file=sys.stderr)
        return 2
    data = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    hits = scan(data, values)
    for label, number in hits[:20]:
        print(f"SECRET VALUE FOUND: {label} on input line {number}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
