"""The company's recurring workflows (``workflows.json``): one name for what a message, a
task, a prepared autopilot item or a person's real action is about, so the replay
(``operating.autopilot_replay``) scores each workflow of each role and the live engine
switches each one on only after it passed.

``classify`` reads keyword families over the subject and text with the counterpart's side
(``customer``, ``supplier``, ``internal``, ``internal_out``: our mail to an outside address
no party record states, ``unknown``); the first workflow of the priority order that holds
wins, else ``other_external`` / ``other_internal``. ``of_item`` names a prepared item's
workflow: its autopilot's when that autopilot has one, else the record it is about, else its
event's words. An unknown family, side or workflow refuses the file (``WorkflowsInvalid``).
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any, Mapping

FILE = Path(__file__).with_name("workflows.json")
SIDES = ("customer", "supplier", "internal", "internal_out", "unknown")
EXTERNAL_SIDES = ("customer", "supplier", "internal_out", "unknown")


class WorkflowsInvalid(ValueError):
    pass


@lru_cache(maxsize=4)
def load(path: str | None = None) -> dict[str, Any]:
    raw = json.loads(Path(path or FILE).read_text(encoding="utf-8"))
    if raw.get("schema") != "plx-workflows/v1":
        raise WorkflowsInvalid("schema")
    families = {k: re.compile(v, re.I | re.M) for k, v in raw["families"].items()}
    names = set()
    for w in raw["workflows"]:
        if set(w) - {"id", "any", "side_in", "external"} or not w.get("any"):
            raise WorkflowsInvalid(f"workflow {w.get('id')}")
        if set(w["any"]) - set(families) or set(w.get("side_in") or ()) - set(SIDES):
            raise WorkflowsInvalid(f"workflow {w['id']}")
        names.add(w["id"])
    names |= set(raw["fallback"].values())
    for table in ("autopilot_workflow", "record_workflow"):
        if set(raw[table].values()) - names:
            raise WorkflowsInvalid(table)
    return {**raw, "compiled": families, "names": frozenset(names)}


def names() -> frozenset[str]:
    return load()["names"]


def classify(subject: str, text: str, side: str) -> str:
    spec = load()
    if side not in SIDES:
        side = "unknown"
    blob = f"{subject or ''}\n{text or ''}"
    hits = {k for k, rx in spec["compiled"].items() if rx.search(blob)}
    external = side in EXTERNAL_SIDES
    for w in spec["workflows"]:
        if not hits & set(w["any"]):
            continue
        if w.get("side_in") and side not in w["side_in"]:
            continue
        if "external" in w and bool(w["external"]) != external:
            continue
        return w["id"]
    return spec["fallback"]["external" if external else "internal"]


def of_item(autopilot: str, event: Mapping[str, Any]) -> str:
    spec = load()
    if autopilot in spec["autopilot_workflow"]:
        return spec["autopilot_workflow"][autopilot]
    otype = str(event.get("object_type") or "")
    if otype in spec["record_workflow"]:
        return spec["record_workflow"][otype]
    fields = event.get("fields") or {}
    subject = str(event.get("subject") or fields.get("title") or fields.get("subject") or "")
    return classify(subject, str(event.get("text") or ""), str(event.get("side") or "internal"))
