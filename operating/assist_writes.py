"""Prepared system writes wired to dry-run adapters (ASSIST specs 8.5 F8).

Translates a prepared session's or matter's system steps (``plx.prepare_write`` proposals or
plan ``system_write`` steps) targeting Business Central (``bc``), Calendar (``calendar``),
Planner (``planner``), SharePoint (``sharepoint``), or Mission Control (``mc``) into validated,
structured dry-run write intents with current (``before``) and proposed (``after``) state.

Safety invariants (enforced fail-closed):
* Zero live external writes: every adapter is instantiated with ``transport=None``; ``mode`` is
  always ``"dry_run"`` and ``perform()`` is never called.
* 0 automatic on class B: money, invoices, payments, PO/SO totals or prices, credit notes,
  charges, approvals, deletes, and external sends are always ``safety_class = "class_b"``,
  ``confirm_only = True``, and ``auto_allowed = False``. ``enforce_execution_policy`` refuses
  automatic execution on any class B or confirm-only intent even if fields were mutated.
* Reader permissions, people exclusion, door visibility, and sensitive-content screens are
  enforced before any ``before`` field is exposed; unreadable or denied records fail closed.

Reuses ``operating.autopilot_write_adapters``, ``operating.write_classes``,
``operating.prepared_writes``, ``operating.autopilot_write``, and
``operating.autopilot_write_readiness`` with no duplicate adapter or safety-class tables.

Evidence label: ``isolated-fixture`` (``operating/tests/test_assist_f8_writes.py``).
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa
from operating import autopilot_write_readiness as readiness
from operating import prepared_writes as pw
from operating import write_classes as wc

SUPPORTED_SYSTEMS: tuple[str, ...] = readiness.SYSTEMS
DRY_RUN_ONLY_SYSTEMS: frozenset[str] = frozenset({"mc"})

# Operations and payload fields that always force class_b (confirm-only, auto_allowed=False)
# on top of write_classes.write_class (money, invoices, payments, PO totals/prices, credit
# notes, approvals, deletes, and external sends).
_ALWAYS_CLASS_B_OPS: frozenset[str] = frozenset({
    "approval_decide",
    "purchase_order_delete",
    "sales_order_delete",
    "sales_order_charge_line",
    "sales_invoice_charge_line",
    "purchase_order_create",
    "sales_order_create",
    "purchase_order_update",
    "sales_order_update",
    "purchase_order_line_create",
    "purchase_order_line_update",
    "sales_order_line_create",
    "sales_order_line_update",
    "file_replace",
})
_MONEY_FIELD_NAMES: frozenset[str] = frozenset({
    "unitprice", "directunitcost", "amount", "totalamount", "total", "price", "cost",
    "discountpercent", "discountamount", "creditamount", "paymentamount", "invoiceamount",
    "usd", "currencycode",
})
_SENSITIVE_MARKINGS: frozenset[str] = frozenset({
    "pay", "payroll", "hr", "compensation", "personal", "private", "restricted",
})
# Operations that mutate or decide an existing record and therefore require a readable current
# state so ``before`` is always sourced from the system/store, never invented.
_REQUIRES_CURRENT_READ: frozenset[str] = frozenset({
    "purchase_order_update",
    "sales_order_update",
    "purchase_order_line_update",
    "sales_order_line_update",
    "purchase_order_delete",
    "sales_order_delete",
    "event_update",
    "event_respond",
    "task_update",
    "list_item_update",
    "file_replace",
    "approval_decide",
})

ReadCurrent = Callable[[pw.Person, Mapping[str, Any]], Optional[Mapping[str, Any]]]
MayCheck = Callable[[pw.Person, Mapping[str, Any]], Optional[str]]
DoorCheck = Callable[[Mapping[str, Any]], bool]
ScreenCheck = Callable[[str], Optional[str]]


def dry_run_adapters(environ: Optional[Mapping[str, str]] = None) -> dict[str, wa.Adapter]:
    """Build dry-run-only adapter instances (``transport=None`` on every adapter) so no
    prepared write check or preview can ever send an external request."""
    env = dict(os.environ if environ is None else environ)
    adapters: dict[str, wa.Adapter] = {}
    for cls in wa.ADAPTER_TYPES:
        if cls.system in SUPPORTED_SYSTEMS:
            adapters[cls.kind] = cls(None, environ=env)
    return adapters


def _normalize_person(owner: pw.Person | str) -> pw.Person:
    if isinstance(owner, pw.Person):
        return owner
    text = str(owner or "").strip().lower()
    if not text:
        raise pw.Refused(["A signed-in reader identity is required to prepare a write."], 403)
    return pw.Person(ids=(text,), full=False)


def _extract_step(step: Mapping[str, Any]) -> dict[str, Any]:
    """Accept either a raw system step or a ``plx.prepare_write`` proposal envelope."""
    if not isinstance(step, Mapping):
        raise pw.Refused(["System step must be an object."])
    if "arguments" in step and isinstance(step.get("arguments"), Mapping):
        tool = str(step.get("tool") or "")
        if tool and tool != pw.TOOL_NAME:
            raise pw.Refused([f"Unsupported write tool {tool}."])
        raw = dict(step["arguments"])
    else:
        raw = dict(step)
    system = str(raw.get("system") or "").strip().lower()
    operation = str(raw.get("operation") or raw.get("action") or "").strip()
    target = dict(raw.get("target") or {}) if isinstance(raw.get("target"), Mapping) else {}
    change_raw = raw.get("change") if "change" in raw else raw.get("payload")
    change = dict(change_raw or {}) if isinstance(change_raw, Mapping) else {}
    precondition = (dict(raw.get("precondition") or {})
                    if isinstance(raw.get("precondition"), Mapping) else {})
    prior = dict(raw.get("prior") or {}) if isinstance(raw.get("prior"), Mapping) else {}
    people = [str(p) for p in (raw.get("people") or ()) if isinstance(p, str) and p.strip()]
    reason = str(raw.get("reason") or raw.get("about") or "")
    title = str(raw.get("title") or reason)
    target_handle = str(raw.get("target_handle") or raw.get("record") or "").strip()
    out: dict[str, Any] = {
        "system": system,
        "operation": operation,
        "target": target,
        "change": change,
        "people": people,
        "reason": reason,
        "title": title,
    }
    if precondition:
        out["precondition"] = precondition
    if prior:
        out["prior"] = prior
    if target_handle:
        out["target_handle"] = target_handle
    return out


def _has_money_fields(value: Any) -> bool:
    if isinstance(value, Mapping):
        for k, v in value.items():
            key = str(k).lower().replace("_", "").replace("-", "")
            if key in _MONEY_FIELD_NAMES and v is not None:
                return True
            if _has_money_fields(v):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_has_money_fields(item) for item in value)
    return False


def classify_safety(action: Mapping[str, Any], *, item: Optional[Mapping[str, Any]] = None,
                    rules: Optional[Mapping[str, Any]] = None, workflow: str = "",
                    known_domains: Iterable[str] = ()) -> tuple[str, list[str]]:
    """Return ``("class_a" | "class_b", reasons)`` for ``action``.

    Reuses ``write_classes.write_class`` and promotes any money-bearing payload, invoice,
    payment, credit note, approval, delete, or external send to ``"class_b"`` fail-closed.
    Class C operations (no write path) are refused.
    """
    base = wc.write_class(action, item=item, rules=rules, workflow=workflow,
                          known_domains=known_domains)
    if base == "C":
        raise pw.Refused([f"{action.get('system')}: class C has no write adapter (entry sheet only)."])
    reasons: list[str] = []
    if base == "B":
        reasons.append("write_classes:B")
    op = str(action.get("operation") or "").strip().lower()
    if op in _ALWAYS_CLASS_B_OPS:
        reasons.append(f"confirm_only_operation:{op}")
    if any(tok in op for tok in ("delete", "approval", "invoice", "payment", "credit", "charge")):
        reasons.append(f"sensitive_operation:{op}")
    if _has_money_fields(action.get("payload") or {}):
        reasons.append("money_field_present")
    if action.get("kind") == "mc_approval":
        reasons.append("approval_requires_confirmation")
    if reasons:
        return "class_b", list(dict.fromkeys(reasons))
    return "class_a", []


def readiness_of(system: str, *, state: Optional[Path] = None,
                 environ: Optional[Mapping[str, str]] = None,
                 readiness_map: Optional[Mapping[str, Any]] = None,
                 now: Optional[datetime] = None) -> dict[str, Any]:
    """Honest adapter readiness for ``system`` from the readiness probe or injected map."""
    found = readiness_map
    if found is None and state is not None:
        found = readiness.read(Path(state), os.environ if environ is None else environ, now=now)
    systems = (found or {}).get("systems") if isinstance(found, Mapping) else None
    entry = systems.get(system) if isinstance(systems, Mapping) else None
    if not isinstance(entry, Mapping):
        return {
            "status": "unprobed",
            "live_flag": False,
            "dry_run_only": True,
            "missing": ["no recent write readiness probe"],
        }
    status = str(entry.get("status") or "blocked")
    missing = [str(m) for m in (entry.get("missing") or ()) if m]
    live_flag = bool(entry.get("live_flag"))
    if system in DRY_RUN_ONLY_SYSTEMS:
        # Mission Control stays dry-run only in F8 regardless of any local flag.
        if status == "ready-live":
            status = "ready-dry-run"
        return {
            "status": status,
            "live_flag": False,
            "dry_run_only": True,
            "missing": missing,
        }
    return {
        "status": status,
        "live_flag": live_flag,
        "dry_run_only": status != "ready-live",
        "missing": missing,
    }


def _target_handle(system: str, operation: str, target: Mapping[str, Any],
                   explicit: str, got: Optional[Mapping[str, Any]]) -> str:
    if explicit:
        return explicit
    if isinstance(got, Mapping) and got.get("handle"):
        return str(got["handle"])
    parts = [system]
    for key in ("document_id", "line_id", "record_id", "task_id", "gate_id",
                "event_id", "site_id", "list_id", "item_id", "drive_id", "parent_id"):
        val = target.get(key)
        if val:
            parts.append(f"{key}={val}")
    if len(parts) == 1:
        parts.append(operation or "new")
    return ":".join(parts)


def _check_record_visibility(got: Mapping[str, Any], *, door: Optional[DoorCheck],
                             screen: Optional[ScreenCheck]) -> None:
    if got.get("denied"):
        raise pw.Refused(["Target record is not readable by this user."], 403)
    if got.get("unreadable"):
        raise pw.Refused(["Target record cannot be read now, so before state cannot be shown."], 422)
    markings = {str(m).strip().lower() for m in (got.get("markings") or ()) if str(m).strip()}
    if door is not None:
        if not door(got):
            raise pw.Refused(["Target record did not pass the reader access door."], 403)
    elif markings & _SENSITIVE_MARKINGS:
        raise pw.Refused(["Target record carries sensitive markings and cannot be exposed without door clearance."], 403)
    if screen is not None:
        blob = json.dumps({"name": got.get("name"), "values": got.get("values")},
                          sort_keys=True, default=str)
        why = screen(blob)
        if why:
            raise pw.Refused([f"Target record withheld by screen ({why})."], 403)


def _build_before_after(action: Mapping[str, Any], got: Optional[Mapping[str, Any]]
                        ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    op = str(action.get("operation") or "")
    payload = action.get("payload") if isinstance(action.get("payload"), Mapping) else {}
    values = (got or {}).get("values") if isinstance((got or {}).get("values"), Mapping) else {}
    if op.endswith("_delete"):
        before = {str(k): pw._plain(v, str(k)) for k, v in values.items()}
        after: dict[str, Any] = {"deleted": True}
        diff = [{"field": k, "before": v, "after": None} for k, v in before.items()]
        diff.append({"field": "deleted", "before": False, "after": True})
        return before, after, diff
    if action.get("kind") == "mc_approval":
        before = {
            "status": pw._plain(values.get("status", "pending"), "status"),
            "requested_by": pw._plain((got or {}).get("requested_by"), "requested_by"),
        }
        after = {
            "decision": action.get("recommendation"),
            "scope": pw._plain(payload.get("scope"), "scope"),
        }
        diff = [
            {"field": "status", "before": before["status"], "after": action.get("recommendation")},
            {"field": "scope", "before": None, "after": after["scope"]},
        ]
        return before, after, diff
    before = {}
    after = {}
    diff = []
    for field, val in payload.items():
        key = str(field)
        prev = values.get(key) if values else None
        before[key] = pw._plain(prev, key)
        after[key] = pw._plain(val, key)
        diff.append({"field": key, "before": before[key], "after": after[key]})
    return before, after, diff


def _intent_id(owner: str, system: str, operation: str, target_handle: str,
               after: Mapping[str, Any]) -> str:
    seed = json.dumps(
        {"owner": owner, "system": system, "operation": operation,
         "target_handle": target_handle, "after": after},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return "awi_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def build_write_intent(
    step: Mapping[str, Any],
    owner: pw.Person | str,
    *,
    read_current: Optional[ReadCurrent] = None,
    may: Optional[MayCheck] = None,
    guard: Any = None,
    door: Optional[DoorCheck] = None,
    screen: Optional[ScreenCheck] = None,
    state: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    readiness_map: Optional[Mapping[str, Any]] = None,
    known_domains: Iterable[str] = (),
    rules: Optional[Mapping[str, Any]] = None,
    workflow: str = "",
    origin: str = "assist",
    citations: Sequence[str] = (),
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Validate and translate one prepared system step into a structured dry-run write intent.

    Never performs an external write (adapters are always created with ``transport=None``).
    Raises ``prepared_writes.Refused`` on any schema, permission, exclusion, visibility, or
    unsupported-system failure.
    """
    person = _normalize_person(owner)
    owner_id = person.email or person.ids[0]
    req = _extract_step(step)
    system = req["system"]
    operation = req["operation"]
    explicit_handle = req.pop("target_handle", "")

    if system in pw.NEVER:
        raise pw.Refused([pw.NEVER[system]])
    if system not in SUPPORTED_SYSTEMS:
        raise pw.Refused([
            f"Unsupported target system {system or '(none)'}: expected one of "
            f"{', '.join(SUPPORTED_SYSTEMS)}."
        ])

    # Screen proposed text before any adapter work or record read.
    if screen is not None:
        proposed_blob = json.dumps({"change": req["change"], "reason": req["reason"]},
                                   sort_keys=True, default=str)
        why_screen = screen(proposed_blob)
        if why_screen:
            raise pw.Refused([f"Proposed write withheld by screen ({why_screen})."], 403)

    env = dict(os.environ if environ is None else environ)
    adapters = dry_run_adapters(env)

    # Probe the action shape so we can check reader permission before reading current state.
    probe_item = pw.item_of(req, owner_id, "awi_probe", origin, citations)
    probe_actions = aw.actions_of(probe_item)
    if len(probe_actions) != 1 or probe_actions[0].get("kind") not in aw.KINDS:
        raise pw.Refused([f"{system}: unsupported operation {operation or '(none)'}."])
    probe_action = probe_actions[0]

    # Fail-closed reader permission check BEFORE reading any target record.
    if not person.full and probe_action.get("kind") not in pw.OWN:
        if may is None:
            raise pw.Refused([
                f"Cannot verify your write access for {system}: nothing is prepared."
            ], 403)
        denied = may(person, probe_action)
        if denied:
            raise pw.Refused([f"You cannot make this {system} change: {denied}."], 403)

    # Read current state (fail-closed on unreadable / denied / sensitive records).
    got: Optional[Mapping[str, Any]] = None
    if read_current is not None:
        try:
            raw_got = read_current(person, probe_action)
        except Exception as exc:  # noqa: BLE001 - fail closed on any read error
            raise pw.Refused([
                f"Current record could not be read from {system} ({type(exc).__name__})."
            ], 422) from None
        if isinstance(raw_got, Mapping):
            _check_record_visibility(raw_got, door=door, screen=screen)
            got = dict(raw_got)

    needs_read = operation in _REQUIRES_CURRENT_READ or bool(req.get("prior"))
    if needs_read and got is None:
        raise pw.Refused([
            f"Current record state from {system} is required before preparing {operation}."
        ], 422)

    requester: Optional[str] = None
    if system == "mc" and operation == "approval_decide":
        requester = str((got or {}).get("requested_by") or "").strip().lower() or None
        if requester is None:
            raise pw.Refused(["Mission Control gate requester could not be read."], 422)
        scope = (got or {}).get("scope")
        if not isinstance(scope, Mapping) or not scope.get("kind"):
            raise pw.Refused(["Mission Control gate shows no scope to decide."], 422)
        req = {**req, "change": {**dict(req.get("change") or {}), "scope": dict(scope)}}
    elif got is not None:
        req, read_problems = pw.Desk._as_read(req, got)
        if read_problems:
            raise pw.Refused(read_problems, 422)

    # Build the final executor item and action, then validate against the dry-run adapter.
    item = pw.item_of(req, owner_id, "awi_item", origin, citations, requested_by=requester)
    actions = aw.actions_of(item)
    if len(actions) != 1 or actions[0].get("kind") not in aw.KINDS:
        raise pw.Refused([f"{system}: malformed action for {operation}."])
    action = actions[0]
    if (system == "mc" and operation == "task_update" and got is not None
            and ("addLabels" in (action.get("payload") or {})
                 or "removeLabels" in (action.get("payload") or {}))):
        vals = got.get("values") if isinstance(got.get("values"), Mapping) else {}
        if "labels" in vals:
            pre = dict(action.get("precondition") or {})
            prior = dict(pre.get("prior") or {})
            prior.setdefault("labels", vals["labels"])
            pre["prior"] = prior
            action = {**action, "precondition": pre}
    adapter = adapters.get(str(action.get("kind")))
    if adapter is None:
        raise pw.Refused([f"No dry-run adapter for {system} ({action.get('kind')})."])

    # People exclusion check (fail-closed: unavailable guard refuses).
    all_people = (
        aw._addresses(item, action)
        + [str(p) for p in adapter.people(action)]
        + [str(p) for p in (got or {}).get("people") or () if p]
    )
    excl_reason = aw.excluded(guard, owner_id, all_people, known_domains)
    if excl_reason:
        raise pw.Refused([f"Write refused by people exclusion ({excl_reason})."], 403)

    # Validate every field against the real adapter schema (transport is None: pure dry-run).
    try:
        request_preview = adapter.request(action)
        undo_preview = adapter.undo_request(action, request_preview)
    except wa.AdapterRefused as exc:
        raise pw.Refused(list(exc.problems), 422) from None
    except (KeyError, TypeError, ValueError) as exc:
        raise pw.Refused([f"{system}: invalid write payload ({type(exc).__name__})."], 422) from None

    # Validate prior/before consistency when prior values were supplied.
    before_rows: list[dict[str, Any]] = []
    before_problems = pw.Desk._before(action, got, False, before_rows)
    if before_problems:
        raise pw.Refused(before_problems, 422)

    # Classify safety and enforce confirm-only / 0 automatic on class B.
    safety_class, safety_reasons = classify_safety(
        action, item=item, rules=rules, workflow=workflow, known_domains=known_domains
    )
    ready = readiness_of(system, state=state, environ=env, readiness_map=readiness_map, now=now)
    proposal_only = bool(request_preview.get("proposal_only"))
    if safety_class == "class_b":
        confirm_only = True
        auto_allowed = False
    else:
        confirm_only = bool(proposal_only or ready["dry_run_only"])
        auto_allowed = bool(not confirm_only and ready["status"] == "ready-live")

    before, after, diff = _build_before_after(action, got)
    handle = _target_handle(system, operation, action.get("target") or {}, explicit_handle, got)
    intent_id = _intent_id(owner_id, system, operation, handle, after)

    return {
        "intent_id": intent_id,
        "system": system,
        "action": operation,
        "kind": str(action.get("kind")),
        "target_handle": handle,
        "target": pw._plain(dict(action.get("target") or {})),
        "record_name": (got or {}).get("name"),
        "before": before,
        "after": after,
        "diff": diff,
        "safety_class": safety_class,
        "safety_reasons": safety_reasons,
        "confirm_only": confirm_only,
        "auto_allowed": auto_allowed,
        "mode": "dry_run",
        "readiness": ready,
        "proposal_only": proposal_only,
        "reason": req["reason"],
        "request_preview": pw._plain(request_preview),
        "undo_note": adapter.undo_note(),
        "undo_preview": pw._plain(undo_preview) if undo_preview is not None else None,
        "prepared_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
    }


