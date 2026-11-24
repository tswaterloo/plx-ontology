"""Payment card numbers never leave the Workbench unmasked (``operating.card_text``).

The rule, both ways: a card number in a card layout, in a network's issued range, passing
Luhn, shows as its last four digits, and the CVV/CVC and expiry beside it on the form are
masked; order, PO, tracking, phone and barcode numbers, digests and message ids (the shapes
the served data holds) stay as they are. Then every exit that hands text out: the MCP tool
result a model reads (the session relay) or a connector gets (host code gets it as built),
every ``claude`` CLI prompt (``general_answer.claude_call``, the planner, the understander,
a rewrite, the warm front-door session, an Ask session's task and first reads), the alarm
mail and generated export files. The API pages, Ask stream and drafted messages are in
``operator_api.tests.test_app_api.CardNumbersNeverServed``.

The card numbers here are the networks' published test numbers, never a real one.
Evidence label: isolated-fixture. No network, no model.
"""

from __future__ import annotations

import io
import json
import queue
import threading
import unittest
import zipfile
from unittest import mock

from operating.card_text import (DOT, has_card_number, mask_card_numbers, mask_served,
                                 mask_served_in)

VISA = "4111 1111 1111 1111"
MASTER = "5555-5555-5555-4444"
AMEX = "3782 822463 10005"
SHOWN = f"{DOT * 4} {DOT * 4} {DOT * 4} "
FORM = ("CREDIT CARD AUTHORIZATION FORM\nCardholder Name: Pat Doe\nCard Type: VISA\n"
        f"Card Number: {VISA}\nExpiration Date:\n09/28\nCVV: 737\n"
        "Amount: $1,250.00 CAD\nPO 4500123456   Date: 11/14/2026")
DIGITS = ("4111111111111111", "5555555555554444", "378282246310005")


def leaked(text: str) -> bool:
    flat = "".join(ch for ch in text if ch.isdigit())
    return any(d in flat for d in DIGITS)


class CardRule(unittest.TestCase):
    def test_cards_show_only_their_last_four(self):
        for raw, last in ((VISA, "1111"), (MASTER, "4444"), (AMEX, "0005"),
                          ("4111111111111111", "1111"), ("4111 1111 1111 1111", "1111"),
                          ("5555 5555 5555 4444 123", "4444")):
            with self.subTest(raw=raw[-6:]):
                out = mask_card_numbers(f"Paid with card {raw}, thanks")
                self.assertIn(SHOWN + last, out)
                self.assertFalse(leaked(out))
                self.assertTrue(has_card_number(f"card {raw}"))
                # at the very start and end of a text (a cell, a field, a JSON value)
                self.assertIn(SHOWN + last, mask_card_numbers(raw))
                self.assertIn(SHOWN + last, mask_card_numbers(f"card {raw}"))
                self.assertEqual(mask_card_numbers(out), out)   # idempotent

    def test_an_authorisation_form_loses_its_code_and_expiry_too(self):
        out = mask_card_numbers(FORM)
        self.assertIn(SHOWN + "1111", out)
        self.assertIn("CVV: " + DOT * 3, out)
        self.assertIn("Expiration Date:\n" + DOT * 2 + "/" + DOT * 2, out)
        self.assertNotIn("737", out)
        self.assertNotIn("09/28", out)
        # the rest of the form reads as it did
        for kept in ("Pat Doe", "$1,250.00 CAD", "PO 4500123456", "Date: 11/14/2026"):
            self.assertIn(kept, out)
        bare = mask_card_numbers(f"card {VISA} 12/27 thanks")
        self.assertIn(DOT * 2 + "/" + DOT * 2, bare)

    def test_numbers_that_are_not_cards_stay(self):
        texts = (
            "PO 4500123456 and SO 1000089711, Sage IN1000118446",
            "UPS 1Z999AA10123456784, FedEx 794612345678, USPS 9400111899223197428490",
            "Canada Post 7023210039414604 and Purolator 329012345678",
            "call 416-555-1234 or +1 (905) 555-0199 ext 2201",
            "EAN 4006381333931, GTIN 40063813339310",
            "invoice total 4111111111111111111111 units",          # too long a run
            "ref 1234-5678-9012-3456",                             # fails Luhn
            "account 9999 9999 9999 9995",                         # Luhn, no network
            "sha256 ab4111111111111111cd and <4111111111111111.JavaMail@host>",
            "lot 12-4111-1111-1111-1111",                          # continues a number
            "CVV 123 exp 12/27 on a page with no card",
        )
        for text in texts:
            with self.subTest(text=text[:20]):
                self.assertEqual(mask_card_numbers(text), text)
                self.assertFalse(has_card_number(text))

    def test_unknown_input_is_returned_unchanged(self):
        for value in (None, "", 4111111111111111, b"4111111111111111", ["x"]):
            self.assertEqual(mask_card_numbers(value), value)    # type: ignore[arg-type]
        self.assertFalse(has_card_number(None))
        nested = mask_served_in({"a": [f"card {VISA}", ("t", 1)], "n": 3, "k": None})
        self.assertEqual(nested, {"a": [f"card {SHOWN}1111", ["t", 1]], "n": 3, "k": None})
        self.assertIn("[secret]", mask_served(f"card {VISA} password: Welcome123!"))


