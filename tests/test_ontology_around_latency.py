"""plx.around on a high-degree object: a bounded number of store queries per call,
and the same objects, links and citations as the base code path. An empty-hop
plx.around and plx.timeline do the same work (statements and SQLite VM steps) on a store
ten times larger, read the store-wide slice facts once per store generation, and answer
byte for byte as the code before they derived absence from those facts did. So do a
one-hop plx.around whose page cites values (the cited values and the corroborating
records are read by L1 handle, through store v7's handle-keyed version index): on the
live store such calls scanned every value, past the 10 s call budget.

The store is the synthetic fixture of tests/test_ontology_tools_traverse.py plus one
party with thousands of orders, one order with thousands of shipments and thousands of
corroborated mail mentions (some finance-marked, some superseded). Queries are counted
with the connection's trace callback. The base result comes from the traverse module at
the commit before this change (``git show``), run over the same store and policy.
All identifiers are synthetic.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from operating import ontology_tools as ot
from operating import ontology_tools_traverse as tt
from tests import test_ontology_tools_traverse as fx

ROOT = Path(__file__).resolve().parents[1]
BASE_SHA = "6887afa79c437a46df3c00791b7d562025d7998c"
# The traverse module before absence and the event-type maps came from cached store facts.
FACTS_BASE_SHA = "a6d76e03cb965bfb2770a86cbfe90b304e585076"
LIMIT = 50
DEGREE = 1000          # links per link type on the high-degree objects
CALLS = (
    {"from": fx.P1, "path": [{"link_type": "Order.placed_by", "direction": "reverse"}]},
    {"from": fx.O1, "path": [{"link_type": "Shipment.ships_order", "direction": "reverse"}]},
    {"from": fx.O1, "path": [{"link_type": "MailMessage.mentions_order",
                              "direction": "reverse"}]},
    {"from": fx.P1, "path": [{"link_type": "Order.placed_by", "direction": "reverse"},
                             {"link_type": "Shipment.ships_order", "direction": "reverse"}]},
    {"from": fx.O1, "path": [{"link_type": "Order.placed_by", "direction": "forward"}]},
)
ACTORS = (fx.FULL, fx.OPS)


def high_degree(n):
    """``n`` links each of three link types on P1 / O1: tiers vary, every seventh order
    is finance-marked (hidden from OPS), every eleventh shipment link is superseded, and
    each mail mention cites the mail subject and P1's record as corroboration."""
    def extra(s):
        p1 = s.handles[fx.P1]
        for i in range(n):
            order = f"Order:portal.order:h-{i:05d}"
            s.obj(order, "api:public.order", fx.api("order", f"H{i}"), "fm_bronze",
                  markings='["finance"]' if i % 7 == 0 else "[]",
                  display=("number", "identifier", f"H{i}"))
            s.link(f"l-h-{i:05d}", "Order.placed_by", order, fx.P1,
                   "verified" if i % 3 else "stored_by_text_join")
            ship = f"Shipment:portal.shipment:hs-{i:05d}"
            s.obj(ship, "api:public.shipment", fx.api("shipment", f"hs{i}"), "fm_bronze",
                  display=("number", "identifier", f"HS{i}"))
            s.link(f"l-hs-{i:05d}", "Shipment.ships_order", ship, fx.O1,
                   superseded="2099-01-15T00:00:00Z" if i % 11 == 0 else None)
            if i < 40:
                s.link(f"l-hso-{i:05d}", "Shipment.ships_order", ship, order)
            mail = f"MailMessage:mail.inbox:hm-{i:05d}"
            handle = f"graph-mail://mbx-1/hm-{i}@ck1"
            s.obj(mail, "mail:inbox@example.test", handle, "mail",
                  display=("subject", "text", f"About ORD-1 #{i}"))
            reason = [{"feature": "identifier_mention", "agrees": True,
                       "mention_group": f"g-{i % 50}",
                       "citations": [{"l1_handle": handle, "field_path": "row.subject",
                                      "digest": fx._h(handle), "lineage": "mail"}]},
                      {"feature": "party_domain", "agrees": True,
                       "citations": [{"l1_handle": p1, "field_path": "row.name",
                                      "digest": fx._h(p1), "lineage": "fm_bronze"}]}]
            s.conn.execute(
                "INSERT INTO links(link_id, link_type, from_object, to_object, tier, "
                "binding_kind, reason, why_not, evidence_handles, markings, recorded_at, "
                "superseded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"l-hm-{i:05d}", "MailMessage.mentions_order", mail, fx.O1,
                 "corroborated", "mention", json.dumps(reason), "[]",
                 json.dumps([handle, s.handles[fx.O1]]), "[]", fx.T0, None))
    return fx.build(extra=extra)


