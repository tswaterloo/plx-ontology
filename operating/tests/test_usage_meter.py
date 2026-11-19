"""Usage metering: the OTLP/HTTP receiver's lines and the report's sums.

Evidence label: ``isolated-fixture`` (synthetic OTLP payloads shaped like Claude Code's;
a receiver on an ephemeral 127.0.0.1 port; no model is called).
"""
from __future__ import annotations

import datetime as dt
import gzip
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from operating import usage_meter as um
from operating import usage_report as ur


def kv(key, value):
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": value}
    return {"key": key, "value": v}


RES = {"attributes": [kv("service.name", "claude-code"), kv("plx.source", "plx-api"),
                      kv("plx.host", "box"), kv("plx.caller", "operating.agent_session")]}
NOW_NS = str(int(dt.datetime(2026, 11, 19, 22, 30, tzinfo=dt.timezone.utc).timestamp() * 1e9))


def request_event(model="claude-opus-5-5", cost=0.5, **extra):
    attrs = [kv("event.name", "api_request"), kv("model", model), kv("input_tokens", 10),
             kv("output_tokens", 20), kv("cache_read_tokens", 300),
             kv("cache_creation_tokens", 40), kv("cost_usd", cost),
             kv("session.id", "s1")] + [kv(k, v) for k, v in extra.items()]
    return {"timeUnixNano": NOW_NS, "body": {"stringValue": "claude_code.api_request"},
            "attributes": attrs}


def logs(*records):
    return {"resourceLogs": [{"resource": RES, "scopeLogs": [{"logRecords": list(records)}]}]}


class FlattenTests(unittest.TestCase):
    def test_an_event_keeps_its_numbers_and_never_its_text(self):
        rec = request_event(prompt="the secret question", tool_input="rm -rf x",
                            note="y" * 1000)
        [line] = um.lines_for("/v1/logs", logs(rec))
        self.assertEqual((line["kind"], line["name"]), ("event", "api_request"))
        self.assertEqual(line["attrs"]["input_tokens"], 10)
        self.assertEqual(line["attrs"]["cost_usd"], 0.5)
        self.assertEqual(line["res"]["plx.source"], "plx-api")
        self.assertEqual(line["ts"], "2026-11-19T22:30:00.000Z")
        self.assertNotIn("prompt", line["attrs"])
        self.assertNotIn("tool_input", line["attrs"])
        self.assertEqual(line["attrs"]["note"], "<1000 chars>")
        self.assertNotIn("secret", json.dumps(line))

    def test_metric_points_carry_value_temporality_and_attributes(self):
        payload = {"resourceMetrics": [{"resource": RES, "scopeMetrics": [{"metrics": [
            {"name": "claude_code.token.usage", "unit": "tokens", "sum": {
                "aggregationTemporality": 1, "isMonotonic": True, "dataPoints": [
                    {"asInt": "649", "timeUnixNano": NOW_NS,
                     "attributes": [kv("type", "cacheRead"), kv("model", "m")]}]}},
            {"name": "claude_code.cost.usage", "sum": {"aggregationTemporality": 2,
                "dataPoints": [{"asDouble": 0.25, "timeUnixNano": NOW_NS}]}}]}]}]}
        lines = um.lines_for("/v1/metrics", payload)
        self.assertEqual([(l["name"], l["value"], l.get("temporality")) for l in lines],
                         [("claude_code.token.usage", 649, "delta"),
                          ("claude_code.cost.usage", 0.25, "cumulative")])
        self.assertEqual(lines[0]["attrs"], {"type": "cacheRead", "model": "m"})
        self.assertEqual(um.lines_for("/v1/traces", payload), [])


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sink = um.Sink(self.tmp.name)
        import http.server
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), um.handler_for(self.sink))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path, body, ctype="application/json", gz=False):
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        headers = {"Content-Type": ctype}
        if gz:
            data, headers["Content-Encoding"] = gzip.compress(data), "gzip"
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request("POST", path, body=data, headers=headers)
        status = conn.getresponse().status
        conn.close()
        return status

    def test_posts_become_lines_in_the_days_file_and_the_report_sums_them(self):
        self.assertEqual(self.post("/v1/logs", logs(request_event(), request_event(
            model="claude-haiku-5-5", cost=0.01))), 200)
        self.assertEqual(self.post("/v1/logs", logs(request_event()), gz=True), 200)
        self.assertEqual(self.post("/v1/metrics", b"\x0a\x01", ctype="application/x-protobuf"),
                         400)
        files = list(Path(self.tmp.name).glob("usage-*.jsonl"))
        self.assertEqual(len(files), 1)
        lines = [json.loads(l) for l in files[0].read_text().splitlines()]
        self.assertEqual([l["kind"] for l in lines], ["event", "event", "event", "unparsed"])
        start = dt.datetime(2026, 11, 19, 22, 0, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 11, 19, 23, 0, tzinfo=dt.timezone.utc)
        # the files are named by the UTC day of receipt (today); the events are dated 11-19
        summary = ur.summarize((dict(l, _t=ur.when(l)) for l in lines), ["source", "model"],
                               True)
        self.assertEqual(summary["unparsed"], 1)
        self.assertEqual(summary["total"]["requests"], 3)
        self.assertAlmostEqual(summary["total"]["cost"], 1.01)
        self.assertEqual(summary["total"]["cache_read"], 900)
        opus = summary["rows"][("11-19 17:00", "plx-api", "claude-opus-5-5")]
        self.assertEqual((opus["requests"], opus["input"], opus["output"]), (2, 20, 40))
        text = ur.table(summary, ["source", "model"], True)
        self.assertIn("TOTAL", text)
        self.assertIn("unparsed posts: 1", text)
        line = ur.one_line(ur.summarize((dict(l, _t=ur.when(l)) for l in lines), ["source"],
                                        False), "6h")
        self.assertTrue(line.startswith("usage 6h: $1.01 API-eq, 3 req"), line)
        self.assertIn("plx-api $1.01", line)
        day = dt.datetime(2026, 11, 19, 23, 59, tzinfo=dt.timezone.utc)
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        um.Sink(other.name).write(um.lines_for("/v1/logs", logs(request_event())), now=day)
        self.assertTrue((Path(other.name) / "usage-20261119.jsonl").exists())
        self.assertEqual(len(list(ur.read(other.name, start, end))), 1)
        self.assertEqual(list(ur.read(other.name, end, end + dt.timedelta(hours=1))), [])
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request("GET", "/health")
        self.assertEqual(json.loads(conn.getresponse().read())["lines"], 4)
        conn.close()

    def test_since_parses_windows(self):
        now = dt.datetime(2026, 11, 19, 12, tzinfo=dt.timezone.utc)
        self.assertEqual(ur.since("6h", now), now - dt.timedelta(hours=6))
        self.assertEqual(ur.since("30m", now), now - dt.timedelta(minutes=30))
        with self.assertRaises(ValueError):
            ur.since("yesterday", now)


if __name__ == "__main__":
    unittest.main()