class ModelPromptsAreMasked(unittest.TestCase):
    """No ``claude`` CLI call is handed a card number."""

    def test_general_answer_call_text_and_image_turn(self):
        from operating import general_answer
        sent: list[str] = []
        with mock.patch("operating.model_ledger.reused", return_value=(False, None)), \
                mock.patch.object(general_answer, "_claude_reply",
                                  side_effect=lambda argv, text, *a: sent.append(text) or "ok"):
            general_answer.claude_call(FORM)
            general_answer.claude_call(FORM, images=[{"media_type": "image/png", "data": "AA"}])
        self.assertEqual(len(sent), 2)
        for text in sent:
            self.assertFalse(leaked(text))
            self.assertIn("1111", text)

    def test_planner_prompt(self):
        from operating.agent_loop import ClaudeCodePlanner
        prompts: list[str] = []

        def spawn(argv, env, timeout, prompt):
            prompts.append(prompt)
            return "{}"
        with mock.patch("operating.model_ledger.reused", return_value=(False, None)), \
                mock.patch("operating.model_ledger.record"):
            try:
                ClaudeCodePlanner(spawn=spawn)({"results": [FORM]})
            except Exception:   # an unusable reply is the loop's to refuse
                pass
        self.assertEqual(len(prompts), 1)
        self.assertFalse(leaked(prompts[0]))

    def test_understanding_model_reads_the_question_and_context_masked(self):
        from operator_api.cli import masked_understanding
        seen: list = []

        class Model:
            def __call__(self, question, context, timeout):
                seen.append(json.dumps([question, context]))

        def understand(question, *, llm=None, **kw):
            llm(question, {"history": [FORM]}, 1.0)
            return question
        with mock.patch("operating.query_understanding.ClaudeUnderstander", Model), \
                mock.patch.dict("os.environ", {"PLX_UNDERSTAND_LLM": ""}):
            self.assertEqual(masked_understanding(understand)(FORM), FORM)
        self.assertEqual(len(seen), 1)
        self.assertFalse(leaked(seen[0]))
        self.assertIn("1111", seen[0])

    def test_a_rewrite_call_is_sent_masked(self):
        from operating.card_text import masked_call
        seen: list[str] = []
        self.assertIsNone(masked_call(None))
        masked_call(lambda prompt: seen.append(prompt))(FORM)
        self.assertFalse(leaked(seen[0]))

    def test_an_ask_session_gets_its_task_and_first_reads_masked(self):
        from operator_api.agent_ask import AgentAsk
        calls: list = []

        def stream(argv, cwd, env, timeout, **options):
            calls.append((argv, options["feed"]() if options.get("feed") else ""))
            return 0, ""
        with mock.patch("operating.agent_session._stream", stream):
            spawn = AgentAsk().agent_for(lambda line: None).spawn
            self.assertTrue(spawn.stops and spawn.feeds)
            spawn(["claude", "-p", FORM], "/", {}, 5, feed=lambda: FORM)
        self.assertEqual(len(calls), 1)
        argv, fed = calls[0]
        self.assertFalse(leaked(" ".join(argv)) or leaked(fed))
        self.assertIn("1111", fed)


