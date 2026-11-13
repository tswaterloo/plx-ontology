#!/usr/bin/env python3
"""Render the host's systemd units from the templates in deploy/ (no username or home path
is committed; the units name them as ``@PLX_USER@``, ``@PLX_GROUP@``, ``@PLX_HOME@``,
``@PLX_CURRENT@`` and ``@PLX_HOSTNAME@``).

    python3 deploy/render.py --out DIR [--env ~/.config/plx/deploy.env] [--user]

System units (default): ``deploy/systemd/*.{service,timer,path}``, the host drop-ins
``deploy/systemd/host/<unit>.d/*.conf`` and ``deploy/backup/*.{service,timer}`` land in DIR
laid out as ``/etc/systemd/system`` (``DIR/<unit>`` and ``DIR/<unit>.d/<conf>``), for the
supervisor to copy with sudo. ``--user`` renders the user units (``deploy/user``) laid out
as ``~/.config/systemd/user``. A placeholder without a value stops the render (exit 1).
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

DEPLOY = Path(__file__).resolve().parent
PLACEHOLDER = re.compile(r"@(PLX_[A-Z_]+)@")
UNIT = ("*.service", "*.timer", "*.path")


def load_env(path: Path, environ=os.environ) -> dict[str, str]:
    """KEY=value lines of the host file over the defaults deploy/lib.sh uses."""
    values = {"PLX_USER": environ.get("USER", ""), "PLX_HOME": environ.get("HOME", ""),
              "PLX_RELEASES": "/data/plx-releases", "PLX_REPO": "/data/OntologyProject"}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    values.setdefault("PLX_GROUP", values["PLX_USER"])
    values.setdefault("PLX_CURRENT", values["PLX_RELEASES"].rstrip("/") + "/current")
    values.setdefault("PLX_HOSTNAME", "localhost")
    return {k: v for k, v in values.items() if v and "<" not in v}


def sources(user: bool, api_user: bool = False) -> list[tuple[Path, str]]:
    """(template, path relative to the install dir). ``api_user``: also the drop-ins that
    run the API as its own user (deploy/systemd/api-user, deploy/release/README-api-user.md),
    a separate switch from the release units."""
    found: list[tuple[Path, str]] = []
    if user:
        for pattern in UNIT:
            found += [(p, p.name) for p in sorted((DEPLOY / "user").glob(pattern))]
        return found
    for folder in (DEPLOY / "systemd", DEPLOY / "backup"):
        for pattern in UNIT:
            found += [(p, p.name) for p in sorted(folder.glob(pattern))]
    folders = [DEPLOY / "systemd" / "host"] + ([DEPLOY / "systemd" / "api-user"] if api_user else [])
    for folder in folders:
        for conf in sorted(folder.glob("*.d/*.conf")):
            found.append((conf, f"{conf.parent.name}/{conf.name}"))
    return found


def render(text: str, values: dict[str, str]) -> str:
    missing = sorted({m for m in PLACEHOLDER.findall(text) if m not in values})
    if missing:
        raise KeyError(", ".join(missing))
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--env", type=Path,
                        default=Path(os.environ.get("PLX_DEPLOY_ENV",
                                                    Path.home() / ".config/plx/deploy.env")))
    parser.add_argument("--user", action="store_true", help="user units (deploy/user)")
    parser.add_argument("--api-user", action="store_true",
                        help="also the drop-ins that run the API as its own user")
    args = parser.parse_args(argv)
    values = load_env(args.env)
    for template, rel in sources(args.user, args.api_user):
        try:
            text = render(template.read_text(), values)
        except KeyError as missing:
            print(f"{template}: no value for {missing} (set it in {args.env})", file=sys.stderr)
            return 1
        target = args.out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
