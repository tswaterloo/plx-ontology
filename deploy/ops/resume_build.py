"""Resume the interrupted generation build on the EXISTING build-input snapshot.

The host was OOM-killed during the finish steps; its kept build (projected.json checkpoint)
can only resume when the build inputs equal the ones it pinned. A host restart would
re-snapshot the live stores (mail moved on), so this calls the host's own build path on
the snapshot already under <state>/host/build-inputs, unchanged. Read-only on sources.
"""
from __future__ import annotations

import os
import json
import sys
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, os.environ.get("PLX_CURRENT", "/data/plx-releases/current"))

from ontology import project  # noqa: E402
from operating import host as H  # noqa: E402

STATE = Path("/data/plx-state")
CONFIG = Path(os.environ.get("PLX_OPS_PRIVATE", "/data/scratch/cfg"), "ingest_continuous.local.json")
ROOT = STATE / H.HOST_DIR / H.BUILD_INPUTS_DIR


def main() -> int:
    config = H.HostConfig.from_file(CONFIG)
    host = H.Host(STATE, config)
    portal = ROOT / "portal" / "source-mirror.sqlite"
    with ExitStack() as stack:
        sources = {"portal_api": stack.enter_context(project.MirrorCaptures(portal))}
        sources.update(H.state_captures(ROOT / "state", stack,
                                        internal_domains=host._internal_domains()))
        built = H.build_projection(STATE, sources, tenant=config.tenant,
                                   space=host._space_guard, checkpoint=host._disk_floor)
    print(json.dumps({k: built.get(k) for k in ("generation", "mode", "content_digest",
                                                  "build")}, default=str)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