class OtherExits(unittest.TestCase):
    def test_alarm_mail_body_and_subject(self):
        from operating.ops_notify import GraphMail
        posted: list[bytes] = []
        mail = GraphMail({"owner_address": "owner@plx.example", "sender": "ops@plx.example"},
                         post=lambda url, headers, body: posted.append(body) or (202, b""))
        with mock.patch.object(GraphMail, "_token", return_value="t"):
            mail({"to": ["owner@plx.example"], "subject": f"failed on {VISA}",
                  "body": FORM})
        self.assertEqual(len(posted), 1)
        message = json.loads(posted[0])["message"]
        self.assertFalse(leaked(message["subject"] + message["body"]["content"]))
        self.assertIn("1111", message["body"]["content"])

    def test_generated_export_file(self):
        from operating.instruct_files import make
        data = make("docx", FORM)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            text = "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())
        self.assertFalse(leaked(text))
        self.assertIn("Pat Doe", text)


class ToolResultsToAgents(unittest.TestCase):
    """A tool result leaving for a model (the session's MCP relay, ``connect``) or a
    connector (``operator_api.serving``) holds no card; other messages pass byte for byte;
    in-process host code gets the result as built (it masks at its own exits)."""

    RESULT = {"jsonrpc": "2.0", "id": 7, "result": {
        "content": [{"type": "text", "text": json.dumps({"text": FORM})}],
        "structuredContent": {"text": FORM, "rows": [{"note": f"card {VISA}"}]}}}

    def test_a_tool_result_line_is_masked_and_others_kept(self):
        from operating.card_text import masked_mcp_line
        line = json.dumps(self.RESULT).encode() + b"\n"
        out = masked_mcp_line(line)
        self.assertTrue(out.endswith(b"\n"))
        self.assertFalse(leaked(out.decode("utf-8")))
        self.assertEqual(json.loads(out)["result"]["structuredContent"]["rows"][0]["note"],
                         f"card {SHOWN}1111")
        for kept in (b'{"jsonrpc":"2.0","method":"notifications/x","params":{"n":"'
                     + VISA.encode() + b'"}}\n', b"not json " + VISA.encode() + b"\n",
                     json.dumps({"id": 1, "result": {"content": [{"text": "PO 4500123456"}]}}
                                ).encode() + b"\n"):
            self.assertEqual(masked_mcp_line(kept), kept)

    def test_the_session_relay_masks_what_reaches_the_model(self):
        import os
        import sys
        import tempfile
        from operating import agent_session
        line = json.dumps(self.RESULT).encode() + b"\n"
        with tempfile.TemporaryDirectory() as tmp:
            to, back = os.path.join(tmp, "to"), os.path.join(tmp, "back")
            os.mkfifo(to)
            os.mkfifo(back)
            stdin_r, stdin_w = os.pipe()
            stdout_r, stdout_w = os.pipe()
            os.close(stdin_w)

            def gate() -> None:
                up = os.open(to, os.O_RDONLY)
                down = os.open(back, os.O_WRONLY)
                half = len(line) // 2          # a line split over two reads
                os.write(down, line[:half])
                os.write(down, line[half:] + b'{"id":8,"result":{}}\n')
                os.close(down)
                os.close(up)
            threading.Thread(target=gate, daemon=True).start()
            fake_in, fake_out = mock.Mock(), mock.Mock()
            fake_in.fileno.return_value, fake_out.fileno.return_value = stdin_r, stdout_w
            with mock.patch.object(sys, "stdin", fake_in), \
                    mock.patch.object(sys, "stdout", fake_out):
                self.assertEqual(agent_session.connect(to, back), 0)
            os.close(stdout_w)
            got = b""
            while True:
                chunk = os.read(stdout_r, 1 << 16)
                if not chunk:
                    break
                got += chunk
            os.close(stdout_r)
            os.close(stdin_r)
        first, second = got.split(b"\n")[:2]
        self.assertFalse(leaked(first.decode("utf-8")))
        self.assertIn("1111", first.decode("utf-8"))
        self.assertEqual(second, b'{"id":8,"result":{}}')

    def test_a_connector_result_is_masked_and_host_code_gets_it_as_built(self):
        from operator_api.serving import _Watched
        result = self.RESULT

        class Access:
            def agent_server(self):
                server = mock.Mock()
                server.handle.side_effect = \
                    lambda message, **kw: json.loads(json.dumps(result))
                return server
        server = _Watched(Access(), lambda name: None).agent_server()
        call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "plx.read", "arguments": {}}}
        self.assertFalse(leaked(json.dumps(server.handle(call), ensure_ascii=False)))
        self.assertTrue(leaked(json.dumps(server.handle(call, host_code=True))))

    def test_the_stdio_host_masks_a_tool_result_for_any_client(self):
        # a client that reads the stdio host directly (no session relay, no connector
        # wrapper) still gets the result masked; other responses go as built
        from operating.mcp_server import _serve_lines
        result = self.RESULT
        handshake = {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": VISA}}}

        class Server:
            peer_role = None
            outbound: list = []

            def handle(self, message):
                return json.loads(json.dumps(handshake if message["id"] == 1 else result))
        reader = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
                            b'{"jsonrpc":"2.0","id":7,"method":"tools/call"}\n')
        writer = io.BytesIO()
        _serve_lines(Server(), reader, writer)
        first, second = writer.getvalue().decode("utf-8").splitlines()
        self.assertEqual(json.loads(first), handshake)
        self.assertFalse(leaked(second))
        self.assertEqual(json.loads(second)["result"]["structuredContent"]["rows"][0]["note"],
                         f"card {SHOWN}1111")

    def test_the_remote_employee_host_masks_a_tool_result(self):
        from operating import mcp_http_host
        result = self.RESULT
        host = object.__new__(mcp_http_host.McpHttpHost)
        host.allowed_origins, host.clock = frozenset(), lambda: 0.0
        host.sessions = mcp_http_host.OrderedDict()
        server = mock.Mock(outbound=[])
        server.handle.side_effect = lambda message: json.loads(json.dumps(result))
        host.sessions["s1"] = mcp_http_host._Session("employee:a", server, 0.0)
        who = mock.Mock()
        who.employee.principal = "employee:a"
        with mock.patch.object(mcp_http_host.McpHttpHost, "authenticate",
                               return_value=who):
            status, _, body = host.respond(
                "POST", "/mcp", {"Content-Type": "application/json",
                                 "Mcp-Session-Id": "s1", "Authorization": "Bearer t"},
                json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                            "params": {"name": "plx.read"}}).encode())
        self.assertEqual(status, 200)
        self.assertFalse(leaked(body.decode("utf-8")))
        self.assertIn("1111", body.decode("utf-8"))


