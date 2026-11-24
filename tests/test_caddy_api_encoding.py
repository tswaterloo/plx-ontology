"""The real deploy/Caddyfile's API route compresses JSON answers for a reader that accepts
it (Home's ~260 KB of JSON is 6-18x smaller) and never an event stream: the Ask's events
still arrive one at a time while the answer goes on. Run against a stand-in API behind the
real Caddy. Evidence label: ``isolated-fixture``."""

from __future__ import annotations

import gzip
import http.client
import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.test_deploy_warm_switch import caddy_snippets, free_port

CADDY = shutil.which("caddy")
BIG = {"rows": [{"id": i, "text": "the same words again " * 4} for i in range(2000)]}
RELEASE = threading.Event()


class Api(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/api/gzstream":   # the API's own compressed stream (stream_encoding)
            from operator_api.stream_encoding import gzip_chunks
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            chunks = gzip_chunks(iter([b'{"list":[]}\n', b'{"id":"t1"}\n']))
            self.wfile.write(next(chunks))
            self.wfile.flush()
            RELEASE.wait(10)
            for chunk in chunks:
                self.wfile.write(chunk)
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"event: first\ndata: {}\n\n" + b" " * 2048 + b"\n")
            self.wfile.flush()
            RELEASE.wait(10)
            self.wfile.write(b"event: last\ndata: {}\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        body = json.dumps(BIG).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipIf(CADDY is None, "caddy not installed")
class ApiEncodingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="enc-", dir="/tmp"))
        cls.api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Api)
        threading.Thread(target=cls.api.serve_forever, daemon=True).start()
        cls.port = free_port()
        cls.app_port = free_port()   # the web app: nothing listens until a test starts it
        caddyfile = cls.tmp / "Caddyfile"
        caddyfile.write_text(
            "{\n\tadmin unix/%s/admin.sock\n\tauto_https off\n}\n\n%s\n\nhttp://127.0.0.1:%d {\n"
            "\timport plx_workbench\n}\n" % (cls.tmp, caddy_snippets(), cls.port))
        env = {**os.environ, "XDG_DATA_HOME": str(cls.tmp / "xdg"),
               "XDG_CONFIG_HOME": str(cls.tmp / "xdg"),
               "PLX_API_UPSTREAM": f"127.0.0.1:{cls.api.server_address[1]}",
               "PLX_API_STANDBY": f"unix/{cls.tmp}/none.sock",
               "PLX_APP_UPSTREAM": f"127.0.0.1:{cls.app_port}"}
        cls.caddy = subprocess.Popen([CADDY, "run", "--config", str(caddyfile), "--adapter",
                                      "caddyfile"], env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.caddy.terminate()
        cls.caddy.wait(10)
        cls.api.shutdown()
        shutil.rmtree(cls.tmp, True)

    def get(self, path, encoding):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request("GET", path, headers={"Accept-Encoding": encoding} if encoding else {})
        return conn, conn.getresponse()

    def test_a_json_answer_is_compressed_for_a_reader_that_accepts_it_and_reads_the_same(self):
        _, plain = self.get("/api/home", None)
        raw = plain.read()
        self.assertIsNone(plain.getheader("Content-Encoding"))
        self.assertEqual(json.loads(raw), BIG)
        _, zipped = self.get("/api/home", "gzip")
        body = zipped.read()
        self.assertEqual(zipped.getheader("Content-Encoding"), "gzip")
        self.assertLess(len(body), len(raw) / 5)
        self.assertEqual(json.loads(gzip.decompress(body)), BIG)

    def test_an_event_stream_is_never_compressed_and_its_first_event_arrives_at_once(self):
        RELEASE.clear()
        conn, got = self.get("/api/stream", "gzip, zstd")
        self.addCleanup(conn.close)
        try:
            self.assertIsNone(got.getheader("Content-Encoding"))
            first = got.readline()
            self.assertIn(b"event: first", first)   # before the answer ends
        finally:
            RELEASE.set()
            conn.close()

    def test_a_stream_the_api_compressed_passes_through_and_its_first_line_arrives_at_once(self):
        import zlib
        RELEASE.clear()
        conn, got = self.get("/api/gzstream", "gzip, zstd")
        self.addCleanup(conn.close)
        try:
            self.assertEqual(got.getheader("Content-Encoding"), "gzip")   # not encoded twice
            d = zlib.decompressobj(31)
            self.assertEqual(d.decompress(got.read1(65536)), b'{"list":[]}\n')   # before the end
            RELEASE.set()
            self.assertEqual(d.decompress(got.read()), b'{"id":"t1"}\n')
            self.assertTrue(d.eof)
        finally:
            RELEASE.set()
            conn.close()

    def test_a_page_asked_while_the_web_app_restarts_waits_for_it_and_never_fails(self):
        # a deploy restarts the web app (plx-app: next start) when its tree changed; for its
        # boot (seconds) nothing listens on its port. A reader's page or script asked then
        # waits for it instead of getting the "restarting" error page
        class App(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b"<html>the app</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        started = []

        def boot():
            time.sleep(1.5)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", self.app_port), App)
            started.append(server)
            server.serve_forever()
        threading.Thread(target=boot, daemon=True).start()
        try:
            t0 = time.monotonic()
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            self.addCleanup(conn.close)
            conn.request("GET", "/records")
            got = conn.getresponse()
            self.assertEqual(got.status, 200)
            self.assertEqual(got.read(), b"<html>the app</html>")
            self.assertGreater(time.monotonic() - t0, 1.0)   # it was asked before the app was up
        finally:
            for server in started:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
