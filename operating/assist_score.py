"""Nightly per-kind & per-person Assist score (Fable ASSIST-SPECS §8.5 F10, AS1).

Model-free comparison of prepared first acts in ``assist.matters`` against the
owner's real subsequent acts in ``assist.actions`` (same owner, matching thread
or record handle after ``basis_at``). Pure ``n of m`` counts; no adjectives,
no LLM calls, strictly ``READ ONLY`` against Postgres.

    python3 -m operating.assist_score --state-dir /data/plx-state --days 7
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Iterable, Mapping, Optional, Sequence

MESSAGE_ACT_KINDS = frozenset({"reply", "forward", "new_message", "send_document", "attach", "follow_up"})

_ACTION_KIND_MAP: dict[str, str] = {
    "mail_sent": "reply",
    "teams_sent": "reply",
    "reply": "reply",
    "forward": "forward",
    "new_message": "new_message",
    "send_document": "send_document",
    "attach": "attach",
    "follow_up": "follow_up",
    "approval": "decide",
    "decide": "decide",
    "approve": "decide",
    "question": "ask_person",
    "ask_person": "ask_person",
    "planner_created": "task",
    "task": "task",
    "system_write": "system_write",
    "system_entry": "system_write",
    "sharepoint_edit": "system_write",
}


def canonical_act_kind(raw: Optional[str], *, system: str = "") -> str:
    """Normalize an ``assist.actions.kind`` or ``assist.matters.next_act`` to a canonical kind."""
    k = str(raw or "").strip().lower()
    if not k:
        return ""
    if k in _ACTION_KIND_MAP:
        return _ACTION_KIND_MAP[k]
    if k.startswith(("sage_", "filemaker_", "bc_", "portal_", "sharepoint_")):
        return "system_write"
    if system in ("sage", "filemaker", "bc", "portal", "sharepoint"):
        return "system_write"
    return k


def _norm_addr(v: Any) -> str:
    s = str(v or "").strip()
    if "<" in s and ">" in s:
        inner = s[s.rfind("<") + 1 : s.rfind(">")].strip()
        if "@" in inner:
            s = inner
    return s.lower()


def _parse_recipients(raw: Any) -> list[str]:
    if not raw:
        return []
    items = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple, set)) else []
    out: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        for part in text.replace(";", ",").split(","):
            addr = _norm_addr(part)
            if addr:
                out.add(addr)
    return sorted(out)


def _prepared_first_act(matter: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Extract ``(prepared_kind, prepared_recipients)`` from a matter row."""
    plan = matter.get("plan")
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except (json.JSONDecodeError, TypeError, ValueError):
            plan = {}
    if not isinstance(plan, Mapping):
        plan = {}
    acts = plan.get("acts")
    first: Mapping[str, Any] = (
        acts[0] if isinstance(acts, list) and acts and isinstance(acts[0], Mapping) else {}
    )
    raw_kind = (
        first.get("act")
        or first.get("kind")
        or plan.get("first_act")
        or plan.get("act")
        or plan.get("next_act")
        or matter.get("next_act")
        or ""
    )
    kind = canonical_act_kind(str(raw_kind), system=str(first.get("system") or ""))
    recips = _parse_recipients(
        first.get("to")
        or first.get("recipients")
        or plan.get("recipients")
        or plan.get("to")
    )
    if not recips and matter.get("counterpart"):
        c = _norm_addr(matter.get("counterpart"))
        if "@" in c:
            recips = [c]
    return kind, recips


def _matter_handles(matter: Mapping[str, Any]) -> tuple[str, set[str]]:
    """Return ``(thread_key, record_handles)`` for matching against ``assist.actions``."""
    sources = matter.get("sources")
    if isinstance(sources, str):
        try:
            sources = json.loads(sources)
        except (json.JSONDecodeError, TypeError, ValueError):
            sources = {}
    if not isinstance(sources, Mapping):
        sources = {}
    records = {str(r).strip() for r in (sources.get("records") or []) if str(r).strip()}
    subject = str(matter.get("subject_key") or "").strip()
    kind = str(matter.get("kind") or "").strip()
    thread_key = subject if kind == "conversation" else ""
    if subject and kind in ("record", "task", "approval", "routine"):
        records.add(subject)
    return thread_key, records