class WatcherSessionInput(unittest.TestCase):
    def test_a_session_process_gets_its_task_and_fed_input_masked(self):
        # the AI watcher builds its own ClaudeCodeAgent: the process it starts sees the
        # task argument and the fed first reads masked (a real child process)
        import sys
        from operating.agent_session import _stream
        code = "import sys; print(sys.argv[1]); print(sys.stdin.read())"
        status, out = _stream([sys.executable, "-c", code, FORM], "/", {}, 60,
                              feed=lambda: f"first read: {VISA}")
        self.assertEqual(status, 0)
        self.assertFalse(leaked(out))
        self.assertIn("Cardholder Name: Pat Doe", out)
        self.assertIn(f"first read: {SHOWN}1111", out)


def luhn_cards(count: int, seed: int = 7) -> list[str]:
    """``count`` synthetic Visa-range numbers passing Luhn (test data, no real card)."""
    import random
    rng = random.Random(seed)
    out = []
    for _ in range(count):
        digits = [4] + [rng.randint(0, 9) for _ in range(14)]
        total = 0
        for i, d in enumerate(reversed(digits)):
            d = d * 2 if i % 2 == 0 else d
            total += d - 9 if d > 9 else d
        out.append("".join(map(str, digits + [(10 - total % 10) % 10])))
    return out


