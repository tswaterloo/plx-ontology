"""Order-margin eval (family F10): ``plx.investigate`` against the independent oracle, BC only.

    python3 -m scripts.eval_order_margin fixture
    python3 -m scripts.eval_order_margin live --db DIR/capture.sqlite --work-dir DIR/product \
        [--run-id auto] [--count 12] [--min-scored 3] [--max-wrong-rate 0.10] \
        [--total-budget-seconds 2400]

Both modes read Business Central only, so they run while the Portal reporting API is down.

* ``live``: one full GET-only capture of every BC entity set the ontology binds
  (``integrations.bc_read.CAPTURE_ENTITY_SETS``: the sales sets the oracle reads, among them
  ``salesCreditMemos`` and their lines and ``customers``, and the rest, so that a complete
  capture proves the BC family whole and the product drops ``credit_memos_not_captured``) of
  every pinned company into ``--db`` (``integrations.bc_read.capture_probe``; credentials from
  the environment, never printed; in a verifier session its ``--db`` is the session's store,
  topped up: the units the session completed are not read again, ``capture.units_reused``;
  any other unit is read again, ``capture.capture_age_seconds`` is the oldest complete unit's
  age), projected alone
  (``ontology.project_bc.BcCaptures``) into a throwaway store under ``--work-dir``. Evidence
  label ``live-read``. The oracle takes a read as whole per company and entity set, from
  that unit of the capture report (``unit_completeness``), so an incomplete unit of one
  company never blocks another's orders; the adapter is
  ``live-unverified`` until the verifier records a pass of ``verify/live-checks/
  eval.order_margin.json``.
* ``fixture``: the synthetic world of ``evals.oracle.order_margin.fixture_world`` (not PLX
  data) as in-memory BC captures. Evidence label ``isolated-fixture``.

Per run: the order numbers that some invoice of the company names (``orderNumber``) are
drawn by a seed (the run id), whether BC still serves the order or deleted it once shipped and
invoiced (``unserved``: no ``salesOrders`` record; its root is the order the posted documents
stand for). The product's root is the one ``SalesOrder`` object the order's records (its
``salesOrders`` record, invoices and shipments) cite: none is ``not_held``, several is
``duplicated`` (one order, two identities: a fail). Per item the oracle (``evals.oracle.order_margin.reading``: raw
captured records only, it never imports ``ontology/``) and the product (``plx.investigate``
family F10 from the order's object, through the case host's own tool table
(``operating.ontology_host.tool_table``) with a per-reader citation resolver over the
captures, so the eval scores the tools an employee gets: the claim gate, the reader's
``read_scope`` and the per-company capture claims the store carries
(``capture_claims``; without a claim the family-level proof stands and
``credit_memos_not_captured`` keeps the gap)) answer revenue, booked cost and margin per
order line and for the order.Per figure: ``agree`` (both decided, equal Decimals), ``wrong`` (decided
differently, or the product states a figure the oracle leaves undecided: a margin over missing
cost is never right; or any figure without the product gap matching an oracle caveat
(``CAVEAT_GAPS``: credit memos not read completely, a credit memo of the customer applied to
no invoice), which would overstate what is known), ``abstain`` (the oracle decided, the
product did not). An item is ``wrong`` with any wrong figure, else ``abstain_ok`` with any
abstention, else ``correct``; ``excluded`` when the oracle decides nothing or cannot read the
order, ``not_held`` when the store holds no root, ``product_error`` when ``plx.investigate``
errs (never an abstention), ``duplicated`` as above. ``coverage`` (scored / sampled) stands beside ``accuracy``
(correct / scored) and ``answered`` (oracle-decided figures the product decided). Items are partitioned dev / held_out by
``evals.oracle.order_margin.partition`` (related documents and near-duplicate order numbers
in one partition); ``partition_violations`` must be 0.

Pass: the run finished in its budget, ``scored >= --min-scored``, ``wrong <= floor(rate x
scored)``, at least one ``correct``, no ``product_error`` or ``duplicated``, no partition
violation and no leak
(``row_data_check``). Output: counts, rates and enums only; never a value, number, id or
citation. Exit 0 pass, 3 fail, 1 error.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

if __package__ in (None, ""):  # ``python3 scripts/eval_order_margin.py`` as well as ``-m``
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.oracle import order_margin as oracle  # noqa: E402
from evals.oracle.source import BcReads, decimal_of  # noqa: E402

SCHEMA = "plx-eval-order-margin/v1"
TENANT = "plx"
PRINCIPAL = "eval:order-margin"
SETS = oracle.FIXTURE_SETS
LINE_PARENT = {"salesOrderLines": "salesOrders", "salesInvoiceLines": "salesInvoices",
               "salesCreditMemoLines": "salesCreditMemos"}
OUTCOMES = ("correct", "wrong", "abstain_ok")
# Oracle caveat -> the product gap reason (operating/order_margin.py) that must carry it.
CAVEAT_GAPS = {oracle.CREDIT_CAVEAT: "credit_memos_not_captured",
               oracle.UNATTRIBUTED_CAVEAT: "credit_memo_unattributed"}
DEFAULT_COUNT = 12
DEFAULT_MIN_SCORED = 3
DEFAULT_MAX_WRONG_RATE = 0.10
DEFAULT_BUDGET = 2400.0
CAPTURE_SHARE = 0.75
FIXTURE_COMPANIES = ("f1000000-0000-4000-8000-000000000001",
                     "f1000000-0000-4000-8000-000000000002")
FIXTURE_BOOKS = dict(zip(FIXTURE_COMPANIES, ("manufacturing", "distribution")))


# --------------------------------------------------------------------------- captures

class MemoryCaptures:
    """``bc`` L1 captures (``ontology.project.CaptureSource``) of in-memory raw BC records:
    one current version each, lineage ``bc_<book>``, every set a complete traversal."""

    source = "bc"

    def __init__(self, records: Sequence[Mapping[str, Any]], books: Mapping[str, str]) -> None:
        from ontology.project import CapturedRecord

        self.books = {k.lower(): v for k, v in books.items()}
        self.rows: dict[str, list[Any]] = {}
        self.views: dict[str, dict[str, Any]] = {}
        for record in records:
            company = record["company_id"].lower()
            book = self.books.get(company)
            unit = f"bc:{company}/{record['entity_set']}"
            self.views[record["citation"]] = {"citation": record["citation"],
                                              "row": dict(record["fields"]),
                                              "lineage": f"bc_{book}" if book else None}
            self.rows.setdefault(record["entity_set"], []).append(CapturedRecord(
                source="bc", relation=record["entity_set"],
                record_key=f"{company}/{record['id']}", row=dict(record["fields"]),
                l1_handle=record["citation"], field_prefix="row",
                digest=hashlib.sha256(json.dumps(record["fields"], sort_keys=True)
                                      .encode()).hexdigest(),
                recorded_at="2026-01-01T00:00:00.000000Z", superseded_at=None,
                last_seen_at="2026-01-01T00:00:00.000000Z", audience_key=unit,
                capture_lineage=f"bc_{book}" if book else None))

    def records(self, relations: Sequence[str]) -> dict[str, list[Any]]:
        return {r: list(self.rows.get(r, ())) for r in relations}

    def describe(self) -> dict[str, Any]:
        """Freshness facts as ``ontology.project_bc.BcCaptures.describe`` states them: every
        set is a complete traversal of every company of the fixture world, so each carries
        that company's complete claim (``scopes``), the unit-level proof the host reads
        (``operating.ontology_tools_investigate.capture_claims``)."""
        at = "2026-01-01T00:00:00Z"
        scopes = [{"scope": f"company={c}", "company_id": c, "complete": True, "read_at": at}
                  for c in sorted(self.books)]
        return {"generation": "bc-fixture", "relations": {
            r: {"last_refresh_at": at, "last_capture_id": None, "last_full_traversal_at": at,
                "scopes": list(scopes)} for r in sorted(self.rows)}}

    def resolve(self, handle: str, actor: Any) -> dict[str, Any] | None:
        return self.views.get(handle)


def fixture_records() -> list[dict[str, Any]]:
    out = []
    for record in oracle.fixture_world(FIXTURE_COMPANIES):
        record = dict(record)
        record["citation"] = (f"bc://fixture/{record['company_id']}/{record['entity_set']}/"
                              f"{record['id']}@0000000000000000-1")
        out.append(record)
    return out


def store_records(store: Any, actor: Any, db: Path) -> list[dict[str, Any]]:
    """Every current captured BC record of the sales sets, as raw oracle records (a unit
    the store refuses raises: the oracle never reads a partial set as whole)."""
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        units = [u for (u,) in conn.execute("SELECT DISTINCT unit FROM versions WHERE kind = 'bc'")]
    wanted = set(SETS) | set(LINE_PARENT)
    out: list[dict[str, Any]] = []
    for unit in sorted(units):
        company, _, entity_set = unit.partition(":")[2].partition("/")
        if entity_set not in wanted:
            continue
        after = None
        while True:
            page = store.records(actor, "bc", unit, after_path=after, limit=1000)
            for item in page["items"]:
                fields = dict(item["row"])
                record = {"company_id": company, "entity_set": entity_set,
                          "id": str(fields.get("id")), "fields": fields,
                          "citation": item["citation"]}
                if entity_set in LINE_PARENT:
                    record.update(document_set=LINE_PARENT[entity_set],
                                  document_id=fields.get("documentId"))
                out.append(record)
            after = page.get("next_after_path")
            if not after:
                break
    return out


# --------------------------------------------------------------------------- product

class Product:
    """The ontology store of the captures and the case host's tool table over it."""

    def __init__(self, source: Any, work_dir: Path,
                 rows: Callable[[str, Any], Mapping[str, Any] | None]) -> None:
        from ontology import identifiers, store_ddl
        from ontology.project import build_store
        from ontology.schema import load_schema
        from ontology.types import Actor
        from operating import ontology_tools as ot
        from operating.ontology_host import tool_table

        work_dir.mkdir(parents=True, exist_ok=True)
        self.path = work_dir / "ontology.sqlite"
        if self.path.exists():
            self.path.unlink()
        build_store(self.path, load_schema(), {"bc": source}, normalizer=identifiers.normalize)
        self.reader = ot.StoreReader.open(self.path)
        policy = ot.ReadPolicy(tenant=TENANT, audiences={"bc:*": {PRINCIPAL}},
                               markings={m: {PRINCIPAL} for m in store_ddl.MARKINGS})
        base = ot.OntologyTools(self.reader, policy, schema=ot.default_schema(),
                                grammar=ot.default_grammar())
        # The host's own table (``operating/ontology_host.tool_table``), so the eval scores
        # the tools an employee gets: the claim gate over ``rows`` as this reader's citation
        # resolver, ``read_scope`` and the per-company capture claims of ``capture_claims``.
        self.table = tool_table(base, SimpleNamespace(resolve_citation=rows, claim_row=rows))
        self.actor = Actor(TENANT, frozenset({PRINCIPAL}))

    def close(self) -> None:
        self.reader.conn.close()

    def roots(self, citations: Sequence[str]) -> list[str]:
        """The ``SalesOrder`` objects these records are cited by (one order: exactly one)."""
        with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as conn:
            return [row[0] for row in conn.execute(
                "SELECT DISTINCT object_id FROM object_versions WHERE l1_handle IN "
                "(SELECT value FROM json_each(?)) AND object_id LIKE 'SalesOrder:%' "
                "ORDER BY object_id", (json.dumps(list(citations)),))]

    def answer(self, object_id: str) -> dict[str, Any]:
        """The product's figures over every page of its answer."""
        args: dict[str, Any] = {"family": oracle.FAMILY, "root": object_id}
        whole: dict[str, list[Any]] = {"answer": [], "gaps": []}
        while True:
            result = self.table["plx.investigate"]["handler"](args, self.actor)
            if "error" in result:
                return {"error": str(result["error"].get("code"))}
            for key in whole:
                whole[key] += result.get(key) or []
            cursor = (result.get("completeness") or {}).get("next_cursor")
            if not cursor:
                return figures(whole)
            args = dict(args, cursor=cursor)


