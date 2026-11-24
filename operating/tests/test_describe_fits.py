"""plx.sql {"describe": "<record type>"} fits one response (isolated fixture: an ephemeral
Postgres store through the generic views' door, as ``test_generic_views``).

A record type whose records carry many long free-text fields (a certificate's extracted
text, live 11-21: ``result_too_large`` and the reader saw none of its fields) is described
with its fields named, its categorical fields' commonest values kept and what was cut said
in ``trimmed``; a type that fits keeps its whole sample, as before.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from operating import ontology_tools as ot
from operating import ontology_tools_sql as ts
from operating.tests import test_generic_views as gvt
from operating.tests import test_ontology_tools_table as tab
from tests import test_ontology_tools_evidence as ev

FREE = [f"text_{i:02d}" for i in range(30)]
DOMAIN = json.loads(json.dumps(gvt.DOMAIN))
DOMAIN["object_types"].append(ev._type(
    "CertificateOfAnalysis", [("lot", "text"), ("supplier", "text")]
    + [(name, "text") for name in FREE],
    [("gen.coa", "graph_mail")], display=["lot"]))
for _binding in DOMAIN["object_types"][-1]["bindings"]:
    _binding["relation"] = _binding["id"]
SCHEMA = ot.SchemaView.from_documents([DOMAIN])
SUPPLIERS = ("Croda", "DSM", "Azelis")
_BASE_BUILD = gvt.build


def build():
    s, ids = _BASE_BUILD()
    for n in range(60):
        gvt.obj(s, "CertificateOfAnalysis", "gen.coa", f"c{n}",
                {"lot": f"L{n:03d}", "supplier": SUPPLIERS[n % 3],
                 **{name: f"{name} of lot {n}: " + "assay within limits " * 30
                    for name in FREE}},
                audience="api:public.x", handle=f"api://gen.coa/c{n}@0000000000000001-1")
    s.conn.commit()
    return s, ids


class DescribeFits(gvt.Fixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patches = [mock.patch.object(gvt, "build", build),
                       mock.patch.object(gvt, "SCHEMA", SCHEMA)]
        for patch in cls.patches:
            patch.start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        for patch in cls.patches:
            patch.stop()

    def test_a_wide_type_is_described_within_one_response(self):
        # the case tools' bound (48,000 bytes): the Ask's own (800,000) holds this type whole
        with mock.patch("operating.mcp_server.ASK_RESULT_BYTES", 48000):
            out = self.call({"describe": "CertificateOfAnalysis"}, tab.FULL)
            self.assertNotIn("error", out, str(out)[:500])
            self.assertTrue(ts._fits_response(out))
        sample = out["sample"]
        self.assertEqual(sample["records_you_can_read"], 60)
        self.assertTrue(sample["trimmed"])
        # the categorical field keeps its values; free text is left out of the commonest
        common = sample["commonest_values"]
        self.assertEqual(sorted(v[0] for v in common["supplier"]), sorted(SUPPLIERS))
        self.assertFalse(set(FREE) & set(common))
        # every field is still named by the sample row(s)
        self.assertTrue(sample["rows"])
        self.assertLessEqual(set(FREE) | {"lot", "supplier"}, set(sample["rows"][0]["fields"]))
        # a reader who may read none of it gets no row and no value
        none = self.call({"describe": "CertificateOfAnalysis"}, tab.NOBODY)
        self.assertEqual((none["sample"]["records_you_can_read"], none["sample"]["rows"]),
                         (0, []))
        self.assertNotIn("assay within limits", json.dumps(none))

    def test_a_type_that_fits_keeps_its_whole_sample(self):
        out = self.call({"describe": "MCTask"}, tab.FULL)
        self.assertNotIn("trimmed", out["sample"])
        self.assertEqual(len(out["sample"]["rows"]), 3)


if __name__ == "__main__":
    unittest.main()
