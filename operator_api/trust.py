"""The Trust page (``GET /api/trust``): what a reader needs to judge the Workbench, from
the stores that already hold it, nothing measured here.

* Freshness per source system: the newest capture taken (the host's capture ledger,
  ``host/host.sqlite`` ``source_state``), how far the served data reaches into it (what
  the delta read of each source and Postgres applied, ``served_reads``), the capture's
  age, and the state in one plain sentence, with a failed read's reason in words
  (``freshness``).
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

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Optional

from operating import eval_results as results_file
from operating import alert_kinds
from operating import autopilot_write as aw

from . import models as m
from .autopilot_area import _instant, _iso  # one copy of the time helpers
from .serving import LEDGER_SYSTEMS, STORE_SYSTEMS, eastern_words
from ontology.delta import READS_KEY
from ontology.project import STALE_AFTER_SECONDS

# the write-capable systems and the flag each answers to (integrations.live_source_guard)
ACTION_FLAG = "PLX_ACTIONS_LIVE"           # live_source_guard.ACTION_FLAG
SENDERS_ENV = "PLX_ACTIONS_SENDERS"        # graph_send.SENDERS_ENV
WRITES_FLAG = "PLX_WRITES_LIVE_SYSTEMS"    # live_source_guard.WRITES_FLAG
WRITE_SYSTEMS = (("bc", "Business Central"), ("sharepoint", "SharePoint"),
                 ("planner", "Planner"), ("calendar", "Calendar"))
NO_WRITE_PATH = (("mc", "Mission Control"), ("sage", "Sage"), ("portal", "Portal"),
                 ("filemaker", "FileMaker"))


def _pct(text: Any) -> Optional[float]:
    if text is None:
        return None
    return float((Decimal(str(text)) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


# A ledger source -> the part of its system it reads, named where a system has two
LEDGER_PARTS = {"sharepoint": "SharePoint files", "sharepoint_lists": "SharePoint lists"}
# a capture's error code (``operating.ingest_forever``) -> what happened, in plain words;
# any other code is "stopped with an error" (a code is never shown to a person)
CAPTURE_ERRORS = {
    "no_units_reported": "it finished without reporting anything it read",
    "child_timeout": "it ran out of time",
    "credentials_absent": "its sign-in details were missing",
    "capture_gap": "it left a gap that has to be read again",
}
# captured records the served data has not taken in after this long are behind: one
# delta cycle is ~6 minutes (``ops_health`` ``served_lag_hours``, the paged alarm's bound)
SERVED_BEHIND_SECONDS = 3600
# a ledger source -> the delta source whose reads say how far the served data holds it
# (``ontology.delta.SOURCES``: SharePoint files and lists are one scan)
LEDGER_DELTA = {"portal": "portal_api", "bc": "bc", "mail": "graph_mail",
                "sharepoint": "sharepoint", "sharepoint_lists": "sharepoint",
                "teams": "teams", "mc": "mission_control"}
# the system's own word for a source no ``LEDGER_PARTS`` entry names
SOURCE_WORDS = {"portal": "the Portal", "bc": "Business Central", "mail": "email",
                "teams": "Teams", "mc": "Mission Control"}
# why a source was skipped (``ontology.delta.SKIP_REASONS``), in words; any other reason
# is "it was left out"
SKIP_WORDS = {"people_exclusion_unavailable": "the list of people whose mail is kept out "
              "could not be read, so no mail was read"}


def _ledger(state_dir: Optional[Path]) -> list[tuple[Any, ...]]:
    ledger = Path(state_dir) / "host" / "host.sqlite" if state_dir else None
    if ledger is None or not ledger.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=1)
        try:
            return conn.execute("SELECT source, captured_at, error, attempted_at FROM "
                                "source_state WHERE configured = 1").fetchall()
        except sqlite3.OperationalError:   # a ledger from before attempts were kept
            return conn.execute("SELECT source, captured_at, NULL, NULL FROM source_state "
                                "WHERE configured = 1").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _captured(rows: list[tuple[Any, ...]]) -> dict[str, str]:
    """System -> the oldest of its sources' newest captures (a system is as fresh as its
    least fresh source), from the capture ledger."""
    out: dict[str, str] = {}
    for source, captured, _error, _attempted in rows:
        system = LEDGER_SYSTEMS.get(source)
        at = _instant(captured)
        if system is None or at is None:
            continue
        held = _instant(out.get(system))
        if held is None or at < held:
            out[system] = _iso(at)
    return out


def _failures(rows: list[tuple[Any, ...]], now: datetime) -> dict[str, str]:
    """System -> one plain sentence per source whose last capture attempt failed: when,
    what happened and when the last good read was."""
    out: dict[str, list[str]] = {}
    for source, captured, error, attempted in rows:
        system = LEDGER_SYSTEMS.get(source)
        if system is None or not error:
            continue
        part = LEDGER_PARTS.get(source, system)
        what = CAPTURE_ERRORS.get(str(error), "it stopped with an error")
        tried, good = _instant(attempted), _instant(captured)
        words = (f"The last read of {part}" + (f", at {_clock(tried, now)}," if tried else "")
                 + f" failed: {what}.")
        words += (f" The last good read was at {_clock(good, now)}." if good else
                  " No read of it has worked yet.")
        out.setdefault(system, []).append(words)
    return {system: " ".join(words) for system, words in out.items()}


def _clock(at: datetime, now: Optional[datetime] = None) -> str:
    """'10:16 AM ET' today (US Eastern), else '16 Nov 7:05 PM ET'."""
    words = eastern_words(at)                       # '16 Nov 2026 7:05 PM ET'
    day, month, _year, clock = words.split(" ", 3)
    today = eastern_words(now or datetime.now(timezone.utc)).split(" ", 3)[:2]
    return clock if [day, month] == today else f"{day} {month} {clock}"


def served_reads(backend: Any) -> Optional[dict[str, dict[str, Any]]]:
    """Per delta source, how far the served data reaches into its capture: what the
    delta's cycles read of it and Postgres applied (``ontology.store_meta``
    ``delta_reads``, written by ``operating.pg.ontology_delta.note_reads`` from
    ``ontology.delta.cycle_reads``): ``served_through`` (everything its capture committed
    before is served), ``state`` of the newest read (complete, capped, skipped, waiting)
    and ``left`` / ``reason``. None when the data is not Postgres's, the meta is absent
    or unreadable (never a guess)."""
    from operating.pg import ontology_reader
    if not ontology_reader.pg_only():
        return None
    try:
        meta = ontology_reader.store_meta([READS_KEY]) or {}
        value = json.loads(meta.get(READS_KEY) or "null")
    except Exception:  # noqa: BLE001 - unreadable: nothing said
        return None
    if not isinstance(value, dict):
        return None
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, dict)}


def _source_served(source: str, captured: Optional[datetime],
                   entry: Optional[Mapping[str, Any]], now: datetime
                   ) -> tuple[Optional[datetime], Optional[str]]:
    """(served up to, why it is behind in one sentence or None) for one ledger source
    from its delta read (``served_reads``)."""
    part = LEDGER_PARTS.get(source, SOURCE_WORDS.get(source, "this source"))
    if entry is None:
        return None, f"Behind: no update of the app's data has read {part} yet."
    through = _instant(entry.get("served_through"))
    served = through if captured is None or through is None else min(captured, through)
    state = entry.get("state")
    if state == "skipped":
        why = SKIP_WORDS.get(str(entry.get("reason")), "it was left out")
        return served, (f"Behind: the last update of the app's data did not read {part}: "
                        f"{why}.")
    if state == "capped":
        left = entry.get("left")
        many = f"{left:,} changed records" if isinstance(left, int) and left > 0 else \
            "changed records"
        return served, (f"Behind: the app's data is catching up on {part}; {many} wait "
                        "for the next updates.")
    if state == "waiting":
        return served, (f"Behind: the newest update that read {part} waits for an earlier "
                        "one to reach the app's data.")
    if state != "complete" or through is None:
        return served, f"Behind: the app's data cannot say how far it holds {part}."
    if entry.get("refresh_overdue") is True:
        return served, (f"Behind: some of {part} were not read within their usual interval.")
    if captured is not None and captured > through and \
            (now - through).total_seconds() > SERVED_BEHIND_SECONDS:
        return served, (f"Behind: records of {part} read since {_clock(through, now)} are "
                        "not in the app yet; they normally arrive within a few minutes.")
    return served, None


def freshness(backend: Any, now: Optional[datetime] = None) -> list[m.TrustFreshness]:
    """Per system: the newest capture (ledger), how far the served data reaches into it,
    the capture's age, whether the app is behind and, in one sentence, why.

    Served, with a delta over Postgres (``served_reads``): per source, its capture time
    when the delta read that capture completely and Postgres holds it, else how far it
    does (the scan start of its newest complete read); a system is as far as its least
    served source. Behind: a source's newest read was skipped, capped or waits on an
    earlier layer; a capture waits more than ``SERVED_BEHIND_SECONDS`` past the served
    position; or the newest capture is older than the system's freshness bound. A failed
    read with a recent good one is said, not called behind. Without the delta's record,
    the served projection's own capture times (``Backend.freshness``: a generation's
    build states them)."""
    now = now or datetime.now(timezone.utc)
    state_dir = getattr(backend, "state_dir", None)
    ledger = _ledger(state_dir)
    captured = _captured(ledger)
    failed = _failures(ledger, now)
    reads = served_reads(backend)
    served: dict[str, Any] = {}
    if reads is None:
        try:
            served = {s.system: s for s in backend.freshness().sources}
        except Exception:  # noqa: BLE001 - no served data readable: captured times only
            served = {}
    bounds = getattr(backend, "freshness_bounds", None) or {}
    rows: list[m.TrustFreshness] = []
    for system in sorted(set(captured) | set(served)):
        at = _instant(captured.get(system))
        row = m.TrustFreshness(system=system, captured_at=captured.get(system))
        if at is not None:
            row.lag_seconds = max(0, int((now - at).total_seconds()))
        held = served.get(system)
        if held is not None:
            row.capture = held.capture
        behind: list[str] = []
        if reads is not None:
            reach: list[Optional[datetime]] = []
            for source, when in _ledger_times(ledger).items():
                if LEDGER_SYSTEMS.get(source) != system:
                    continue
                got, why = _source_served(source, when, reads.get(LEDGER_DELTA.get(source, "")),
                                          now)
                reach.append(got)
                if why and why not in behind:
                    behind.append(why)
            if reach and all(r is not None for r in reach):
                row.served_at = _iso(min(r for r in reach if r is not None))
        elif held is not None:
            row.served_at = held.as_of
        if failed.get(system):
            row.capture = "failed"
        bound = max((float(v) for k, v in bounds.items() if STORE_SYSTEMS.get(k) == system),
                    default=STALE_AFTER_SECONDS)
        old = row.lag_seconds is None or row.lag_seconds > bound
        if reads is None:
            row.stale = bool(held.stale) if held is not None else True
        else:
            row.stale = bool(behind) or old
        row.state_words = _state_words(row, reads is not None, behind, old, bound,
                                       failed.get(system))
        rows.append(row)
    if state_dir:
        rows += carried(Path(state_dir), {r.system: r for r in rows}, now)
    return rows


# The systems one capture carries (``operating.ops_health.source_checks``, Admin's own
# checks): Sage and FileMaker come in with the Portal mirror, Calendar and Planner with the
# meetings capture. Each is as fresh as its own newest refresh, within its carrier's bound,
# and no fresher than the carrier's way into the app.
CARRIED = {"capture:sage": ("Sage", "Portal"), "capture:filemaker": ("FileMaker", "Portal"),
           "capture:calendar": ("Calendar", None), "capture:planner": ("Planner", None)}


def carried(state: Path, held: Mapping[str, m.TrustFreshness],
            now: datetime) -> list[m.TrustFreshness]:
    """One row per carried system Admin checks (``ops_health.source_checks``): the same
    age and the same bound, so Trust and Admin never disagree on it. A carrier that is not
    configured gives no row (Admin has none either); an unreadable store says so."""
    from operating import ops_health
    try:
        checks = ops_health.source_checks(state, now, ops_health.targets(os.environ))
    except Exception:  # noqa: BLE001 - unreadable: the rows say so, never a guess
        checks = [{"name": name, "value": None, "state": "unknown", "target": None,
                   "detail": "unreadable"} for name in CARRIED]
    out: list[m.TrustFreshness] = []
    for check in checks:
        name = check.get("name")
        if name not in CARRIED:
            continue
        system, via = CARRIED[name]
        row = m.TrustFreshness(system=system)
        minutes = check.get("value")
        if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
            at = now - timedelta(minutes=float(minutes))
            row.captured_at = _iso(at)
            row.lag_seconds = max(0, int(float(minutes) * 60))
        carrier = held.get(via) if via else None
        bound = check.get("target")
        if row.captured_at is None:
            row.stale = True
            row.state_words = "Never read yet." if check.get("state") != "unknown" or \
                check.get("detail") != "unreadable" else "Behind: its store could not be read."
        elif check.get("state") == "breach":
            row.stale = True
            row.state_words = (f"Behind: not read for over {_hours(float(bound) * 60)}."
                               if isinstance(bound, (int, float)) else "Behind.")
        elif carrier is not None and carrier.stale:
            row.stale = True
            row.served_at = carrier.served_at
            row.state_words = (f"Behind: it reaches the app with the {via} data, which is "
                               "behind.")
        else:
            row.served_at = carrier.served_at if carrier is not None else None
            row.state_words = "Up to date: everything read is in the app."
        out.append(row)
    return sorted(out, key=lambda r: r.system)


def _ledger_times(rows: list[tuple[Any, ...]]) -> dict[str, Optional[datetime]]:
    """Ledger source -> its newest capture (None: never read or an unreadable time)."""
    return {source: _instant(captured) for source, captured, _e, _a in rows
            if source in LEDGER_SYSTEMS}


def _hours(seconds: float) -> str:
    h = seconds / 3600
    return f"{h:g} hours" if h < 48 else f"{h / 24:g} days"


def _state_words(row: m.TrustFreshness, delta: bool, behind: list[str], old: bool,
                 bound: float, failure: Optional[str]) -> str:
    if row.captured_at is None:
        words = "Never read yet."
    elif behind:
        words = " ".join(behind)
    elif old:
        words = f"Behind: not read for over {_hours(bound)}."
    elif not delta and row.served_at is None:
        words = "Read, but not in the app's data yet."
    elif delta and row.served_at != row.captured_at:
        words = "Up to date: the newest read joins the app's data within a few minutes."
    elif not delta and row.stale:
        words = "Behind: the app's data holds an older read."
    else:
        words = "Up to date: everything read is in the app."
    return words + (" " + failure if failure else "")


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
          sw: list[m.TrustSwitch], g: Optional[m.TrustGate] = None,
          approved: frozenset[str] = frozenset()) -> list[m.TrustLine]:
    """``approved``: the action kinds the owner opened to person-approved writes
    (``autopilot_write.approved_kinds``): those go out on a person's approval and
    confirmation without the gate, and the gate line says so (API-020)."""
    out = []
    systems = ", ".join(r.system for r in fresh if r.served_at)
    if systems:
        out.append(m.TrustLine(state="live", words=f"Reads {systems}; never writes to them."))
    on = [k for k in kinds if k.on]
    if on:
        out.append(m.TrustLine(state="live", words=f"{len(on)} alert types on Home, "
                               "each hand-checked at 90% or better on 15 or more items."))
    sending = [s for s in sw if s.state == "on"]
    for s in sending:
        out.append(m.TrustLine(state="live", words=f"{s.system}: {s.words}"))
    drafted = sorted({r.workflow for r in rows if r.draft})
    if drafted:
        n = len(drafted)
        out.append(m.TrustLine(state="live", words=f"Draft: {n} autopilot workflow"
                               f"{'s prepare' if n != 1 else ' prepares'} drafts for approval "
                               "(marked Draft under Autopilot workflows below). Nothing goes "
                               "out until its owner accepts and confirms."))
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
                               f"{_pct_words(g.need_percent)} or better, and only for a person "
                               "who switched it on for themselves"
                               + (", or for " + ", ".join(sorted(_write_words(k)
                                                                 for k in approved))
                                  + " when a person approves and confirms the item"
                                  if approved else "") + "."))
    off = [s.system for s in sw if s.state == "off"]
    if off:
        out.append(m.TrustLine(state="gated", words="Writes switched off: " + ", ".join(off)
                               + "."))
    unmeasured = sum(1 for k in kinds if k.judged is None)
    if unmeasured:
        out.append(m.TrustLine(state="pending", words=f"{unmeasured} alert types not "
                               "measured yet: they show on Home's Alerts tab only, never "
                               "under Needs you."))
    if not sets or not any(s.held_out for s in sets):
        out.append(m.TrustLine(state="pending", words="Held-out answer scores: not measured "
                               "yet on this server. Its score on questions never used for "
                               "tuning shows here once a held-out run records it."))
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
    # the autopilot page's own served copy (``WorkspaceBackend.autopilot_area``: kept per
    # reader under the same access rules, read again when its inputs move), not a fresh
    # read of the whole area on every visit (11-13: warm Trust 2.5-12.5 s)
    # read under the inputs served now; one served stale (its queue or switches moved
    # since) is read again here, so Trust never shows an older workflow state
    served = getattr(backend, "autopilot_area", None)
    fresh_read = getattr(backend, "_area_read", None)

    def area(who: Any) -> Any:
        got = None
        if callable(served):
            try:
                got = served(who)
            except Exception:  # noqa: BLE001 - busy or refused: read it here instead
                got = None
        if got is None or getattr(got, "stale_since", None) is not None:
            got = fresh_read(who) if callable(fresh_read) else got
        if got is None:
            raise LookupError("no autopilot area")
        return got

    if callable(served) or callable(fresh_read):
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
                       lines=lines(fresh, sets, kinds, rows, sw, g,
                                   frozenset(aw.approved_kinds(env))), switches=sw)


# person-approved write kinds (``autopilot_write`` approved kinds) in plain words
_WRITE_WORDS = {"bc_write": "Business Central changes", "send_mail": "mail",
                "sharepoint_write": "SharePoint files", "planner_write": "Planner tasks",
                "calendar_write": "calendar entries"}


def _write_words(kind: str) -> str:
    return _WRITE_WORDS.get(kind, kind.replace("_", " "))