def base_module(sha=BASE_SHA):
    """The traverse module as of ``sha``, imported under its own name."""
    try:
        source = subprocess.run(
            ["git", "show", f"{sha}:operating/ontology_tools_traverse.py"], cwd=ROOT,
            check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise unittest.SkipTest(f"base commit {sha} not readable: {exc}") from exc
    name = f"_base_ontology_tools_traverse_{sha[:12]}"
    module = sys.modules.setdefault(name, types.ModuleType(name))
    if not hasattr(module, "TraverseTools"):
        exec(compile(source, f"{sha[:12]}:operating/ontology_tools_traverse.py",
                     "exec"), module.__dict__)
    return module


def comparable(result):
    """A result without its per-call set handle and cursor."""
    out = copy.deepcopy(result)
    out.pop("set", None)
    out.get("completeness", {}).pop("next_cursor", None)
    return out


class Counter:
    def __init__(self, conn):
        self.n = 0
        conn.set_trace_callback(self.count)

    def count(self, _sql):
        self.n += 1


def counts(store):
    """Store statements per plx.around call, in CALLS x ACTORS order, on a fresh tool
    over ``store`` (the first call pays the per-store-state reads)."""
    t, _ = fx.tools(store)
    counter = Counter(store.conn)
    out = []
    for args in CALLS:
        for actor in ACTORS:
            counter.n = 0
            result = t.call("plx.around", {**args, "limit": LIMIT}, actor)
            assert "error" not in result, result
            out.append(counter.n)
    store.conn.close()
    return out


class AroundLatencyTests(unittest.TestCase):
    def test_query_count_does_not_grow_with_degree_beyond_the_page(self):
        small = counts(high_degree(LIMIT + 10))
        large = counts(high_degree(DEGREE))
        self.assertEqual(large, small)
        # Bounded by the page, never by the degree.
        self.assertLess(max(large), 2 * LIMIT)

    def test_same_objects_links_and_citations_as_the_base_code_path(self):
        base = base_module()
        store = high_degree(DEGREE)
        policy_tools = ot.OntologyTools(
            ot.StoreReader(store.conn), fx.POLICY, item_gate=fx.item_gate, schema=fx.SCHEMA,
            grammar=None, contracts=fx.CONTRACTS, clock=lambda: fx.NOW,
            monotonic=fx.Clock().monotonic)
        old = base.TraverseTools(policy_tools)
        new, _ = fx.tools(store)

        def compare(label, times, pages):
            seen = {}
            for i, args in enumerate(CALLS):
                for at in times:
                    for actor in ACTORS:
                        call = {**args, "limit": LIMIT, **({"as_known_at": at} if at else {})}
                        with self.subTest(label, call=call, actor=sorted(actor.principals)):
                            got = new.call("plx.around", call, actor)
                            fx.ok(self, "plx.around", got)
                            want = old.call("plx.around", call, actor)
                            self.assertEqual(comparable(got), comparable(want))
                            self.assertTrue(got["edges"])
                            seen[i, at, actor.principals] = comparable(got)
                            nxt = got["completeness"].get("next_cursor")
                            if pages and nxt:  # the next page through each tool's cursor
                                want_next = want["completeness"]["next_cursor"]
                                self.assertEqual(
                                    comparable(new.call("plx.around", {**call, "cursor": nxt},
                                                        actor)),
                                    comparable(old.call("plx.around",
                                                        {**call, "cursor": want_next},
                                                        actor)))
            return seen

        before = compare("as built", (None, "2099-01-20T00:00:00Z"), True)
        # A write after the first calls (a finance-marked link of a hop's type and a
        # superseded shipment) must reach the cached store-state reads.
        store.conn.execute("UPDATE links SET markings = '[\"finance\"]' "
                           "WHERE link_id = 'l-hs-00001'")
        store.conn.execute("UPDATE object_versions SET superseded_at = "
                           "'2099-01-18T00:00:00Z' WHERE object_id = "
                           "'Shipment:portal.shipment:hs-00002'")
        store.conn.commit()
        after = compare("after a write", (None,), False)
        self.assertNotEqual(after, {key: before[key] for key in after})
        store.conn.close()


# --------------------------------------------------------------------------- store size

ORPHAN = "Order:portal.order:o-7"       # an order with no party: its placed_by hop is empty
SIZE = 30                               # filler groups of the small store (the large: 10x)


def sized(groups, extra=None):
    """The fixture store plus ``groups`` filler groups unrelated to the probed roots (a
    party, two orders placed by it, a shipment and four events of the declared types;
    every fifth order and placed_by link finance-marked), ``ORPHAN`` and a mail mention
    of O2 corroborated by P1's record: the slices and the values every probed call reads
    grow with ``groups``, the roots' neighbourhoods do not."""
    def fill(s):
        s.obj(ORPHAN, "api:public.order", fx.api("order", "ORD-7"), "fm_bronze",
              display=("number", "identifier", "ORD-7"))
        _corroborated_mention(s, "MailMessage:mail.inbox:m-2", "graph-mail://mbx-1/m-2@ck1",
                              fx.O2)
        for g in range(groups):
            party = f"Party:portal.party:f-{g:05d}"
            s.obj(party, "api:public.party", fx.api("party", f"f{g}"), "fm_bronze",
                  display=("name", "text", f"Filler {g}"))
            for k in range(2):
                order = f"Order:portal.order:f-{g:05d}-{k}"
                s.obj(order, "api:public.order", fx.api("order", f"F{g}-{k}"), "fm_bronze",
                      markings='["finance"]' if (2 * g + k) % 5 == 0 else "[]",
                      display=("number", "identifier", f"F{g}-{k}"))
                s.link(f"l-f-{g:05d}-{k}", "Order.placed_by", order, party,
                       markings='["finance"]' if (2 * g + k) % 5 == 0 else "[]",
                       superseded="2099-01-15T00:00:00Z" if k else None)
                s.event(f"E:f-{g:05d}-{k}-ordered", "Order.ordered", "source_business_date",
                        [(order, "order")], time="2099-01-10")
            ship = f"Shipment:portal.shipment:f-{g:05d}"
            s.obj(ship, "api:public.shipment", fx.api("shipment", f"fs{g}"), "fm_bronze",
                  display=("number", "identifier", f"FS{g}"))
            s.link(f"l-fs-{g:05d}", "Shipment.ships_order", ship,
                   f"Order:portal.order:f-{g:05d}-0")
            s.event(f"E:fs-{g:05d}", "Shipment.shipped", "source_business_date",
                    [(ship, "shipment"), (f"Order:portal.order:f-{g:05d}-0", "order")],
                    time="2099-01-20")
            s.event(f"E:f-{g:05d}-required", "Order.required", "planned",
                    [(f"Order:portal.order:f-{g:05d}-1", "order")], time="2099-01-05")
        if extra:
            extra(s)
    return fx.build(extra=fill)


def _corroborated_mention(s, mail, handle, order):
    s.obj(mail, "mail:inbox@example.test", handle, "mail",
          display=("subject", "text", "About an order"))
    reason = [{"feature": "identifier_mention", "agrees": True, "mention_group": "g-0",
               "citations": [{"l1_handle": handle, "field_path": "row.subject",
                              "digest": fx._h(handle), "lineage": "mail"}]},
              {"feature": "party_domain", "agrees": True,
               "citations": [{"l1_handle": s.handles[fx.P1], "field_path": "row.name",
                              "digest": fx._h(s.handles[fx.P1]), "lineage": "fm_bronze"}]}]
    s.conn.execute(
        "INSERT INTO links(link_id, link_type, from_object, to_object, tier, binding_kind, "
        "reason, why_not, evidence_handles, markings, recorded_at, superseded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("l-mo-" + mail[-3:], "MailMessage.mentions_order", mail, order, "corroborated",
         "mention", json.dumps(reason), "[]", json.dumps([handle, s.handles[order]]), "[]",
         fx.T0, None))


