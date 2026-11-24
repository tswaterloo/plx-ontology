"""``plx.sql {"chain": ...}`` (Ask spec 16.10 mechanism 2) at its boundary: a record followed
through its links both ways in one read, as each reader's door allows, over the generic
fixture (``test_generic_views``) plus hubs of 1, 10 and 60 linked records.

Checked: a handle and a number each find the record; every linked record is a row once (none
lost, none twice at N = 1, 10, 60), cited and paged; the profile and ``chain.reached`` give the
exact counts; hop 2 walks a neighbour's links; a guess-tier link is listed with its tier and
not walked from; a number that fits two records returns both and walks neither until a party
or a type picks one; a reader sees no record or link its door hides; unknown input and a
record that is not there fail closed with a typed error."""
from __future__ import annotations

import json
import time
import unittest
from unittest import mock

from ontology.json_contract import validate
from operating import ontology_tools_sql as ts
from operating import ontology_tools_sql_rows as rows_mod
from operating.tests import test_generic_views as tgv
from operating.tests.test_generic_views import FULL, NOBODY, OPS, Fixture, obj
from tests import test_ontology_tools_evidence as ev


_BUILD = tgv.build


def build_with_hubs():
    s, ids = _BUILD()
    hubs = {}
    for n in (1, 10, 60):
        hubs[n] = obj(s, "Customer", "portal.app_customer", f"hub{n}",
                      {"name": f"Hub {n}", "code": f"H{n}"})
        for k in range(n):
            t = obj(s, "MCTask", "mission_control.tasks", f"h{n}-{k}",
                    {"title": f"Hub task {n}-{k}", "status": "open", "due": "2026-11-20"},
                    audience="api:mc.tasks", handle=f"mc://tasks/h{n}-{k}")
            s.link(f"hl{n}-{k}", "MCTask.about", t, hubs[n], "corroborated")
    # a guess: hub L --lead-- task L --about--> hub Z (the second hop must not follow it)
    hubs["L"] = obj(s, "Customer", "portal.app_customer", "hubL", {"name": "Hub L"})
    hubs["Z"] = obj(s, "Customer", "portal.app_customer", "hubZ", {"name": "Hub Z"})
    guess = obj(s, "MCTask", "mission_control.tasks", "lead1", {"title": "Guessed task",
                                                                 "status": "open"},
                audience="api:mc.tasks", handle="mc://tasks/lead1")
    s.link("lg1", "MCTask.about", guess, hubs["L"], "lead")
    s.link("lg2", "MCTask.about", guess, hubs["Z"], "corroborated")
    for tag, job in (("a", "Alpha job"), ("b", "Beta job")):   # one name, two unlinked records
        twin = obj(s, "Customer", "portal.app_customer", f"twin{tag}", {"name": "Twin Co"})
        t = obj(s, "MCTask", "mission_control.tasks", f"twin-{tag}", {"title": job,
                                                                       "status": "open"},
                audience="api:mc.tasks", handle=f"mc://tasks/twin-{tag}")
        s.link(f"tw-{tag}", "MCTask.about", t, twin, "corroborated")
    lead = [obj(s, "Customer", "portal.app_customer", f"lead{c}", {"name": "Lead Co"})
            for c in "ab"]
    s.link("ld-same", "Customer.same_record_as", lead[0], lead[1], "lead")
    for c, o in zip("ab", lead):
        t = obj(s, "MCTask", "mission_control.tasks", f"lead-{c}", {"title": f"Lead job {c}",
                                                                     "status": "open"},
                audience="api:mc.tasks", handle=f"mc://tasks/lead-{c}")
        s.link(f"ld-{c}", "MCTask.about", t, o, "corroborated")
    s.conn.commit()
    return s, ids