def figures(result: Mapping[str, Any]) -> dict[str, Any]:
    """Per order line (BC line id) and for the order: the product's decided figures."""
    lines: dict[str, dict[str, Any]] = {}
    total: dict[str, Any] = {}
    for entry in result.get("answer", ()):
        if entry.get("kind") != "computed":
            continue
        name = entry["name"]
        figure = next((f for f in oracle.FIGURES if name.endswith("_" + f)), None)
        if "_line_" in name and figure:
            key = json.loads(entry["basis"][0].split(":", 2)[2]).get("id")
            lines.setdefault(str(key), {})[figure] = entry["value"]
        elif name == "order_margin":
            total["margin"] = entry["value"]
        elif figure and name == f"order_margin_{figure}":
            total[figure] = entry["value"]
    reasons = {g.get("reason") for g in result.get("gaps") or ()}
    return {"lines": lines, "total": total,
            "caveats": sorted(c for c, gap in CAVEAT_GAPS.items() if gap in reasons)}


# --------------------------------------------------------------------------- scoring

def score(reading: Mapping[str, Any], answer: Mapping[str, Any]) -> dict[str, Any]:
    """Figure by figure (module docstring): ``outcome`` and the agree / wrong / abstain
    counts. A product figure the oracle has no line for is checked as undecided there."""
    counts = {"agree": 0, "wrong": 0, "abstain": 0, "oracle_decided": 0, "uncaveated": 0}

    def compare(expected: Any, got: Any) -> None:
        if expected is not None:
            counts["oracle_decided"] += 1
        if got is None:
            counts["abstain"] += expected is not None
        elif expected is None or decimal_of(got) != decimal_of(expected):
            counts["wrong"] += 1
        else:
            counts["agree"] += 1

    product_lines = answer.get("lines") or {}
    for line_id in set(reading["lines"]) | set(product_lines):
        for figure in oracle.FIGURES:
            compare((reading["lines"].get(line_id) or {}).get(figure),
                    (product_lines.get(line_id) or {}).get(figure))
    for figure in oracle.FIGURES:
        compare(reading["total"].get(figure), (answer.get("total") or {}).get(figure))
    if (set(reading.get("caveats") or ()) - set(answer.get("caveats") or ())
            and counts["agree"] + counts["wrong"]):
        counts["uncaveated"] = 1  # a figure stated without a caveat the oracle holds
    if counts["wrong"] or counts["uncaveated"]:
        outcome = "wrong"
    elif not counts["oracle_decided"]:
        outcome = "excluded"
    elif counts["abstain"]:
        outcome = "abstain_ok"
    else:
        outcome = "correct"
    return {"outcome": outcome, **counts}


