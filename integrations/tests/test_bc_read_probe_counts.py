"""What the BC probe and capture count, through an injected transport, never a live read.

``probe --finance-entities``: an entity set BC serves empty (200, empty ``value``) is no
finance error; a set BC refuses for one company while another serves it is a
``finance_refused`` row, not counted when the spec expects it for that company
(``--expected-refusal``, company book, entity set and BC code) and a finance error at once
when it does not; any other failed
read is one, typed by its HTTP status. A transport failure is sent again after the
probe's pauses before it counts (``ProbeBackoff``), within a sleep budget and the probe's
wall cap.
``capture``: ``units_total`` is every planned unit, ``units_reached`` the ones a call
reached before a budget stop, and ``bc.capture_full`` fails a call that did not reach them
all. Reach: a document header page carries its lines
(``$expand``), one GET for N headers instead of N + 1, and falls back to the per-header
read wherever the expansion is refused or not whole. A unit a run completed that began
before the session's ``fresh_since`` is stale, never ``all_complete``."""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlsplit

from verify import live_verify
from integrations import bc_read
from integrations.capture_store import CaptureStore, bc_unit
from integrations.live_source_grammar import BC_LINE_PARENT_FIELD
from integrations.live_source_guard import BoundedBackoff, LiveSourceAudience, NotVisible
from integrations.tests.test_bc_capture import (ALL_UNITS, BOOKS, CO, ENV, SERVICE, FakeBc,
                                                Harness, gid)
from ontology.core import Actor
from scripts import ontology_projection_probe as projection

SPECS = Path(__file__).resolve().parents[2] / "verify" / "live-checks"
BAD_REQUEST = (400, {}, b'{"error":{"code":"BadRequest"}}')
DENIED = (403, {}, b'{"error":{"code":"Authorization_RequestDenied"}}')
JOURNALS = ("customerPaymentJournals", "vendorPaymentJournals")


def spec(name: str) -> dict:
    return json.loads((SPECS / f"{name}.json").read_text())


def header(company: str, entity_set: str) -> str:
    return rf"{company}\)/{entity_set}(\?|$)"


def header_read(entity_set: str) -> dict:
    """The ``request_shape`` of the probe's header read (``$top=1``)."""
    return {"path": entity_set, "options": ["top"], "select": [], "filter": [], "orderby": []}


class Clock:
    """A monotonic clock that only the injected sleeps (and ``Slow`` GETs) advance."""

    def __init__(self) -> None:
        self.now = 0.0
        self.unit_pauses: list[tuple[float, float]] = []  # (started at, seconds)

    def __call__(self) -> float:
        return self.now

    def unit_sleep(self, seconds: float) -> None:
        self.unit_pauses.append((self.now, seconds))
        self.now += seconds


class Slow:
    """Every BC GET takes ``seconds`` of the clock (a slow or timing-out source)."""

    def __init__(self, inner, clock: Clock, seconds: float) -> None:
        self.inner, self.clock, self.seconds = inner, clock, seconds

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        if method == "GET":
            self.clock.now += self.seconds
        return self.inner(method, url, headers, body, timeout, max_bytes)


class Cut:
    """Raises URLError (as urllib does on a failed connect) for the first ``times`` GETs
    whose URL matches ``pattern``; the fake BC answers everything else."""

    def __init__(self, inner, pattern: str, times: int) -> None:
        self.inner, self.pattern, self.times = inner, re.compile(pattern), times

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        if method == "GET" and self.times and self.pattern.search(unquote(url)):
            self.times -= 1
            raise URLError("connect failed")
        return self.inner(method, url, headers, body, timeout, max_bytes)


def run(sender, clock: Clock | None = None, **kwargs) -> dict:
    clock = clock or Clock()
    kwargs.setdefault("finance_entities", True)
    kwargs.setdefault("books", BOOKS)
    retry = kwargs.pop("retry", None) or bc_read.ProbeBackoff(sleep=clock.unit_sleep,
                                                              timer=clock)
    report = bc_read.probe(ENV, sender=sender, retry=retry, **kwargs)
    return dict(report, evidence_label="live-read")


def headers(report: dict, key: str = "finance_entities") -> dict:
    return {(c["company_index"], e["entity_set"]): e for c in report[key]
            for e in c["entities"]}