def _action_matches_matter(action: Mapping[str, Any], matter: Mapping[str, Any]) -> bool:
    """True if ``action`` is a human act by the matter's owner on the same thread/record after ``basis_at``."""
    if str(action.get("not_human") or "").strip():
        return False
    if _norm_addr(action.get("person")) != _norm_addr(matter.get("owner_person")):
        return False
    at = action.get("at")
    basis_at = matter.get("basis_at")
    if at is not None and basis_at is not None and at < basis_at:
        return False
    thread_key, records = _matter_handles(matter)
    act_thread = str(action.get("thread_key") or "").strip()
    if thread_key and act_thread and act_thread == thread_key:
        return True
    act_target = str(action.get("target_handle") or "").strip()
    if act_target and act_target in records:
        return True
    fields = action.get("fields")
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except (json.JSONDecodeError, TypeError, ValueError):
            fields = {}
    if isinstance(fields, Mapping):
        act_records = {str(r).strip() for r in (fields.get("records") or []) if str(r).strip()}
        if records & act_records:
            return True
    return False


def _bucket() -> dict[str, Any]:
    return {
        "kind_matched": 0,
        "kind_total": 0,
        "recipient_matched": 0,
        "recipient_total": 0,
        "no_real_act_yet": 0,
    }


def _finalize_bucket(b: Mapping[str, int]) -> dict[str, Any]:
    km, kt = b["kind_matched"], b["kind_total"]
    rm, rt = b["recipient_matched"], b["recipient_total"]
    return {
        "kind_matched": km,
        "kind_total": kt,
        "kind_score": f"{km} of {kt}",
        "recipient_matched": rm,
        "recipient_total": rt,
        "recipient_score": f"{rm} of {rt}",
        "no_real_act_yet": b["no_real_act_yet"],
    }


def _owner_excluded(owner: str, excl: set[str], guard: Any) -> bool:
    if owner in excl:
        return True
    if guard is None:
        return False
    if getattr(guard, "unavailable", None):
        return True
    if "@" in owner and not guard.decides(owner):
        return True
    return bool(guard.is_excluded(owner))


