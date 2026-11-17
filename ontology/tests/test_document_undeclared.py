"""AUD-13 (audit B #16): a document-field reference to a record type no link type declares
was counted ``undeclared_target`` and dropped (171,971 in the gen 11 build). The top pairs
are declared now (``domains/document_mentions.json``)."""
from __future__ import annotations

import unittest

from ontology import mentions
from ontology.schema import load_schema

DOCS = ("SupplierConfirmation", "ForwarderQuote", "PackingSlip", "AssemblyOrderPacket",
        "CustomerPoDocument", "SupplierInvoice", "FreightBill", "CertificateOfAnalysis",
        "DeliveryReceipt", "DecisionRecord")
TARGETS = ("SalesInvoice", "SalesOrder", "AssemblyOrder", "Batch", "BatchSchedule")


class Declared(unittest.TestCase):
    def test_every_document_class_declares_the_top_record_types_it_names(self):
        links = load_schema().link_types
        declared = mentions.mention_link_types(links)
        missing = [(d, t) for d in DOCS for t in TARGETS if (d, t) not in declared]
        self.assertEqual(missing, [])
        for pair in ((d, t) for d in DOCS for t in TARGETS):
            link = links[declared[pair]]
            self.assertEqual({b["tier_ceiling"] for b in link["bindings"]}, {"lead"})
            self.assertEqual({b["kind"] for b in link["bindings"]}, {"mention"})


if __name__ == "__main__":
    unittest.main()
