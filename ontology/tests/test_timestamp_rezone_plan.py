"""C-09 on the train: a zone declared on a timestamp source where none was is a set-based
rewrite of its served text (``units.timestamp_rezone``, ``operating.pg.timestamp_rezone``),
not a re-projection of its units; a zone on anything a link, identity key, identifier,
other property or capture-observed event reads stays in the unit diff."""

from __future__ import annotations

import copy
import dataclasses
import unittest

from ontology import step_migrate as sm


def _strip(schema, *, props=(), events=()):
    """``schema`` as if the listed property sources / event times declared no zone."""
    types = copy.deepcopy(schema.object_types)
    for tid, name in props:
        for p in types[tid]["properties"]:
            if p["name"] == name:
                p["sources"] = [sm._unzoned(s) for s in p["sources"]]
    evs = copy.deepcopy(schema.event_types)
    for eid in events:
        evs[eid]["time"] = sm._unzoned(evs[eid]["time"])
    return dataclasses.replace(schema, object_types=types, event_types=evs)


def _zoned(schema):
    """(type, property) of every source declaring a zone, and every zoned event time."""
    props = sorted({(tid, p["name"]) for tid, t in schema.object_types.items()
                    for p in t.get("properties", ()) for s in p.get("sources", ())
                    if sm._zone_of(s)})
    events = sorted(e for e, ev in schema.event_types.items()
                    if sm._zone_of(ev.get("time") or {}))
    return props, events


class RezonePlanTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from ontology.schema import load_schema
        cls.new = load_schema()
        cls.props, cls.events = _zoned(cls.new)
        cls.code = sm.code_digests(None)
        cls.present = sorted(sm.STEPS)

    def test_the_schema_declares_zones(self):
        self.assertTrue(self.props)
        self.assertTrue(self.events)

    def test_a_zone_only_change_is_a_rezone_not_a_unit(self):
        served = _strip(self.new, props=self.props, events=self.events)
        plan = sm.plan_schemas(served, self.new, self.code, self.code, self.present)
        units = plan["units"]
        self.assertEqual(units["refused"], [])
        rezone = units["timestamp_rezone"]
        got = {(c["type"], c["property"]) for c in rezone["values"]}
        # every zoned source is a carrier unless something else reads the property
        self.assertTrue(got)
        self.assertLessEqual(got, set(self.props))
        # what stays in the unit diff is only what a refusal keeps there: a property two
        # records of a same_record link both hold (Projector._conflict compares it); its
        # type's units are rewritten, nothing else is
        kept = set(self.props) - got
        for tid, name in kept:
            holders = {s["binding"] for p in self.new.object_types[tid]["properties"]
                       if p["name"] == name for s in p["sources"]}
            self.assertTrue(any(
                lb.get("kind") == "same_record"
                and {lb.get("from_binding"), lb.get("to_binding")} <= holders
                for link in self.new.link_types.values()
                if link.get("from") == tid and link.get("to") == tid
                for lb in link.get("bindings", ())), (tid, name))
        kept_types = {tid for tid, _ in kept}

        def type_of(unit):
            name = unit[2:].split("#")[0].split("|")[0]
            if unit.startswith("b:"):
                return self.new.bindings[name][0]
            return self.new.link_types[name]["from"]
        self.assertLessEqual({type_of(u) for u in units["units"]["affected"]}, kept_types,
                             units["units"]["affected"])
        # an event time on a binding whose unit is rewritten anyway is the projection's
        rewritten = {u[2:].split("#")[0] for u in units["units"]["affected"]
                     if u.startswith("b:")}
        self.assertEqual({c["event_type"] for c in rezone["events"]},
                         {e for e in self.events
                          if self.new.event_types[e]["time"]["binding"] not in rewritten})
        for c in rezone["values"] + rezone["events"]:
            self.assertEqual(len(c["zone"]), 1)
            self.assertTrue(c["relation"])
            self.assertIn(f"{c['type']}:{c['binding']}:", units["scope"])
            self.assertIn(c["type"], units["touched_types"])
        # the served digest is the plan's from: the diff compared against a patched copy
        self.assertEqual(units["from"], served.digest)

    def test_a_zone_on_a_property_a_link_reads_rewrites_its_units(self):
        served = _strip(self.new, props=self.props)
        tid, name = self.props[0]
        binding = next(s["binding"] for p in self.new.object_types[tid]["properties"]
                       if p["name"] == name for s in p["sources"] if sm._zone_of(s))
        links = dict(self.new.link_types)
        links["Test.compares_stamp"] = {
            "id": "Test.compares_stamp", "from": tid, "to": tid, "cardinality": "N:N",
            "bindings": [{"kind": "same_record", "pairs": [{"from": name, "to": name}]}]}
        linked = dataclasses.replace(self.new, link_types=links)
        out = sm.rezone_carriers(served, linked)
        self.assertNotIn((tid, name), {(c["type"], c["property"]) for c in out["values"]})
        # the property keeps the served (unzoned) source in what the unit diff compares
        prop = next(p for p in out["served"].object_types[tid]["properties"]
                    if p["name"] == name)
        self.assertFalse(any(sm._zone_of(s) for s in prop["sources"]))
        self.assertTrue(any(c["binding"] != binding or c["property"] != name
                            for c in out["values"]))

    def test_a_zone_on_a_property_same_record_links_compare_rewrites_its_units(self):
        # Projector._conflict compares every property both ends of a same_record link hold
        served = _strip(self.new, props=self.props)
        tid, name = self.props[0]
        binding = next(s["binding"] for p in self.new.object_types[tid]["properties"]
                       if p["name"] == name for s in p["sources"] if sm._zone_of(s))
        links = dict(self.new.link_types)
        links["Test.same_as"] = {
            "id": "Test.same_as", "from": tid, "to": tid, "cardinality": "N:N",
            "bindings": [{"kind": "same_record", "from_binding": binding,
                          "to_binding": binding, "via": []}]}
        linked = dataclasses.replace(self.new, link_types=links)
        out = sm.rezone_carriers(served, linked)
        self.assertNotIn((tid, name), {(c["type"], c["property"]) for c in out["values"]})

    def test_a_zone_changed_from_another_zone_or_with_other_changes_is_no_carrier(self):
        tid, name = self.props[0]
        types = copy.deepcopy(self.new.object_types)
        for p in types[tid]["properties"]:
            if p["name"] == name:
                p["sources"] = [{**sm._unzoned(s), "naive_zone": "UTC"} if sm._zone_of(s)
                                and "wall_clock_zone" in s else
                                {**sm._unzoned(s), "wall_clock_zone": "America/New_York"}
                                for s in p["sources"]]
        other_zone = dataclasses.replace(self.new, object_types=types)
        out = sm.rezone_carriers(other_zone, self.new)
        self.assertNotIn((tid, name), {(c["type"], c["property"]) for c in out["values"]})
        types = copy.deepcopy(self.new.object_types)
        for p in types[tid]["properties"]:
            if p["name"] == name:
                p["sources"] = [{**sm._unzoned(s), "path": s["path"] + "_was"}
                                for s in p["sources"]]
        moved = dataclasses.replace(self.new, object_types=types)
        out = sm.rezone_carriers(moved, self.new)
        self.assertNotIn((tid, name), {(c["type"], c["property"]) for c in out["values"]})

    def test_no_change_no_carrier(self):
        out = sm.rezone_carriers(self.new, self.new)
        self.assertEqual((out["values"], out["events"]), ([], []))
        self.assertIs(out["served"], self.new)


if __name__ == "__main__":
    unittest.main()
