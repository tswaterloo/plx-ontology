"""What the landed-cost contract and its Portal engine port share: the
error every input refusal raises and the cost treatment every record carries.

Kept apart so the frozen Portal port (``operating.landed_cost_portal``) and the Workbench
contract (``operating.landed_cost``) each import them without importing each other."""
from __future__ import annotations

from ontology.core import ValidationError

# Current Portal decision (ERP-D-015): freight and duty are expensed, so a landed cost computed
# here is an analytic estimate and never the item's cost.
COST_TREATMENT = {
    "kind": "analytic_estimate_not_item_cost",
    "current_portal_decision": "expense (ERP-D-015, Approved 2026-09-10): freight and duty post "
                               "to Sage G/L (packaging freight 41130, packaging duty 41131)",
    "open_question": "Vince's step 18 asks to reopen ERP-D-015 (capitalise freight and duty "
                     "into item cost via BC Item Charges); until then this is not item cost",
    "source": "docs/projects/business-central-erp-modernization/decision-log.md@5724575#L25; "
              "portal/src/lib/procurement/landed-gl.ts@5724575#L1-L104",
}


class LandedCostError(ValidationError):
    pass
