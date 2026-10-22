"""Flagship margin: the order margin of one sales order, through the flagship's own workspace.

``scripts.eval_flagship_path`` scores the margin of the customer order each purchase order feeds
(``eval_flagship_path.margin_step``). This module holds the verdict over one ``order_margin``
answer (``answer_step``) and the run's sample (``margin_sample``): one sales order the
flagship workspace's reader can find, picked by a fixed hash order (no id is named
anywhere), asked through ``ObjectWorkspace.investigate`` family ``order_margin``, the call
the console's object view makes. It runs as the flagship's staff principal with the
markings that reader holds, through the host access object: no reader of its own, no
synthetic grant. A reader without the finance marking gets the typed gap
(``margin_withheld``), never a figure.

The sample is not the purchase order's customer order, so its ``status`` is
``unrelated_sample`` (never ``pass``); ``sample`` carries the verdict on the answer:
``margin_shown`` (a margin with its citations), ``margin_withheld``, a fail
(``margin_names_only_missing_inputs``, ``margin_without_citation``) or ``blocked``
(``sales_order_not_held``: the reader finds no sales order). Counts and enums only,
unless ``identify`` (then also the object id, the figures and each gap subject).
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

SCHEMA = "plx-flagship-margin/v2"
FAMILY = "order_margin"
WITHHELD = "marking_withheld"   # the gap a reader without the finance marking gets
PICK_LIMIT = 20
SALES_ORDER_FILTERS = ({"property": "order_number", "op": "present"},)


def step(status: str, reason: str, **detail: Any) -> dict[str, Any]:
    return {"schema": SCHEMA, "status": status, "reason": reason, **detail}


def grounded_in(entry: Mapping[str, Any], computed: Mapping[str, Mapping[str, Any]]
                ) -> set[str]:
    """The records an entry's figure stands on: the ``basis`` of it and of every computed
    figure it is built from (``inputs``), transitively."""
    seen: set[str] = set()
    basis: set[str] = set()
    todo = [entry]
    while todo:
        current = todo.pop()
        if current.get("name") in seen:
            continue
        seen.add(current.get("name"))
        basis.update(str(b) for b in current.get("basis") or ())
        todo += [computed[n] for n in current.get("inputs") or () if n in computed]
    return basis


def answer_step(investigated: Mapping[str, Any], *, identify: bool) -> dict[str, Any]:
    """The verdict over one ``ObjectWorkspace.investigate`` ``order_margin`` result: a
    margin with citations passes; a withheld margin is the typed gap; gaps alone fail."""
    result = investigated.get("investigation") or {}
    if result.get("error"):
        return step("fail", f"investigate_error:{result['error'].get('code')}")
    computed = {e["name"]: e for e in result.get("answer") or () if e.get("kind") == "computed"}
    margin = computed.get("order_margin") or {}
    gaps = result.get("gaps") or []
    codes = sorted({str(g.get("reason") or g.get("code")) for g in gaps})
    cited = sorted({str(e["citation"].get("l1_handle")) for e in result.get("answer") or ()
                    if isinstance(e.get("citation"), dict) and e["citation"].get("l1_handle")})
    grounded = grounded_in(margin, computed)
    detail: dict[str, Any] = {
        "computed": len(computed), "gaps": len(gaps), "missing_inputs": codes,
        "citations": len(cited), "basis_records": len(grounded),
        "answer_complete": (result.get("completeness") or {}).get("complete"),
        "answerability": (result.get("answerability") or {}).get("state")}
    withheld = WITHHELD in codes or "withheld_by_permission" in (
        (result.get("answerability") or {}).get("reasons") or ())
    if identify:
        detail["figures"] = {name: (None if withheld else e.get("value"))
                             for name, e in sorted(computed.items())}
        detail["gap_subjects"] = [str(g.get("subject"))[:300] for g in gaps]
    if withheld:
        if margin.get("value") is not None:
            return step("fail", "withheld_margin_shown_a_figure", **detail)
        return step("blocked", "margin_withheld", gap=WITHHELD, **detail)
    if margin.get("value") is not None:
        if not grounded or not cited:
            return step("fail", "margin_without_citation", **detail)
        return step("pass", "margin_shown", **detail)
    if codes:
        return step("fail", "margin_names_only_missing_inputs", **detail)
    return step("fail", "no_margin_and_no_missing_input_named", **detail)


def margin_sample(workspace: Any, *, identify: bool = False) -> dict[str, Any]:
    """One sales order's margin through ``workspace`` (module doc): ``unrelated_sample``
    with its verdict in ``sample``, or ``blocked`` when the reader finds no sales order."""
    found = workspace._call("plx.find", {"object_type": "SalesOrder", "limit": PICK_LIMIT,
                                         "filters": list(SALES_ORDER_FILTERS)})
    objects = sorted({(c.get("object") or c).get("object_id")
                      for c in found.get("candidates") or ()
                      if (c.get("object") or c).get("object_type") == "SalesOrder"} - {None},
                     key=lambda oid: hashlib.sha256(oid.encode()).hexdigest())
    if not objects:
        return step("blocked", "sales_order_not_held")
    try:
        investigated = workspace.investigate(FAMILY, objects[0])
    except (LookupError, KeyError, ValueError, PermissionError) as error:
        sample = step("fail", f"investigate_refused:{error.args[0] if error.args else error}")
    else:
        sample = answer_step(investigated, identify=identify)
    sample.pop("schema", None)
    out = step("unrelated_sample", "sample_not_fed_by_the_purchase_order", sample=sample)
    if identify:
        out["object_id"] = objects[0]
    return out