def _rate(part: int, whole: int) -> str:
    return f"{part}/{whole}"


def eval_block(rows: Sequence[Mapping[str, Any]], *, complete: bool, min_scored: int,
               max_wrong_rate: float, violations: int) -> dict[str, Any]:
    by = {name: sum(1 for r in rows if r["outcome"] == name)
          for name in (*OUTCOMES, "excluded", "not_held", "product_error", "duplicated")}
    scored = sum(by[o] for o in OUTCOMES)
    wrong_max = math.floor(max_wrong_rate * scored)
    decided = sum(r.get("oracle_decided", 0) for r in rows if r["outcome"] in OUTCOMES)
    answered = sum(r.get("agree", 0) + r.get("wrong", 0) for r in rows
                   if r["outcome"] in OUTCOMES)
    reasons = []
    if not complete:
        reasons.append("incomplete")
    if scored < min_scored:
        reasons.append("below_min_scored")
    if by["wrong"] > wrong_max:
        reasons.append("wrong_above_bound")
    if not by["correct"]:
        reasons.append("no_correct_answer")
    if by["product_error"]:
        reasons.append("product_error")
    if by["duplicated"]:
        reasons.append("duplicated_identity")
    if violations:
        reasons.append("partition_violation")
    partitions = {p: sum(1 for r in rows if r.get("partition") == p) for p in oracle.PARTITIONS}
    # Per oracle caveat: how many answered items carried its product gap (a count, no value).
    caveated = {gap: sum(1 for r in rows if caveat in (r.get("caveats") or ()))
                for caveat, gap in CAVEAT_GAPS.items()}
    return {"sampled": len(rows), **by, "unserved": sum(1 for r in rows if r.get("unserved")),
            "scored": scored, "coverage": _rate(scored, len(rows)),
            "accuracy": _rate(by["correct"], scored), "answered": _rate(answered, decided),
            "wrong_max": wrong_max, "wrong_within_bound": by["wrong"] <= wrong_max,
            "by_partition": partitions, "product_caveats": caveated,
            "partition_violations": violations,
            "complete": complete, "fail_reasons": reasons, "pass": not reasons}