EMPTY_HOP = {"from": ORPHAN, "path": [{"link_type": "Order.placed_by",
                                       "direction": "forward"}]}
# The live slow shape (ontology.traverse slowest_around): a one-hop page citing values.
CITED_HOPS = ({"from": fx.O1, "path": [{"link_type": "Shipment.ships_order",
                                        "direction": "reverse"}]},
              {"from": fx.O2, "path": [{"link_type": "MailMessage.mentions_order",
                                        "direction": "reverse"}]})
WORK_CALLS = (("plx.around", EMPTY_HOP), ("plx.timeline", {"from": fx.O2}),
              ("plx.timeline", {"from": fx.O1}),
              *(("plx.around", hop) for hop in CITED_HOPS))


class Work:
    """Statements (trace callback) and SQLite VM steps (progress handler) of one call."""

    def __init__(self, conn):
        self.conn, self.statements, self.steps, self.sql = conn, 0, 0, []
        conn.set_trace_callback(self._statement)
        conn.set_progress_handler(self._step, 1)

    def _statement(self, sql):
        self.statements += 1
        self.sql.append(sql)

    def _step(self):
        self.steps += 1
        return 0

    def reset(self):
        self.statements, self.steps, self.sql = 0, 0, []


def work(store):
    """(statements, VM steps) per WORK_CALLS x ACTORS call after a first call of each
    (the per-store-generation reads), plus each answer's no_match / no_event states."""
    t, _ = fx.tools(store)
    meter = Work(store.conn)
    out, states = [], []
    for name, args in WORK_CALLS:
        for actor in ACTORS:
            t.call(name, args, actor)
            meter.reset()
            result = t.call(name, args, actor)
            assert "error" not in result, result
            out.append((meter.statements, meter.steps))
            states.append((sorted(g["state"] for g in result["gaps"]
                                  if g["reason"] in ("no_match", "no_event")),
                           json.dumps(result.get("edges", [])).count('"citations"')))
    store.conn.close()
    return out, states


