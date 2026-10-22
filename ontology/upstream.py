"""Upstream checkouts and their product code, shared by the source registry
(``scripts/source_registry.py``), the drift check (``scripts/check_upstream_drift.py``)
and the MCP pin (``ontology/generator/mcp.py``). Here, not under ``scripts/``, so the
ontology never imports scripts."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Mapping

_NOT_PRODUCT_DIR = re.compile(r"(?:^|/)(?:node_modules|__tests__|__mocks__|tests?|fixtures|e2e|"
                              r"docs?|examples?|tmp|artifacts|public|dist|build|\.[^/]+)/")
_NOT_PRODUCT_FILE = re.compile(r"\.(?:test|spec|stories)\.[a-z]+$|\.d\.ts$")
_APP_ROUTER = re.compile(r"(?:^|/)src/app/")
_TEST_DIR = re.compile(r"(?:^|/)(?:node_modules|__tests__|__mocks__)/")


def is_product(path: str) -> bool:
    """Not a test, fixture, doc, example, tmp, run artifact, built (``public``, ``dist``,
    ``build``), hidden or vendored file. Below a Next.js ``src/app/`` only test directories
    count: there a ``tests`` or ``doc`` segment is part of a URL."""
    if _NOT_PRODUCT_FILE.search(path):
        return False
    m = _APP_ROUTER.search(path)
    if m is None:
        return not _NOT_PRODUCT_DIR.search(path)
    return not _NOT_PRODUCT_DIR.search(path[:m.end()]) and not _TEST_DIR.search(path[m.end():])


def checkout_for(repo: str, overrides: Mapping[str, str], pins: Mapping) -> Path:
    """The local checkout of ``repo``, first match wins: ``overrides[repo]``; env
    ``PLX_DRIFT_CHECKOUT_<REPO>`` (upper-cased, non-alphanumerics become ``_``); the pin
    file's ``repos.<REPO>.default_checkout``; ``/home/user/<REPO>``."""
    if repo in overrides:
        return Path(overrides[repo])
    env_key = "PLX_DRIFT_CHECKOUT_" + re.sub(r"[^A-Za-z0-9]", "_", repo).upper()
    if os.environ.get(env_key):
        return Path(os.environ[env_key])
    declared = (pins.get("repos", {}).get(repo) or {}).get("default_checkout")
    return Path(declared or f"/home/user/{repo}")