# --------------------------------------------------------------------------- run

def _named(record: Mapping[str, Any]) -> str:
    """The order number a sales order, invoice or shipment record names (else blank)."""
    field = "number" if record["entity_set"] == "salesOrders" else "orderNumber"
    return str(record["fields"].get(field) or "").strip()


def sample(records: Sequence[Mapping[str, Any]], *, run_id: str, count: int
           ) -> list[dict[str, Any]]:
    """Seeded (company, order number) pairs an invoice of the company names, with the
    citations of the order's records (its ``salesOrders`` record, invoices and shipments)
    and whether BC still serves it."""
    pool = sorted({(r["company_id"].lower(), _named(r)) for r in records
                   if r["entity_set"] == "salesInvoices" and _named(r)})
    random.Random(hashlib.sha256(run_id.encode()).hexdigest()).shuffle(pool)
    out = []
    for company, number in pool[:count]:
        mine = [r for r in records if r["company_id"].lower() == company and r["entity_set"]
                in ("salesOrders", "salesInvoices", "salesShipments") and _named(r) == number]
        out.append({"company_id": company, "number": number,
                    "citations": [r["citation"] for r in mine],
                    "unserved": not any(r["entity_set"] == "salesOrders" for r in mine)})
    return out


def unit_completeness(capture: Mapping[str, Any], books: Mapping[str, str]
                      ) -> dict[str, dict[str, bool]]:
    """Per pinned company (GUID, lower): each set the oracle reads -> whether the capture
    report (``companies[*].units[*]``) read that company's unit whole. The report names a
    company by its book only, so a company whose book is not held exactly once (missing,
    or pinned or listed twice) and a unit never reached are incomplete."""
    from integrations.bc_read import capture_unit

    companies = capture.get("companies") or []
    pinned = list(books.values())
    out: dict[str, dict[str, bool]] = {}
    for company_id, book in books.items():
        held = [c for c in companies if c.get("book") == book]
        out[company_id.lower()] = {} if len(held) != 1 or pinned.count(book) != 1 else {
            name: (capture_unit(held[0], name) or {}).get("complete") is True
            for name in (*SETS, *LINE_PARENT)}
    return out