class JsonStaysJson(unittest.TestCase):
    """Masking JSON text (an MCP text part that is itself JSON, an Ask session's
    stream-json input line) masks its values and never breaks the document."""

    INNERS = ('<a href="https://files.example/f?token=Abc123XYZ987">open</a>',
              'see ?sig=abcdEFGH1234%2F" now', 'password: abcdefgh1"',
              'API_KEY=abcd1234efgh"', 'pwd: "x y z\\"more"',
              "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEabc", f"card {VISA} CVV 737")

    def test_secret_and_card_values_inside_json_mask_and_parse(self):
        for inner in self.INNERS:
            for doc in (json.dumps({"content": [{"type": "text", "text": inner}]}),
                        json.dumps({"type": "user", "message": {"content": inner}}) + "\n",
                        json.dumps([inner], ensure_ascii=False)):
                with self.subTest(inner=inner[:24], doc=doc[:8]):
                    out = mask_served(doc)
                    value = json.loads(out)               # still one JSON document
                    flat = json.dumps(value)
                    self.assertFalse(leaked(flat))
                    for secret in ("Abc123XYZ987", "abcdEFGH1234", "abcdefgh1",
                                   "abcd1234efgh", "x y z", "MIIEabc", "737"):
                        self.assertNotIn(secret, flat)
                    self.assertEqual(doc.endswith("\n"), out.endswith("\n"))
                    self.assertEqual(mask_served(out), out)          # idempotent
        # a text part that is JSON holding JSON, and a credential named only by its key
        nested = json.dumps({"text": json.dumps({"api_key": "abcd1234efgh", "n": 1}),
                             "password": "hunter2hunter2", "page_token": "Cg0xMjM0NTY",
                             "Card": VISA, "cvv": "737"})
        out = json.loads(mask_served(nested))
        self.assertEqual(json.loads(out["text"])["api_key"], "[secret]")
        self.assertEqual(out["password"], "[secret]")
        self.assertEqual(out["page_token"], "Cg0xMjM0NTY")      # a paging cursor stays
        self.assertEqual((out["Card"], out["cvv"]), (SHOWN + "1111", DOT * 3))
        # JSON with nothing to mask comes back byte for byte (no re-serialising)
        plain = '{"key":  "ORD-1234", "n": [1, 2]}'
        self.assertEqual(mask_served(plain), plain)

    def test_deeply_nested_json_is_masked_never_raises(self):
        for depth in (10, 990, 3000):
            doc = "[" * depth + f'"password: abcdefgh12 card {VISA}"' + "]" * depth
            with self.subTest(depth=depth):
                out = mask_served(doc)
                self.assertNotIn("abcdefgh12", out)
                self.assertFalse(leaked(out))

    def test_the_mcp_line_and_session_feed_stay_json(self):
        from operating.card_text import masked_mcp_line
        part = json.dumps({"href": "https://x.example/a?token=Abc123XYZ987\"", "card": VISA})
        line = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": part}]}}).encode() + b"\n"
        message = json.loads(masked_mcp_line(line))
        inner = json.loads(message["result"]["content"][0]["text"])
        self.assertEqual(inner["card"], SHOWN + "1111")
        self.assertNotIn("Abc123XYZ987", inner["href"])


class MaskingIsLinear(unittest.TestCase):
    def test_card_dense_text_masks_in_linear_time(self):
        # was quadratic in the number of cards: 600 KB holding 20k cards took 17 s
        import time
        cards = luhn_cards(20000)
        text = "\n".join(f"{i},{card},CVV 123" for i, card in enumerate(cards))
        self.assertGreater(len(text), 550_000)
        started = time.perf_counter()
        out = mask_card_numbers(text)
        took = time.perf_counter() - started
        self.assertLess(took, 4.0, f"{took:.1f} s")
        self.assertEqual(out.count(SHOWN), len(cards))
        self.assertNotIn("CVV 123", out)
        flat = "".join(ch for ch in out if ch.isdigit())
        self.assertFalse(any(card in flat for card in cards[:200]))
        # twice the cards, about twice the time (not four times)
        double = text + "\n" + text
        started = time.perf_counter()
        mask_card_numbers(double)
        self.assertLess(time.perf_counter() - started, max(4 * took, 1.0))

    def test_a_code_far_from_every_card_stays(self):
        # the bisected proximity check keeps its reach: within NEAR masked, beyond kept
        from operating.card_text import NEAR
        cards = luhn_cards(3, seed=11)
        gap = " " * (NEAR + 40)
        text = (f"CVV 111 {gap}card {cards[0]} CVV 222{gap}CVV 333{gap}"
                f"card {cards[1]}{gap}card {cards[2]} code CVV 444{gap}CVV 555")
        out = mask_card_numbers(text)
        for kept in ("CVV 111", "CVV 333", "CVV 555"):
            self.assertIn(kept, out)
        for gone in ("CVV 222", "CVV 444"):
            self.assertNotIn(gone, out)
        self.assertEqual(out.count("CVV " + DOT * 3), 2)