class FinanceErrorCountTests(unittest.TestCase):
    def test_an_empty_served_set_is_no_error_and_a_failed_read_is_typed(self):
        fake = FakeBc({"purchaseCreditMemos": 0, "itemLedgerEntries": 0}, companies=CO)
        clean = run(fake)
        rows = headers(clean)
        self.assertEqual(len(rows), len(CO) * len(bc_read.FINANCE_ENTITY_SETS))
        self.assertEqual((rows[(1, "purchaseCreditMemos")]["records_returned"],
                          rows[(1, "purchaseCreditMemos")]["has_etag"]), (0, None))
        self.assertEqual((clean["finance_errors"], clean["finance_entity_errors"],
                          clean["finance_not_exposed"], clean["finance_refused"],
                          clean["finance_served"], clean["stopped_reason"]),
                         (0, [], [], [], len(rows), None))
        # The fake rows carry no allowlisted field names: only those checks fail here.
        shape = live_verify.check_expectations(spec("bc.ledgers"), 0, clean)
        self.assertTrue(shape and all("_absent not empty" in f for f in shape), shape)

        fake = FakeBc({"purchaseCreditMemos": 0}, companies=CO)
        fake.fail(header(CO[2], "vendorPaymentJournals"),
                  (500, {}, b'{"error":{"code":"Internal_ServerError"}}'))
        fake.fail(header(CO[0], "agedAccountsPayables"),
                  (404, {}, b'{"error":{"code":"BadRequest_NotFound"}}'))
        fake.fail(rf"{CO[0]}\)/customerPaymentJournals\([^)]+\)/customerPayments",
                  (403, {}, b'{"error":{"code":"Authorization_RequestDenied"}}'))
        failed = run(fake)
        errors = {(e["company_index"], e["entity_set"], e["part"]): e["http_status"]
                  for e in failed["finance_entity_errors"]}
        self.assertEqual(errors, {(2, "vendorPaymentJournals", "header"): 500,
                                  (0, "customerPayments", "lines"): 403})
        self.assertEqual((failed["finance_errors"], failed["finance_refused"]), (2, []))
        # A 404 stays a coded gap, never counted as an error.
        self.assertEqual(failed["finance_not_exposed"],
                         [{"company_index": 0, "entity_set": "agedAccountsPayables"}])
        self.assertEqual(set(live_verify.check_expectations(spec("bc.ledgers"), 0, failed))
                         - set(shape), {"finance_errors != expected",
                                        "finance_entities[*].entities[*].lines.error present"})
        # The live evidence names each failed read's status and code class.
        recorded = live_verify._recorded(spec("bc.ledgers"), failed)
        self.assertEqual((recorded["finance_entity_errors[*].http_status"],
                          recorded["finance_entity_errors[*].error_code"]),
                         ([e["http_status"] for e in failed["finance_entity_errors"]],
                          [e["error_code"] for e in failed["finance_entity_errors"]]))
        self.assertEqual(sorted(recorded["finance_entity_errors[*].http_status"]), [403, 500])

    def test_an_expected_refusal_is_reported_and_an_unexpected_one_fails_at_once(self):
        """The 2026-10-07..10 dailies' four errors: payment journals of companies 1 and 2
        refused (capture: source_refused) while company 0 served them. Decided:
        such a refusal is named by entity set, company and BC
        code; the spec lists the ones it expects (as data), which are reported and not
        counted; any other fails the check at once, never a blocked window. Scoped by
        company: one company's listed refusal never excuses
        another company's refusal of the same set with the same code."""
        refusals = [(CO[1], JOURNALS[0]), (CO[2], JOURNALS[0]), (CO[2], JOURNALS[1])]

        def refusing() -> FakeBc:
            fake = FakeBc(companies=CO)
            for company, name in refusals:
                fake.fail(header(company, name), DENIED)
            return fake
        ledgers = spec("bc.ledgers")
        shape = live_verify.check_expectations(ledgers, 0, run(FakeBc(companies=CO)))
        book = BOOKS["production"]
        named = [{"company_index": CO.index(company), "entity_set": name,
                  "error_class": "SourceUnavailable", "http_status": 403,
                  "error_code": "authorization_request_denied", "detail": "http_403",
                  "message_class": None, "request": header_read(name)}
                 for company, name in refusals]
        every = [f"{book[company]}:{name}:authorization_request_denied"
                 for company, name in refusals]
        for label, expected, counted in (
                ("none expected", [], [0, 1, 2]),
                ("every refusal expected", every, []),
                # Company 1's refusal is listed; company 2's of the same set and code is not.
                ("one company's refusal expected", every[:1], [1, 2]),
                ("another BC code expected", [v.replace("authorization_request_denied",
                                                        "bad_request") for v in every],
                 [0, 1, 2])):
            with self.subTest(label):
                report = run(refusing(), expected_refusals=expected)
                self.assertEqual(report["finance_refused"], [
                    dict(row, book=book[refusals[i][0]], expected=i not in counted)
                    for i, row in enumerate(named)])
                self.assertEqual(report["finance_entity_errors"],
                                 [dict(named[i], part="header") for i in counted])
                self.assertEqual(report["finance_errors"], len(counted))
                self.assertNotIn("records_returned", headers(report)[(1, JOURNALS[0])])
                # Never blocked (no 24 h window, never host_misconfiguration): a pass when
                # every refusal is expected, a failure at once when one is not.
                self.assertNotIn("blocked_upstream", report)
                self.assertEqual(live_verify.blocked_parts(report), [])
                failures = live_verify.check_expectations(ledgers, 0, report)
                self.assertEqual(sorted(failures), sorted(
                    shape + (["finance_errors != expected"] if counted else [])))
                recorded = live_verify._recorded(ledgers, report)
                self.assertEqual((recorded["finance_refused[*].book"],
                                  recorded["finance_refused[*].entity_set"],
                                  recorded["finance_refused[*].http_status"],
                                  recorded["finance_refused[*].error_code"],
                                  recorded["finance_refused[*].expected"]),
                                 ([book[c] for c, _n in refusals],
                                  [n for _c, n in refusals], [403] * 3,
                                  ["authorization_request_denied"] * 3,
                                  [i not in counted for i in range(3)]))

        # No proof that BC refused this company only: each stays a counted error, expected
        # or not.
        for label, script in (
                ("refused in every company", [(c, BAD_REQUEST) for c in CO]),
                ("no BC error code (a proxy page)",
                 [(CO[1], (403, {}, b"<html>Forbidden</html>"))])):
            with self.subTest(label):
                fake = FakeBc(companies=CO)
                for company, response in script:
                    fake.fail(header(company, JOURNALS[0]), response)
                report = run(fake, expected_refusals=[
                    f"{b}:{JOURNALS[0]}:bad_request" for b in BOOKS["production"].values()])
                self.assertEqual(report["finance_refused"], [])
                self.assertEqual(report["finance_errors"], len(script))
                self.assertIn("finance_errors != expected",
                              live_verify.check_expectations(ledgers, 0, report))

    def test_a_400_names_its_message_class_and_only_a_spec_entry_makes_it_expected(self):
        """The dailies of 2026-10-11 recorded the payment journals of
        companies 1 and 2 as HTTP 400, code ``other``, nothing more. The probe records the
        BC code and the class of the BC message (never its text, which carries names and
        filter values) on every refusal row, as diagnosis only:
        the classes are English patterns no live message confirmed, so a 400 naming a
        missing journal template or batch is expected only for the company a spec
        ``--expected-refusal`` entry lists (book, set, BC code), never for its message."""
        planted = ("ACME-PLANTED", "CASHRCPT-77", "4711.50")

        def bc_error(code: str, message: str, status: int = 400) -> tuple:
            return (status, {}, json.dumps({"error": {"code": code, "message": message}})
                    .encode())
        template = bc_error("Internal_RecordNotFound",
                            "There is no Gen. Journal Template within the filter.  Filters: "
                            f"Type: Cash Receipts, Name: {planted[1]}, Owner: {planted[0]}")
        batch = bc_error("Application_DialogException",
                         f"The Gen. Journal Batch does not exist. Identification fields and "
                         f"values: Journal Template Name='{planted[1]}',Name='{planted[0]}'")
        book = BOOKS["production"]
        setup = [(CO[1], JOURNALS[0], template), (CO[2], JOURNALS[0], template),
                 (CO[2], JOURNALS[1], batch)]
        classes = [("internal_record_not_found", "journal_template_missing")] * 2 + [
            ("application_dialog_exception", "journal_batch_missing")]
        listed = [f"{book[c]}:{n}:{code}" for (c, n, _r), (code, _m) in zip(setup, classes)]
        ledgers = spec("bc.ledgers")
        shape = live_verify.check_expectations(ledgers, 0, run(FakeBc(companies=CO)))
        for label, script, entries, refused, errors in (
                # internal_record_not_found is BC's generic record-missing code, not evidence
                # of an unserved set (only internal_data_not_found_filter is, see
                # test_bc_read_entities): unlisted, it fails at once like a missing batch.
                ("journal template and batch missing, no entry", setup, [],
                 [(c, m, False) for c, m in classes], classes),
                ("journal template and batch missing, listed", setup, listed,
                 [(c, m, True) for c, m in classes], []),
                # Company 1's entry never excuses company 2's same set, code and class.
                ("one company listed", setup, listed[:1],
                 [(c, m, i == 0) for i, (c, m) in enumerate(classes)], classes[1:]),
                # A setup table of another subject is no journal setup (1970 minor 1).
                ("currency setup missing",
                 [(CO[1], JOURNALS[0], bc_error("BadRequest",
                                                "Currency setup was not found."))], [],
                 [("bad_request", "record_not_found", False)],
                 [("bad_request", "record_not_found")]),
                ("payment journal setup missing",
                 [(CO[1], JOURNALS[0], bc_error("BadRequest", "The Payment Registration "
                                                "Setup does not exist."))], [],
                 [("bad_request", "setup_missing", False)], [("bad_request", "setup_missing")]),
                ("unknown code, unclassified message",
                 [(CO[1], JOURNALS[0], bc_error("Acme_Custom", f"{planted[0]} owes "
                                                f"{planted[2]}"))], [],
                 [("other", "unclassified", False)], [("other", "unclassified")]),
                ("a field validation 400",
                 [(CO[1], JOURNALS[1], bc_error("BadRequest", "Amount must be positive"))], [],
                 [("bad_request", "field_validation", False)],
                 [("bad_request", "field_validation")]),
                ("a setup message naming a permission",
                 [(CO[1], JOURNALS[0], bc_error("BadRequest", "The Gen. Journal Template "
                                                "does not exist or you lack permission"))], [],
                 [("bad_request", "permission_denied", False)],
                 [("bad_request", "permission_denied")]),
                ("no message at all", [(CO[1], JOURNALS[0], BAD_REQUEST)], [],
                 [("bad_request", None, False)], [("bad_request", None)]),
                ("setup message on a 403",
                 [(CO[1], JOURNALS[0], bc_error("Forbidden", "The Gen. Journal Template "
                                                "does not exist.", 403))], [],
                 [("forbidden", "journal_template_missing", False)],
                 [("forbidden", "journal_template_missing")]),
                # Listed or not, a set no company served is no refusal: an error.
                ("refused in every company", [(c, JOURNALS[0], template) for c in CO],
                 [f"{b}:{JOURNALS[0]}:internal_record_not_found" for b in book.values()], [],
                 [classes[0]] * 3)):
            with self.subTest(label):
                fake = FakeBc(companies=CO)
                for company, name, response in script:
                    fake.fail(header(company, name), response)
                report = run(fake, expected_refusals=entries)
                self.assertEqual([(r["error_code"], r["message_class"], r["expected"])
                                  for r in report["finance_refused"]], refused)
                self.assertEqual([(e["error_code"], e["message_class"])
                                  for e in report["finance_entity_errors"]], errors)
                self.assertEqual(report["finance_errors"], len(errors))
                failures = live_verify.check_expectations(ledgers, 0, report)
                self.assertEqual(sorted(failures), sorted(
                    shape + (["finance_errors != expected"] if errors else [])))
                recorded = live_verify._recorded(ledgers, report)
                for field in ("message_class", "expected"):
                    self.assertEqual(recorded.get(f"finance_refused[*].{field}", []),
                                     [r[field] for r in report["finance_refused"]])
                self.assertEqual(recorded.get("finance_entity_errors[*].message_class", []),
                                 [e["message_class"] for e in report["finance_entity_errors"]])
                text = json.dumps(report)
                self.assertFalse([p for p in planted if p in text], label)

    def test_a_journal_400_names_bc_code_and_request_and_an_unsupported_read_is_never_sent(
            self):
        """Every bc.ledgers record named the payment
        journals of consol and distribution refused 400 with code ``other`` (BC's code was
        outside the vocabulary), so no ``--expected-refusal`` entry could cite one. The probe
        records BC's own platform code ``<Category>_<Name>`` as its token, never a code that
        could carry a value, and each failed read's request shape (no value). The request
        is the header read ``$top=1``, which the pinned ``$metadata`` supports; a read naming
        a field the pin lacks is never sent."""
        planted = ("4711", "PLX Consol", "5f0d1c7e", "Page ID: 255")

        def journal_400(code: str) -> tuple:
            # BC's error body for the journal pages (``error.code`` and ``error.message``,
            # whose text names the template filter): which platform code BC sends here is
            # undocumented, so each code shape is one case.
            return (400, {}, json.dumps({"error": {"code": code, "message": (
                "There is no Gen. Journal Template within the filter.  Filters: Type: Cash "
                "Receipts, Recurring: No, Page ID: 255  CorrelationId:  "
                "5f0d1c7e-0000-4000-8000-000000004711.")}}).encode())
        book = BOOKS["production"]
        refused = [(c, n) for c in CO[1:] for n in JOURNALS]
        ledgers = spec("bc.ledgers")
        shape = live_verify.check_expectations(ledgers, 0, run(FakeBc(companies=CO)))
        for code, token in (("BadRequest_JournalTemplateMissing",
                             "bad_request_journal_template_missing"),
                            ("Internal_NoRecordWithinFilter", "internal_no_record_within_filter"),
                            # A code that could carry a value reads other.
                            ("Internal_Record4711", "other"), ("Internal_PLX Consol", "other"),
                            ("Acme_JournalTemplate", "other"), ("Internal_" + "A" * 60, "other")):
            entries = [] if token == "other" else [f"{book[c]}:{n}:{token}" for c, n in refused]
            for listed in ([], entries) if entries else ([],):
                with self.subTest(code=code, listed=bool(listed)):
                    fake = FakeBc(companies=CO)
                    for company, name in refused:
                        fake.fail(header(company, name), journal_400(code))
                    report = run(fake, expected_refusals=listed)
                    self.assertEqual(
                        [(r["book"], r["entity_set"], r["http_status"], r["error_code"],
                          r["message_class"], r["request"], r["expected"])
                         for r in report["finance_refused"]],
                        [(book[c], n, 400, token, "journal_template_missing", header_read(n),
                          bool(listed)) for c, n in refused])
                    self.assertNotIn("reason", report["finance_refused"][0])
                    self.assertEqual(report["finance_errors"], 0 if listed else 4)
                    self.assertEqual(sorted(live_verify.check_expectations(ledgers, 0, report)),
                                     sorted(shape + ([] if listed
                                                     else ["finance_errors != expected"])))
                    recorded = live_verify._recorded(ledgers, report)
                    self.assertEqual((recorded["finance_refused[*].error_code"],
                                      recorded["finance_refused[*].request"]),
                                     ([token] * 4, [header_read(n) for _c, n in refused]))
                    self.assertEqual(recorded.get("finance_entity_errors[*].request", []),
                                     [header_read(n) for _c, n in refused][:0 if listed else 4])
                    self.assertFalse([p for p in planted if p in json.dumps(report)])
            if token == "other":
                with self.assertRaises(ValueError):
                    bc_read.expected_refusal(f"consol:{JOURNALS[0]}:{token}",
                                             set(book.values()))

        # The pinned $metadata lacks a name of the set's $select allowlist: that read is
        # never sent (the select check fails typed and the check names the set), the
        # header read still is.
        pinned = dict(bc_read.pinned_properties())
        pinned[JOURNALS[0]] = pinned[JOURNALS[0]] - {"displayName"}
        fake = FakeBc(companies=CO)
        with mock.patch.object(bc_read, "_PINNED", pinned):
            report = run(fake)
        reads = [unquote(urlsplit(url).query) for _m, url, _h in fake.calls
                 if urlsplit(url).path.endswith("/" + JOURNALS[0])]
        self.assertEqual(len(reads), len(CO))
        self.assertFalse([q for q in reads if "$select" in q])
        self.assertEqual({(i, e["select_accepted"], e.get("select_error"))
                          for (i, name), e in headers(report).items() if name == JOURNALS[0]},
                         {(i, False, "metadata_unsupported") for i in range(len(CO))})
        self.assertEqual(report["finance_select_rejected"], [JOURNALS[0]])
        self.assertTrue([f for f in live_verify.check_expectations(ledgers, 0, report)
                         if f.startswith("finance_select_rejected")])

    def test_the_spec_command_names_its_expected_refusals_and_unknown_ones_fail_closed(self):
        command = spec("bc.ledgers")["command"]
        self.assertEqual(command[:3], ["python3", "-m", "integrations.bc_read"])
        listed = [command[i + 1] for i, arg in enumerate(command)
                  if arg == "--expected-refusal"]
        books = set(bc_read.load_company_books()["production"].values())
        self.assertEqual([bc_read.expected_refusal(value, books) for value in listed],
                         [tuple(value.split(":")) for value in listed])
        clock = Clock()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bc_read.main(command[3:], environ=ENV, sender=FakeBc(companies=CO),
                                          retry=bc_read.ProbeBackoff(sleep=clock.unit_sleep,
                                                                     timer=clock)), 0)
        self.assertEqual(json.loads(out.getvalue())["finance_errors"], 0)
        # The company-less form scoped nothing: refused too.
        for bad in (f"{JOURNALS[0]}:bad_request", f"unpinned:{JOURNALS[0]}:bad_request",
                    "consol:purchaseOrders:bad_request", f"consol:{JOURNALS[0]}:other",
                    f"consol:{JOURNALS[0]}:NotACode", f"consol:{JOURNALS[0]}:bad_request:x",
                    ""):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    bc_read.expected_refusal(bad, books)
                fake = FakeBc(companies=CO)
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(bc_read.main(command[3:] + ["--expected-refusal", bad],
                                                  environ=ENV, sender=fake), 1)
                self.assertEqual(fake.calls, [])  # refused before any request

    def test_a_book_pinned_to_two_companies_is_refused_at_load(self):
        """One ``--expected-refusal`` entry names one book: a map with two companies on
        that book (or one company pinned twice) would excuse both, so it never loads."""
        one, two = "fbd2ba74-f148-f111-b476-7ced8db443c4", "ca41a27f-809d-f111-8072-6045bd5bf768"
        for label, pins in (("two companies, one book", {one: "consol", two: "consol"}),
                            ("one company twice", {one: "consol", one.upper(): "distribution"})):
            with self.subTest(label):
                document = {"schema": bc_read.BOOKS_SCHEMA, "environments": {"production": pins}}
                with self.assertRaisesRegex(ValueError, "'consol'|'distribution'"):
                    bc_read.load_company_books(document)
        # The same book in two environments is one company per environment: loaded.
        books = bc_read.load_company_books({"schema": bc_read.BOOKS_SCHEMA, "environments": {
            "production": {one: "consol"}, "sandbox": {one: "consol"}}})
        self.assertEqual(books["sandbox"], {one: "consol"})