def run_items(records: Sequence[Mapping[str, Any]], product: Product, books: Mapping[str, str],
              *, run_id: str, count: int, complete: bool | Mapping[str, Mapping[str, bool]],
              min_scored: int,
              max_wrong_rate: float, deadline: Callable[[], bool] = lambda: False
              ) -> dict[str, Any]:
    roots = sample(records, run_id=run_id, count=count)
    books = {k.lower(): v for k, v in books.items()}
    rows: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    finished = True
    for root in roots:
        if deadline():
            finished = False
            break
        company, number = root["company_id"], root["number"]
        # One company's read is whole where its own units are, whatever another's are.
        port = oracle.RecordPort(records, company, complete=complete if isinstance(
            complete, bool) else complete.get(company, {}))
        reading = oracle.reading(BcReads(port), number, book=books.get(company))
        items.append({"number": number, "sources": reading.get("sources") or []})
        if "blocked" in reading:
            rows.append({"outcome": "excluded", "unserved": root["unserved"]})
            continue
        object_ids = product.roots(root["citations"])
        if len(object_ids) != 1:
            rows.append({"outcome": "duplicated" if object_ids else "not_held",
                         "unserved": root["unserved"]})
            continue
        answer = product.answer(object_ids[0])
        rows.append(dict({"outcome": "product_error"} if "error" in answer
                         else dict(score(reading, answer), caveats=answer["caveats"]),
                         unserved=root["unserved"]))
    partitions = oracle.partition(items)
    for row, part in zip(rows, partitions):
        row["partition"] = part
    block = eval_block(rows, complete=finished, min_scored=min_scored,
                       max_wrong_rate=max_wrong_rate,
                       violations=oracle.violations(items, partitions))
    digest = hashlib.sha256("\x1f".join(sorted(
        hashlib.sha256(f"{r['company_id']}/{r['number']}".encode()).hexdigest()[:16]
        for r in roots)).encode()).hexdigest()[:16]
    return {"schema": SCHEMA, "run_id": run_id, "items_digest": digest, "eval": block,
            "values_published": False}


def _leak_values(records: Sequence[Mapping[str, Any]]) -> list[str]:
    values = []
    for record in records:
        values += [record["id"], record["citation"]]
        values += [str(v) for k, v in record["fields"].items()
                   if k in ("number", "orderNumber", "documentNumber", "itemNumber",
                            "lineObjectNumber")]
    return values


def fixture(args: argparse.Namespace) -> dict[str, Any]:
    records = fixture_records()
    source = MemoryCaptures(records, FIXTURE_BOOKS)
    with tempfile.TemporaryDirectory(prefix="plx-margin-") as scratch:
        product = Product(source, Path(scratch), source.resolve)
        try:
            report = run_items(records, product, FIXTURE_BOOKS, run_id=args.run_id,
                               count=args.count, complete=True, min_scored=args.min_scored,
                               max_wrong_rate=args.max_wrong_rate)
        finally:
            product.close()
    report.update(evidence_label="isolated-fixture", adapter_status="fixture")
    return report