def slice_reads(sql):
    return [s for s in sql if "GROUP BY 1, 2, 3" in s]


class StoreSizeTests(unittest.TestCase):
    def test_empty_hop_timeline_and_cited_hop_work_does_not_grow_with_the_store(self):
        small, small_states = work(sized(SIZE))
        large, large_states = work(sized(10 * SIZE))
        self.assertEqual(large, small)
        self.assertEqual(large_states, small_states)
        # The paths that claim absence ran: FULL's empty hop is absent_in_source, OPS's
        # (finance-marked placed_by links) unknown; O2's own events likewise.
        self.assertEqual([g for g, _ in small_states[:2]], [["absent_in_source"], ["unknown"]])
        self.assertEqual([g for g, _ in small_states[2:4]],
                         [["absent_in_source"] * 3, ["unknown"] * 3])
        # Each cited hop's page cites values for both readers.
        self.assertTrue(all(cited for _, cited in small_states[6:]))

    def test_store_facts_are_read_once_per_store_generation(self):
        store = sized(SIZE)
        t, _ = fx.tools(store)
        meter = Work(store.conn)

        def generation():
            meter.reset()
            for _ in range(3):
                for name, args in WORK_CALLS:
                    for actor in ACTORS:
                        self.assertNotIn("error", t.call(name, args, actor))
            return list(meter.sql)

        first = generation()
        # One slice read per kind (object, link, event) for every call of a generation,
        # and every read a later generation of the same calls skips ran once.
        self.assertEqual(len(slice_reads(first)), 3)
        later = generation()
        self.assertEqual(slice_reads(later), [])
        once = set(first) - set(later)
        self.assertEqual({sql: first.count(sql) for sql in once}, dict.fromkeys(once, 1))
        # A commit is a new generation: each read runs once more, and its answer counts
        # (a placed_by link citing a mail item: no reader may claim absence any more).
        store.conn.execute("UPDATE links SET evidence_handles = "
                           "'[\"graph-mail://mbx-1/x@ck1\"]' WHERE link_id = 'l-f-00001-0'")
        store.conn.commit()
        meter.reset()
        full = t.call("plx.around", EMPTY_HOP, fx.FULL)
        t.call("plx.around", EMPTY_HOP, fx.OPS)
        self.assertEqual(len(slice_reads(meter.sql)), 3)
        self.assertEqual([g["state"] for g in full["gaps"] if g["reason"] == "no_match"],
                         ["unknown"])
        store.conn.close()


