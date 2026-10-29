"""Run the Workbench app API.

    python3 -m operator_api serve --mock [--bind 127.0.0.1] [--port 8780]
    python3 -m operator_api serve --dev --state-dir DIR ... (operator_api.server)
    python3 -m operator_api serve --entra-resource FILE --state-dir DIR ...
    python3 -m operator_api openapi [--write | --check]

``openapi`` prints the document generated from the models; ``--write`` refreshes
``operator_api/openapi.json``; ``--check`` exits 1 when the committed file differs.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

OPENAPI_FILE = Path(__file__).with_name("openapi.json")


def openapi_text() -> str:
    from .app import create_app, openapi_document
    from .auth import DevAuth, fixture_identity
    from .mock import MockBackend

    app = create_app(MockBackend(), DevAuth(fixture_identity("plx", ["staff:demo"]),
                                            mode="mock"))
    return json.dumps(openapi_document(app), indent=2, sort_keys=True,
                      ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["openapi"]:
        text = openapi_text()
        if "--write" in args:
            OPENAPI_FILE.write_text(text, "utf-8")
            return 0
        if "--check" in args:
            same = OPENAPI_FILE.is_file() and OPENAPI_FILE.read_text("utf-8") == text
            print("openapi.json is current" if same else
                  "openapi.json is stale: run python3 -m operator_api openapi --write")
            return 0 if same else 1
        sys.stdout.write(text)
        return 0
    if args[:1] == ["serve"]:
        from .server import main as serve_main
        return serve_main(args[1:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