class TransientReadTests(unittest.TestCase):
    """The 2026-10-08/10 hourly bc.case_entities failures: one read each, a different set
    every time, ``retried.urlerror`` (a failed connect that outlasted the GET's retries)."""

    CASES = (("bc.case_entities", "case_entities", {"case_entities": True,
                                                    "finance_entities": False},
              CO[2], "purchaseReceipts"),
             ("bc.ledgers", "finance_entities", {}, CO[2], "vendorPaymentJournals"))

    def test_a_urlerror_that_succeeds_on_a_later_attempt_is_no_error_and_one_that_persists_fails(
            self):
        attempts = 1 + BoundedBackoff().max_retries  # one GET's attempts
        for name, key, kwargs, company, entity_set in self.CASES:
            with self.subTest(name):
                check = spec(name)
                clean = run(FakeBc(companies=CO), **kwargs)
                shape = live_verify.check_expectations(check, 0, clean)
                where = (CO.index(company), entity_set)

                for cut in range(1, attempts):
                    clock = Clock()
                    healed = run(Cut(FakeBc(companies=CO), header(company, entity_set), cut),
                                 clock, **kwargs)
                    self.assertEqual(healed[key], clean[key])
                    pauses = list(bc_read.PROBE_UNIT_PAUSES[:cut])
                    self.assertEqual((healed["unit_retries"], healed["unit_retry_sleep_seconds"],
                                      [s for _t, s in clock.unit_pauses]),
                                     (cut, sum(pauses), pauses))
                    self.assertEqual(live_verify.check_expectations(check, 0, healed), shape)

                clock = Clock()
                down = run(Cut(FakeBc(companies=CO), header(company, entity_set), 10 ** 6),
                           clock, **kwargs)
                failure = headers(down, key)[where]["failure"]
                self.assertEqual((failure["http_status"], failure["detail"]),
                                 (None, "retried.urlerror"))
                self.assertEqual([s for _t, s in clock.unit_pauses],
                                 list(bc_read.PROBE_UNIT_PAUSES))
                failures = live_verify.check_expectations(check, 0, down)
                self.assertTrue(set(failures) - set(shape), failures)
                if key == "finance_entities":
                    self.assertEqual((down["finance_errors"], down["finance_refused"]), (1, []))

    def test_an_outage_is_retried_only_within_the_budget(self):
        # Every finance read fails at the transport (the company listing and the probe's
        # own first entity read answer).
        everything = rf"\)/(?:{'|'.join(bc_read.FINANCE_ENTITY_SETS)})"
        clock = Clock()
        report = run(Cut(FakeBc(companies=CO), everything, 10 ** 6), clock)
        self.assertEqual(report["finance_errors"], len(CO) * len(bc_read.FINANCE_ENTITY_SETS))
        self.assertEqual((report["finance_refused"], report["finance_served"]), ([], 0))
        self.assertGreater(report["unit_retries"], 0)
        self.assertLessEqual(report["unit_retry_sleep_seconds"],
                             bc_read.PROBE_RETRY_BUDGET_SECONDS)
        self.assertEqual(report["stopped_reason"], None)

    def test_a_probe_whose_reads_and_retries_would_pass_the_wall_cap_stops_inside_it(self):
        """The 180 s ProbeRetry deadline only stopped new retries: a slow source still ran
        the probe past the check's 300 s timeout (killed, no verdict). Now no read starts,
        and no GET is sent again, that could end past ``PROBE_WALL_SECONDS``; each read cut
        is a typed ``wall_cap`` failure."""
        wall = bc_read.PROBE_WALL_SECONDS
        everything = rf"\)/(?:{'|'.join(bc_read.FINANCE_ENTITY_SETS)})"
        for label, failing in (("slow source", 0), ("slow source failing", 10 ** 6)):
            with self.subTest(label):
                clock = Clock()
                sender = Slow(Cut(FakeBc(companies=CO), everything, failing), clock, 10.0)
                report = run(sender, clock, retry=bc_read.ProbeBackoff(
                    sleep=clock.unit_sleep, timer=clock, budget_seconds=10 ** 6))
                self.assertLessEqual(clock.now, wall)
                self.assertEqual(report["stopped_reason"], "wall_cap")
                capped = [e for e in report["finance_entity_errors"]
                          if e["detail"] == "wall_cap"]
                self.assertTrue(capped)
                self.assertTrue(all((e["error_class"], e["http_status"]) ==
                                    ("SourceUnavailable", None) for e in capped), capped)
                self.assertEqual(report["finance_errors"], len(report["finance_entity_errors"]))
                self.assertIn("finance_errors != expected",
                              live_verify.check_expectations(spec("bc.ledgers"), 0, report))
                self.assertTrue(all(start + pause + bc_read.PROBE_ATTEMPT_SECONDS <= wall
                                    for start, pause in clock.unit_pauses), clock.unit_pauses)
                if failing:
                    self.assertGreater(report["unit_retries"], 0)

    def test_every_check_running_the_probe_times_out_after_its_wall_cap(self):
        """bc.adapter_probe killed the probe at 120 s, before its 240 s wall cap could type
        the stop. Every spec running ``bc_read probe`` leaves the cap and one more attempt
        (startup, the token POST, printing) inside its timeout."""
        probes = {}
        for path in sorted(SPECS.glob("*.json")):
            check = json.loads(path.read_text())
            if check.get("command", [])[:4] == ["python3", "-m", "integrations.bc_read",
                                                "probe"]:
                probes[check["id"]] = check.get("timeout_seconds",
                                                live_verify.DEFAULT_TIMEOUT)
        self.assertLessEqual({"bc.adapter_probe", "bc.case_entities", "bc.ledgers"},
                             set(probes))
        for check_id, timeout in probes.items():
            with self.subTest(check_id):
                self.assertGreater(timeout, bc_read.PROBE_WALL_SECONDS
                                   + bc_read.PROBE_ATTEMPT_SECONDS)