# --------------------------------------------------------------------------- base path

def _orphan_chain(s):
    """An order with no party behind an invoice and a shipment."""
    s.obj("Invoice:portal.invoice:i-9", "api:public.invoice", fx.api("invoice", "i-9"),
          "fm_bronze")
    s.obj("Shipment:portal.shipment:s-9", "api:public.shipment", fx.api("shipment", "s-9"),
          "fm_bronze")
    s.obj(ORPHAN, "api:public.order", fx.api("order", "ORD-7"), "fm_bronze")
    s.link("l-bs-9", "Invoice.bills_shipment", "Invoice:portal.invoice:i-9",
           "Shipment:portal.shipment:s-9")
    s.link("l-so-9", "Shipment.ships_order", "Shipment:portal.shipment:s-9", ORPHAN)


def _orphan_marked(s):
    """The orphan chain; a finance-marked placed_by link elsewhere (OPS may not rule
    out that it is the orphan's)."""
    _orphan_chain(s)
    s.link("l-pb-x", "Order.placed_by", fx.O1, fx.P1, markings='["finance"]')


def _orphan_earlier_gated(s):
    """The orphan chain; a ships_order link elsewhere citing a mail item: no reader may
    take the hop before the empty one as whole, so none may rule out the orphan's party."""
    _orphan_chain(s)
    s.link("l-so-x", "Shipment.ships_order", fx.S1, fx.O2, extra_handles=[fx.M1_H])


def _orphan(s):
    s.obj(ORPHAN, "api:public.order", fx.api("order", "ORD-7"), "fm_bronze")


def _shared_handle(s):
    """A second shipment captured from S1's record holds values at S1's cited cell
    (finance-marked) and at a cell only it holds that S1's ships_order link also cites:
    a stored value of an object no edge ends at, so no reader gets that citation."""
    other, handle = "Shipment:portal.shipment:s-8", s.handles[fx.S1]
    s.obj(other, "api:public.shipment", handle, "fm_bronze")
    s.val(other, "number", "identifier", "SHP8", handle, "fm_bronze", markings='["finance"]')
    s.val(other, "weight", "text", "12 kg", handle, "fm_bronze")
    reason = json.loads(s.conn.execute(
        "SELECT reason FROM links WHERE link_id = 'l-so-1'").fetchone()[0])
    reason[0]["citations"].append({"l1_handle": handle, "field_path": "row.weight",
                                   "digest": fx._h(handle), "lineage": "fm_bronze"})
    s.conn.execute("UPDATE links SET reason = ? WHERE link_id = 'l-so-1'",
                   (json.dumps(reason),))


def _orphan_marked_party(s):
    """The orphan, and a finance-marked party: OPS may not rule out that it is the
    orphan's (the marking is on the object, no link carries one)."""
    _orphan(s)
    s.obj("Party:portal.party:p-9", "api:public.party", fx.api("party", "p-9"), "fm_bronze",
          markings='["finance"]')


def _report(*gaps):
    def extra(s):
        _orphan(s)
        for gap in gaps:
            s.report_gaps.append(gap(s))
    return extra


