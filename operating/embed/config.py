"""Settings of the embedding run, from the environment or the operator's ``plx.env``.

Only the names in ``KEYS`` are read from the env file (``PLX_ENV_FILE``, default
``~/.config/plx/plx.env``); the process environment wins. Values are never printed:
``describe`` reports names and presence only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

KEYS = ("PLX_EMBED_MODEL", "PLX_EMBED_ALLOW_REMOTE", "OPENAI_API_KEY", "PLX_PG_DSN",
        "PLX_EXCLUDED_PEOPLE_OIDS", "PLX_EXCLUDED_PEOPLE_FILE", "PLX_EMBED_BATCH",
        "PLX_EMBED_THREADS", "PLX_EMBED_DIMENSIONS", "PLX_EMBED_PRECISION", "HF_HOME", "PLX_WORKBENCH_STATE_DIR")
# The remote
# provider stays wired behind PLX_EMBED_ALLOW_REMOTE=1 and is chosen by PLX_EMBED_MODEL.
DEFAULT_MODEL = "BAAI/bge-m3"
LOCAL_MODEL = "BAAI/bge-m3"
DEFAULT_DSN = "postgresql:///plx"
DEFAULT_STATE = "/data/plx-state"
SECRET = frozenset({"OPENAI_API_KEY", "PLX_EXCLUDED_PEOPLE_OIDS"})


def _env_file() -> Path:
    return Path(os.environ.get("PLX_ENV_FILE") or Path.home() / ".config/plx/plx.env")


def _parse(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.removeprefix("export ").strip()
        if name in KEYS:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            out[name] = value
    return out


def settings(environ: Mapping[str, str] | None = None, *, env_file: Path | None = None
             ) -> dict[str, str]:
    """The run's settings: ``KEYS`` from the env file, overridden by the environment."""
    environ = os.environ if environ is None else environ
    merged = _parse(env_file or _env_file())
    merged.update({k: v for k, v in environ.items() if k in KEYS})
    return merged


def describe(values: Mapping[str, str]) -> dict[str, str]:
    """Names with their non-secret values, secrets as present/absent only."""
    return {k: ("<set>" if values.get(k) else "<absent>") if k in SECRET
            else values.get(k, "<absent>") for k in KEYS}


__all__ = ["DEFAULT_DSN", "DEFAULT_MODEL", "DEFAULT_STATE", "KEYS", "LOCAL_MODEL", "describe",
           "settings"]