class PlannedUnitCountTests(unittest.TestCase):
    def test_a_budget_stop_reports_every_planned_unit(self):
        sets = ("purchaseOrders", "vendors")
        planned = len(CO) * (len(sets) + 1 + 1)  # each set, purchaseOrderLines, company
        reach = "units_reached != units_total"
        for n in (1, 10, 60):
            with self.subTest(records=n):
                h = Harness(self, FakeBc({"purchaseOrders": n, "vendors": n}, companies=CO))
                cut = h.run(entity_sets=sets, budget_seconds=6)
                self.assertEqual(cut["stopped_reason"], "budget")
                self.assertEqual(cut["units_total"], planned)
                self.assertLess(cut["units_reached"], planned)
                self.assertEqual(cut["units_reached"],
                                 sum(len(c["units"]) for c in cut["companies"]))
                self.assertFalse(cut["all_complete"])
                self.assertIn(reach, live_verify.check_expectations(
                    spec("bc.capture_full"), 0, cut))
                done = h.run(entity_sets=sets)
                self.assertEqual((done["units_total"], done["units_reached"],
                                  done["units_complete"], done["all_complete"]),
                                 (planned, planned, planned, True))
                self.assertNotIn(reach, live_verify.check_expectations(
                    spec("bc.capture_full"), 0, done))