def _garbage_event(s):
    s.event("E:o2-required-bad", "Order.required", "planned", [(fx.O2, "order")],
            time="not-a-date")


def _gated_rows(s):
    """An Order.required event and a placed_by link citing a mail item: no reader may
    claim either absent (the orphan's party, O2's required date)."""
    _orphan(s)
    s.event("E:o4-required", "Order.required", "planned", [(fx.O4, "order")],
            time="2099-01-22", handle_of=fx.M1)
    s.link("l-pb-g", "Order.placed_by", fx.O2, fx.P1, extra_handles=[fx.M1_H])


# label -> (store, wide: every held object as a root, as known at a past time too, under
# PARTIAL and for OUTSIDER; else the orders and the orphan chain, now, for FULL and OPS)
STORES = {
    "fixture": (lambda: fx.build(), True),
    "orphan_marked": (lambda: fx.build(extra=_orphan_marked), True),
    "orphan": (lambda: fx.build(extra=_orphan_marked_party), False),
    "orphan_earlier_gated": (lambda: fx.build(extra=_orphan_earlier_gated), False),
    "own_key_unparsed": (lambda: fx.build(extra=_report(lambda s: {
        "code": "parse_not_text", "where": "Order.party_ref@portal.order", "count": 1,
        "samples": [s.handles[ORPHAN]]})), False),
    "other_key_unparsed": (lambda: fx.build(extra=_report(lambda s: {
        "code": "parse_not_identifier", "where": "Party.number@portal.party", "count": 1,
        "samples": [s.handles[fx.P1]]})), False),
    "zero_date": (lambda: fx.build(extra=lambda s: s.report_gaps.append({
        "code": "event_time_zero_date", "where": "Order.ordered", "count": 1,
        "samples": [s.handles[fx.O2]]})), False),
    "garbage_event": (lambda: fx.build(extra=_garbage_event), False),
    "gated_rows": (lambda: fx.build(extra=_gated_rows), False),
    "sized": (lambda: sized(SIZE), False),
    "shared_handle": (lambda: fx.build(extra=_shared_handle), True),
}
PARTIAL = ot.ReadPolicy(tenant="plx", audiences={"api:*": {"ops"}, "api:public.party": set()})
MULTI_HOP = (
    [{"link_type": "Invoice.bills_shipment", "direction": "forward"},
     {"link_type": "Shipment.ships_order", "direction": "forward"},
     {"link_type": "Order.placed_by", "direction": "forward"}],
    [{"link_type": "Shipment.ships_order", "direction": "forward"},
     {"link_type": "Order.placed_by", "direction": "forward"}],
)


def calls(store, wide):
    """Each root (``STORES``): one hop of every declared link type both ways that starts
    at its type, the multi-hop paths, and its timeline (whole, of one order event type,
    an order's also windowed)."""
    roots = sorted({row[0] for row in store.conn.execute("SELECT object_id FROM objects")
                    if not row[0].startswith(("Order:portal.order:f-", "Party:portal.party:f-",
                                              "Shipment:portal.shipment:f-"))
                    and (wide or row[0].startswith("Order:") or row[0].endswith("-9"))})
    for root in roots:
        for at in ({}, {"as_known_at": "2099-01-15"}) if wide else ({},):
            for link_type, link in sorted(fx.SCHEMA.link_types.items()):
                for direction, start in (("forward", "from"), ("reverse", "to")):
                    if root.startswith(link[start] + ":"):
                        yield "plx.around", {"from": root, "path": [
                            {"link_type": link_type, "direction": direction}], **at}
            for path in MULTI_HOP:
                if root.startswith(fx.SCHEMA.link_types[path[0]["link_type"]]["from"] + ":"):
                    yield "plx.around", {"from": root, "path": path, **at}
            yield "plx.timeline", {"from": root, **at}
            yield "plx.timeline", {"from": root, "event_types": ["Order.required"], **at}
            if root.startswith("Order:"):
                yield "plx.timeline", {"from": root, "since": "2099-01-06", **at}


