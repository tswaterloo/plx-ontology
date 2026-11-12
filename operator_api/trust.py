"""The Trust page (``GET /api/trust``): what a reader needs to judge the Workbench, from
the stores that already hold it, nothing measured here.

* Freshness per source system: the newest capture taken (the host's capture ledger,
  ``host/host.sqlite`` ``source_state``), the newest capture the served data holds
  (``Backend.freshness``, the same as Home's line) and the lag between them.
* Answer quality: the latest dev and held-out score per question set
  (``operating.eval_results``, written by the eval runner); "not measured yet" when absent.
* Alert precision per alert type (``operating/alert_kinds.json``): judged, correct, on or
  off; a type never measured says so.
* Autopilot: each workflow's mode, practice-day score and fresh-day countdown, the rows
  of the autopilot area as this reader may see them (``AutopilotAreaMixin``).
* What is live, gated and pending, and the write switch of each system, from the
  server's own flags.

The owner and the Team readers only (``app.ops_reader``); anyone else gets 404. Counts,
times and switch states only: no record, message or person's data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Optional

from operating import eval_results as results_file
from operating import alert_kinds
from operating import autopilot_write as aw

from . import models as m
from .serving import LEDGER_SYSTEMS

# the write-capable systems and the flag each answers to (integrations.live_source_guard)
ACTION_FLAG = "PLX_ACTIONS_LIVE"           # live_source_guard.ACTION_FLAG
SENDERS_ENV = "PLX_ACTIONS_SENDERS"        # graph_send.SENDERS_ENV
WRITES_FLAG = "PLX_WRITES_LIVE_SYSTEMS"    # live_source_guard.WRITES_FLAG
WRITE_SYSTEMS = (("bc", "Business Central"), ("sharepoint", "SharePoint"),
                 ("planner", "Planner"), ("calendar", "Calendar"))
NO_WRITE_PATH = (("mc", "Mission Control"), ("sage", "Sage"), ("portal", "Portal"),
                 ("filemaker", "FileMaker"))


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _instant(raw: Any) -> Optional[datetime]:
    try:
        out = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return out if out.tzinfo else None


def _pct(text: Any) -> Optional[float]:
    if text is None:
        return None
    return float((Decimal(str(text)) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _captured(state_dir: Optional[Path]) -> dict[str, str]:
    """System -> the oldest of its sources' newest captures (a system is as fresh as its
    least fresh source), from the capture ledger."""
    ledger = Path(state_dir) / "host" / "host.sqlite" if state_dir else None
    if ledger is None or not ledger.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=1)
        try:
            rows = conn.execute("SELECT source, captured_at FROM source_state "
                                "WHERE configured = 1").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    out: dict[str, str] = {}
    for source, captured in rows:
        system = LEDGER_SYSTEMS.get(source)
        at = _instant(captured)
        if system is None or at is None:
            continue
        held = _instant(out.get(system))
        if held is None or at < held:
            out[system] = _iso(at)
    return out


def freshness(backend: Any) -> list[m.TrustFreshness]:
    try:
        served = backend.freshness().sources
    except Exception:  # noqa: BLE001 - no served data readable: captured times only
        served = []
    captured = _captured(getattr(backend, "state_dir", None))
    rows: dict[str, m.TrustFreshness] = {}
    for s in served:
        rows[s.system] = m.TrustFreshness(system=s.system, served_at=s.as_of,
                                          stale=s.stale, capture=s.capture)
    for system, at in captured.items():
        row = rows.setdefault(system, m.TrustFreshness(system=system, stale=True))
        row.captured_at = at
    for row in rows.values():
        a, b = _instant(row.captured_at), _instant(row.served_at)
        if a is not None and b is not None:
            row.lag_seconds = max(0, int((a - b).total_seconds()))
    return sorted(rows.values(), key=lambda r: r.system)


def answers(state_dir: Optional[Path], environ: Mapping[str, str]) -> list[m.TrustAnswerSet]:
    found = results_file.read(results_file.path_for(state_dir, environ))

    def score(e: Optional[Mapping[str, Any]]) -> Optional[m.TrustAnswerScore]:
        if not e:
            return None
        return m.TrustAnswerScore(at=e["at"], runs=int(e["runs"]), items=int(e["items"]),
                                  correct=int(e["correct"]), answered=int(e["answered"]),
                                  accuracy=_pct(e.get("accuracy")),
                                  coverage=_pct(e.get("coverage")),
                                  evidence_label=str(e["evidence_label"]))
    return [m.TrustAnswerSet(name=name, dev=score(splits.get("dev")),
                             held_out=score(splits.get("held_out")))
            for name, splits in sorted(found.items())]


def alerts() -> tuple[str, list[m.TrustAlert]]:
    reg = alert_kinds.registry()
    out = []
    for t in reg.get("types") or ():
        p = t.get("precision") if isinstance(t.get("precision"), Mapping) else None
        judged = correct = None
        pct = None
        if p is not None:
            try:
                judged = int(p["checked"])
                value = Decimal(str(p["value"]))
                if judged <= 0 or not 0 <= value <= 1:
                    raise ValueError("counts")
                pct = float((value * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
                # the registry keeps a rounded rate: a count only when it gives a whole
                # number of items (21% of 59 is no count of correct items)
                exact = value * judged
                whole = exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                correct = int(whole) if abs(exact - whole) <= Decimal("0.05") else None
            except (KeyError, ValueError, TypeError, ArithmeticError):
                judged = correct = pct = None   # unreadable: shown as not measured
        out.append(m.TrustAlert(
            type=str(t.get("type")), kind=str(t.get("kind") or ""),
            label=str(t.get("label") or t.get("type")),
            on=t.get("default") == "on", home=bool(t.get("home")),
            judged=judged, correct=correct, percent=pct,
            measured_on=str(p.get("on")) if p and judged is not None and p.get("on") else None,
            note=str(t.get("unmeasured") or (p or {}).get("note") or "")[:300] or None))
    out.sort(key=lambda a: (not a.on, a.judged is None, a.kind, a.label))
    return str(reg.get("rule") or ""), out


def switches(environ: Mapping[str, str]) -> list[m.TrustSwitch]:
    out = []
    live = environ.get(ACTION_FLAG) == "1"
    senders = [s for s in (environ.get(SENDERS_ENV) or "").split(",") if s.strip()]
    if live and senders:
        n = len(senders)
        words = (f"On for {n} sender{'s' if n != 1 else ''}: each message is sent only after "
                 "its writer confirms it.")
    else:
        words = "Off: nothing is sent."
    out.append(m.TrustSwitch(system="Email and Teams messages",
                             state="on" if live and senders else "off", words=words))
    opened = {s.strip().lower() for s in (environ.get(WRITES_FLAG) or "").split(",")
              if s.strip()}
    for key, name in WRITE_SYSTEMS:
        on = key in opened
        out.append(m.TrustSwitch(system=name, state="on" if on else "off",
                                 words="On: approved writes go out after the approver "
                                       "confirms." if on else
                                       "Off: approved writes are recorded as dry runs."))
    for _key, name in NO_WRITE_PATH:
        out.append(m.TrustSwitch(system=name, state="none",
                                 words="Read only: the Workbench has no write path here."))
    return out


def gate() -> Optional[m.TrustGate]:
    """The write gate's numbers from the write rules (``rules.gate``); None when the rules
    are unreadable (the write path then performs nothing)."""
    try:
        g = aw.load_rules()["gate"]
        need = int(g["min_judged"])
        bar = Decimal(str(g["precision"]))
        if need < 1 or not 0 < bar <= 1:
            return None
    except (aw.RulesInvalid, KeyError, ValueError, TypeError, ArithmeticError):
        return None
    pct = (bar * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return m.TrustGate(need_judged=need, need_percent=float(pct))


def _pct_words(value: float) -> str:
    return f"{value:g}%"


def lines(fresh: list[m.TrustFreshness], sets: list[m.TrustAnswerSet],
          kinds: list[m.TrustAlert], rows: list[m.AutopilotWorkflowRow],
          sw: list[m.TrustSwitch], g: Optional[m.TrustGate] = None) -> list[m.TrustLine]:
    out = []
    systems = ", ".join(r.system for r in fresh if r.served_at)
    if systems:
        out.append(m.TrustLine(state="live", words=f"Reading {systems}, read only."))
    on = [k for k in kinds if k.on]
    if on:
        out.append(m.TrustLine(state="live", words=f"{len(on)} alert types on My work, "
                               "each hand-checked at 90% or better on 15 or more items."))
    sending = [s for s in sw if s.state == "on"]
    for s in sending:
        out.append(m.TrustLine(state="live", words=f"{s.system}: {s.words}"))
    drafted = sorted({r.workflow for r in rows if r.draft})
    if drafted:
        out.append(m.TrustLine(state="live", words="Draft: the autopilot prepares "
                               + ", ".join(w.replace("_", " ") for w in drafted)
                               + " for approval. Nothing goes out until its owner accepts "
                               "and confirms."))
    if any(r.state == "live" for r in rows):
        n = sum(1 for r in rows if r.state == "live")
        out.append(m.TrustLine(state="live", words=f"{n} autopilot workflow"
                               f"{'s' if n != 1 else ''} acting on approval."))
    if g is None:
        out.append(m.TrustLine(state="gated", words="Autopilot sends and writes: the gate "
                               "rules cannot be read, so no workflow acts."))
    else:
        out.append(m.TrustLine(state="gated", words="Autopilot sends and writes: a workflow "
                               f"acts only after {g.need_judged} judged held-out items at "
                               f"{_pct_words(g.need_percent)} or better and the owner's switch."))
    off = [s.system for s in sw if s.state == "off"]
    if off:
        out.append(m.TrustLine(state="gated", words="Writes switched off: " + ", ".join(off)
                               + "."))
    unmeasured = sum(1 for k in kinds if k.judged is None)
    if unmeasured:
        out.append(m.TrustLine(state="pending", words=f"{unmeasured} alert types not "
                               "measured yet: they show under Watching only."))
    if not sets or not any(s.held_out for s in sets):
        out.append(m.TrustLine(state="pending", words="Held-out answer scores: not measured "
                               "yet."))
    if rows and not any(r.countdown for r in rows):
        out.append(m.TrustLine(state="pending", words="Autopilot gate counts: no held-out "
                               "item judged yet; the count starts once a replay scores a "
                               "whole new day or a past day no tuning used."))
    return out


def build(backend: Any, reader: Any, environ: Optional[Mapping[str, str]] = None,
          now: Optional[datetime] = None) -> m.TrustPage:
    if environ is None:
        found = getattr(backend, "_ap_env", None)
        environ = found() if callable(found) else os.environ
    env = environ
    state_dir = getattr(backend, "state_dir", None)
    fresh = freshness(backend)
    sets = answers(state_dir, env)
    rule, kinds = alerts()
    rows: list[m.AutopilotWorkflowRow] = []
    note = None
    area = getattr(backend, "_area_read", None)
    if callable(area):
        try:
            rows = list(area(reader).workflows)
        except Exception:  # noqa: BLE001 - the page stands without the autopilot part
            note = "The autopilot's scores could not be read just now."
    if not rows and note is None:
        note = "No workflow has been scored or put in draft on this server yet."
    sw = switches(env)
    g = gate()
    return m.TrustPage(checked_at=_iso(now or datetime.now(timezone.utc)), freshness=fresh,
                       answers=sets, alert_rule=rule, alerts=kinds, autopilot=rows,
                       autopilot_note=note, autopilot_gate=g,
                       lines=lines(fresh, sets, kinds, rows, sw, g), switches=sw)