class HumanizeRewriteIsMasked(unittest.TestCase):
    TEXT = f"Here's the thing: PO 106001 was paid by card {VISA} for Acme [1]."

    def test_the_rewrite_model_sees_the_text_masked_and_its_check_passes(self):
        from operating import humanize
        seen: list[str] = []
        good = f"PO 106001 was paid by card {SHOWN}1111 for Acme [1]."
        got = humanize.polish(self.TEXT, rewrite=lambda p: seen.append(p) or good)
        self.assertTrue(seen)
        self.assertFalse(leaked(seen[0]))
        self.assertIn(SHOWN + "1111", seen[0])
        # the fact check passes for the masked text, and the rewrite is used, masked
        self.assertTrue(humanize.preserved(mask_served(self.TEXT), good))
        self.assertEqual(("rewritten", good), (got.action, got.text))
        # a reply carrying the full number again is no deletion of the masked text
        unmasked = f"PO 106001 was paid by card {VISA} for Acme [1]."
        got = humanize.polish(self.TEXT, rewrite=lambda p: unmasked)
        self.assertEqual((self.TEXT, "kept"), (got.text, got.action))

    def test_the_production_rewriter_sends_its_prompt_masked(self):
        # ``PLX_HUMANIZE=rewrite`` callers (general answer, instruct, AI watcher) pass no
        # rewrite of their own: the ``claude -p`` process gets the prompt masked
        from operating import humanize
        sent: list[str] = []

        def run(argv, input, **kwargs):
            sent.append(input)
            return mock.Mock(stdout=json.dumps({"result": "PO 106001 was paid [1]."}))
        with mock.patch.object(humanize.subprocess, "run", side_effect=run), \
                mock.patch("operating.model_ledger.record"):
            humanize.rewriter("claude-test")(f"Rewrite: {FORM}")
        self.assertEqual(len(sent), 1)
        self.assertFalse(leaked(sent[0]))
        self.assertIn("Cardholder Name: Pat Doe", sent[0])


class RememberedMasking(unittest.TestCase):
    """``mask_served`` answers a string it has masked before from a table (speed plan 9.3):
    the answer is always the scan's, for a card, a credential, plain text and a string past
    the table's size; and the table stays bounded."""

    def setUp(self):
        from operating import card_text as ct
        self.ct = ct

    def scan(self, text):
        return self.ct._secrets(self.ct.mask_card_numbers(text))

    def test_every_answer_is_the_scan_first_time_and_again(self):
        samples = [FORM, VISA, "plain words about an order", "", "x" * 12,
                   "password: hunter2hunter2 for the portal",
                   'api_key="sk-live-0123456789abcdef0123"', "PO 4500123456 shipped",
                   "n" * (self.ct.REMEMBER_LONGEST + 5) + " " + VISA]
        for text in samples * 2:
            with self.subTest(text=text[:20]):
                self.assertEqual(mask_served(text), self.scan(text))
        self.assertFalse(leaked(mask_served(FORM)))
        self.assertFalse(leaked(mask_served(FORM)))   # the remembered answer is masked too

    def test_a_string_past_the_size_is_scanned_each_time_and_not_kept(self):
        long_text = "w " * self.ct.REMEMBER_LONGEST + VISA
        before = len(self.ct._REMEMBERED)
        self.assertFalse(leaked(mask_served(long_text)))
        self.assertEqual(len(self.ct._REMEMBERED), before)

    def test_the_table_is_bounded(self):
        with mock.patch.object(self.ct, "REMEMBER_MOST", 50):
            for n in range(500):
                mask_served(f"order note number {n}")
            self.assertLessEqual(len(self.ct._REMEMBERED), 50)


if __name__ == "__main__":
    unittest.main()