class BaseReader(ot.StoreReader):
    """The store reader's citation read before it was keyed by handle (a scan)."""

    def values_by_citation(self, pairs):
        if not pairs:
            return []
        return list(self.conn.execute(
            "SELECT * FROM object_values WHERE (l1_handle, field_path) IN "
            "(SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') "
            "FROM json_each(?))", (json.dumps([list(p) for p in pairs]),)))


class BasePathTests(unittest.TestCase):
    def test_answers_are_byte_equal_to_the_base_path(self):
        base = base_module(FACTS_BASE_SHA)
        states = set()
        for label, (make, wide) in STORES.items():
            for policy in (fx.POLICY, PARTIAL) if wide else (fx.POLICY,):
                store = make()

                def tools(reader):
                    return ot.OntologyTools(
                        reader(store.conn), policy, item_gate=fx.item_gate,
                        schema=fx.SCHEMA, grammar=None, contracts=fx.CONTRACTS,
                        clock=lambda: fx.NOW, monotonic=fx.Clock().monotonic)
                old = base.TraverseTools(tools(BaseReader))
                new = tt.TraverseTools(tools(ot.StoreReader))
                actors = ((fx.OPS,) if policy is PARTIAL else
                          (fx.FULL, fx.OPS, fx.OUTSIDER) if wide else (fx.FULL, fx.OPS))
                for name, args in calls(store, wide):
                    for actor in actors:
                        got, want = new.call(name, args, actor), old.call(name, args, actor)
                        with self.subTest(label, name=name, args=args,
                                          actor=sorted(actor.principals)):
                            self.assertEqual(json.dumps(comparable(got)),
                                             json.dumps(comparable(want)))
                        states.update((g["reason"], g["state"]) for g in got.get("gaps", ())
                                      if g["reason"] in ("no_match", "no_event"))
                store.conn.close()
        # The comparison covered both outcomes of every absence claim.
        self.assertLessEqual({("no_match", "absent_in_source"), ("no_match", "unknown"),
                              ("no_event", "absent_in_source"), ("no_event", "unknown")},
                             states)


# ------------------------------------------------------------------ the host opens a generation

def host_table(store):
    """The host's tool table over ``store`` (``ontology_host.tool_table``, as the served
    host builds it when it opens a store generation), and the connection's work meter."""
    from operating import ontology_host
    base = ot.OntologyTools(
        ot.StoreReader(store.conn), fx.POLICY, item_gate=fx.item_gate, schema=fx.SCHEMA,
        grammar=None, contracts=fx.CONTRACTS, clock=lambda: fx.NOW,
        monotonic=fx.Clock().monotonic)
    table = ontology_host.tool_table(base, types.SimpleNamespace())
    return table, Work(store.conn)


def first_call_work(groups, name, args, actor):
    """(statements, VM steps) of the first call of ``name`` after the host opened the
    store generation, and the call's answer."""
    store = sized(groups)
    table, meter = host_table(store)
    meter.reset()
    result = table[name]["handler"](args, actor)
    assert "error" not in result, result
    out = (meter.statements, meter.steps)
    store.conn.close()
    return out, result


def swapped_host_calls(groups, name, args, actor):
    """VM steps of the first and of the second ``name`` call through ``request()`` of a
    served host whose store file was swapped for a new generation after it opened the
    first (the way a host publishes a projection): the first call's table is built, if at
    all, inside it."""
    from operating import ontology_host
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "projection.sqlite"

        def publish():
            source = sized(groups)
            staged = Path(tmp) / "next.sqlite"
            target = sqlite3.connect(staged)
            source.conn.backup(target)
            target.close()
            source.conn.close()
            os.replace(staged, path)
        publish()
        host = ontology_host.CaseHostTools(types.SimpleNamespace(), fx.FULL, store_path=path,
                                           schema=fx.SCHEMA, bc=False)
        try:
            steps = []
            with mock.patch.object(ontology_host, "live_lookups", return_value={}), \
                    mock.patch.object(ontology_host, "item_gate_from_case_runtime",
                                      return_value=fx.item_gate):
                host.open_generation()
                publish()
                for _ in range(2):
                    with host.request():
                        host.tools.clock = lambda: fx.NOW   # the fixture's dates are in 2099
                        host.tools.policy = fx.POLICY
                        meter = Work(host.tools.store.conn)
                        meter.reset()
                        result = host.table[name]["handler"](args, actor)
                        assert "error" not in result, result
                        steps.append(meter.steps)
        finally:
            host.close()
    return steps


