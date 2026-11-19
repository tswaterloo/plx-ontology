"""Truth rows -> grading spec, shared by the family modules (no entity or value lives here)."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from .core import D, num, spec

# What a person may write for a recorded state (the grader needs one of them).
STATE_WORDS = {
    "open": ("open", "outstanding", "not yet shipped", "not shipped", "pending", "in progress", "active"),
    "closed": ("closed", "shipped", "complete", "completed", "fulfilled", "invoiced", "delivered", "done"),
    "draft": ("draft",), "unknown": ("unknown", "unclear", "not known", "can't tell", "cannot tell"),
    "realized": ("realized", "produced", "made", "completed", "complete"),
    "cancelled": ("cancelled", "canceled"), "allocated": ("allocated",), "committed": ("committed",),
    "generated": ("generated", "planned"), "released": ("released", "passed", "approved"),
    "on-hold": ("on hold", "on-hold", "held", "hold"), "rejected": ("rejected", "failed"),
    "merged": ("merged", "done", "complete", "completed", "shipped"), "backlog": ("backlog", "not started", "to do", "todo"),
    "verified": ("verified", "done", "complete"), "progress": ("progress", "in progress", "underway", "being worked"),
    "review": ("review", "in review", "under review"), "approved": ("approved",), "pending": ("pending", "waiting", "awaiting"),
    "finished": ("finished", "used up", "consumed", "complete"), "expired": ("expired",), "quarantine": ("quarantine", "quarantined", "on hold"),
    "scheduled": ("scheduled", "planned"), "active": ("active", "open", "ongoing"),
    "track": ("on track", "track", "green"), "risk": ("at risk", "risk", "amber", "yellow"), "off": ("off track", "red", "behind"),
}
YES_PAID = ("paid", "settled", "cleared")
NOT_PAID = ("not been paid", "unpaid", "not paid", "hasn't been paid", "has not been paid", "still open", "outstanding",
            "still owe", "still due", "open balance", "remains open", "not yet paid", "not settled")


def state_words(value: Any) -> tuple[str, ...]:
    v = str(value or "").strip().lower()
    return STATE_WORDS.get(v, (v,)) if v else ()


def first(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return rows[0] if len(rows) == 1 else None


def text(col: str, rule: str) -> Callable:
    def g(rows, params):
        r = first(rows)
        if not r or r.get(col) in (None, "", "None"):
            return None
        return spec(rule, names=[str(r[col])])
    return g


def state(col: str, rule: str) -> Callable:
    def g(rows, params):
        r = first(rows)
        if not r or not r.get(col):
            return None
        return spec(f"{rule}: {r[col]}", must_any=state_words(r[col]))
    return g


def number(col: str, rule: str, cur: str | None = None, tol: float | None = None) -> Callable:
    def g(rows, params):
        r = first(rows)
        if not r or r.get(col) is None or D(r[col]) == 0:
            return None   # a recorded 0 usually means "not set": not a fair question
        return spec(rule, numbers=[num(r[col], col, tol, currency=(r.get(cur) if cur else None))])
    return g


def date(col: str, rule: str, tol_days: int = 0) -> Callable:
    def g(rows, params):
        r = first(rows)
        v = r and r.get(col)
        if not v or str(v)[:4] in ("None", "0001"):
            return None
        return spec(rule, dates=[str(v)[:10]], date_tol_days=tol_days)
    return g


def count(rule: str, col: str = "n", names_col: str | None = None, max_list: int = 15, alts_col: str | None = None) -> Callable:
    """A count; zero states 'none'; small results must also name min(3, n) of the members."""
    def g(rows, params):
        if not rows:
            return None
        r = rows[0]
        n = int(r[col] or 0)
        if n == 0:
            return spec(rule + " (none)", numbers=[num(0, "count")], zero_ok=True)
        names = list(r.get(names_col) or ())[:max_list] if names_col and n <= max_list else []
        alts = [r[alts_col]] if alts_col and r.get(alts_col) is not None else ()
        return spec(rule, numbers=[num(n, "count", alts=alts)], names=names, min_names=min(3, len(names)))
    return g


def money_by_currency(col: str, cur: str, rule: str, alts_col: str | None = None, zero_ok: bool = True) -> Callable:
    """One amount per currency row (never summed across currencies); none -> zero."""
    def g(rows, params):
        rows = [r for r in rows if r.get(col) is not None and D(r[col]) != 0]
        if not rows:
            return spec(rule + " (nothing)", numbers=[num(0, "amount")], zero_ok=True) if zero_ok else None
        nums = [num(r[col], f"{r.get(cur)} {col}", alts=[r[alts_col]] if alts_col and r.get(alts_col) is not None else (),
                    currency=r.get(cur)) for r in rows]
        return spec(rule, numbers=nums)
    return g


def ranked(name_col: str, rule: str, k: int = 5, need: int = 3, value_col: str | None = None, cur: str | None = None) -> Callable:
    def g(rows, params):
        if len(rows) < need:
            return None
        top = rows[:k]
        nums = [num(top[0][value_col], "top value", currency=top[0].get(cur) if cur else None)] if value_col else []
        return spec(rule, names=[r[name_col] for r in top], min_names=need, numbers=nums, min_numbers=0)
    return g


def members(col: str, rule: str, alt_col: str | None = None, need: int = 3, max_list: int = 25) -> Callable:
    """A set: the answer names min(need, n) members (code or, where given, its name)."""
    def g(rows, params):
        vals = [[r[col]] + ([r[alt_col]] if alt_col and r.get(alt_col) else []) for r in rows if r.get(col)]
        if not vals:
            return None
        vals = vals[:max_list]
        return spec(rule, names=vals, min_names=min(need, len(vals)))
    return g


def missing(rule: str) -> Callable:
    """Impossible: the identifier exists nowhere; the answer must say it cannot be found."""
    def g(rows, params):
        if rows and int(rows[0].get("n") or 0) > 0:
            return None
        return spec(rule, cannot=True)
    return g


def premise(rule: str, words: Sequence[str], col: str = "ok") -> Callable:
    """False premise: the truth row confirms the premise is false; the answer must say so."""
    def g(rows, params):
        r = first(rows)
        if not r or not r.get(col):
            return None
        return spec(rule, must_any=words)
    return g