def enforce_execution_policy(
    intent: Mapping[str, Any],
    *,
    auto: bool = False,
    confirmed_by: Optional[str] = None,
    known_domains: Iterable[str] = (),
) -> None:
    """Fail-closed execution policy check.

    * Refuses automatic execution (``auto=True``) on ANY class B, money-bearing,
      confirm-only, or dry-run-only intent (``0 automatic on class B``).
    * Re-derives safety classification from the intent's system, action, and after fields
      so a tampered intent cannot bypass confirm-only protection.
    * Refuses live execution without a named ``confirmed_by`` when ``confirm_only`` holds.
    """
    system = str(intent.get("system") or "").strip().lower()
    operation = str(intent.get("action") or intent.get("operation") or "").strip()
    after = intent.get("after") if isinstance(intent.get("after"), Mapping) else {}
    recheck_action = {
        "system": system,
        "operation": operation,
        "kind": intent.get("kind") or aw.WRITE_SYSTEMS.get(system, ""),
        "payload": after,
        "target": intent.get("target") or {},
    }
    derived_class, _ = classify_safety(recheck_action, known_domains=known_domains)
    declared_class = str(intent.get("safety_class") or "")
    effective_class = "class_b" if (derived_class == "class_b" or declared_class != "class_a") else "class_a"

    if auto:
        if effective_class != "class_a":
            raise pw.Refused([
                f"Automatic execution is forbidden on {effective_class} writes "
                f"({system}.{operation} is confirm-only)."
            ], 403)
        if intent.get("confirm_only") is not False or intent.get("auto_allowed") is not True:
            raise pw.Refused([
                f"Automatic execution is forbidden: {system}.{operation} requires confirmation."
            ], 403)
        readiness_info = intent.get("readiness") if isinstance(intent.get("readiness"), Mapping) else {}
        if readiness_info.get("dry_run_only") is not False or readiness_info.get("status") != "ready-live":
            raise pw.Refused([
                f"Automatic execution is forbidden: {system} adapter is not ready-live."
            ], 403)
    else:
        if (effective_class == "class_b" or intent.get("confirm_only")) and not str(confirmed_by or "").strip():
            raise pw.Refused([
                f"Explicit confirmation by a named person is required for {system}.{operation}."
            ], 403)


def prepare_session_writes(
    proposals: Sequence[Mapping[str, Any]],
    owner: pw.Person | str,
    **kwargs: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Translate a prepared session's system write proposals into ``(intents, refusals)``.

    Non-system proposals (e.g. ``mail`` or ``teams`` drafts) are skipped here because they are
    handled by the message draft path. Any refusal is returned in ``refusals`` with its plain
    reason rather than dropping silently or crashing the session.
    """
    intents: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    for raw in proposals:
        if not isinstance(raw, Mapping):
            continue
        args = raw.get("arguments") if isinstance(raw.get("arguments"), Mapping) else raw
        system = str((args or {}).get("system") or "").strip().lower()
        if system in pw.DRAFT_SYSTEMS:
            continue
        try:
            intents.append(build_write_intent(raw, owner, **kwargs))
        except pw.Refused as exc:
            refusals.append({
                "system": system,
                "action": str((args or {}).get("operation") or (args or {}).get("action") or ""),
                "problems": list(exc.problems),
                "status": exc.status,
            })
    return intents, refusals