class ColdCallTests(unittest.TestCase):
    def test_first_around_and_timeline_call_work_does_not_grow_with_the_store(self):
        # Failing first on main: the first call read every table of the store (19,255 VM
        # steps at SIZE, 159,655 at 10x SIZE); the host now reads them as it opens the
        # generation, so the first call does what a later one does.
        for name, args in (("plx.around", EMPTY_HOP), ("plx.timeline", {"from": fx.O2}),
                           ("plx.around", CITED_HOPS[0])):
            for actor in ACTORS:
                with self.subTest(name=name, args=args, actor=sorted(actor.principals)):
                    small, _ = first_call_work(SIZE, name, args, actor)
                    large, _ = first_call_work(10 * SIZE, name, args, actor)
                    self.assertEqual(large, small)

    def test_the_first_call_after_a_store_swap_does_what_a_later_call_does(self):
        # Failing first on cc45652..874e03c: request() dropped the table on a new
        # generation and the first call rebuilt it, paying the store-wide reads itself.
        for name, args in (("plx.around", EMPTY_HOP), ("plx.timeline", {"from": fx.O2})):
            for groups in (SIZE, 10 * SIZE):
                with self.subTest(name=name, groups=groups):
                    first, later = swapped_host_calls(groups, name, args, fx.FULL)
                    self.assertEqual(first, later)

    def test_the_first_call_reads_none_of_the_store_wide_slices(self):
        store = sized(SIZE)
        table, meter = host_table(store)
        meter.reset()
        table["plx.around"]["handler"](EMPTY_HOP, fx.FULL)
        self.assertEqual(slice_reads(meter.sql), [])
        store.conn.close()

    def test_primed_answers_are_the_unprimed_answers_for_each_reader(self):
        # Reader-dependent views stay per reader: the primed facts are the same for
        # every reader, so FULL and OPS (finance withheld) still differ as they did.
        answers = {}
        for primed in (False, True):
            store = sized(SIZE)
            if primed:
                table, _ = host_table(store)
                call = lambda n, a, actor, t=table: t[n]["handler"](a, actor)
            else:
                tools, _ = fx.tools(store)
                call = lambda n, a, actor, t=tools: t.call(n, a, actor)
            got = {}
            for name, args in WORK_CALLS:
                for actor in (fx.OPS, fx.FULL, fx.OPS):   # OPS after FULL: no leak back
                    got[name, json.dumps(args), tuple(sorted(actor.principals))] = \
                        json.dumps(comparable(call(name, args, actor)), sort_keys=True)
            answers[primed] = got
            store.conn.close()
        self.assertEqual(answers[True], answers[False])
        self.assertNotEqual(
            answers[True]["plx.around", json.dumps(EMPTY_HOP), tuple(sorted(fx.FULL.principals))],
            answers[True]["plx.around", json.dumps(EMPTY_HOP), tuple(sorted(fx.OPS.principals))])

    def test_a_primed_table_refills_when_its_store_changes(self):
        store = sized(SIZE)
        table, _ = host_table(store)
        before = table["plx.around"]["handler"](EMPTY_HOP, fx.FULL)
        self.assertEqual([g["state"] for g in before["gaps"] if g["reason"] == "no_match"],
                         ["absent_in_source"])
        store.conn.execute("UPDATE links SET evidence_handles = "
                           "'[\"graph-mail://mbx-1/x@ck1\"]' WHERE link_id = 'l-f-00001-0'")
        store.conn.commit()
        after = table["plx.around"]["handler"](EMPTY_HOP, fx.FULL)
        self.assertEqual([g["state"] for g in after["gaps"] if g["reason"] == "no_match"],
                         ["unknown"])
        store.conn.close()


if __name__ == "__main__":
    unittest.main()
