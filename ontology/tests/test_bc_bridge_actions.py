"""Action-type rules bc_bridge.json's BC number-series Manual Nos. actions exposed.

The two action types stay bound to their BC web-service actions (tests/test_gen_bc.py
bound_action). The Portal's runBcSync also calls both actions on every run, fail-open, with no
human and no MC approval (plx-customer-portal@f3e3a5399d42 sync-runner.ts), and each action
type cites those calls: the purchase order action on the Setup Order Nos., the customer series
of the company and of the Distribution company (sync-runner.ts:272) and the S-ORD sales-series
fallback. Every such citation is pinned in devloop/upstream-pins.json with the sha256 of its
lines, so it is checked without the Portal checkout; only the comparison with the checked-out
file skips. The Workbench reads no No. Series record, so both verify by person_check, never
read_back. Over every action type, a ``read_back`` targets a type some source serves (the
loader refuses a user-objective record or scope on any owner-system write, ontology/schema.py
OWNER_REFERENCE_PARAMS, tested in test_schema.py). Every pending-file pointer in a domain names
a file that exists.
"""

from __future__ import annotations

import copy
from hashlib import sha256
import json
from pathlib import Path
import re
import unittest

from ontology import json_contract
from ontology import schema as s
from scripts import check_upstream_drift as drift

ROOT = Path(__file__).resolve().parents[2]
PORTAL = "plx-customer-portal"
SYNC_RUNNER = "portal/src/lib/business-central/sync-runner.ts"
CUSTOMER_NOS = "portal/src/lib/business-central/customer-manual-nos.ts"
PENDING = re.compile(r"ontology/schema/pending/[a-z0-9_]+\.json")
SERIES = {"EnsurePurchaseOrderManualNos": "PLXPurchaseOrderRef_EnsureOrderManualNos",
          "EnsureSalesOrderManualNos": "PLXSalesBridge_EnsureSalesOrderManualNos"}
S_ORD = ("PLXPurchaseOrderRef_EnsureOrderManualNos", "BC_SALES_ORDER_NO_SERIES", '"S-ORD"')
# What each cited Portal range holds: its call and the catch that keeps the sync going.
CITED = {
    (SYNC_RUNNER, "358-368"): ("PLXPurchaseOrderRef_EnsureOrderManualNos", "catch"),
    (SYNC_RUNNER, "232-245"): ("ensureCustomerManualNos(companyName)", "catch"),
    (SYNC_RUNNER, "265-283"): ("ensureCustomerManualNos(distName)", "catch"),
    (SYNC_RUNNER, "394-405"): (*S_ORD, "companyName", "catch"),
    (SYNC_RUNNER, "454-465"): (*S_ORD, "distName", "catch"),
    (SYNC_RUNNER, "370-392"): ("PLXSalesBridge_EnsureSalesOrderManualNos", "catch"),
    (SYNC_RUNNER, "440-453"): ("PLXSalesBridge_EnsureSalesOrderManualNos", "distName", "catch"),
    (CUSTOMER_NOS, "20-22"): ("PLXPurchaseOrderRef_EnsureOrderManualNos", '"C-CUST"'),
}


def portal_citations(compiled) -> list[dict]:
    return [e for aid in SERIES for e in compiled.action_types[aid]["evidence"]
            if e.get("path") in (SYNC_RUNNER, CUSTOMER_NOS)]


def pin_of(pins: dict, citation: dict) -> dict | None:
    found = [p for p in pins["pins"] if (p["repo"], p["path"]) == (PORTAL, citation["path"])
             and p["commit"].startswith(citation["commit"]) and p["status"] == "resolved"]
    return found[0] if len(found) == 1 else None


def lines_sha256(text: list[str], span: str) -> str:
    first, last = map(int, span.split("-"))
    return sha256("\n".join(text[first - 1:last]).encode("utf-8")).hexdigest()


def unserved_read_backs(compiled) -> list[str]:
    return sorted(aid for aid, a in compiled.action_types.items()
                  if a["verification"] == "read_back"
                  and (not compiled.object_types[a["target"]]["bindings"]
                       or compiled.object_types[a["target"]]["availability"]["state"]
                       == "not_served"))


