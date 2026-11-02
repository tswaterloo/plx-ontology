"""Short handle aliases (``operating.handle_aliases``) and named value claims at the answer
gate: an alias stands for a handle only in the session that minted it, every other
alias-shaped string is refused, and a value claim's name never loosens a check.

Evidence label: isolated-fixture. The end-to-end path (the real stdio host behind the tool
gate, aliases on the wire, the reply rendered from the gate's decision) is
``evals.tests.test_answer_gate_one_path`` ``test_written_once_with_aliases_...``.
"""

from __future__ import annotations

import unittest

from operating import answer_gate
from operating.handle_aliases import Aliases, shorten_result

DOC = "original://doc-1#page=2"
ROW = "bc://purchaseOrders/PO-1"


class AliasTests(unittest.TestCase):
    def test_round_trip_and_only_whole_handles_are_aliased(self):
        aliases = Aliases(tag="KTR")
        shown = aliases.shorten({"citation": DOC, "rows": [{"l1_handle": ROW}],
                                 "text": f"see {DOC}", "n": 3})
        self.assertEqual(shown, {"citation": "@KTR1", "rows": [{"l1_handle": "@KTR2"}],
                                 "text": f"see {DOC}", "n": 3})
        self.assertEqual(aliases.shorten(DOC), "@KTR1")          # one alias per handle
        self.assertEqual(aliases.restore(shown)["citation"], DOC)
        self.assertEqual(aliases.expand({"citation": "@KTR2", "text": "per @KTR1."}),
                         {"citation": ROW, "text": f"per {DOC}."})
        self.assertEqual(aliases.expand("@KTR2#page=3"), ROW + "#page=3")

    def test_an_alias_this_session_never_minted_is_refused_never_mapped(self):
        mine, other = Aliases(tag="KTR"), Aliases(tag="BCD")
        mine.shorten(DOC)
        other.shorten(ROW)
        for given in ("@BCD1", "@KTR2", "@KTR99"):
            with self.subTest(given):
                self.assertEqual(mine.unknown_aliases({"citation": given}), [given])
                self.assertEqual(mine.expand(given), given)          # left as written
        self.assertEqual(mine.unknown_aliases(
            {"query": "mail to ops@KTR1.example, ref x@BCD1"}), [])  # an address is no alias

    def test_a_tool_result_is_aliased_in_both_forms_the_client_reads(self):
        aliases = Aliases(tag="KTR")
        out = shorten_result(aliases, {
            "content": [{"type": "text", "text": '{"citation": "%s"}' % DOC},
                        {"type": "text", "text": "not json " + DOC}],
            "structuredContent": {"citation": DOC}, "isError": False})
        self.assertEqual(out["structuredContent"], {"citation": "@KTR1"})
        self.assertEqual(out["content"][0]["text"], '{"citation": "@KTR1"}')
        self.assertEqual(out["content"][1]["text"], "not json " + DOC)


class NamedValueClaimTests(unittest.TestCase):
    def test_a_name_of_another_shape_refuses_its_claim_before_any_check(self):
        for name in ("has space", "1st", "x" * 65, 7):
            with self.subTest(name=name):
                gated = answer_gate.gate_answer({"claims": [{
                    "text": "The order states it.", "citations": [ROW],
                    "value_claims": [{"name": name, "value": "OPEN", "citation": ROW,
                                      "field": "row.status"}]}]}, None, seen={ROW},
                    returned={}, may_repair=False)
                self.assertEqual(gated["claims"], [])
                (refused,) = gated["unsupported"]
                self.assertEqual(refused["reason"], "value_claims_invalid")


if __name__ == "__main__":
    unittest.main()