def live(args: argparse.Namespace, environ: Mapping[str, str], sender: Any = None,
         books: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, Any]:
    from integrations import bc_read
    from integrations.capture_store import CaptureStore
    from integrations.live_source_guard import LiveSourceAudience
    from ontology.project_bc import BcCaptures
    from ontology.types import Actor
    from scripts.eval_unfamiliar_sample import row_data_check

    started = time.monotonic()
    budget = float(args.total_budget_seconds)
    # In a verifier session --db is its store (filled by bc.capture_full): the units the
    # session completed are not read again. A store from an earlier invocation is re-read.
    capture = bc_read.capture_probe(environ, sender=sender, db=args.db,
                                    budget_seconds=budget * CAPTURE_SHARE,
                                    entity_sets=bc_read.CAPTURE_ENTITY_SETS,
                                    mode="full", passes=1, resolve_sample=0, books=books,
                                    fresh_since=bc_read.session_fresh_since(environ))
    _, transport, tenant_id, environment = bc_read.build_bc(environ, sender=sender)
    tenant = f"bc-tenant:{tenant_id.lower()}"
    audience = LiveSourceAudience({f"bc:{environment.lower()}:*": ["capture:operator"]},
                                  tenant=tenant)
    operator = Actor(tenant, frozenset({"capture:operator"}), "restricted")
    store = CaptureStore(Path(args.db), audience)
    pinned = (books or bc_read.load_company_books()).get(environment.lower(), {})
    try:
        records = store_records(store, operator, Path(args.db))

        def rows(handle: str, actor: Any) -> Mapping[str, Any] | None:
            result = store.resolve(handle, operator)
            return result if result.get("ok") else None
        with BcCaptures(args.db) as source:
            product = Product(source, Path(args.work_dir), rows)
        try:
            report = run_items(records, product, pinned, run_id=args.run_id, count=args.count,
                               complete=unit_completeness(capture, pinned),
                               min_scored=args.min_scored, max_wrong_rate=args.max_wrong_rate,
                               deadline=lambda: time.monotonic() - started > budget)
        finally:
            product.close()
    finally:
        store.close()
    report.update(evidence_label="injected-transport" if sender else "live-read",
                  adapter_status="live-unverified",
                  environment_is_production=capture["environment_is_production"],
                  capture={"all_complete": capture["all_complete"],
                           "topped_up": capture["units_reused"] > 0,
                           "units_reused": capture["units_reused"],
                           "capture_age_seconds": capture["capture_age_seconds"],
                           "units_total": capture["units_total"],
                           "units_reached": capture["units_reached"],
                           "units_complete": capture["units_complete"],
                           "stopped_reason": capture["stopped_reason"]})
    text = json.dumps(report, sort_keys=True)
    report["row_data_check"] = row_data_check(text, _leak_values(records))
    if report["row_data_check"]["leaked"]:
        report["eval"]["fail_reasons"].append("leak")
        report["eval"]["pass"] = False
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None, books: Mapping[str, Mapping[str, str]] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_order_margin",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("fixture", "live"):
        sub = commands.add_parser(name)
        sub.add_argument("--run-id", default="fixture" if name == "fixture" else "auto")
        sub.add_argument("--count", type=int, default=DEFAULT_COUNT)
        sub.add_argument("--min-scored", type=int, default=DEFAULT_MIN_SCORED)
        sub.add_argument("--max-wrong-rate", type=float, default=DEFAULT_MAX_WRONG_RATE)
        if name == "live":
            sub.add_argument("--db", type=Path, required=True, help="BC capture store file")
            sub.add_argument("--work-dir", type=Path, required=True,
                             help="throwaway directory for the product's ontology store")
            sub.add_argument("--total-budget-seconds", type=float, default=DEFAULT_BUDGET)
    args = parser.parse_args(argv)
    if args.min_scored > args.count:
        parser.error("--min-scored exceeds --count: the check could never pass")
    if args.run_id == "auto":
        args.run_id = "live-" + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    try:
        report = fixture(args) if args.command == "fixture" else live(
            args, os.environ if environ is None else environ, sender, books)
    except Exception as exc:  # noqa: BLE001 - a typed error line, never a value
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report["eval"]["pass"] else 3


__all__ = ["MemoryCaptures", "Product", "eval_block", "figures", "main", "run_items", "sample",
           "score"]


if __name__ == "__main__":
    raise SystemExit(main())