class BcNumberSeriesActions(unittest.TestCase):
    def test_each_cites_the_automatic_fail_open_portal_sync_calls(self):
        compiled = s.load_schema()
        for aid, action in SERIES.items():
            act = compiled.action_types[aid]
            self.assertEqual(act["registry_entries"],
                             [f"business-central.web_service_action:{action}"])
            for phrase in ("automatically, on every runBcSync run",
                           "no human and no MC approval", "fail-open",
                           "BC_SALES_ORDER_NO_SERIES, default S-ORD", "sync-runner.ts:394-405"):
                self.assertIn(phrase, act["description"], aid)
            # The Workbench cannot read a No. Series: the person in BC confirms, no read_back.
            self.assertEqual(act["verification"], "person_check", aid)
            notes = [e["note"] for e in act["evidence"] if e.get("path") == SYNC_RUNNER]
            self.assertTrue(notes and all("fail-open" in n or "sync continues" in n
                                          for n in notes), aid)
            self.assertIn(action, " ".join(notes))
        po = compiled.action_types["EnsurePurchaseOrderManualNos"]
        self.assertIn("the Distribution company (sync-runner.ts:272)", po["description"])
        self.assertEqual({(e["path"], e["lines"]) for e in portal_citations(compiled)},
                         set(CITED))

    def test_each_portal_citation_is_pinned_with_the_sha256_of_its_lines(self):
        # Runs without the Portal checkout: the pin is the citation's authority.
        pins = json.loads(drift.DEFAULT_PINS.read_text(encoding="utf-8"))
        for e in portal_citations(s.load_schema()):
            pin = pin_of(pins, e)
            self.assertIsNotNone(pin, f"{e['path']}@{e['commit']} is not pinned")
            self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(pin.get("cited_lines", {}).get(e["lines"], ""), r"^[0-9a-f]{64}$",
                             f"{e['path']}:{e['lines']} has no pinned line hash")

    def test_the_checked_out_portal_holds_each_cited_call_and_its_catch(self):
        pins = json.loads(drift.DEFAULT_PINS.read_text(encoding="utf-8"))
        checkout = Path(pins["repos"][PORTAL]["default_checkout"])
        for e in portal_citations(s.load_schema()):
            pin = pin_of(pins, e)
            self.assertIsNotNone(pin)
            # The pinned commit, or HEAD when its file is byte-identical to the pin (a
            # shallow checkout may lack the commit).
            data = drift.git(checkout, "show", f"{pin['commit']}:{e['path']}")
            if data is None:
                data = drift.git(checkout, "show", f"HEAD:{e['path']}")
                if data is not None and sha256(data).hexdigest() != pin["sha256"]:
                    data = None
            if data is None:
                self.skipTest(f"{e['path']} at {pin['commit'][:12]} is not checked out here")
            self.assertEqual(sha256(data).hexdigest(), pin["sha256"], e["path"])
            text = data.decode("utf-8").splitlines()
            self.assertEqual(lines_sha256(text, e["lines"]),
                             pin.get("cited_lines", {}).get(e["lines"]), (e["path"], e["lines"]))
            first, last = map(int, e["lines"].split("-"))
            cited = "\n".join(text[first - 1:last])
            for token in CITED[(e["path"], e["lines"])]:
                self.assertIn(token, cited, (e["path"], e["lines"]))
            self.assertNotIn("throw", cited, (e["path"], e["lines"]))

    def test_person_check_is_only_an_owner_system_verification(self):
        # An unknown verification, or person_check on a Workbench-internal action, fails the
        # format: the Workbench's own effects are its log, never a person's word.
        fmt = json.loads(s.FORMAT_PATH.read_text(encoding="utf-8"))
        doc = json.loads((s.SCHEMA_DIR / "domains" / "bc_bridge.json").read_text("utf-8"))
        self.assertEqual(json_contract.validate(doc, fmt, root=fmt), [])
        act = doc["action_types"][0]
        for change in ({"verification": "trusted"},
                       {"executor": "workbench_internal", "owner_system": "workbench",
                        "approval": {"route": "none"}}):
            probe = copy.deepcopy(doc)
            probe["action_types"][0] = {**act, **change}
            self.assertTrue(json_contract.validate(probe, fmt, root=fmt), change)

    def test_no_action_type_reads_back_a_type_no_source_serves(self):
        for pending in (True, False):
            compiled = s.load_schema(include_pending=pending)
            self.assertEqual(unserved_read_backs(compiled), [])
        # The probe sees one: a read-back of the (committed, unserved) Portal warehouse.
        probe = copy.deepcopy(compiled)
        probe.action_types["EnsurePurchaseOrderManualNos"].update(target="Warehouse",
                                                                  verification="read_back")
        self.assertEqual(unserved_read_backs(probe), ["EnsurePurchaseOrderManualNos"])

    def test_pending_pointers_name_a_file_that_declares_the_type(self):
        for path in sorted((s.SCHEMA_DIR / "domains").glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            for pointer in PENDING.findall(path.read_text(encoding="utf-8")):
                self.assertTrue((ROOT / pointer).is_file(), f"{path.name}: {pointer}")
            for t in doc.get("object_types", []):
                for pointer in PENDING.findall(t["description"]):
                    if "replaces this placeholder" in t["description"]:
                        declared = json.loads((ROOT / pointer).read_text(encoding="utf-8"))
                        self.assertIn(t["id"], {d["id"] for d in declared["object_types"]},
                                      f"{path.name} {t['id']}: {pointer}")


if __name__ == "__main__":
    unittest.main()
