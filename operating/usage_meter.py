"""Every Claude Code model call on the box and the Sparks, metered: a local OTLP/HTTP receiver.

Every ``claude`` process started with ``CLAUDE_CODE_ENABLE_TELEMETRY=1`` and the OTLP
exporter pointed here (``OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318``,
``OTEL_EXPORTER_OTLP_PROTOCOL=http/json``) sends its events (``claude_code.api_request``:
model, input/output/cache tokens, ``cost_usd``, duration, session id) and its metrics
(``claude_code.token.usage``, ``claude_code.cost.usage``) here, including the headless
``claude -p --no-session-persistence`` calls that leave no transcript. Each event and each
metric data point becomes one JSON line in ``<dir>/usage-YYYYMMDD.jsonl`` (UTC date of
receipt), with the emitter's resource attributes (``plx.source``: the systemd unit or
``login``; ``plx.host``; ``plx.caller``: the module that started the process).

No prompt, response or tool text is ever written: the content switches
(``OTEL_LOG_USER_PROMPTS`` and the like) stay off where the exporter is configured, and
here every attribute named in ``DENY`` is dropped and any long string is cut to its length.

``python3 operating/usage_meter.py serve [--bind 127.0.0.1] [--port 4318] [--dir D]``:
the receiver (stdlib only, so it runs as a plain script too). ``GET /health``: lines
written since start. Reading the files: ``python3 -m operating.usage_report``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import http.server
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any, Iterable, Iterator, Mapping, Optional

DEFAULT_DIR = "/data/plx-state/usage"
DEFAULT_PORT = 4318
MAX_BODY = 32 * 1024 * 1024
MAX_STRING = 256
# Attributes that can carry text a person or a model wrote: never written.
DENY = frozenset({
    "prompt", "user_prompt", "prompt_text", "system_prompt", "messages", "response",
    "response_text", "assistant_response", "completion", "content", "body", "raw_body",
    "request_body", "response_body", "tool_parameters", "tool_input", "tool_output",
    "tool_result", "tool_content", "input", "output", "arguments", "command"})


def any_value(value: Mapping[str, Any]) -> Any:
    """An OTLP/JSON ``AnyValue`` as a plain value."""
    if not isinstance(value, Mapping):
        return None
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in value:
            return value[key]
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return None
    if "arrayValue" in value:
        return [any_value(v) for v in (value["arrayValue"] or {}).get("values") or []]
    if "kvlistValue" in value:
        return attributes((value["kvlistValue"] or {}).get("values") or [])
    return None


def clean(key: str, value: Any) -> tuple[bool, Any]:
    """(keep, value): a denied key is dropped; a long string becomes its length only."""
    if key.lower() in DENY or key.lower().rsplit(".", 1)[-1] in DENY:
        return False, None
    if isinstance(value, str) and len(value) > MAX_STRING:
        return True, f"<{len(value)} chars>"
    return True, value


def attributes(items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        key = str((item or {}).get("key") or "")
        keep, value = clean(key, any_value((item or {}).get("value") or {}))
        if key and keep:
            out[key] = value
    return out


def stamp(nanos: Any) -> Optional[str]:
    try:
        n = int(nanos)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return dt.datetime.fromtimestamp(n / 1e9, dt.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def log_lines(payload: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """One line per OTLP log record (a Claude Code event)."""
    for rl in payload.get("resourceLogs") or []:
        res = attributes(((rl or {}).get("resource") or {}).get("attributes") or [])
        for sl in (rl or {}).get("scopeLogs") or []:
            for rec in (sl or {}).get("logRecords") or []:
                attrs = attributes(rec.get("attributes") or [])
                body = any_value(rec.get("body") or {})
                name = attrs.get("event.name") or (body if isinstance(body, str)
                                                   and len(body) <= 128 else None)
                yield {"ts": stamp(rec.get("timeUnixNano"))
                       or stamp(rec.get("observedTimeUnixNano")),
                       "kind": "event", "name": name, "res": res, "attrs": attrs}


def metric_lines(payload: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """One line per OTLP metric data point (sum, gauge; a histogram as count/sum)."""
    for rm in payload.get("resourceMetrics") or []:
        res = attributes(((rm or {}).get("resource") or {}).get("attributes") or [])
        for sm in (rm or {}).get("scopeMetrics") or []:
            for metric in (sm or {}).get("metrics") or []:
                for shape in ("sum", "gauge", "histogram"):
                    body = metric.get(shape)
                    if not isinstance(body, Mapping):
                        continue
                    temporality = {1: "delta", 2: "cumulative"}.get(
                        body.get("aggregationTemporality"))
                    for point in body.get("dataPoints") or []:
                        if shape == "histogram":
                            value: Any = {"count": int(point.get("count") or 0),
                                          "sum": point.get("sum")}
                        elif "asDouble" in point:
                            value = point["asDouble"]
                        else:
                            value = any_value({"intValue": point.get("asInt")})
                        line = {"ts": stamp(point.get("timeUnixNano")), "kind": "metric",
                                "name": metric.get("name"), "unit": metric.get("unit"),
                                "value": value, "res": res,
                                "attrs": attributes(point.get("attributes") or []),
                                "start": stamp(point.get("startTimeUnixNano"))}
                        if temporality:
                            line["temporality"] = temporality
                        yield line


def lines_for(path: str, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    if path.endswith("/v1/logs"):
        return list(log_lines(payload))
    if path.endswith("/v1/metrics"):
        return list(metric_lines(payload))
    return []          # traces: not kept (the events carry the numbers)


class Sink:
    """Appends lines to the day's file, one writer at a time."""

    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.lock = threading.Lock()
        self.written = 0

    def write(self, lines: list[dict[str, Any]], *, now: Optional[dt.datetime] = None) -> int:
        if not lines:
            return 0
        now = now or dt.datetime.now(dt.timezone.utc)
        rx = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        text = "".join(json.dumps({**line, "rx": rx}, separators=(",", ":"),
                                  sort_keys=True, default=str) + "\n" for line in lines)
        path = self.dir / f"usage-{now:%Y%m%d}.jsonl"
        with self.lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
            try:
                os.write(fd, text.encode("utf-8"))
            finally:
                os.close(fd)
            self.written += len(lines)
        return len(lines)


