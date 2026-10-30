"""Fixtures for the live verifier's tests: a temporary repo root with a fake probe module.

"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import textwrap

from verify import live_verify as lv

FAKE_MODULE = "scripts.fake_probe"
SECRET = "s3cr3t-Value-that-must-never-appear-0123"
FAKE_SOURCE = textwrap.dedent('''
    import json, os, pathlib, sys, time
    mode = sys.argv[1]
    if len(sys.argv) > 2:
        pathlib.Path(sys.argv[2]).write_text("ran")
    if mode == "ok":
        print(json.dumps({"token": "ok", "count": 3, "nested": {"n": 2, "flag": True},
                          "items": [{"name": "Acme Customer Ltd", "absent": []},
                                    {"name": "Other", "absent": []}],
                          "status": "current", "label": "Customer PO 12345 for Jane"}))
    elif mode == "leak":
        key = os.environ.get("FAKE_API_KEY", "")
        print(json.dumps({"token": "ok", "status": key,
                          "message": "Authorization: Bearer abcdefghijklmnop " + key,
                          "other": "plxwb_ABCDEFGHIJKLMNOPQRSTUV client_secret=hunter2hunter2",
                          "mail": "someone@example.com",
                          "blob": "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"}))
        print(json.dumps({"error": "SourceUnavailable", "message": "denied for " + key}),
              file=sys.stderr)
        sys.exit(1)
    elif mode == "env":
        pathlib.Path(sys.argv[2]).write_text(json.dumps(sorted(os.environ)))
        print(json.dumps({"token": "ok"}))
    elif mode == "bc":
        print(json.dumps({"token": "ok", "bc": bool(os.environ.get("BC_CLIENT_SECRET"))}))
    elif mode == "sleep":
        time.sleep(30)
    elif mode == "fail":
        print(json.dumps({"error": "SourceUnavailable", "message": "HTTP 503"}), file=sys.stderr)
        sys.exit(1)
    elif mode == "degraded":
        print(json.dumps({"token": "ok", "degraded": True, "available_relation_count": 89,
                          "blocked_upstream": [
                              {"part": name, "source": "portal-workbench-api",
                               "cause": "relation_unavailable",
                               "reason": "148 approved columns are missing or not granted"}
                              for name in ("sage.oe_order_detail", "sage.oe_order_header")]}))
    elif mode == "notready":
        print(json.dumps({"error": "WorkbenchNotReady", "probe": "portal-health",
                          "reason": "health: recovery is not true"}), file=sys.stderr)
        sys.exit(1)
    elif mode == "echo":
        print(os.environ["FAKE_DOC"])
        sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
    elif mode == "text":
        print("not json")
    elif mode == "trace":
        print(json.dumps({"token": "ok"}))
        raise ValueError("customer Acme Cosmetics Inc row 4471 value 99.12")
    elif mode in ("custom", "stdlib"):  # the traceback's innermost frames: fake_errors
        from scripts import fake_errors
        print(json.dumps({"token": "ok"}))
        (fake_errors.resolve if mode == "custom" else fake_errors.parse)("{Acme 4471")
    elif mode == "garbage":
        print("customer Acme Cosmetics Inc owes 99.12", file=sys.stderr)
        sys.exit(3)
    elif mode == "keys":
        print(json.dumps({"token": "ok", "by_customer": {"Acme Cosmetics Inc": 3, "acme": 1},
                          "checks": {"chain_ok": True, "rows": 4},
                          "nested": {"inner": {"x_ok": True}}}))
    elif mode == "share":  # argv: marker, a session path, a log [, crash]
        target, log = pathlib.Path(sys.argv[3]), pathlib.Path(sys.argv[4])
        with log.open("a") as handle:
            handle.write(json.dumps({"dir": str(target.parent), "seen": target.exists(),
                                     "mode": target.parent.stat().st_mode & 0o777,
                                     "since": os.environ.get("PLX_CAPTURE_FRESH_SINCE")})
                         + "\\n")
        time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
        target.write_text("captured")
        if sys.argv[5:] == ["crash"]:
            sys.exit(1)
        print(json.dumps({"token": "ok"}))
    elif mode == "hoststate":  # argv: --state-dir DIR; a host cycle's state
        pathlib.Path(sys.argv[3]).mkdir()
        (pathlib.Path(sys.argv[3]) / "cycle").write_text("cycle-written")
        print(json.dumps({"token": "ok"}))
    elif mode == "readstate":  # argv: a log; what the host-scoped check was given
        state = os.environ.get("PLX_WORKBENCH_STATE_DIR", "")
        pathlib.Path(sys.argv[2]).write_text("ran")
        print(json.dumps({"token": "ok", "state_has_cycle": bool(state) and
                          (pathlib.Path(state) / "cycle").read_text() == "cycle-written"}))
    elif mode == "argv":
        pathlib.Path(sys.argv[2]).write_text(json.dumps(sys.argv[3:]))
        print(json.dumps({"token": "ok"}))
    elif mode == "artifact":  # argv: the declared path; writes FAKE_ARTIFACT's bytes there
        out, source = pathlib.Path(sys.argv[2]), pathlib.Path(os.environ["FAKE_ARTIFACT"])
        out.unlink()
        if os.environ.get("FAKE_LINK"):
            out.symlink_to(source)
        else:
            out.write_bytes(source.read_bytes())
        (out.parent / "other.xml").write_bytes(source.read_bytes())  # never declared
        print(json.dumps({"token": "ok"}))
        sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
    elif mode == "big":
        print(json.dumps({"token": "ok", **{f"field_{i}": {"a": i, "b": [1, 2, 3]}
                                             for i in range(400)}}))
''')
# A custom exception class without an Error suffix whose message spans lines, raised from a
# stdlib error (a chained traceback), and a stdlib error raised inside the stdlib.
FAKE_ERRORS = textwrap.dedent('''
    import json


    class RowUnresolvable(Exception):
        pass


    def resolve(text):
        try:
            return json.loads(text)
        except ValueError as exc:
            raise RowUnresolvable("customer Acme Cosmetics Inc\\nrow 4471 value 99.12") from exc


    def parse(text):
        return json.JSONDecoder().decode(text)
''')


def spec(spec_id: str = "fake.ok", *args: str, **extra) -> dict:
    expect = extra.pop("expect", {"exit_code": 0, "json_fields": ["token"]})
    return {"id": spec_id, "description": "fake probe for tests",
            "command": ["python3", "-m", FAKE_MODULE, *(args or ("ok",))],
            "expect": expect, **extra}


class FakeRoot:
    """A temp repo root with a fake scripts.fake_probe module and a specs dir."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "__init__.py").write_text("")
        (self.root / "scripts" / "fake_probe.py").write_text(FAKE_SOURCE)
        (self.root / "scripts" / "fake_errors.py").write_text(FAKE_ERRORS)
        self.specs = self.root / "devloop" / "live-checks"
        self.specs.mkdir(parents=True)
        self.env = {"PATH": os.environ.get("PATH", ""), "FAKE_API_KEY": SECRET}

    def write(self, value: dict) -> None:
        (self.specs / f"{value['id']}.json").write_text(json.dumps(value))

    def run(self, value: dict, **kwargs) -> dict:
        return lv.run_spec(value, root=self.root, environ=kwargs.pop("environ", self.env),
                           allowed_modules={FAKE_MODULE}, **kwargs)

    def main(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = lv.main(list(argv), root=self.root, environ=self.env,
                           allowed_modules={FAKE_MODULE})
        return code, out.getvalue()

    def close(self) -> None:
        self._tmp.cleanup()