class ChainRead(Fixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.object(tgv, "build", build_with_hubs):
            super().setUpClass()

    def chain(self, args, actor=FULL):
        out = self.call({"chain": args} if isinstance(args, str) else args, actor)
        if "error" not in out:
            self.assertEqual(validate(out, ts._CONTRACT["output"], root=ev.CONTRACTS), [],
                             json.dumps(out)[:1500])
        return out

    @staticmethod
    def cells(out):
        text = lambda v: v["untrusted_text"] if isinstance(v, dict) else v  # noqa: E731
        return [dict(zip(out["columns"], [text(c) for c in r["cells"]])) for r in out["rows"]]

    def test_a_handle_and_a_number_find_the_same_record_and_every_link_is_one_row(self):
        by_handle = self.chain("mc://tasks/t10-9")
        self.assertNotIn("error", by_handle, by_handle)
        rows = self.cells(by_handle)
        self.assertEqual(rows[0]["direction"], "start")
        self.assertEqual(rows[0]["handle"], "mc://tasks/t10-9")
        self.assertTrue(all(r["cite"].startswith(by_handle["result"] + "#row=")
                            for r in by_handle["rows"]))
        by_label = self.chain("Task 10-9")
        self.assertEqual(self.cells(by_label), rows)
        # the task is about acme_p (corroborated): one link out, the record at its far end
        far = [r for r in rows if r["hop"] == "1"]
        self.assertEqual([(r["direction"], r["link_type"], r["type"], r["label"])
                          for r in far], [("out", "MCTask.about", "Customer", "Acme Ltd")])
        self.assertEqual(by_handle["chain"]["reached"], {"Customer": 1, "MCTask": 1})

    def test_facts_carry_the_declared_amounts_with_their_currency(self):
        rows = self.cells(self.chain("IN-2"))
        start = next(r for r in rows if r["direction"] == "start")
        facts = json.loads(start["facts"])
        self.assertEqual(float(facts["total_amount"]), 12.5)
        self.assertEqual(facts["total_amount_currency"], "USD")
        self.assertNotEqual({r["facts"] for r in rows}, {"{}"})

    def test_candidates_joined_only_by_a_guess_each_walk_their_own_links(self):
        out = self.chain("Lead Co")
        rows = self.cells(out)
        self.assertFalse(out["chain"]["ambiguous"])
        self.assertIn("guess-tier", out["note"])
        self.assertEqual(sorted(r["label"] for r in rows if r["hop"] == "1"
                                and r["type"] == "MCTask"), ["Lead job a", "Lead job b"])
        self.assertEqual([r["tier"] for r in rows if r["link_type"] == "Customer.same_record_as"],
                         ["lead"])

    def test_both_directions_and_two_hops(self):
        one = self.cells(self.chain({"chain": "IN-2"}))
        self.assertEqual([(r["direction"], r["type"]) for r in one if r["hop"] == "1"],
                         [("out", "Customer")])
        two = self.chain({"chain": "IN-2", "hops": 2})
        rows = self.cells(two)
        hop2 = [r for r in rows if r["hop"] == "2"]
        # acme_s (Sage) is held twice with acme_p (identity link) and billed on IN-1 and IN-2
        self.assertIn(("in", "SalesInvoice", "IN-1"),
                      [(r["direction"], r["type"], r["label"]) for r in hop2])
        self.assertIn(("in", "Customer", "Acme Ltd"),
                      [(r["direction"], r["type"], r["label"]) for r in hop2])
        # a link seen from both ends is one row
        handles = [(r["via"], r["handle"], r["link_type"]) for r in rows]
        self.assertEqual(len(handles), len(set(handles)))
        self.assertEqual(two["profile"]["rows"], len(rows))
        self.assertEqual(sum(two["chain"]["reached"].values()), len(rows))

    def test_cardinality_1_10_60_no_row_lost_or_doubled_and_paged(self):
        for n in (1, 10, 60):
            started = time.monotonic()
            out = self.chain(f"Hub {n}")
            self.assertNotIn("error", out, out)
            rows = self.cells(out)
            self.assertEqual(out["profile"]["rows"], n + 1)
            self.assertEqual(sorted(r["handle"] for r in rows if r["hop"] == "1"),
                             sorted(f"mc://tasks/h{n}-{k}" for k in range(n)))
            self.assertEqual(out["chain"]["reached"], {"Customer": 1, "MCTask": n})
            self.assertLess(time.monotonic() - started, 30)
        # the rest of a long result is read by paging it, every row once
        with mock.patch.object(rows_mod, "SHOWN_ROWS", 25):
            first = self.chain("Hub 60")
            self.assertEqual(first["row_count"], 25)
            seen = [r["cite"] for r in first["rows"]]
            offset = 25
            while offset < first["profile"]["rows"]:
                page = self.call({"page": first["result"], "offset": offset})
                seen += [r["cite"] for r in page["rows"]]
                offset += page["row_count"]
            self.assertEqual(len(seen), 61)
            self.assertEqual(len(set(seen)), 61)

    def test_a_guess_link_is_listed_with_its_tier_and_not_walked_from(self):
        out = self.chain({"chain": "Hub L", "hops": 2})
        rows = self.cells(out)
        guessed = [r for r in rows if r["handle"] == "mc://tasks/lead1"]
        self.assertEqual([(r["hop"], r["tier"]) for r in guessed], [("1", "lead")])
        # hub Z is reachable only through the guess link: not in the chain
        self.assertNotIn("Hub Z", [r["label"] for r in rows])
        self.assertEqual(out["chain"]["guess_links_listed_not_walked"], 1)

    def test_records_that_are_linked_to_one_another_are_walked_as_one_chain(self):
        both = self.chain("ACME LTD")   # the Portal and the Sage copy, joined by a verified link
        rows = self.cells(both)
        self.assertEqual([r["direction"] for r in rows if r["hop"] == "0"], ["start", "start"])
        self.assertFalse(both["chain"]["ambiguous"])
        self.assertIn("linked to one another", both["note"])
        self.assertIn("IN-2", [r["label"] for r in rows])

    def test_a_number_that_fits_two_unlinked_records_is_never_resolved_by_taking_one(self):
        both = self.chain("Twin Co")
        rows = self.cells(both)
        self.assertEqual({r["direction"] for r in rows}, {"candidate"})
        self.assertEqual(len(rows), 2)
        self.assertTrue(both["chain"]["ambiguous"])
        self.assertIn("pick one", both["note"])
        self.assertEqual(both["chain"]["records"], 2)
        # a party picks the one linked to a record whose label holds it
        picked = self.chain({"chain": "Twin Co", "party": "Beta"})
        self.assertFalse(picked["chain"]["ambiguous"])
        self.assertEqual([r["label"] for r in self.cells(picked) if r["hop"] == "1"],
                         ["Beta job"])
        # a type that is not the record's finds nothing, and says so
        wrong = self.chain({"chain": "Twin Co", "type": "SalesInvoice"})
        self.assertEqual(wrong["error"]["code"], "not_found")
        # a party no neighbour holds leaves the candidates, never a guess
        none = self.chain({"chain": "Twin Co", "party": "no such name"})
        self.assertTrue(none["chain"]["ambiguous"])
        self.assertIn("matched no linked record", none["note"])

    def test_each_reader_sees_only_what_its_door_allows(self):
        full = json.dumps(self.chain("IN-1"))
        self.assertIn("Hidden Co", full)    # the finance-marked customer, to a reader holding it
        ops = json.dumps(self.chain("IN-1", OPS))
        for secret in tgv.OPS_SECRET:
            self.assertNotIn(secret, ops)
        # a record the reader's door hides is no record to it
        gone = self.chain("Hidden Co", OPS)
        self.assertEqual(gone["error"]["code"], "not_found")
        nobody = self.chain("IN-1", NOBODY)
        self.assertTrue("error" in nobody or all(
            r["direction"] != "out" for r in self.cells(nobody)), nobody)
        # never a record nobody may read, whatever the reader
        for name in ("GONE-9", "Excluded task"):
            self.assertEqual(self.chain(name)["error"]["code"], "not_found")

    def test_unknown_and_malformed_input_fails_closed(self):
        for args in ({"chain": "IN-1", "sql": "SELECT 1"},
                     {"chain": "IN-1", "hops": 0}, {"chain": "IN-1", "hops": "2"},
                     {"chain": "IN-1", "hops": True}, {"chain": ""}, {"chain": 7},
                     {"chain": "IN-1", "party": ""}, {"chain": "IN-1", "by": "x"},
                     {"hops": 2}, {"party": "x", "sql": "SELECT 1"}):
            out = self.call(args)
            self.assertIn("error", out, args)
            self.assertIn(out["error"]["code"], ("invalid_request", "invalid_sql"), args)
        missing = self.call({"chain": "NO-SUCH-NUMBER-77"})
        self.assertEqual(missing["error"]["code"], "not_found")
        # a quote or a LIKE wildcard in the number is text, never part of the statement
        for odd in ("IN-1' OR '1'='1", "%", "_N-1", "IN-1\\"):
            self.assertEqual(self.call({"chain": odd})["error"]["code"], "not_found", odd)


if __name__ == "__main__":
    unittest.main()