def score_matters(
    matters: Iterable[Mapping[str, Any]],
    actions: Iterable[Mapping[str, Any]],
    *,
    excluded_people: Optional[Iterable[str]] = None,
    exclusion_guard: Any = None,
    window_days: int = 7,
    as_of: Optional[datetime] = None,
    not_measured: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Compute per-kind and per-person ``n of m`` Assist scores (model-free)."""
    excl = {_norm_addr(p) for p in (excluded_people or ()) if _norm_addr(p)}
    now = as_of or datetime.now(timezone.utc)
    if exclusion_guard is not None and getattr(exclusion_guard, "unavailable", None):
        reasons = list(not_measured or ())
        reasons.append(f"people_exclusion_unavailable:{exclusion_guard.unavailable}")
        return {
            "schema": "assist.nightly_score.v1",
            "as_of": now.astimezone(timezone.utc).isoformat(),
            "window_days": int(window_days),
            "totals": _finalize_bucket(_bucket()),
            "by_kind": {},
            "by_person": {},
            "unmatched_real_acts": 0,
            "no_real_act_yet": 0,
            "not_measured": reasons,
        }
    actions_list = [a for a in actions if not str(a.get("not_human") or "").strip()]

    by_owner_actions: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for a in actions_list:
        p = _norm_addr(a.get("person"))
        if p and not _owner_excluded(p, excl, exclusion_guard):
            by_owner_actions[p].append(a)
    for p in by_owner_actions:
        by_owner_actions[p].sort(key=lambda x: str(x.get("at") or ""))

    totals = _bucket()
    by_kind: dict[str, dict[str, int]] = defaultdict(_bucket)
    by_person: dict[str, dict[str, int]] = defaultdict(_bucket)
    matched_action_keys: set[str] = set()
    excluded_omitted = 0

    for m in matters:
        owner = _norm_addr(m.get("owner_person"))
        if not owner or _owner_excluded(owner, excl, exclusion_guard):
            if owner and _owner_excluded(owner, excl, exclusion_guard):
                excluded_omitted += 1
            continue
        prep_kind, prep_recips = _prepared_first_act(m)
        if not prep_kind or prep_kind == "nothing":
            continue

        candidates = [
            a for a in by_owner_actions.get(owner, ())
            if _action_matches_matter(a, m)
        ]
        if not candidates:
            totals["no_real_act_yet"] += 1
            by_kind[prep_kind]["no_real_act_yet"] += 1
            by_person[owner]["no_real_act_yet"] += 1
            continue

        real = candidates[0]
        if real.get("action_key"):
            matched_action_keys.add(str(real["action_key"]))
        real_kind = canonical_act_kind(
            str(real.get("kind") or ""),
            system=str(real.get("system") or ""),
        )
        kind_ok = real_kind == prep_kind

        for b in (totals, by_kind[prep_kind], by_person[owner]):
            b["kind_total"] += 1
            if kind_ok:
                b["kind_matched"] += 1

        if prep_kind in MESSAGE_ACT_KINDS and prep_recips:
            real_recips = {_norm_addr(r) for r in (real.get("recipients") or ()) if _norm_addr(r)}
            recip_ok = bool(set(prep_recips) & real_recips)
            for b in (totals, by_kind[prep_kind], by_person[owner]):
                b["recipient_total"] += 1
                if recip_ok:
                    b["recipient_matched"] += 1

    unmatched_real = sum(
        1 for p, acts in by_owner_actions.items()
        for a in acts
        if str(a.get("action_key") or "") not in matched_action_keys
    )

    reasons = list(not_measured or ())
    if excluded_omitted:
        reasons.append(f"excluded_people_omitted:{excluded_omitted}")

    return {
        "schema": "assist.nightly_score.v1",
        "as_of": now.astimezone(timezone.utc).isoformat(),
        "window_days": int(window_days),
        "totals": _finalize_bucket(totals),
        "by_kind": {k: _finalize_bucket(v) for k, v in sorted(by_kind.items())},
        "by_person": {k: _finalize_bucket(v) for k, v in sorted(by_person.items())},
        "unmatched_real_acts": unmatched_real,
        "no_real_act_yet": totals["no_real_act_yet"],
        "not_measured": reasons,
    }


def write_snapshot(out_path: Path, payload: Mapping[str, Any]) -> Path:
    """Atomically write ``0600`` JSON snapshot inside a ``0700`` directory."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(out_path.parent, 0o700)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".nightly-score-", dir=str(out_path.parent))
    try:
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp_name, out_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return out_path


def _load_excluded_guard() -> tuple[Any, Optional[str]]:
    """Fail-closed people exclusion loader using ``integrations.people_exclusion``."""
    try:
        from integrations.people_exclusion import PeopleExclusion, environ_with_exclusion
        guard = PeopleExclusion.from_env(environ_with_exclusion(os.environ))
        if guard.unavailable:
            return None, f"people_exclusion_unavailable:{guard.unavailable}"
        return guard, None
    except Exception:  # noqa: BLE001 - fail closed when exclusion cannot load
        return None, "people_exclusion_load_failed"


def fetch_from_postgres(dsn: str, *, days: int = 7) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Strictly ``READ ONLY`` fetch of ``assist.matters`` and ``assist.actions``."""
    import psycopg
    from psycopg.rows import dict_row

    since = datetime.now(timezone.utc) - timedelta(days=days)
    with psycopg.connect(dsn, row_factory=dict_row, options="-c default_transaction_read_only=on") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT matter_id, owner_person, kind, subject_key, sources, basis_at,
                       next_act, counterpart, state, plan, plan_status
                FROM assist.matters
                WHERE basis_at >= %s
                  AND next_act IS NOT NULL
                  AND next_act <> 'nothing'
                """,
                (since,),
            )
            matters = list(cur.fetchall())
            cur.execute(
                """
                SELECT action_key, person, at, system, kind, target_handle,
                       thread_key, recipients, fields, not_human
                FROM assist.actions
                WHERE at >= %s
                  AND not_human = ''
                ORDER BY at ASC
                """,
                (since,),
            )
            actions = list(cur.fetchall())
    return matters, actions


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m operating.assist_score")
    p.add_argument("--state-dir", default=os.environ.get("PLX_STATE_DIR", "/data/plx-state"))
    p.add_argument("--out", default=None, help="Output JSON path (default: <state-dir>/assist/nightly-score.json)")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--dsn", default=os.environ.get("PLX_PG_DSN") or os.environ.get("DATABASE_URL"))
    a = p.parse_args(argv)

    out_path = Path(a.out) if a.out else Path(a.state_dir) / "assist" / "nightly-score.json"
    excl_guard, excl_reason = _load_excluded_guard()
    not_measured: list[str] = []
    if excl_reason:
        not_measured.append(excl_reason)
        matters, actions = [], []
    elif a.dsn:
        matters, actions = fetch_from_postgres(a.dsn, days=a.days)
    else:
        not_measured.append("no_pg_dsn")
        matters, actions = [], []

    result = score_matters(
        matters,
        actions,
        exclusion_guard=excl_guard,
        window_days=a.days,
        not_measured=not_measured,
    )
    write_snapshot(out_path, result)
    print(json.dumps({
        "schema": result["schema"],
        "window_days": result["window_days"],
        "totals": result["totals"],
        "out": str(out_path),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
