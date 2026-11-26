"""Role routing and org-chart roster derivation (``operating.autopilot_roster``,
``operating.autopilot``, ``operating.autopilot_qc_entry``): operational workflows route to
their department staff (QA/QC -> quality, Purchasing -> purchasing, Accounting -> finance/AP/AR,
Sales -> customer_service), IT/software/executive staff are excluded from operational pools,
and an empty operational role never falls back to the approver roster."""
from __future__ import annotations

from datetime import date
import unittest
from unittest import mock

from operating import autopilot as ap
from operating import autopilot_qc_entry as qc
from operating import autopilot_roster as roster
from operating.tests.test_proactive_threads import people


class OperationalRoleRouting(unittest.TestCase):
    def test_empty_operational_role_never_falls_back_to_approvers(self):
        directory = {
            "vince@petrasoap.com": "u-vince",
            "theo@petrasoap.com": "u-theo",
            "ali@petrasoap.com": "u-ali",
        }
        with mock.patch("operating.autopilot._approvers", return_value=["u-vince", "u-theo"]):
            owners, basis = ap.owners_of(
                "role:quality", {"fields": {}}, directory=directory, addresses_of={}, roles={},
            )
            self.assertEqual((owners, basis), ([], "role_unassigned"))

            # role:owner still falls back to approvers when roles["owner"] is absent
            owners, basis = ap.owners_of(
                "role:owner", {"fields": {}}, directory=directory, addresses_of={}, roles={},
            )
            self.assertEqual((owners, basis), (["u-vince", "u-theo"], None))

            # populated role:quality routes to the quality pool (never approvers)
            owners, basis = ap.owners_of(
                "role:quality",
                {"fields": {}},
                directory=directory,
                addresses_of={},
                roles={"quality": ["u-ali"]},
            )
            self.assertEqual((owners, basis), (["pool:quality"], None))

    def test_non_operational_staff_and_department_role_matching(self):
        # Software engineers and IT/exec roles are excluded from operational pools even when
        # their department happens to be "Production" or "IT".
        self.assertTrue(roster.is_non_operational_staff("Production", "Software Engineer"))
        self.assertTrue(roster.is_non_operational_staff("IT", "CEO"))
        self.assertTrue(roster.is_non_operational_staff("IT", "Systems Administrator"))
        self.assertFalse(roster.is_non_operational_staff("QA/QC", "QA/QC Manager"))
        self.assertFalse(roster.is_non_operational_staff("QA/QC", "Senior QA/QC Technician"))

        for role in roster.DEPARTMENT_RULES:
            self.assertFalse(roster.staff_matches_role(role, "Production", "Software Engineer"), role)
            self.assertFalse(roster.staff_matches_role(role, "IT", "CEO"), role)

        # QA/QC department and titles match quality only
        for dept, title in (
            ("QA/QC", "QA/QC Manager"),
            ("QA/QC", "Senior QA/QC Technician"),
            ("QA/QC", "QA/QC Technician"),
            ("Quality", "Regulatory Compliance Specialist"),
        ):
            self.assertTrue(roster.staff_matches_role("quality", dept, title), (dept, title))
            self.assertFalse(roster.staff_matches_role("purchasing", dept, title), (dept, title))

        # Other operational departments match their intended roles
        self.assertTrue(roster.staff_matches_role("purchasing", "Purchasing", "Senior Buyer"))
        self.assertTrue(roster.staff_matches_role("finance", "Accounting", "Controller"))
        self.assertTrue(roster.staff_matches_role("payables", "Accounting", "AP Clerk"))
        self.assertTrue(roster.staff_matches_role("receivables", "Accounting", "AR Specialist"))
        self.assertTrue(roster.staff_matches_role("customer_service", "Sales", "Account Manager"))
        self.assertTrue(roster.staff_matches_role("production_planning", "Production", "Production Planner"))
        self.assertTrue(roster.staff_matches_role("rnd", "R&D", "Formulation Chemist"))

    def test_derive_from_staff_routes_qc_holds_to_qa_department_only(self):
        staff_rows = [
            {"person_id": "u-ali", "department": "QA/QC", "title": "QA/QC Manager"},
            {"person_id": "u-luis", "department": "QA/QC", "title": "Senior QA/QC Technician"},
            {"person_id": "u-deep", "department": "QA/QC", "title": "QA/QC Technician"},
            {"person_id": "u-theo", "department": "Production", "title": "Software Engineer"},
            {"person_id": "u-vince", "department": "IT", "title": "CEO"},
            {"person_id": "u-buyer", "department": "Purchasing", "title": "Buyer"},
            {"person_id": "u-sales", "department": "Sales", "title": "Account Manager"},
            {"person_id": "u-ap", "department": "Accounting", "title": "AP Specialist"},
        ]
        p = people()
        with mock.patch.object(p, "identity_clear", return_value=True):
            qa_members = roster.derive_from_staff(staff_rows, "quality", p)
            self.assertEqual(qa_members, ["u-ali", "u-deep", "u-luis"])
            self.assertNotIn("u-theo", qa_members)
            self.assertNotIn("u-vince", qa_members)

            purch_members = roster.derive_from_staff(staff_rows, "purchasing", p)
            self.assertEqual(purch_members, ["u-buyer"])

            sales_members = roster.derive_from_staff(staff_rows, "customer_service", p)
            self.assertEqual(sales_members, ["u-sales"])

            ap_members = roster.derive_from_staff(staff_rows, "payables", p)
            self.assertEqual(ap_members, ["u-ap"])

    def test_qc_entry_keyer_skips_non_operational_staff(self):
        peers = [
            (date(2026, 10, 20), "Batch:2", {"tested_by": {"value": "theo"}}),
            (date(2026, 10, 19), "Batch:1", {"tested_by": {"value": "ali"}}),
        ]
        p = people()
        directory = {
            "theo@petrasoap.com": "u-theo",
            "ali@petrasoap.com": "u-ali",
        }
        staff_rows = [
            {"person_id": "u-theo", "department": "Production", "title": "Software Engineer"},
            {"person_id": "u-ali", "department": "QA/QC", "title": "QA/QC Manager"},
        ]
        with mock.patch.object(
            ap, "_people_maps", return_value=(directory, {}, ("petrasoap.com",)),
        ), mock.patch.object(p, "identity_clear", return_value=True):
            self.assertEqual(
                qc.keyer(None, peers, "tested_by", p, staff_rows=staff_rows),
                "u-ali",
            )


if __name__ == "__main__":
    unittest.main()
