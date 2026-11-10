"""Autopilot write path: one executor that performs an autopilot's prepared item in the
owning system, once the item's workflow passed its write gate and the owner switched it on.

Everything here ships OFF. An item is performed live only when every one of these holds,
checked in this order (the first that fails decides the outcome; every outcome is a row
of the audit log):

1. **Kill switches**: the global one (``KILL_ENV`` or ``KILL_FILE`` in the state dir, and
   the autopilot's own ``PLX_AUTOPILOT_OFF``/``autopilot.off``) and the per-workflow one
   (``WORKFLOW_KILL_ENV`` or ``WORKFLOW_KILL_FILE``): ``killed``.
2. **Rules** (``RULES_FILE``, ``load_rules``): a rules document this code does not
   understand refuses everything (``refused``).
3. **Action** (``actions_of``): the item's prepared write spec, MC approval or drafted
   message, as one or more actions of a known kind; anything else is ``refused``.
4. **People exclusion**: the owner and every address the action reaches or acts for are
   cleared by the people-exclusion guard; an unavailable guard, an excluded person or an
   undecidable tenant identity refuses it (``excluded``).
5. **Routine only** (``routine``): never money, prices, credit, permissions, migrations,
   production changes, new customers or vendors, legal or people matters, an incomplete
   or judgment-call item, a workflow the history shows people split on, or any kind,
   operation or MC scope the rules do not list (``not_routine``).
6. **Write gate** (``write_state``): the workflow passed >= 90% on >= 15 judged items on
   fresh held-out days (``record_scores``), the owner was told once
   (``gate_passed`` -> ``owner_notified`` rows), and the owner's live flag names it
   (``LIVE_ENV`` and ``LIVE_FILE``) with the master switch ``MASTER_ENV=1``. Otherwise the
   action is recorded as a ``dry_run`` with the exact request it would send.
7. **Rate limit** per workflow (hour and day, live attempts in the audit log):
   ``rate_limited``.
8. **Idempotency**: the key is the digest of the item, kind, target and exact payload; a
   key already ``done``, ``failed``, ``pending`` or ``unknown`` is never sent again
   (``duplicate``; ``pending``/``unknown`` wait for a person to reconcile).
9. **Adapter** (``operating.autopilot_write_adapters``): ``unavailable`` when the system
   has no write transport here; otherwise a ``pending`` row, the one request, and the
   outcome row with the inverse request where the system can undo (``undo``).

The audit log (``AUDIT_FILE``) is the hash-chained append-only journal of
``card_actions.Journal``: what, where, why, citations, result, key, mode. Nothing in this
module sends HTTP; adapters receive injected transports.

CLI (no flag is ever set by it)::

    python3 -m operating.autopilot_write status --state DIR
    python3 -m operating.autopilot_write record-scores --state DIR --run OUT_RUN
    python3 -m operating.autopilot_write notices --state DIR
    python3 -m operating.autopilot_write ack-notice --state DIR --role R --workflow W
    python3 -m operating.autopilot_write run --state DIR --queue QUEUE [--item ID]

Evidence label of tests: ``injected-transport``; live-unverified.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

from operating import autopilot as ap
from operating import autopilot_write_adapters as wa
from operating.card_actions import Journal

RULES_FILE = Path(__file__).with_name("autopilot_write_rules.json")
RULES_SCHEMA = "plx-autopilot-write-rules/v1"
WRITE_GATE_FILE = "autopilot-write-gate.json"
WRITE_GATE_SCHEMA = "plx-autopilot-write-gate/v1"
AUDIT_FILE = "autopilot-writes/audit.jsonl"
MASTER_ENV = "PLX_AUTOPILOT_WRITES"              # "1": the owner's master switch
LIVE_ENV = "PLX_AUTOPILOT_WRITES_LIVE"           # "role:workflow,..." the owner switched on
LIVE_FILE = "autopilot-writes-live.json"         # {"live": ["role:workflow", ...]}
KILL_ENV = "PLX_AUTOPILOT_WRITES_OFF"
KILL_FILE = "autopilot-writes.off"
WORKFLOW_KILL_ENV = "PLX_AUTOPILOT_WRITES_OFF_WORKFLOWS"
WORKFLOW_KILL_FILE = "autopilot-writes-off.json"  # {"workflows": [...]}
KINDS = ("mc_approval", "send_mail", "send_teams", "bc_write", "sage_write", "portal_write",
         "filemaker_write")
WRITE_SYSTEMS = {"bc": "bc_write", "sage": "sage_write", "portal": "portal_write",
                 "filemaker": "filemaker_write"}
SETTLED = frozenset({"done", "failed"})
UNSURE = frozenset({"pending", "unknown"})
_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
_SERVICE = re.compile(r"^(?:sp_|agent:|svc[_:-]|service[_:-])", re.IGNORECASE)
_ROLE_FLOW = re.compile(r"^[a-z0-9_:@.-]{1,120}$")


class RulesInvalid(ValueError):
    """The rules document is not one this code understands: nothing is performed."""


# ------------------------------------------------------------------ rules

def load_rules(path: Path | None = None) -> dict[str, Any]:
    """The routine rules, validated: schema, gate numbers, every action kind known, every
    term class an action names defined and compilable."""
    try:
        raw = json.loads(Path(path or RULES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RulesInvalid(f"unreadable: {type(error).__name__}") from None
    if not isinstance(raw, dict) or raw.get("schema") != RULES_SCHEMA:
        raise RulesInvalid("schema")
    try:
        gate = raw["gate"]
        precision, minimum = Decimal(str(gate["precision"])), int(gate["min_judged"])
        # never looser than the product gate (90% on 15)
        if precision < ap.GATE_PRECISION or minimum < ap.GATE_MIN_JUDGED:
            raise RulesInvalid("gate looser than the product gate")
        _instant(raw["fresh_from"])
        classes = {name: [re.compile(rx, re.IGNORECASE) for rx in rxs]
                   for name, rxs in raw["term_classes"].items()}
        for kind, spec in raw["actions"].items():
            if kind not in KINDS:
                raise RulesInvalid(f"unknown action kind {kind}")
            for name in spec.get("deny_term_classes") or ():
                if name not in classes:
                    raise RulesInvalid(f"unknown term class {name}")
        limits = raw["rate_limits"]
        int(limits["per_hour"]), int(limits["per_day"])
        [str(f).lower() for f in raw["deny_fields"]]
        list(raw.get("contested_workflows") or ())
    except RulesInvalid:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, re.error) as error:
        raise RulesInvalid(f"malformed: {type(error).__name__}") from None
    return {**raw, "_classes": classes}


# ------------------------------------------------------------------ actions

def _norm_lines(payload: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    for nav in ("salesOrderLines", "purchaseOrderLines"):
        if nav in out and "lines" not in out:
            out["lines"] = out.pop(nav)
    return out


def actions_of(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The actions an item's prepared work stands for, in the order they run (an entry
    before the message that confirms it). Unknown shapes give ``{"kind": "unknown"}``."""
    work = (item.get("body") or {}).get("prepared") or {}
    happened = (item.get("body") or {}).get("happened") or {}
    out: list[dict[str, Any]] = []
    act = work.get("action")
    if isinstance(act, Mapping):
        if act.get("kind") == "mc_approval":
            out.append({"kind": "mc_approval", "system": "mc", "operation": "approval_decide",
                        "target": {"task_id": act.get("task_id"), "gate_id": act.get("gate")},
                        "payload": {"scope": act.get("scope"),
                                    "note": "Routine approval by the owner's autopilot."},
                        "people": [act.get("requested_by")],
                        "recommendation": work.get("recommendation")})
        else:
            out.append({"kind": "unknown", "detail": str(act.get("kind"))[:40]})
    spec = work.get("write")
    if isinstance(spec, Mapping):
        kind = WRITE_SYSTEMS.get(str(spec.get("system") or ""), "unknown")
        out.append({"kind": kind, "system": spec.get("system"),
                    "operation": spec.get("operation"),
                    "target": dict(spec.get("target") or {}),
                    "payload": _norm_lines(spec.get("payload") or {}),
                    "precondition": spec.get("precondition"),
                    "reason": spec.get("reason")})
    draft = work.get("draft")
    if isinstance(draft, Mapping) and draft.get("channel") in ("email", "mail"):
        to = [t.get("address") if isinstance(t, Mapping) else t for t in draft.get("to") or ()]
        cc = [t.get("address") if isinstance(t, Mapping) else t for t in draft.get("cc") or ()]
        out.append({"kind": "send_mail", "system": "graph", "operation": "send_email",
                    "target": {"sender": item.get("owner")},
                    "payload": {"to": [a for a in to if a], "cc": [a for a in cc if a],
                                "subject": draft.get("subject"), "body": draft.get("body")},
                    "unknown_domains": list(draft.get("unknown_domains") or ())})
    elif isinstance(draft, Mapping) and draft.get("channel") == "teams":
        out.append({"kind": "send_teams", "system": "graph", "operation": "teams_message",
                    "target": {"chat_id": draft.get("chat_id")},
                    "payload": {"chat_id": draft.get("chat_id"), "body": draft.get("body")},
                    "people": list(draft.get("members") or ())})
    elif isinstance(draft, Mapping) and draft and "choice" not in draft:
        out.append({"kind": "unknown", "detail": f"draft:{str(draft.get('channel'))[:20]}"})
    for a in out:
        # the person the item answers is acted on too: cleared like every recipient
        a["people"] = [p for p in [*(a.get("people") or ()), happened.get("sender")] if p]
    return out


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def idempotency_key(item: Mapping[str, Any], action: Mapping[str, Any]) -> str:
    """The same item, kind, target and exact payload always give the same key."""
    body = {"item": item.get("item_id"), "kind": action.get("kind"),
            "operation": action.get("operation"), "target": action.get("target"),
            "payload": action.get("payload")}
    return "aw_" + hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()[:32]


