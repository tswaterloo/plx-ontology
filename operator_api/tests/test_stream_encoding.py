"""Streamed answers (the Insights dashboard, the Ask) compressed by the API with a flush per
chunk: the same lines arrive, each as soon as it is written, at a fraction of the bytes; a
request that does not take gzip gets the plain stream."""

import json
import unittest
import zlib

from fastapi.testclient import TestClient

from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.mock import MockBackend
from operator_api.stream_encoding import gzip_chunks, takes_gzip
from operator_api.tests.test_app_api import events, signed_in


class Dashboard(MockBackend):
    """MockBackend with an Insights dashboard of ``n`` tiles."""

    def __init__(self, n: int):
        super().__init__()
        self.n = n

    def insights_list(self, who):   # pragma: no cover - only its presence is checked
        raise AssertionError("not read")

    def insights_stream(self, who):
        yield {"list": {"items": [{"id": f"t{i}"} for i in range(self.n)]}}
        for i in range(self.n):
            yield {"id": f"t{i}", "result": {"rows": [[i, "Acme Supply", 1234.5]] * 20}}


def client(n: int) -> TestClient:
    app = create_app(Dashboard(n), DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock"),
                     secure_cookie=False)
    c = TestClient(app, base_url="http://testserver")
    assert c.post("/api/auth/dev-login", json={}).status_code == 200
    return c


def raw(response) -> bytes:
    return b"".join(response.iter_raw())


class InsightsStream(unittest.TestCase):
    def test_gzip_carries_every_line_once_in_order_at_any_size(self):
        for n in (1, 10, 60):
            c = client(n)
            with c.stream("GET", "/api/insights/all/stream",
                          headers={"Accept-Encoding": "gzip"}) as r:
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.headers["content-encoding"], "gzip")
                self.assertEqual(r.headers["vary"], "Accept-Encoding")
                body = raw(r)
            lines = [json.loads(x) for x in zlib.decompress(body, 31).splitlines()]
            with c.stream("GET", "/api/insights/all/stream",
                          headers={"Accept-Encoding": "identity"}) as r:
                self.assertNotIn("content-encoding", r.headers)
                plain = raw(r)
            self.assertEqual(lines, [json.loads(x) for x in plain.splitlines()])
            self.assertEqual([x["id"] for x in lines[1:]], [f"t{i}" for i in range(n)])
            if n == 60:
                self.assertLess(len(body), len(plain) / 5)

    def test_a_request_that_does_not_take_gzip_gets_the_plain_stream(self):
        c = client(3)
        for header in ("", "identity", "gzip;q=0", "br", "gzip;q=x", "*;q=0"):
            with c.stream("GET", "/api/insights/all/stream",
                          headers={"Accept-Encoding": header}) as r:
                self.assertNotIn("content-encoding", r.headers, header)
                self.assertEqual(len(raw(r).splitlines()), 4, header)


class AskStream(unittest.TestCase):
    def test_the_ask_answer_is_the_same_events_compressed(self):
        c = signed_in()
        q = {"question": "why is the soap order from that canadian guy late"}
        with c.stream("POST", "/api/ask", json=q, headers={"Accept-Encoding": "gzip"}) as r:
            self.assertEqual(r.headers["content-encoding"], "gzip")
            self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
            got = events(zlib.decompress(raw(r), 31).decode())
        plain = c.post("/api/ask", json=q, headers={"Accept-Encoding": "identity"})
        self.assertNotIn("content-encoding", plain.headers)
        want = events(plain.text)
        self.assertEqual([n for n, _ in got], [n for n, _ in want])
        self.assertEqual(got[-1][0], "answer")


class FlushPerChunk(unittest.TestCase):
    def test_each_chunk_decodes_whole_as_soon_as_it_is_sent(self):
        sent = [f'{{"id":"t{i}"}}\n' for i in range(10)] + ["", b"event: answer\n\n"]
        d = zlib.decompressobj(31)
        out = [d.decompress(c) for c in gzip_chunks(iter(sent))]
        want = [s.encode() if isinstance(s, str) else s for s in sent if s]
        self.assertEqual(out[:len(want)], want)   # nothing held back for a later chunk
        self.assertTrue(d.eof)

    def test_closing_the_stream_closes_its_source(self):
        closed = []

        def source():
            try:
                yield "a\n"
                yield "b\n"
            finally:
                closed.append(True)
        g = gzip_chunks(source())
        next(g)
        g.close()
        self.assertEqual(closed, [True])

    def test_accept_encoding_is_read_conservatively(self):
        yes = ["gzip", "gzip, deflate, br, zstd", "br;q=1, gzip;q=0.5", "*", "x-gzip", "GZIP"]
        no = [None, "", "identity", "gzip;q=0", "gzip;q=0.0, *", "*;q=0", "br", "gzip;q=2",
              "gzip;q=-1", "gzip;q=", "x" * 600]
        for h in yes:
            self.assertTrue(takes_gzip(h), h)
        for h in no:
            self.assertFalse(takes_gzip(h), h)


if __name__ == "__main__":
    unittest.main()