def handler_for(sink: Sink):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):   # no access log: the files are the record
            return

        def reply(self, code: int, body: Mapping[str, Any]):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.rstrip("/") == "/health":
                self.reply(200, {"ok": True, "lines": sink.written})
            else:
                self.reply(404, {})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self.reply(413 if length > MAX_BODY else 400, {})
                return
            raw = self.rfile.read(length)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            try:
                if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                if ctype != "application/json":
                    raise ValueError(ctype or "no content type")
                payload = json.loads(raw)
                lines = lines_for(self.path.split("?")[0], payload)
            except (ValueError, OSError, EOFError) as exc:
                sink.write([{"ts": None, "kind": "unparsed", "name": self.path,
                             "res": {}, "attrs": {"content_type": ctype,
                                                  "bytes": len(raw), "why": str(exc)[:80]}}])
                self.reply(400, {})
                return
            sink.write(lines)
            self.reply(200, {})

    return Handler


def serve(bind: str, port: int, directory: str) -> None:
    sink = Sink(directory)
    server = http.server.ThreadingHTTPServer((bind, port), handler_for(sink))
    server.daemon_threads = True
    print(f"usage_meter: {bind}:{port} -> {directory}", flush=True)
    server.serve_forever()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="usage_meter", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--bind", default="127.0.0.1")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--dir", default=os.environ.get("PLX_USAGE_DIR") or DEFAULT_DIR)
    args = ap.parse_args(argv)
    serve(args.bind, args.port, args.dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