# ------------------------------------------------------------------ routine

def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)
    elif value is not None:
        yield str(value)


def _keys(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for k, v in value.items():
            yield str(k)
            yield from _keys(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _keys(v)


def routine(rules: Mapping[str, Any], item: Mapping[str, Any], action: Mapping[str, Any],
            workflow: str, known_domains: Iterable[str] = ()) -> list[str]:
    """Why the action is not routine ([]: routine). Conservative: anything the rules do not
    list, any term class or deny field that matches, an incomplete or judgment-call item."""
    kind = action.get("kind")
    spec = (rules.get("actions") or {}).get(kind)
    if not spec:
        return [f"kind_not_routine:{kind}"]
    why: list[str] = []
    work = (item.get("body") or {}).get("prepared") or {}
    happened = (item.get("body") or {}).get("happened") or {}
    if workflow in set(rules.get("contested_workflows") or ()):
        why.append("contested_workflow")
    if work.get("gaps"):
        why.append("incomplete:gaps")
    if work.get("recommendation") not in (None, "approve") and kind != "mc_approval":
        why.append(f"judgment_call:{work.get('recommendation')}")
    text = "\n".join([str(item.get("title") or ""), str(work.get("summary") or ""),
                      str(happened.get("subject") or ""),
                      str((happened.get("fields") or {}).get("reason") or ""),
                      str((happened.get("fields") or {}).get("title") or ""),
                      str(action.get("reason") or ""), *_strings(action.get("payload"))])
    for name in spec.get("deny_term_classes") or ():
        if any(rx.search(text) for rx in rules["_classes"][name]):
            why.append(f"term:{name}")
    if kind == "mc_approval":
        scope = (action.get("payload") or {}).get("scope") or {}
        if not isinstance(scope, Mapping) or scope.get("kind") not in spec.get("scope_kind_in", ()):
            why.append(f"mc_scope_not_routine:{(scope or {}).get('kind') if isinstance(scope, Mapping) else None}")
        if action.get("recommendation") not in spec.get("recommendation_in", ()):
            why.append(f"no_precedent:{action.get('recommendation')}")
    elif kind in ("send_mail", "send_teams"):
        payload = action.get("payload") or {}
        n = len(payload.get("to") or ()) + len(payload.get("cc") or ())
        if kind == "send_mail" and not 1 <= n <= int(spec.get("max_recipients", 1)):
            why.append(f"recipients:{n}")
        if spec.get("known_domains_only"):
            # a counterpart already in the conversation or a tenant domain; a new one is a
            # judgment call
            known = {d.lower() for d in known_domains}
            sender = str(happened.get("sender") or "")
            if "@" in sender:
                known.add(sender.rsplit("@", 1)[1].lower())
            new = [a for a in [*(payload.get("to") or ()), *(payload.get("cc") or ())]
                   if "@" not in str(a) or str(a).rsplit("@", 1)[1].lower() not in known]
            if new or action.get("unknown_domains"):
                why.append("unknown_domain")
    else:
        ops = spec.get("operations") or {}
        op = ops.get(str(action.get("operation") or ""))
        if op is None:
            why.append(f"operation_not_routine:{action.get('operation')}")
        else:
            need = op.get("require_precondition")
            if need and not (action.get("precondition") or {}).get(need):
                why.append(f"precondition_missing:{need}")
            lines = (action.get("payload") or {}).get("lines") or ()
            if len(lines) > int(op.get("max_lines", 0) or 10 ** 6):
                why.append(f"lines:{len(lines)}")
        deny = [f.lower() for f in rules.get("deny_fields") or ()]
        hits = sorted({k for k in _keys(action.get("payload")) if any(d in k.lower() for d in deny)})
        if hits:
            why.append("field:" + ",".join(hits[:5]))
    return why


# ------------------------------------------------------------------ people

def excluded(guard: Any, owner: str, people: Iterable[str], internal: Iterable[str] = ()
             ) -> str | None:
    """Why the action may not run for these people (None: cleared). The owner must be
    decided and not excluded; every other address must not be excluded, and an address on
    a tenant domain the guard cannot decide is refused (fail closed)."""
    if guard is None or getattr(guard, "unavailable", "unavailable"):
        return "exclusion_unavailable"
    domains = {d.lower() for d in internal}
    known = getattr(guard, "known_domains", None)
    try:
        if callable(known):
            domains |= {str(d).lower() for d in known()}
        if not owner or not guard.decides(owner) or guard.is_excluded(owner):
            return "owner_not_cleared"
        if "@" in owner:
            domains.add(owner.rsplit("@", 1)[1].lower())
        for person in dict.fromkeys(str(p).lower() for p in people if p):
            if _SERVICE.match(person) and "@" not in person:
                continue                       # an MC service principal, not a person
            if guard.decides(person):
                if guard.is_excluded(person):
                    return "person_excluded"
            elif "@" not in person or person.rsplit("@", 1)[1] in domains:
                return "person_undecided"
    except Exception:  # noqa: BLE001 - a guard that cannot answer clears nobody
        return "exclusion_error"
    return None


def _addresses(item: Mapping[str, Any], action: Mapping[str, Any]) -> list[str]:
    """Everyone the action reaches or names: its people, and every address in its
    payload, reason and the item's title and summary."""
    work = (item.get("body") or {}).get("prepared") or {}
    found = list(action.get("people") or ())
    for text in [*_strings(action.get("payload")), str(action.get("reason") or ""),
                 str(item.get("title") or ""), str(work.get("summary") or "")]:
        found += _EMAIL.findall(text)
    return [a.lower() for a in found if isinstance(a, str)]


# ------------------------------------------------------------------ the write gate

def _instant(value: str) -> datetime:
    out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if out.tzinfo is None:
        raise ValueError("naive instant")
    return out


def record_scores(run_dir: Path, gate_path: Path, rules: Mapping[str, Any]) -> dict[str, Any]:
    """Admit a fresh-score run (``/data/scratch/autopilot-6/fresh_score.sh`` OUT_RUN) into
    the write gate: per role, the replay's held-out counts (``autopilot-gate.json``) when
    that role's ``result.json`` is complete and its window starts at or after the rules'
    ``fresh_from`` (days never tuned on). Anything else is listed in ``refused``."""
    run_dir = Path(run_dir)
    fresh_from = _instant(rules["fresh_from"])
    counts = json.loads((run_dir / ap.GATE_FILE).read_text(encoding="utf-8"))
    if counts.get("schema") != ap.GATE_SCHEMA:
        raise ValueError("not a replay gate file")
    results = {}
    for p in sorted(run_dir.glob("*/result.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        results[str(r.get("role"))] = r
    try:
        doc = json.loads(gate_path.read_text(encoding="utf-8"))
        if doc.get("schema") != WRITE_GATE_SCHEMA:
            doc = {}
    except (OSError, ValueError):
        doc = {}
    roles = dict(doc.get("roles") or {})
    admitted, refused = [], {}
    for role, entry in (counts.get("roles") or {}).items():
        r = results.get(role)
        if r is None:
            refused[role] = "no_result"
            continue
        if r.get("status") != "complete":
            refused[role] = f"status:{r.get('status')}"
            continue
        try:
            start = _instant(r["window"][0])
        except (KeyError, IndexError, ValueError, TypeError):
            refused[role] = "window_unreadable"
            continue
        if start < fresh_from:
            refused[role] = "not_fresh"
            continue
        roles[role] = {"members": list(entry.get("members") or ()),
                       "workflows": {f: {"judged": int(v["judged"]), "matched": int(v["matched"])}
                                     for f, v in (entry.get("workflows") or {}).items()},
                       "window": list(r["window"]), "run": run_dir.name}
        admitted.append(role)
    tmp = gate_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"schema": WRITE_GATE_SCHEMA, "roles": roles}, indent=1,
                              sort_keys=True), encoding="utf-8")
    os.replace(tmp, gate_path)
    return {"admitted": sorted(admitted), "refused": refused}


class WriteGate:
    """The write gate read from ``WRITE_GATE_FILE``: per role, members and the fresh
    held-out counts; the verdict is recomputed from the counts (``rules.gate``) and the
    window re-checked against ``fresh_from`` at every read. Missing or unreadable: nothing
    passes."""

    def __init__(self, path: Path, rules: Mapping[str, Any]) -> None:
        precision = Decimal(str(rules["gate"]["precision"]))
        minimum = int(rules["gate"]["min_judged"])
        fresh_from = _instant(rules["fresh_from"])
        self.roles: dict[str, tuple[set[str], set[str], set[str]]] = {}
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            if raw.get("schema") != WRITE_GATE_SCHEMA:
                raise ValueError("schema")
            for name, role in (raw.get("roles") or {}).items():
                if _instant(role["window"][0]) < fresh_from:
                    continue
                ok, seen = set(), set()
                for flow, c in (role.get("workflows") or {}).items():
                    judged, matched = int(c["judged"]), int(c["matched"])
                    seen.add(str(flow))
                    if judged >= minimum and 0 <= matched <= judged and \
                            Decimal(matched) / Decimal(judged) >= precision:
                        ok.add(str(flow))
                self.roles[str(name)] = ({str(m).lower() for m in role.get("members") or ()},
                                         ok, seen)
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            self.roles = {}
        # the product gate's semantics over our verdicts (a workflow our stricter rules or
        # the freshness check refuse is passed in as measured and failed)
        self._gate, _ = ap.gate_of({"schema": ap.GATE_SCHEMA, "roles": {
            n: {"members": sorted(m), "workflows": {
                f: {"judged": ap.GATE_MIN_JUDGED, "matched": ap.GATE_MIN_JUDGED if f in ok else 0}
                for f in seen}}
            for n, (m, ok, seen) in self.roles.items()}})

    def passing(self) -> list[tuple[str, str]]:
        return sorted((r, f) for r, (_m, ok, _s) in self.roles.items() for f in ok)

    def roles_for(self, owner: str, workflow: str, roles: Iterable[str]) -> list[str]:
        """The roles through which ``workflow`` passes for ``owner`` under the product
        gate's semantics (``autopilot.gate_of``); [] when it does not pass."""
        owner, roles = str(owner).lower(), set(roles)
        mine = [n for n, (m, ok, _s) in self.roles.items() if owner in m and workflow in ok]
        if not mine or not self._gate(owner, workflow, roles):
            return []
        personal = [n for n in mine if n.startswith("person:")]
        return personal or ([n for n in mine if n in roles] if roles else mine)


def sync_gate(journal: Journal, gate: WriteGate, now: datetime) -> list[dict[str, Any]]:
    """Record each (role, workflow) whose gate state changed: ``gate_passed`` the first time
    it passes (the owner is then told once, ``owner_notified``), ``gate_lost``
    when a later fresh score no longer passes it (live stops at once)."""
    passing = set(gate.passing())
    added = []
    with journal.locked() as held:
        state: dict[tuple[str, str], str] = {}
        for row in held.rows:
            if row.get("event") in ("gate_passed", "gate_lost"):
                state[(row["role"], row["workflow"])] = row["event"]
        for key in sorted(passing | {k for k, v in state.items() if v == "gate_passed"}):
            now_passes = key in passing
            last = state.get(key)
            if now_passes and last != "gate_passed":
                added.append(held.append({"event": "gate_passed", "role": key[0],
                                          "workflow": key[1], "at": _iso(now),
                                          "status": "passed, awaiting owner notice"}))
            elif not now_passes and last == "gate_passed":
                added.append(held.append({"event": "gate_lost", "role": key[0],
                                          "workflow": key[1], "at": _iso(now)}))
    return added


def notices(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Gate passes the owner has not been told of yet (the owner gets one message each,
    then ``ack_notice``)."""
    pending: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        key = (row.get("role"), row.get("workflow"))
        if row.get("event") == "gate_passed":
            pending[key] = row
        elif row.get("event") in ("owner_notified", "gate_lost"):
            pending.pop(key, None)
    return [{"role": r["role"], "workflow": r["workflow"], "passed_at": r["at"],
             "status": r["status"]} for r in pending.values()]


def ack_notice(journal: Journal, role: str, workflow: str, now: datetime, by: str
               ) -> dict[str, Any]:
    with journal.locked() as held:
        if not any(n["role"] == role and n["workflow"] == workflow for n in notices(held.rows)):
            raise ValueError("no pending notice for this role and workflow")
        return held.append({"event": "owner_notified", "role": role, "workflow": workflow,
                            "at": _iso(now), "by": str(by)[:120],
                            "status": "passed, owner notified; live when the owner's flag is set"})


def _notified(rows: Sequence[Mapping[str, Any]], role: str, workflow: str) -> bool:
    told = False
    for row in rows:
        if row.get("role") == role and row.get("workflow") == workflow:
            if row.get("event") == "owner_notified":
                told = True
            elif row.get("event") in ("gate_passed", "gate_lost"):
                told = False
    return told


def live_flags(environ: Mapping[str, str], state: Path) -> set[str]:
    names = {p.strip().lower() for p in (environ.get(LIVE_ENV) or "").split(",") if p.strip()}
    try:
        raw = json.loads((state / LIVE_FILE).read_text(encoding="utf-8"))
        names |= {str(p).strip().lower() for p in raw.get("live") or ()}
    except (OSError, ValueError, AttributeError):
        pass
    return {n for n in names if _ROLE_FLOW.fullmatch(n)}


def write_state(gate: WriteGate, rows: Sequence[Mapping[str, Any]], environ: Mapping[str, str],
                state: Path, owner: str, workflow: str, roles: Iterable[str]
                ) -> tuple[str, list[str]]:
    """``live`` or ``dry_run`` and what is missing for live."""
    through = gate.roles_for(owner, workflow, roles)
    if not through:
        return "dry_run", ["gate_not_passed"]
    told = [r for r in through if _notified(rows, r, workflow)]
    if not told:
        return "dry_run", ["passed_awaiting_owner_notice"]
    flags = live_flags(environ, state)
    if not any(f"{r}:{workflow}".lower() in flags for r in told):
        return "dry_run", ["owner_flag_off"]
    if environ.get(MASTER_ENV) != "1":
        return "dry_run", [f"master_switch_off:{MASTER_ENV}"]
    return "live", []


# ------------------------------------------------------------------ switches, rates

def killed(environ: Mapping[str, str], state: Path, workflow: str) -> str | None:
    if environ.get(KILL_ENV) or environ.get(ap.KILL_ENV):
        return "global_kill_switch"
    if (state / KILL_FILE).exists() or (state / ap.KILL_FILE).exists():
        return "global_kill_switch"
    off = {w.strip() for w in (environ.get(WORKFLOW_KILL_ENV) or "").split(",") if w.strip()}
    path = state / WORKFLOW_KILL_FILE
    if path.exists():
        try:
            off |= {str(w) for w in json.loads(path.read_text(encoding="utf-8"))["workflows"]}
        except (OSError, ValueError, KeyError, TypeError):
            return "workflow_kill_file_unreadable"
    return "workflow_kill_switch" if workflow in off else None


def rate_limited(rules: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], workflow: str,
                 now: datetime) -> str | None:
    limits = rules["rate_limits"]
    own = (limits.get("workflows") or {}).get(workflow) or {}
    per_hour = int(own.get("per_hour", limits["per_hour"]))
    per_day = int(own.get("per_day", limits["per_day"]))
    starts = [_instant(r["at"]) for r in rows if r.get("event") == "attempt"
              and r.get("workflow") == workflow]
    if sum(1 for t in starts if now - t < timedelta(hours=1)) >= per_hour:
        return f"rate_limited:{per_hour}/hour"
    if sum(1 for t in starts if now - t < timedelta(days=1)) >= per_day:
        return f"rate_limited:{per_day}/day"
    return None


def _outcomes(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows:
        if row.get("event") in ("attempt", "outcome", "reconciled") and row.get("key"):
            out[row["key"]] = row["status"]
    return out


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ the executor

class Executor:
    """Performs autopilot items (module doc). ``adapters``: kind -> adapter; ``guard``: the
    people-exclusion guard; ``registry``: the autopilot registry (for an item's roles)."""

    def __init__(self, state: Path, *, environ: Mapping[str, str] | None = None,
                 adapters: Mapping[str, wa.Adapter] | None = None, guard: Any = None,
                 rules_path: Path | None = None, registry: Mapping[str, Any] | None = None,
                 internal_domains: Iterable[str] = ()) -> None:
        self.state = Path(state)
        self.environ = dict(os.environ if environ is None else environ)
        self.adapters = dict(adapters if adapters is not None
                             else wa.default_adapters(self.environ))
        self.guard = guard
        self.rules_path = rules_path
        self.registry = registry if registry is not None else ap.load_registry()
        self.internal = list(internal_domains)
        self.journal = Journal(self.state / AUDIT_FILE)

    def _known_domains(self) -> set[str]:
        from operating.card_actions import KNOWN_DOMAINS_ENV, INTERNAL_DOMAINS_ENV
        out = {d.lower() for d in self.internal}
        for name in (KNOWN_DOMAINS_ENV, INTERNAL_DOMAINS_ENV):
            out |= {d.strip().lower().lstrip("@") for d in
                    str(self.environ.get(name) or "").split(",") if d.strip()}
        known = getattr(self.guard, "known_domains", None)
        try:
            out |= {str(d).lower() for d in known()} if callable(known) else set()
        except Exception:  # noqa: BLE001 - no directory: only the configured domains
            pass
        return out

    def _spec(self, autopilot: str) -> Mapping[str, Any] | None:
        for spec in self.registry.get("autopilots") or ():
            if spec.get("id") == autopilot:
                return spec
        return None

    def run(self, item: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
        """Every action of ``item``, in order; stops after the first one not ``done`` or
        ``dry_run`` (a confirmation never goes out for an entry that did not happen)."""
        from operating import workflows as wf
        happened = (item.get("body") or {}).get("happened") or {}
        workflow = wf.of_item(str(item.get("autopilot")), happened)
        base = {"item_id": item.get("item_id"), "autopilot": item.get("autopilot"),
                "owner": str(item.get("owner") or "").lower(), "workflow": workflow,
                "citations": [c for c in item.get("citations") or () if isinstance(c, str)][:20],
                "why": str((item.get("body") or {}).get("label") or item.get("title") or "")[:300]}
        state_dir = self.state
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stop = killed(self.environ, state_dir, workflow)
        if not stop and self._spec(str(item.get("autopilot"))) is None:
            stop = "unknown_autopilot"          # no registry entry: no roles, no gate
        try:
            rules = load_rules(self.rules_path)
        except RulesInvalid as error:
            rules, stop = None, stop or f"rules_invalid:{error}"
        actions = actions_of(item)
        if not actions:
            return [self._record({**base, "status": "refused", "reasons": ["no_action"]}, now)]
        results = []
        for action in actions:
            result = self._one(base, item, action, rules, stop, now)
            results.append(result)
            if result["status"] not in ("done", "dry_run"):
                break
        return results

    def _record(self, row: dict[str, Any], now: datetime, event: str = "decision"
                ) -> dict[str, Any]:
        with self.journal.locked() as held:
            return held.append({"event": event, "at": _iso(now), **row})

    def _one(self, base: Mapping[str, Any], item: Mapping[str, Any],
             action: Mapping[str, Any], rules: Mapping[str, Any] | None, stop: str | None,
             now: datetime) -> dict[str, Any]:
        kind = str(action.get("kind"))
        key = idempotency_key(item, action)
        row = {**base, "kind": kind, "system": action.get("system"),
               "operation": action.get("operation"), "target": action.get("target"),
               "key": key}
        if stop:
            return self._record({**row, "status": "killed" if "kill" in stop else "refused",
                                 "reasons": [stop]}, now)
        assert rules is not None
        if kind not in KINDS:
            return self._record({**row, "status": "refused",
                                 "reasons": [f"unknown_action:{action.get('detail', kind)}"]}, now)
        why = excluded(self.guard, base["owner"], _addresses(item, action), self.internal)
        if why:
            return self._record({**row, "status": "excluded", "reasons": [why]}, now)
        not_routine = routine(rules, item, action, base["workflow"], self._known_domains())
        if not_routine:
            return self._record({**row, "status": "not_routine", "reasons": not_routine}, now)
        adapter = self.adapters.get(kind)
        if adapter is None:
            return self._record({**row, "status": "unavailable",
                                 "reasons": ["no_adapter"]}, now)
        try:
            request = adapter.request(action)
        except wa.AdapterRefused as error:
            return self._record({**row, "status": "refused", "reasons": error.problems[:8]}, now)
        row["request"] = _journal_safe(request)
        row["undo_note"] = adapter.undo_note()
        gate = WriteGate(self.state / WRITE_GATE_FILE, rules)
        with self.journal.locked() as held:
            mode, missing = write_state(gate, held.rows, self.environ, self.state,
                                        base["owner"], base["workflow"],
                                        ap.item_roles(self._spec(str(item.get("autopilot")))))
            if mode != "live":
                return held.append({"event": "decision", "at": _iso(now), **row,
                                    "mode": "dry_run", "status": "dry_run",
                                    "reasons": missing})
            prior = _outcomes(held.rows).get(key)
            if prior in SETTLED or prior in UNSURE:
                return held.append({"event": "decision", "at": _iso(now), **row,
                                    "mode": "live", "status": "duplicate",
                                    "reasons": [f"already:{prior}"]})
            limited = rate_limited(rules, held.rows, base["workflow"], now)
            if limited:
                return held.append({"event": "decision", "at": _iso(now), **row,
                                    "mode": "live", "status": "rate_limited",
                                    "reasons": [limited]})
            unavailable = adapter.unavailable()
            if unavailable:
                return held.append({"event": "decision", "at": _iso(now), **row,
                                    "mode": "live", "status": "unavailable",
                                    "reasons": [unavailable]})
            # the attempt is on the log before the request leaves: a crash after it reads
            # as pending, which blocks a repeat until a person reconciles it
            held.append({"event": "attempt", "at": _iso(now), **row, "mode": "live",
                         "status": "pending"})
        result = adapter.perform(request, key)
        status = result.get("status")
        if status not in ("done", "failed", "unknown", "not_performed"):
            status = "unknown"
        inverse = adapter.inverse(request, result) if status == "done" else None
        out = {**row, "mode": "live", "status": status,
               "http_status": result.get("http_status"),
               "reasons": list(result.get("missing") or ()) + (
                   [str(result["detail"])] if result.get("detail") else []),
               "inverse": _journal_safe(inverse) if inverse else None}
        # ``not_performed``: the sender answered a dry run, nothing left; the key stays free
        return self._record(out, now, event="outcome")

    def undo(self, key: str, now: datetime) -> dict[str, Any]:
        """Perform the recorded inverse of a ``done`` action once (its own key), under the
        same kill switch. Refused when the system has no undo or it was already undone."""
        rows = self.journal.rows()
        done = [r for r in rows if r.get("event") == "outcome" and r.get("key") == key
                and r.get("status") == "done"]
        if not done:
            raise ValueError("no done action with this key")
        row = done[-1]
        if not row.get("inverse"):
            raise ValueError("the system has no undo for this action: "
                             + str(row.get("undo_note") or ""))
        stop = killed(self.environ, self.state, str(row.get("workflow")))
        if stop:
            raise ValueError(stop)
        undo_key = "undo_" + key
        if _outcomes(rows).get(undo_key) is not None:
            raise ValueError("already undone or in progress")
        adapter = self.adapters[row["kind"]]
        base = {k: row.get(k) for k in ("item_id", "autopilot", "owner", "workflow", "kind",
                                        "system", "citations")}
        self._record({**base, "key": undo_key, "of": key, "mode": "live",
                      "status": "pending", "request": row["inverse"]}, now, event="attempt")
        result = adapter.perform(row["inverse"], undo_key)
        return self._record({**base, "key": undo_key, "of": key, "mode": "live",
                             "status": result.get("status") if result.get("status") in
                             ("done", "failed", "unknown") else "unknown",
                             "http_status": result.get("http_status")}, now, event="outcome")

    def reconcile(self, key: str, status: str, by: str, now: datetime) -> dict[str, Any]:
        """A person checked the system for an ``unknown``/``pending`` attempt: ``done`` or
        ``failed`` settles the key (never re-sent either way)."""
        if status not in SETTLED:
            raise ValueError("done or failed")
        with self.journal.locked() as held:
            if _outcomes(held.rows).get(key) not in UNSURE:
                raise ValueError("nothing unsure under this key")
            return held.append({"event": "reconciled", "at": _iso(now), "key": key,
                                "status": status, "by": str(by)[:120]})


def _journal_safe(request: Any) -> Any:
    """The request as journalled: never a header that carries a credential."""
    if not isinstance(request, Mapping):
        return request
    out = {k: v for k, v in request.items() if k != "body_json"}
    if isinstance(out.get("headers"), Mapping):
        out["headers"] = {k: v for k, v in out["headers"].items()
                          if k.lower() not in ("authorization", "x-api-key", "cookie")}
    return out


# ------------------------------------------------------------------ status, CLI

def status(state: Path, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Counts and states only: adapters, switches, gate passes and notices, live count."""
    env = dict(os.environ if environ is None else environ)
    rules = load_rules()
    gate = WriteGate(state / WRITE_GATE_FILE, rules)
    journal = Journal(state / AUDIT_FILE)
    rows = journal.rows() if journal.path and journal.path.exists() else []
    return {"schema": "plx-autopilot-write-status/v1",
            "master_switch": env.get(MASTER_ENV) == "1",
            "killed": killed(env, state, "") is not None,
            "live_flags": len(live_flags(env, state)),
            "gate_passing": [f"{r}:{f}" for r, f in gate.passing()],
            "pending_notices": notices(rows),
            "adapters": [a.describe() for a in wa.default_adapters(env).values()],
            "live_writes": sum(1 for r in rows if r.get("event") == "outcome"
                               and r.get("mode") == "live" and r.get("status") == "done"),
            "writes": 0}


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m operating.autopilot_write")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("status", "record-scores", "notices", "ack-notice", "run"):
        s = sub.add_parser(name)
        s.add_argument("--state", default=None,
                       help="state dir (default $PLX_WORKBENCH_STATE_DIR/autopilot-write)")
        if name == "record-scores":
            s.add_argument("--run", required=True)
        if name == "ack-notice":
            s.add_argument("--role", required=True)
            s.add_argument("--workflow", required=True)
            s.add_argument("--by", required=True)
        if name == "run":
            s.add_argument("--queue", required=True)
            s.add_argument("--item", default=None)
    args = p.parse_args(argv)
    base = os.environ.get("PLX_WORKBENCH_STATE_DIR")
    if not args.state and not base:
        p.error("--state or PLX_WORKBENCH_STATE_DIR is needed")
    state = Path(args.state or Path(base) / "autopilot-write")
    now = datetime.now(timezone.utc)
    journal = Journal(state / AUDIT_FILE)
    if args.cmd == "status":
        print(json.dumps(status(state), indent=1, sort_keys=True))
        return 0
    if args.cmd == "record-scores":
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        rules = load_rules()
        out = record_scores(Path(args.run), state / WRITE_GATE_FILE, rules)
        out["recorded"] = len(sync_gate(journal, WriteGate(state / WRITE_GATE_FILE, rules), now))
        print(json.dumps(out, indent=1, sort_keys=True))
        return 0
    if args.cmd == "notices":
        print(json.dumps(notices(journal.rows()), indent=1))
        return 0
    if args.cmd == "ack-notice":
        print(json.dumps(ack_notice(journal, args.role, args.workflow, now, args.by)["status"]))
        return 0
    from operating.card_actions import exclusion_for
    queue = ap.Queue(args.queue)
    try:
        items = [queue.item(args.item)] if args.item else queue.items()
        ex = Executor(state, guard=exclusion_for(os.environ, Path(base) if base else None))
        counts: dict[str, int] = {}
        for item in items:
            if item is None:
                continue
            for r in ex.run(item, now):
                counts[r["status"]] = counts.get(r["status"], 0) + 1
        print(json.dumps({"items": len(items), "outcomes": counts}, sort_keys=True))
    finally:
        queue.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