class ExpandingBc(FakeBc):
    """BC answering ``$expand=<line set>`` on a header listing with each header's lines
    nested in it. ``paged``: headers whose nested lines BC pages (a nested nextLink);
    ``foreign``: headers whose nested lines name another document."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.paged: set[str] = set()
        self.foreign: set[str] = set()

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        parts = urlsplit(url)
        expand = parse_qs(parts.query).get("$expand", [None])[0]
        found = re.search(r"/companies\(([^)]+)\)/(\w+)$", unquote(parts.path))
        if expand is None or found is None:
            return super().__call__(method, url, headers, body, timeout, max_bytes)
        key = found.groups()
        plain = self.data[key]
        self.data[key] = [self.nested(key, row, expand) for row in plain]
        try:
            return super().__call__(method, url, headers, body, timeout, max_bytes)
        finally:
            self.data[key] = plain

    def nested(self, key: tuple[str, str], row: dict, line_set: str) -> dict:
        lines = [dict(line) for line in self.lines.get((*key, row["id"]), [])]
        if row["id"] in self.foreign:
            parent = BC_LINE_PARENT_FIELD.get(line_set, "documentId")
            lines = [dict(line, **{parent: gid(9, 9, 9)}) for line in lines]
        extra = ({f"{line_set}@odata.nextLink": "https://example.invalid/next"}
                 if row["id"] in self.paged else {})
        return {**row, line_set: lines, **extra}


def rows(h: Harness, company: str, entity_set: str) -> list:
    return sorted((json.dumps(i["row"], sort_keys=True) for i in h.store.records(
        SERVICE, "bc", bc_unit(company, entity_set), limit=1000)["items"]))


class ExpandedLinesTests(unittest.TestCase):
    def test_a_header_page_of_n_headers_is_one_get_with_its_lines(self):
        """Every daily bc.capture_full stopped on budget (8 to 41 of 108
        units) spending one GET per document on its lines. N headers on one page are one
        GET with ``$expand``, not N + 1, and store exactly what the per-header read does."""
        sets = ("purchaseOrders", "vendors")
        for n in (1, 10, 60):
            with self.subTest(headers=n):
                fake = ExpandingBc({"purchaseOrders": n}, companies=CO)
                h = Harness(self, fake)
                report = h.run(entity_sets=sets, page_size=bc_read.CAPTURE_PAGE_SIZE)
                gets = fake.bc_gets(rf"{CO[0]}\)/purchaseOrders")
                self.assertEqual([unquote(urlsplit(u).query) for u in gets],
                                 ["$expand=purchaseOrderLines"])
                po = h.unit(report, CO[0], "purchaseOrders")
                lines = h.unit(report, CO[0], "purchaseOrderLines")
                self.assertEqual((po["state"], po["records_current"], po["lines_expanded"]),
                                 ("complete", n, n))
                self.assertEqual((lines["state"], lines["records_current"], lines["requests"]),
                                 ("complete", 2 * n, 0))
                self.assertTrue(report["all_complete"])
                plain = Harness(self, FakeBc({"purchaseOrders": n}, companies=CO))
                plain.run(entity_sets=sets, page_size=bc_read.CAPTURE_PAGE_SIZE)
                self.assertEqual(len(plain.fake.bc_gets(rf"{CO[0]}\)/purchaseOrders")), n + 1)
                for name in ("purchaseOrders", "purchaseOrderLines"):
                    self.assertEqual(rows(h, CO[0], name), rows(plain, CO[0], name), name)
                # A second pass re-reads unchanged documents: no new version of either.
                again = h.run(entity_sets=sets, page_size=bc_read.CAPTURE_PAGE_SIZE)
                self.assertEqual(sum(u["new_versions"] for c in again["companies"]
                                     for u in c["units"]), 0)
                self.assertTrue(again["all_complete"])

    def test_an_expansion_refused_or_not_whole_is_read_per_header(self):
        n = 6
        si = bc_read.CAPTURE_ENTITY_SETS.index("purchaseOrders")
        paged, foreign = gid(0, si, 1), gid(0, si, 4)
        fake = ExpandingBc({"purchaseOrders": n}, companies=CO)
        fake.paged.add(paged)
        fake.foreign.add(foreign)
        h = Harness(self, fake)
        report = h.run(entity_sets=("purchaseOrders",), page_size=3)
        lines = h.unit(report, CO[0], "purchaseOrderLines")
        self.assertEqual((lines["state"], lines["records_current"], lines["dropped"]),
                         ("complete", 2 * n, 0))
        self.assertEqual(h.unit(report, CO[0], "purchaseOrders")["lines_expanded"], n - 2)
        per_header = fake.bc_gets(r"purchaseOrders\([^)]+\)/purchaseOrderLines")
        self.assertEqual(sorted(re.search(r"purchaseOrders\(([^)]+)\)", unquote(u))[1]
                                for u in per_header if CO[0] in u), sorted([paged, foreign]))
        self.assertFalse(fake.bc_gets(r"example\.invalid"))

        # BC refuses the expansion: the unit is read again at once without it.
        fake = ExpandingBc({"purchaseOrders": n}, companies=CO)
        fake.fail(rf"{CO[0]}\)/purchaseOrders\?\$expand", BAD_REQUEST)
        h = Harness(self, fake)
        report = h.run(entity_sets=("purchaseOrders",), page_size=3)
        po = h.unit(report, CO[0], "purchaseOrders")
        self.assertEqual((po["state"], po["records_current"]), ("complete", n))
        self.assertNotIn("lines_expanded", po)
        self.assertEqual(h.unit(report, CO[0], "purchaseOrderLines")["state"], "complete")
        self.assertEqual(len(fake.bc_gets(rf"{CO[0]}\)/purchaseOrders\([^)]+\)/")), n)
        self.assertEqual(len(fake.bc_gets(rf"{CO[0]}\)/purchaseOrders\?\$expand")), 1)
        self.assertTrue(report["all_complete"])

    def test_a_reader_without_the_line_set_never_expands(self):
        """A reader granted the headers but not their lines sends no ``$expand``, and the
        store holds no line of the documents for anyone."""
        fake = ExpandingBc({"purchaseOrders": 3}, companies=CO)
        grants = {"bc:production:companies": ["staff:headers"],
                  **{f"bc:production:{c}:purchaseorders": ["staff:headers"] for c in CO}}
        audience = LiveSourceAudience(grants, tenant="plx", default_classification="restricted")
        actor = Actor("plx", frozenset({"staff:headers"}), "restricted")
        api, _transport, tenant, environment = bc_read.build_bc(ENV, sender=fake)
        reader = bc_read.BusinessCentralReader(api, audience, tenant_id=tenant,
                                               environment=environment)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        store = CaptureStore(Path(scratch.name) / "c.sqlite", audience)
        self.addCleanup(store.close)
        report = bc_read.BcCapture(reader, store, books=BOOKS).capture(
            actor, entity_sets=("purchaseOrders",))
        entry = report["companies"][0]["units"]
        self.assertEqual({u["entity_set"]: u["state"] for u in entry},
                         {"purchaseOrders": "complete", "purchaseOrderLines": "not_visible",
                          "companies": "not_visible"})
        self.assertFalse(fake.bc_gets(r"\$expand|Lines"))
        self.assertIsNone(store.run("bc", bc_unit(CO[0], "purchaseOrderLines")))
        sent = len(fake.calls)
        with self.assertRaises(NotVisible):
            reader.read_document_page(actor, CO[0], "purchaseOrders", None,
                                      query={"$expand": "purchaseOrderLines"})
        self.assertEqual(len(fake.calls), sent)  # refused before any request
        for bad in ({"$expand": "salesOrderLines"}, {}, {"$expand": "purchaseOrderLines,x"}):
            with self.subTest(query=bad), self.assertRaises(ValueError):
                Harness(self, fake).reader.read_document_page(SERVICE, CO[0], "purchaseOrders",
                                                             None, query=bad)


class StaleResumeTests(unittest.TestCase):
    def test_a_run_resumed_after_fresh_since_reports_its_earlier_pages_stale(self):
        """A unit paused in an earlier invocation and finished in this
        session was reported complete, so the call could be all_complete on pages read
        before the session began."""
        fake = FakeBc({"vendors": 9}, companies=CO)
        with tempfile.TemporaryDirectory() as scratch:
            db = Path(scratch) / "capture.sqlite"
            clock = Clock()

            def capture(budget=None, fresh_since=None):
                return bc_read.capture_probe(ENV, sender=Slow(fake, clock, 1.0), db=db,
                                             budget_seconds=budget, entity_sets=("vendors",),
                                             page_size=2, resolve_sample=0, books=BOOKS,
                                             monotonic=clock, mode="full",
                                             fresh_since=fresh_since)
            cut = capture(budget=4)
            self.assertEqual(cut["stopped_reason"], "budget")
            fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            resumed = capture(fresh_since=fresh)
            units = {(c["company_index"], u["entity_set"]): u for c in resumed["companies"]
                     for u in c["units"]}
            self.assertEqual((units[(0, "vendors")]["state"], units[(0, "vendors")]["stale"]),
                             ("complete", True))
            self.assertEqual(resumed["units_stale"],
                             sum(1 for u in units.values() if u["stale"]))
            self.assertEqual(resumed["units_complete"], resumed["units_total"])
            self.assertFalse(resumed["all_complete"])
            check = spec("bc.capture_full")
            self.assertEqual(live_verify.completeness(check, resumed),
                             {"complete": False, "incomplete": ["all_complete"]})
            # The session's next call reads the stale units again; nothing is stale then.
            again = capture(fresh_since=fresh)
            self.assertEqual((again["units_stale"], again["all_complete"]), (0, True))
            self.assertEqual(live_verify.completeness(check, again), {"complete": True})
            self.assertNotIn("units_reached != units_total",
                             live_verify.check_expectations(check, 0, again))
            self.assertEqual([u.get("reused") for c in again["companies"] for u in c["units"]
                              if u["entity_set"] == "vendors"], [None, True, True])
            # The projection probe's budget-stop record keeps the reach.
            block = projection.capture_bc({"environ": {**ENV, bc_read.FRESH_SINCE_ENV: fresh},
                                           "sender": fake, "books": BOOKS}, str(db), None)
            kept = projection.budget_partial({"bc_capture": block})["bc_capture"]
            self.assertEqual((kept["units_reached"], kept["units_total"]),
                             (block["units_total"], len(CO) * ALL_UNITS))


class UnservedEntityTests(unittest.TestCase):
    def test_a_company_that_does_not_serve_a_set_is_a_blocked_part_not_a_finance_error(self):
        """The payment journals of companies 1 and 2
        answered 400 with BC code internal_data_not_found_filter to a plain $top=1 read that
        company 0 serves. The request is not at fault, so the company does not serve the
        set: a blocked part (relation_unavailable) named by book and set, classified by the
        BC code at run time. Any other refusal stays a finance error."""
        ledgers = spec("bc.ledgers")
        shape = live_verify.check_expectations(ledgers, 0, run(FakeBc(companies=CO)))

        def bc_400(code: str) -> tuple:
            return (400, {}, json.dumps({"error": {"code": code}}).encode())
        book = BOOKS["production"]
        unserved = [(c, n) for c in CO[1:]
                    for n in ("customerPaymentJournals", "vendorPaymentJournals")]
        for label, code, listed, blocked, errors in (
                ("not served", "Internal_DataNotFoundFilter", [], 4, 0),
                ("expected beats blocked", "Internal_DataNotFoundFilter", True, 0, 0),
                # Our request or a permission, not the company's setup: an error.
                ("invalid filter", "Application_InvalidFilter", [], 0, 4),
                ("bad request", "BadRequest", [], 0, 4)):
            with self.subTest(label):
                fake = FakeBc(companies=CO)
                for company, name in unserved:
                    fake.fail(header(company, name), bc_400(code))
                token = "internal_data_not_found_filter"
                report = run(fake, expected_refusals=[
                    f"{book[c]}:{n}:{token}" for c, n in unserved] if listed else [])
                self.assertEqual(report["finance_errors"], errors)
                parts = live_verify.blocked_parts(dict(report))
                self.assertEqual([(p["part"], p["cause"]) for p in parts],
                                 [(f"{book[c]}:{n}", "relation_unavailable")
                                  for c, n in unserved][:blocked])
                self.assertEqual(sorted(live_verify.check_expectations(
                    ledgers, 0, report, ledgers["expect_blocked"]) if blocked else
                    live_verify.check_expectations(ledgers, 0, report)),
                    sorted(shape + (["finance_errors != expected"] if errors else [])))
        # Served by no company, a 401 and a transport failure are never "not served".
        fake = FakeBc(companies=CO)
        for company in CO:
            fake.fail(header(company, "customerPaymentJournals"),
                      bc_400("Internal_DataNotFoundFilter"))
        fake.fail(header(CO[1], "vendorPaymentJournals"),
                  (401, {}, b'{"error":{"code":"Authentication_InvalidCredentials"}}'))
        report = run(fake)
        self.assertEqual((report["finance_errors"], "blocked_upstream" in report), (4, False))


TEMPLATE_400 = (400, {}, json.dumps({"error": {
    "code": "Internal_DataNotFoundFilter",
    "message": "There is no Gen. Journal Template within the filter.  Filters: Type: Cash "
               "Receipts"}}).encode())
GAP_COMPANIES = {CO[1]: "consol", CO[2]: "distribution"}
LEDGER_SCOPE = {"production": {
    "required_book": "manufacturing",
    "required_sets": list(JOURNALS),
    "expected_gaps": [{"company": company, "label": label, "sets": list(JOURNALS),
                       "http_status": 400, "error_code": "internal_data_not_found_filter",
                       "message_class": "journal_template_missing", "owner": "Ricardo",
                       "cause": "payment_journal_templates_missing"}
                      for company, label in GAP_COMPANIES.items()]}}
MY_COMPANY = {"production": {CO[0]: "manufacturing"}}


def scoped(sender, **kwargs) -> dict:
    return run(sender, books=MY_COMPANY, ledger_scope=LEDGER_SCOPE, **kwargs)


def template_gaps(fake: FakeBc, companies=(CO[1], CO[2])) -> FakeBc:
    for company in companies:
        for name in JOURNALS:
            fake.fail(header(company, name), TEMPLATE_400)
    return fake


class MyCompanyRequiredTests(unittest.TestCase):
    """My Company is
    bc.ledgers' required book; its payment journals served with lines count toward the
    pass, and the two other companies' journal_template_missing 400 are named expected
    gaps (company, set, owner, cause), never errors and never served."""

    def test_my_company_journals_with_lines_pass_and_the_other_companies_are_named_gaps(self):
        report = scoped(template_gaps(FakeBc(companies=CO)))
        self.assertEqual((report["finance_errors"], report["finance_entity_errors"],
                          report["finance_required_unserved"]), (0, [], []))
        self.assertNotIn("blocked_upstream", report)
        self.assertEqual(report["finance_required"], [
            {"book": "manufacturing", "company_index": 0, "entity_set": name,
             "records_returned": 1, "lines_returned": 2} for name in JOURNALS])
        gaps = report["finance_expected_gaps"]
        self.assertEqual(len(gaps), 4)
        self.assertEqual({(g["company_index"], g["book"], g["entity_set"]) for g in gaps},
                         {(CO.index(c), label, n) for c, label in GAP_COMPANIES.items()
                          for n in JOURNALS})
        for gap in gaps:
            self.assertEqual((gap["reason"], gap["owner"], gap["cause"], gap["http_status"],
                              gap["error_code"], gap["message_class"]),
                             ("journal_template_missing", "Ricardo",
                              "payment_journal_templates_missing", 400,
                              "internal_data_not_found_filter", "journal_template_missing"))
        # Not served, not an error, not a refusal to chase.
        served = [e for c in report["finance_entities"] for e in c["entities"]
                  if "records_returned" in e]
        self.assertEqual(report["finance_served"], len(served))
        self.assertEqual(report["finance_served"], len(bc_read.FINANCE_ENTITY_SETS) + 2 * (
            len(bc_read.FINANCE_ENTITY_SETS) - 2))
        self.assertEqual(report["finance_refused"], [])
        # On the real spec: only the fake-row field checks fail, never a count or a gap.
        failures = live_verify.check_expectations(spec("bc.ledgers"), 0, report)
        self.assertTrue(all("_absent not empty" in f for f in failures), failures)
        recorded = live_verify._recorded(spec("bc.ledgers"), report)
        self.assertEqual(recorded["finance_expected_gaps[*].owner"], ["Ricardo"] * 4)

    def test_a_refusal_in_my_company_still_fails_even_when_it_is_the_unserved_code(self):
        for label, response in (("template 400", TEMPLATE_400), ("denied", DENIED)):
            with self.subTest(label):
                fake = template_gaps(FakeBc(companies=CO))
                fake.fail(header(CO[0], JOURNALS[0]), response)
                report = scoped(fake)
                self.assertEqual([(e["company_index"], e["entity_set"], e["part"])
                                  for e in report["finance_entity_errors"]],
                                 [(0, JOURNALS[0], "header")])
                self.assertEqual(report["finance_errors"], 1)
                self.assertNotIn("blocked_upstream", report)
                self.assertEqual([r["entity_set"] for r in report["finance_required_unserved"]],
                                 [JOURNALS[0]])
                self.assertIn("finance_errors != expected",
                              live_verify.check_expectations(spec("bc.ledgers"), 0, report))
        # When another company served the set, My Company's template refusal is still no
        # "unserved company": an error.
        fake = template_gaps(FakeBc(companies=CO), companies=(CO[2],))
        fake.fail(header(CO[0], JOURNALS[0]), TEMPLATE_400)
        report = scoped(fake)
        self.assertEqual(report["finance_errors"], 1)
        self.assertNotIn("blocked_upstream", report)

    def test_a_gap_is_only_the_listed_company_set_code_and_class(self):
        other_400 = (400, {}, b'{"error":{"code":"BadRequest","message":"x"}}')
        no_template = (400, {}, json.dumps({"error": {
            "code": "Internal_DataNotFoundFilter", "message": "There is no Item."}}).encode())
        for label, company, name, response, errors in (
                ("other code", CO[1], JOURNALS[0], other_400, 1),
                ("other class", CO[1], JOURNALS[1], no_template, 1),
                ("denied", CO[2], JOURNALS[0], DENIED, 1),
                ("a set the gap does not list", CO[1], "itemLedgerEntries", TEMPLATE_400, 1)):
            with self.subTest(label):
                fake = template_gaps(FakeBc(companies=CO))
                fake.script = [(p, r) for p, r in fake.script
                               if not p.search(f"{company})/{name}?$top=1")]
                fake.fail(header(company, name), response)
                report = scoped(fake)
                self.assertNotIn((CO.index(company), name),
                                 {(g["company_index"], g["entity_set"])
                                  for g in report["finance_expected_gaps"]})
                self.assertEqual(report["finance_errors"] + len(report.get(
                    "blocked_upstream", [])), errors)
        # An unlisted company with the same 400 is not excused by the other companies' gaps.
        fake = FakeBc(companies=[*CO, UNPINNED_CO])
        template_gaps(fake, (CO[1], CO[2], UNPINNED_CO))
        report = scoped(fake)
        self.assertEqual(len(report["finance_expected_gaps"]), 4)
        self.assertEqual(len(report["blocked_upstream"]), 2)

    def test_my_company_missing_empty_or_without_lines_is_named_unserved(self):
        # My Company absent from the listing: required, so a named unserved row, not a pass.
        fake = template_gaps(FakeBc(companies=CO[1:]), companies=(CO[1], CO[2]))
        report = scoped(fake)
        self.assertEqual({r["reason"] for r in report["finance_required_unserved"]},
                         {"company_not_listed"})
        self.assertIn("finance_required_unserved not empty",
                      live_verify.check_expectations(spec("bc.ledgers"), 0, report))
        # A journal with no lines is not "served with lines".
        fake = template_gaps(FakeBc({"customerPaymentJournals": 0}, companies=CO))
        report = scoped(fake)
        self.assertEqual([(r["entity_set"], r["reason"])
                          for r in report["finance_required_unserved"]],
                         [("customerPaymentJournals", "no_journal_to_read_lines")])


UNPINNED_CO = "c0000000-0000-4000-8000-0000000000fe"


if __name__ == "__main__":
    unittest.main()
