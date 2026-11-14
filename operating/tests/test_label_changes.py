"""Label and artwork change requests (``operating.label_changes``) through the engine and
the per-reader inbox: a stale request is said only when it names what it is about.

SYNTHETIC fixtures only (``World`` stores, synthetic handles, addresses and names);
evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import timedelta
import json

from operating import label_changes as lc
from operating.tests.test_proactive_engine import AS_OF, Engine, World

CUSTOMER = "buyer@synth-customer.example"
OURS = "ops@synth-tenant.example"
MAILBOX = "mail:00000000-0000-4000-8000-0000000000a1"
ASK = "Please update the label artwork with the new address."


def day(offset: int) -> str:
    return (AS_OF - timedelta(days=offset)).isoformat()


def ask(w: World, *, subject: str = "Label for SYNTH bottle", body: str = ASK,
        sent: int = 30, links: tuple = ()) -> str:
    oid = w.obj("Email", {"conversation_id": f"c{w.n}", "subject": subject,
                          "sender": CUSTOMER, "to_addresses": json.dumps([OURS]),
                          "sent_at": f"{day(sent)}T15:00:00Z", "body_preview": body,
                          "internet_message_id": f"<m{w.n}@synth>"},
                binding="graph_mail.messages", audience=MAILBOX)
    for link_type, target in links:
        w.link(link_type, oid, target)
    return oid


class LabelChanges(Engine):

    def stale(self, **read) -> dict[str, dict]:
        return {g["object_id"]: g for g in self.read(kinds=[lc.STALE], limit=1000,
                                                     **read)["gaps"]}

    def test_a_stale_request_names_its_item_or_customer_and_an_unnamed_one_is_tracked(self):
        for count in (1, 10, 60):
            with self.subTest(n=count):
                w = World()
                items = []
                for i in range(count):
                    item = w.obj("Product", {"name": f"SYNTH Bottle {i}"},
                                 audience="api:items")
                    ask(w, links=(("Email.mentions_product", item),))
                    items.append(item)
                buyer = w.obj("Customer", {"name": "SYNTH Buyer"}, audience="api:customers")
                ask(w, links=(("Email.sent_by_customer", buyer),))
                unnamed = w.obj("Product", {"code": "SYNTH-X"}, audience="api:items")
                ask(w, links=(("Email.mentions_product", unnamed),))   # no name stated
                ask(w)                                                  # names nothing
                ask(w, subject="Labels printed for SYNTH run")          # subject: done
                summary = self.run_engine(w, only=[lc.RULE])
                stale = self.stale()
                self.assertEqual(sorted(stale), sorted([*items, buyer]))
                self.assertIn("for item SYNTH Bottle 0", stale[items[0]]["summary"])
                self.assertIn("for customer SYNTH Buyer", stale[buyer]["summary"])
                self.assertEqual(summary["detectors"][lc.RULE], "complete")
                # the name said is cited: a reader who may not read the item sees no
                # stale alert naming it
                hidden = self.stale(may_read=lambda key: key != "api:items")
                self.assertEqual(sorted(hidden), [buyer])
                self.store.unlink()
                self.inbox.unlink(missing_ok=True)
