"""The app's autopilot area (``/api/autopilot*``): today's prepared items the reader may
see, each with its evidence, drafted action and the reader's decision; per role and
workflow the replay scores (dev and fresh), the write state and the owner's switches; per
reader the workflows they may switch on for themselves (``my_opt_ins``) and one pause; the
write path's audit log with undo where the system supports it.

Reads the autopilot's queue and gate files and the write path's state directory
(``operating.autopilot_write``; ``$state_dir/autopilot-write``). Writes only the
Workbench's own files there: item decisions (a hash-chained journal), and for the
owner-open reader only (decided server-side) the write path's own stop switches (global
kill, per-workflow off), and for every reader their own opt-in rows (``set_opt_in``,
``set_paused``: the reader's own ids only; nobody, the owner included, switches a
workflow on for someone else). An accept runs the item through
``autopilot_write.Executor``, the one gated write path, as the person's approval of that
item (``autopilot_write.Approval``): it records a dry run with the exact request unless the
owner opened the action's kind to approved writes (``PLX_APPROVED_WRITES``) or the workflow
passed its fresh gate for the person, they switched it on for themselves and the master
switch is set. Nothing else here sends or writes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote

from operating import autopilot as ap
from operating import autopilot_engine
from operating import autopilot_events
from operating import autopilot_outcomes as ao
from operating import autopilot_queue
from operating.autopilot_records import object_href
from operating import learning
from operating.alert_kinds import kind_prepared_by
from operating.assist_matters import clean_note_spacing
from .presentation import counted_words
from operating import autopilot_write as aw
from operating import workflows as wf
from operating.card_actions import Journal

from . import models as m
from .assist_switch_area import AssistSwitchMixin, own_conditions as _own_conditions
from .auth import Reader
from .errors import UserFacing

DECISIONS_FILE = "autopilot-writes/decisions.jsonl"
# an alert passed to a colleague (item, to, by, when): the last row of an item holds
HAND_FILE = "autopilot-writes/handed.jsonl"
TAKE_FILE = "autopilot-writes/taken.jsonl"     # who took which pooled item (audit only)
# "never for this sender": a person's own rows, the last for a sender wins
NEVER_FILE = "autopilot-writes/never-senders.jsonl"
# a confirmed job change that went nowhere: offered for review again (``_apa_job_stage``)
JOB_AGAIN = frozenset({"failed", "refused"})
# each reply-from-line attempt (who, when; never the line): the hourly limit counts these
LINE_ATTEMPTS_FILE = "autopilot-writes/line-attempts.jsonl"
MESSAGE_CHARS = 1500      # the message shown beside a prepared reply: the newest one only
CONFIRMS_FILE = "autopilot-writes/live-confirms.jsonl"   # offered and spent (digests)
# which prepared change each reviewed job of an item became (item, index -> prepared id)
JOBS_FILE = "autopilot-writes/jobs.jsonl"
CONFIRM_SECONDS = 600
BULK_LIVE = "A live item is not accepted in bulk: open it and confirm it alone."
SNOOZE_DAYS = 30
SYSTEM_OF_SCHEME = {"graph-mail": "mail", "teams-chat": "teams", "teams-channel": "teams",
                    "mc": "mc", "bc": "bc", "api": "portal", "sharepoint": "sharepoint",
                    "sharepoint-list": "sharepoint", "calendar-event": "meetings",
                    "meeting-transcript": "meetings", "planner-task": "planner"}
WRITE_SYSTEM = {"bc_write": "bc", "sage_write": "sage", "portal_write": "portal",
                "filemaker_write": "filemaker", "send_mail": "mail", "send_teams": "teams",
                "mc_approval": "mc", "mc_task": "mc", "planner_write": "planner",
                "calendar_write": "calendar", "sharepoint_write": "sharepoint"}


# the actions that send the reply itself: an email, or a message posted to the Teams chat
SENDS = frozenset({"send_mail", "send_teams"})

# statuses of an action the write path never performed (nothing was sent or written)
NOT_RUN = frozenset({"dry_run", "needs_confirm", "killed", "refused", "excluded",
                     "not_routine", "unavailable", "rate_limited", "duplicate",
                     "not_performed"})




_ONE_SEGMENT = re.compile(r"^/object/([^/?#]+)/?([?#].*)?$")


def card_href(href: str) -> str:
    """A card link as the app routes it: ``/object/<type>/<id>``. Items prepared before the
    writers named the type say ``/object/<id>`` (Page not found); the id's own prefix is the
    type, so the link is rebuilt from it (preserving any query or fragment). Any other
    address is kept as it is."""
    from urllib.parse import unquote
    one = _ONE_SEGMENT.match(href)
    if one and ":" in unquote(one.group(1)):
        return object_href(unquote(one.group(1))) + (one.group(2) or "")
    return href


def shown_message(text: Any) -> str:
    """The message a prepared reply answers, as shown beside it: the newest message only
    (the quoted thread below it cut, ``learning.reply_core``), at most ``MESSAGE_CHARS``."""
    from operating.learning import reply_core
    core = reply_core(text)
    return core if len(core) <= MESSAGE_CHARS else core[:MESSAGE_CHARS].rstrip() + "..."


def todos_of(prepared: Mapping[str, Any]) -> list[m.AutopilotTodo]:
    """The item's things to do by hand (``autopilot_agent``: a system with no write path,
    a look's question), as the person sees them; one without a ``what`` is no to-do."""
    out = []
    for t in prepared.get("todos") or ():
        if isinstance(t, Mapping) and str(t.get("what") or "").strip():
            out.append(m.AutopilotTodo(system=str(t.get("system") or ""),
                                       record=str(t.get("record") or ""),
                                       what=str(t["what"]).strip()))
    return out


def alert_of(item: Mapping[str, Any], shown: Mapping[str, Any]
             ) -> Optional[m.AutopilotPrepared]:
    """An item with no reply, question or change to prepare (an approval asked of the
    person, a QC hold, a Mission Control question) as an Assist card: what happened, why,
    where it is decided and links to the records. Only Later and Not needed act on it here;
    its decision is taken in its own system (no accept: the write path never runs)."""
    body = shown.get("body") or {}
    prepared = body.get("prepared") or {}
    happened = body.get("happened") or {}
    summary = original = str(prepared.get("summary") or "").strip()
    if not summary or prepared.get("kind") or prepared.get("jobs") or prepared.get("todos") \
            or prepared.get("teams_draft") or prepared.get("your_call") \
            or ((prepared.get("draft") or {}).get("body") if isinstance(
                prepared.get("draft"), Mapping) else prepared.get("draft")):
        return None
    if not any(prepared.get(k) for k in ALERT_BASIS):
        return None   # a summary alone ("nothing to act on") is not a card
    facts, links = decision_view(prepared)
    # one line (and one link) per order, however many systems hold a copy of it: an item
    # made before the handler kept one copy lists the Portal's and BC's records of one order
    affected: list[Mapping[str, Any]] = []
    numbers: set[str] = set()
    for a in prepared.get("affected") or ():
        if not isinstance(a, Mapping):
            continue
        number = autopilot_queue.order_key(a.get("order_number"))
        if number and number in numbers:
            continue
        numbers.add(number)
        affected.append(a)
        facts.append(", ".join(str(a[k]) for k in ("order_number", "customer")
                               if a.get(k)) or "an open order")
    raw = [a for a in prepared.get("affected") or () if isinstance(a, Mapping)]
    if len(affected) < len(raw):
        # a summary written before the copies were folded counts each copy: it says the
        # orders the card lists
        n = len(affected)
        said = "1 open order" if n == 1 else f"{n} open orders"
        summary = re.sub(r"holds up \d+ open orders?", "holds up " + said, summary)
        summary = re.sub(r"\d+ open order\(s\) affected", said + " affected", summary)
    # a source's own "1 open order(s) affected": the plural the count calls for, in every
    # sentence the card says (whatever an old item stored)
    summary = counted_words(summary)
    facts = [counted_words(f) for f in facts]
    options = [str(o) for o in prepared.get("options") or () if isinstance(o, str)]
    if options:
        facts.append("Choices asked: " + "; ".join(options))
    for link in prepared.get("links") or ():
        if isinstance(link, Mapping) and str(link.get("href") or "").startswith("/object/"):
            links.append(m.AutopilotLink(label=str(link.get("label") or "Open"),
                                         href=card_href(str(link["href"]))))
    if not [link for link in links if link.href.startswith("/object/")]:
        named = {str(a.get("object_id")): str(a.get("order_number") or "")
                 for a in affected if a.get("object_id")}
        oids = [str(e.get("object_id")) for e in [
            *(prepared.get("evidence") or ()), *affected,
            prepared.get("task")] if isinstance(e, Mapping) and e.get("object_id")]
        oids = oids or ([str(happened["object_id"])] if happened.get("object_id") else [])
        orders = sum(1 for o in dict.fromkeys(oids) if o.startswith("SalesOrder:"))
        for oid in dict.fromkeys(oids):
            label = {"MCTask": "Open the task", "SalesOrder": "Open the order",
                     "Batch": "Open the lot"}.get(oid.split(":", 1)[0], "Open the record")
            if orders > 1 and named.get(oid):
                # two order links must say which order each one opens
                label = f"Open order {named[oid]}"
            links.append(m.AutopilotLink(label=label, href=object_href(oid)))
    action = prepared.get("action") if isinstance(prepared.get("action"), Mapping) else {}
    note = str(prepared.get("note") or action.get("note") or "").strip()
    # the title is "<workflow>: <summary>": shown above the summary it would say it twice,
    # so the card's heading is the workflow's name and the summary says what happened
    title = str(shown.get("title") or "").strip()
    label = str(body.get("label") or "").strip()
    rest = title[len(label) + 1:].strip() if label and title.startswith(label + ":") else ""
    if title and (title in (summary, original) or (rest and original.startswith(
            rest.rstrip(".…")))):
        title = label
    return m.AutopilotPrepared(
        item_id=item["item_id"], sender=str(happened.get("sender") or ""), channel="system",
        subject=title, kind="alert", summary=summary,
        alert_kind=kind_prepared_by(str(item.get("autopilot") or "")),
        message=counted_words(shown_message(prepared.get("context") or happened.get("snippet"))),
        facts=facts, note=clean_note_spacing(counted_words(note)), links=links,
        created_at=str(item["created_at"]))


def pooled_card(item: Mapping[str, Any], shown: Mapping[str, Any]) -> m.AutopilotPrepared:
    """An item the reader's role holds and nobody has taken (``autopilot_queue.pool_role``):
    what happened and the records only, never its prepared message (that is the taker's
    to check and send once it is theirs). Take it, or leave it to a colleague."""
    body = shown.get("body") or {}
    prepared = body.get("prepared") or {}
    happened = body.get("happened") or {}
    role = autopilot_queue.pool_role(str(item.get("owner") or "")) or ""
    links = [m.AutopilotLink(label=str(link.get("label") or "Open"),
                             href=card_href(str(link["href"])))
             for link in prepared.get("links") or ()
             if isinstance(link, Mapping) and str(link.get("href") or "").startswith("/object/")]
    if not links and happened.get("object_id"):
        links = [m.AutopilotLink(label="Open the record", href=object_href(str(happened["object_id"])))]
    words = role.replace("_", " ")
    return m.AutopilotPrepared(
        item_id=item["item_id"], sender="", channel="system",
        subject=str(body.get("label") or ""), kind="alert",
        summary=str(prepared.get("summary") or "").strip(),
        note=f"No record names who handles this, so it is on everyone in {words}'s list "
             "until one of you takes it.", links=links, pooled=words,
        created_at=str(item["created_at"]))


def decision_view(prepared: Mapping[str, Any]) -> tuple[list[str], list[m.AutopilotLink]]:
    """What a card shows of a prepared decision (an MC approval's recommendation): the
    recommendation word first, then its reasons, and the link to decide it in Mission
    Control (only an address on MC's own task pages; anything else is left out)."""
    from operating import mc_digest
    facts: list[str] = []
    rec = prepared.get("recommendation")
    if isinstance(rec, str) and rec.strip():
        facts.append(f"Recommended: {rec.strip()}")
    facts += [str(r) for r in prepared.get("reasons") or () if isinstance(r, str)]
    links: list[m.AutopilotLink] = []
    mc = prepared.get("mc") if isinstance(prepared.get("mc"), Mapping) else {}
    href = str(mc.get("link") or "")
    if href.startswith(mc_digest.MC_TASKS_URL) and len(href) <= 600 and \
            mc_digest.TASK_ID.match(href[len(mc_digest.MC_TASKS_URL):]):
        links.append(m.AutopilotLink(label="Decide in Mission Control", href=href))
    return facts, links


# what makes an item with nothing to prepare worth the person's look: why, what it holds up,
# what is asked, or the records and where it is decided
ALERT_BASIS = ("reasons", "affected", "options", "links", "evidence", "task", "action", "note")


def carried(rows: Mapping[str, Mapping[str, Any]], replaced: Mapping[str, str],
            keep: Callable[[Mapping[str, Any]], bool] = lambda r: True
            ) -> dict[str, dict[str, Any]]:
    """Rows keyed by item id with each ``keep`` row of a replaced item carried to the item
    replacing it (a pass to a colleague, a snooze: never quietly undone when a newer item
    about the same lot takes its place); the newer item's own row wins."""
    out = {k: dict(v) for k, v in rows.items()}
    for old, row in rows.items():
        new = autopilot_queue.newest_of(old, replaced)
        if new != old and new not in rows and keep(row):
            out[new] = {**row, "item_id": new, "carried_from": old}
    return out


def passed(alert: m.AutopilotPrepared, hands: Mapping[str, Mapping[str, Any]],
           me: set[str]) -> m.AutopilotPrepared:
    """An alert with who passed it to whom (its last ``HAND_FILE`` row)."""
    row = hands.get(alert.item_id)
    if not row:
        return alert
    if row.get("to") in me:
        return alert.model_copy(update={"handed_from": str(row.get("by_name") or "A colleague")})
    return alert.model_copy(update={"handed_to": str(row.get("to_name") or "a colleague")})


def todo_only(prepared: Mapping[str, Any]) -> bool:
    """Whether the item holds only things to do by hand: no reply, question, brief or
    proposed change (its accept is the person's "done", never a write)."""
    from operating import autopilot_jobs as aj
    return bool(todos_of(prepared)) and not (
        prepared.get("draft") or prepared.get("teams_draft") or prepared.get("your_call")
        or prepared.get("brief") or aj.kept(prepared.get("jobs") or ())
        or prepared.get("also_jobs"))


def sender_of(item: Mapping[str, Any]) -> str:
    """The address an item came from, as "never for this sender" keeps it."""
    happened = (item.get("body") or {}).get("happened") or {}
    return str(happened.get("sender") or "").strip().lower()


def decision_sent(dec: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    """Whether an accepted item's reply went out: the decision's own ``sent`` (only the
    email's or Teams message's own result counts: ``SENDS``); a decision recorded before
    ``sent`` was kept, by its outcomes read against the item's actions in the order they ran
    (a send whose own outcome is done). Anything else (no outcomes, no send step) reads as
    not sent."""
    if "sent" in dec:
        return dec.get("sent") is True
    outcomes = dec.get("outcomes")
    if not isinstance(outcomes, list):
        return False
    kinds = [a.get("kind") for a in aw.actions_of(item)]
    return any(kind in SENDS and status == "done"
               for kind, status in zip(kinds, outcomes))


def item_outcome(statuses: list[str]) -> str | None:
    """One word for an accepted item's actions (in order): ``unknown`` when any did not
    say how it ended (pending, unknown: check before retrying), ``done`` when all were
    done, ``partly_done`` when some were done and another was not (it failed or never ran:
    unavailable, refused...), ``failed`` when one failed and none was done; else the first
    one's (dry_run, killed, refused...). The decision's ``steps`` say which was which."""
    if not statuses:
        return None
    # a status the write path does not name is never read as done or as not run
    if any(s not in NOT_RUN and s not in ("done", "failed") for s in statuses):
        return "unknown"
    if all(s == "done" for s in statuses):
        return "done"
    if "done" in statuses:
        return "partly_done"
    return "failed" if "failed" in statuses else statuses[0]


AUDIT_EVENTS = ("decision", "attempt", "outcome", "reconciled", "gate_passed", "gate_lost",
                "owner_notified", "switch", *aw.OPT_IN_EVENTS)
NOT_YOURS = ("Only the person themselves can switch their autopilot on or off. You can see "
             "who has it on, but not change it for them.")
NOT_ELIGIBLE = ("This workflow hasn't passed its check for you, so it can't be switched on "
                "yet.")
NOT_OWNER = "Only the owner changes the autopilot's switches."

try:
    from zoneinfo import ZoneInfo
    _LOCAL: Any = ZoneInfo("America/Toronto")
except Exception:  # noqa: BLE001 - no tz database: the UTC day
    _LOCAL = timezone.utc


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _instant(raw: Any) -> Optional[datetime]:
    try:
        out = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return out if out.tzinfo else None


def _score(counts: Optional[Mapping[str, Any]]) -> Optional[m.AutopilotScore]:
    if not counts:
        return None
    judged, matched = int(counts.get("judged") or 0), int(counts.get("matched") or 0)
    pct = None
    if judged > 0:
        pct = float((Decimal(matched) * 100 / Decimal(judged)).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP))
    return m.AutopilotScore(judged=judged, matched=matched, percent=pct)


def _gate_doc(path: Path, schema: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw.get("roles") or {} if isinstance(raw, dict) and raw.get("schema") == schema \
        else {}


def system_of(item: Mapping[str, Any]) -> str:
    """The system an item's work is in: its prepared entry's system, an MC decision, the
    event's channel, else its first citation's scheme."""
    prepared = (item.get("body") or {}).get("prepared") or {}
    happened = (item.get("body") or {}).get("happened") or {}
    write = prepared.get("write")
    if isinstance(write, Mapping) and write.get("system"):
        return str(write["system"]).lower()
    action = prepared.get("action")
    if isinstance(action, Mapping) and action.get("kind") == "mc_approval":
        return "mc"
    if str(happened.get("object_type") or "") in autopilot_events.TEAMS_TYPES:
        return "teams"
    if ":sage" in str(happened.get("object_id") or "").lower():
        return "sage"
    for cite in item.get("citations") or ():
        scheme = str(cite).split("://", 1)[0]
        if scheme in SYSTEM_OF_SCHEME:
            return SYSTEM_OF_SCHEME[scheme]
    return "other"


def _person_name(role: str) -> str:
    """A ``person:<address>`` role's person, by the address's first word; '' for an opaque
    id or UUID so the directory name lookup resolves it instead."""
    from operating.assist_matters import name_from_token
    raw = role[len("person:"):].strip()
    local = raw.split("@", 1)[0] if "@" in raw else raw
    return name_from_token(local)


class AutopilotAreaMixin(AssistSwitchMixin):
    """Needs ``AutopilotMixin``. Tests may set ``autopilot_write_state`` (the write path's
    state dir), ``autopilot_adapters`` (kind -> adapter; a test never reaches a real
    system), ``autopilot_guard`` (the people exclusion) and ``autopilot_owner``
    (``reader -> bool``); without that hook the owner is the access layer's owner-open
    reader (``_is_owner``), and a backend without one has no owner (fail closed)."""

    autopilot_write_state: Optional[Path] = None
    autopilot_adapters: Any = None
    autopilot_guard: Any = None
    autopilot_owner: Any = None
    autopilot_admin: Any = None
    autopilot_writer: Any = None       # tests: the reply writer (else the environment's model)
    autopilot_people: Any = None       # tests: the people exclusion the writer's screens read

    # ------------------------------------------------------------ plumbing

    def _apa_state(self) -> Optional[Path]:
        if self.autopilot_write_state is not None:
            return Path(self.autopilot_write_state)
        state = getattr(self, "state_dir", None)
        return Path(state) / "autopilot-write" if state else None

    def _apa_is_owner(self, reader: Reader) -> bool:
        if self.autopilot_owner is not None:
            return bool(self.autopilot_owner(reader))
        check = getattr(self, "_is_owner", None)
        return bool(check(reader)) if callable(check) else False

    def _apa_sees_opt_ins(self, reader: Reader) -> bool:
        """The owner or an admin (``autopilot_admin`` hook, else the configured admins of
        ``feedback.is_admin``): sees who switched what on, never switches it for them."""
        if self._apa_is_owner(reader):
            return True
        if self.autopilot_admin is not None:
            return bool(self.autopilot_admin(reader))
        from .feedback import is_admin
        try:
            return bool(is_admin(reader))
        except Exception:  # noqa: BLE001 - undecidable: not an admin
            return False

    def _apa_owner_only(self, reader: Reader) -> Path:
        if not self._apa_is_owner(reader):
            raise UserFacing("forbidden", NOT_OWNER, 403)
        state = self._apa_state()
        if state is None:
            raise UserFacing("action_refused", "The autopilot's state directory is not "
                             "configured on this server.", 409)
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        return state

    def _apa_journal(self, state: Path) -> Journal:
        return Journal(state / aw.AUDIT_FILE)

    def _apa_rows(self, state: Optional[Path]) -> list[dict[str, Any]]:
        if state is None or not (state / aw.AUDIT_FILE).exists():
            return []
        return self._apa_journal(state).rows()

    def _apa_replaced(self) -> dict[str, str]:
        """Which item replaced which (the engine's settle: one card per lot); {} when the
        queue is not there or its note unreadable (nothing is carried then)."""
        path = self._ap_queue_path()  # type: ignore[attr-defined]
        if path is None or not Path(path).exists():
            return {}
        try:
            queue = ap.Queue(path)
            try:
                got = queue.meta(autopilot_queue.SUPERSEDED_BY)
            finally:
                queue.close()
        except (OSError, ValueError, sqlite3.Error):
            return {}
        return {str(k): str(v) for k, v in got.items()} if isinstance(got, Mapping) else {}

    def _apa_decisions(self, state: Optional[Path],
                       replaced: Optional[Mapping[str, str]] = None) -> dict[str, dict[str, Any]]:
        if state is None or not (state / DECISIONS_FILE).exists():
            return {}
        last: dict[str, dict[str, Any]] = {}
        for row in Journal(state / DECISIONS_FILE).rows():
            iid = str(row.get("item_id"))
            if row.get("decision") == "open":
                last.pop(iid, None)
            else:
                last[iid] = row
        return carried(last, self._apa_replaced() if replaced is None else replaced,
                       lambda r: r.get("decision") == "snooze")

    def autopilot_reopen(self, reader: Reader, item_id: str, *,
                         reason: Optional[str] = None) -> bool:
        """Undo a prior decision on a reader's Autopilot queue item in the decisions journal."""
        state = self._apa_state()
        if state is None:
            return False
        me = set(self._apa_me(reader))
        is_owner = self._apa_is_owner(reader)
        decisions_path = state / DECISIONS_FILE
        if not decisions_path.exists():
            return False
        prior = None
        for row in Journal(decisions_path).rows():
            if str(row.get("item_id") or "") == item_id:
                prior = row
        if prior is None or prior.get("decision") == "open":
            return False
        if not (is_owner or prior.get("by") in me):
            return False
        now = datetime.now(timezone.utc)
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with Journal(decisions_path).locked() as held:
            held.append({"event": "item_decision", "item_id": item_id, "decision": "open",
                         "at": _iso(now), "by": sorted(me)[0],
                         "note": (reason or "").strip() or None})
        from .assist_routes import _bump_assist_drafts
        _bump_assist_drafts(self)
        return True

    @staticmethod
    def _apa_never(state: Optional[Path], me: set[str]) -> list[str]:
        """The senders a person said never to prepare anything for (their own rows)."""
        if state is None or not (state / NEVER_FILE).exists():
            return []
        last: dict[str, bool] = {}
        for row in Journal(state / NEVER_FILE).rows():
            if (row.get("by") in me or me & set(row.get("ids") or ())) \
                    and isinstance(row.get("sender"), str):
                last[row["sender"]] = bool(row.get("never"))
        return sorted(s for s, on in last.items() if on)

    def autopilot_never(self, reader: Reader, body: m.AutopilotNeverRequest
                        ) -> m.AutopilotDrafts:
        """The reader says never (or again) to prepare anything for them from one sender:
        their items from that sender leave their list. Only ever their own; never needs
        the sender of an item on their list. Nothing is sent or written anywhere else."""
        state = self._apa_state()
        if state is None:
            raise UserFacing("action_refused", "The autopilot's state directory is not "
                             "configured on this server.", 409)
        sender = body.sender.strip().lower()
        if body.never and not any(
                item["mine"] and sender_of(item) == sender
                for item in self._ap_visible(reader) or ()):  # type: ignore[attr-defined]
            raise UserFacing("not_found", "No item from that sender is on your list.", 404)
        (state / NEVER_FILE).parent.mkdir(parents=True, exist_ok=True)
        with Journal(state / NEVER_FILE).locked() as held:
            held.append({"event": "never_sender", "sender": sender, "never": body.never,
                         "at": _iso(datetime.now(timezone.utc)),
                         "by": sorted(self._apa_me(reader))[0],
                         "ids": sorted(self._apa_me(reader))})
        from .assist_routes import _bump_assist_drafts, matters_list   # after a write: read now, never kept
        _bump_assist_drafts(self)
        return matters_list(self, reader, fresh=True)

    @staticmethod
    def _apa_auto_would(state: Optional[Path], me: set[str]) -> Callable[[Mapping[str, Any]], str]:
        """item -> a dry run of Auto on it, read only: "" unless the reader's replay score
        earned its workflow (``aw.eligible``), else "send" when every action of it is
        routine (``aw.routine``, without the conversation's domains: never laxer than the
        executor) or "keep: <reasons>" when Auto would still wait for the click."""
        try:
            rules = aw.load_rules()
        except aw.RulesInvalid:
            rules = None
        if state is None or rules is None:
            return lambda item: ""
        from operating import assist_switches
        from operating import workflows as wf
        gate = aw.WriteGate(state / aw.WRITE_GATE_FILE, rules)

        def would(item: Mapping[str, Any]) -> str:
            try:
                flow = wf.of_item(str(item.get("autopilot")),
                                  (item.get("body") or {}).get("happened") or {})
                if not any(aw.eligible(gate, p, flow) for p in me):
                    return ""
                actions = aw.actions_of(item)
                why = [r for a in actions for r in aw.routine(rules, item, a, flow)] \
                    if actions else ["no_action"]
                why += assist_switches.reasons(state, me, item, flow,
                                               datetime.now(timezone.utc), gate)
            except Exception:  # noqa: BLE001 - a dry run never stops the list
                return ""
            return "send" if not why else ("keep: " + ", ".join(dict.fromkeys(why)))
        return would

    def _apa_handed(self, state: Optional[Path],
                    replaced: Optional[Mapping[str, str]] = None) -> dict[str, dict[str, Any]]:
        """item -> the last row passing it on (``HAND_FILE``)."""
        if state is None or not (state / HAND_FILE).exists():
            return {}
        return carried({str(row.get("item_id")): row for row in Journal(state / HAND_FILE).rows()},
                       self._apa_replaced() if replaced is None else replaced)

    @staticmethod
    def _apa_to_me(hands: Mapping[str, Mapping[str, Any]], me: set[str]) -> tuple[str, ...]:
        return tuple(i for i, row in hands.items() if row.get("to") in me)

    def autopilot_colleagues(self, reader: Reader) -> m.AutopilotColleagues:
        """Whom an alert can be passed to: every active staff member the people exclusion
        clears now, never the reader (``view_as.ViewAs.people``): a key and a name each;
        nobody without the list or the exclusion (fail closed)."""
        viewing = getattr(self, "view_as", None)
        if viewing is None or not viewing.available:
            return m.AutopilotColleagues(people=[])
        return m.AutopilotColleagues(people=[
            m.ViewAsPerson(**p) for p in viewing.people(self._apa_me(reader))])

    def autopilot_hand(self, reader: Reader, item_id: str, body: m.AutopilotHandRequest
                       ) -> m.AutopilotDrafts:
        """Pass an alert on the reader's list (their own, or one passed to them) to a
        colleague: it shows on the colleague's list too, under the colleague's own access
        (an item they may not read is not shown to them), only to set aside there, and stays
        on the reader's list marked passed. Only an alert; nothing is decided, sent or
        written anywhere else."""
        state = self._apa_state()
        if state is None:
            raise UserFacing("action_refused", "The autopilot's state directory is not "
                             "configured on this server.", 409)
        me, hands = self._apa_me(reader), self._apa_handed(state)
        found = next((i for i in self._ap_visible(  # type: ignore[attr-defined]
            reader, also=self._apa_to_me(hands, me)) or ()
            if i["item_id"] == item_id and (i["mine"] or i.get("handed"))), None)
        if found is None:
            raise UserFacing("not_found", "That item is not on your list.", 404)
        if alert_of(found, autopilot_engine.shown_item(found)) is None:
            raise UserFacing("bad_request", "Only an alert can be passed on here.", 422)
        viewing = getattr(self, "view_as", None)
        to = viewing.named(body.to, me) if viewing is not None and viewing.available else None
        if to is None:
            raise UserFacing("not_found", "That person cannot be given items.", 404)
        (state / HAND_FILE).parent.mkdir(parents=True, exist_ok=True)
        with Journal(state / HAND_FILE).locked() as held:
            held.append({"event": "item_hand", "item_id": item_id, "to": to[0],
                         "to_name": to[1], "by": sorted(me)[0],
                         "by_name": viewing.name_among(me),
                         "at": _iso(datetime.now(timezone.utc)),
                         "note": (body.note or "").strip() or None})
        from .assist_routes import _bump_assist_drafts, matters_list   # after a write: read now, never kept
        _bump_assist_drafts(self)
        return matters_list(self, reader, fresh=True)

    def _apa_exclusion(self, env: Mapping[str, str] | None = None) -> Any:
        """The people exclusion the writer's screens read; None when it cannot be loaded."""
        if self.autopilot_people is not None:
            return self.autopilot_people
        try:
            from integrations.people_exclusion import environ_with_exclusion
            from operating.chunking.permissions import load_exclusion
            base = getattr(self, "state_dir", None)
            return load_exclusion(Path(base), environ_with_exclusion(
                dict(env if env is not None else self._ap_env()))) if base else None  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - no exclusion: fail closed at the caller
            return None

    def _apa_pools(self, reader: Reader) -> tuple[str, ...]:
        """The pooled owners of the roles the reader is in (the roles file); none without
        a reader identity or the file."""
        me = self._apa_me(reader) - {"unknown"}
        if not me:
            return ()
        return tuple(sorted(autopilot_queue.POOL_PREFIX + r for r, people in
                            self._apa_roles().items()
                            if me & {str(p).lower() for p in people or ()}))

    def autopilot_take(self, reader: Reader, item_id: str) -> m.AutopilotDrafts:
        """Take an item the reader's role holds (``pooled_card``): it becomes theirs alone,
        with its prepared work to check and confirm as any item of theirs; gone from the
        others' lists. Refused when someone took it first or it is not the reader's role's.
        Nothing is sent or written anywhere else."""
        state = self._apa_state()
        path = self._ap_queue_path()  # type: ignore[attr-defined]
        if state is None or path is None or not Path(path).exists():
            raise UserFacing("action_refused", "The autopilot's state directory is not "
                             "configured on this server.", 409)
        me = sorted(self._apa_me(reader) - {"unknown"})
        found = next((i for i in self._ap_visible(  # type: ignore[attr-defined]
            reader, pools=self._apa_pools(reader)) or ()
            if i["item_id"] == item_id and i.get("pooled")), None) if me else None
        if found is None:
            raise UserFacing("not_found", "That item is not on your role's list (taken "
                             "already, or done).", 404)
        people = self._apa_exclusion()
        if people is None:
            raise UserFacing("action_refused", "The people screen is not available, so the "
                             "item cannot be made yours now.", 409)
        queue = ap.Queue(Path(path))
        try:
            took = queue.take(item_id, str(found["pooled"]), me[0],
                              readdress=ap.taker_readdress(queue, item_id, me[0], people))
        finally:
            queue.close()
        if not took:
            raise UserFacing("conflict", "A colleague took it first.", 409)
        (state / TAKE_FILE).parent.mkdir(parents=True, exist_ok=True)
        with Journal(state / TAKE_FILE).locked() as held:
            held.append({"event": "item_take", "item_id": item_id, "pool": found["pooled"],
                         "by": me[0], "at": _iso(datetime.now(timezone.utc))})
        from .assist_routes import _bump_assist_drafts, matters_list   # after a write: read now, never kept
        _bump_assist_drafts(self)
        return matters_list(self, reader, fresh=True)

    def _apa_roles(self) -> dict[str, list[str]]:
        """The roles file's members (``autopilot._roles``) beside the queue."""
        path = self._ap_queue_path()  # type: ignore[attr-defined]
        return ap._roles(self._ap_env(), path.parent) if path else {}  # type: ignore[attr-defined]

    def _apa_live_of(self, env: Mapping[str, str], state: Optional[Path]):
        """item -> the kinds an accept would perform live (none without a state dir)."""
        if state is None:
            return lambda _item: []
        executor = aw.Executor(state, environ=dict(env), adapters=self.autopilot_adapters
                               if self.autopilot_adapters is not None else {}, guard=None)
        rows = executor.journal.rows()

        def live_of(item: Mapping[str, Any]) -> list[str]:
            if not item.get("mine"):
                return []
            try:
                return executor.live_kinds(item, rows)
            except Exception:  # noqa: BLE001 - unknown: shown live, so it asks first
                return ["unknown"]
        return live_of

    def _apa_executor(self, state: Path) -> aw.Executor:
        env = dict(self._ap_env())  # type: ignore[attr-defined]
        guard = self.autopilot_guard
        if guard is None:
            from operating.card_actions import exclusion_for
            base = getattr(self, "state_dir", None)
            guard = exclusion_for(env, Path(base) if base else None)
        return aw.Executor(state, environ=env, adapters=self.autopilot_adapters, guard=guard)

    def _apa_drafted(self, env: Mapping[str, str], reader: Reader, me: Any,
                     everyone: bool) -> dict[str, frozenset[str]]:
        """Person id -> the workflows the owner's Draft file (``autopilot.load_draft``)
        turns on for them, resolved as the live feed resolves it
        (``autopilot_engine.draft_owners``): the reader's own entry (an address or id the
        signed-in session holds) as their id; for the owner and admins (``everyone``) every
        entry, an address through the people exclusion's directory. Each person must be
        cleared by the people exclusion; with it unavailable, nobody (fail closed)."""
        draft = ap.load_draft(env)
        mine = sorted(me)
        held = _identities(reader) | set(mine)
        if not draft or not (everyone or held & set(draft)):
            return {}
        guard = self.autopilot_people if self.autopilot_people is not None \
            else self.autopilot_guard
        if guard is None:
            from .feedback import exclusion
            guard = exclusion()
        directory: dict[str, str] = {}
        for who in draft:
            if who in held:
                if len(mine) == 1:
                    directory[who] = mine[0]
            elif everyone and guard is not None:
                try:
                    found = guard.object_id_of(who)
                except Exception:  # noqa: BLE001 - unresolved: nothing in draft for them
                    found = None
                if found:
                    directory[who] = str(found).lower()
        drafted = autopilot_engine.draft_owners(draft, directory, _Cleared(guard))
        return drafted if everyone else {k: v for k, v in drafted.items() if k in me}

    @staticmethod
    def _apa_off(env: Mapping[str, str], state: Optional[Path]) -> tuple[bool, set[str], bool]:
        """(global kill, workflows off, the workflow file unreadable)."""
        if state is None:
            return bool(env.get(aw.KILL_ENV) or env.get(autopilot_queue.KILL_ENV)), set(), False
        glob = aw.killed(env, state, "") == "global_kill_switch"
        off = {w.strip() for w in (env.get(aw.WORKFLOW_KILL_ENV) or "").split(",") if w.strip()}
        path, broken = state / aw.WORKFLOW_KILL_FILE, False
        if path.exists():
            try:
                off |= {str(w) for w in json.loads(path.read_text(encoding="utf-8"))["workflows"]}
            except (OSError, ValueError, KeyError, TypeError):
                broken = True
        return glob, off, broken

    # ------------------------------------------------------------ the area

    def _area_read(self, reader: Reader) -> m.AutopilotArea:
        """The area read now, never a kept copy (``WorkspaceBackend.autopilot_area`` serves
        the reader's kept one): what a decision or a switch returns and reads from."""
        return AutopilotAreaMixin.autopilot_area(self, reader)

    def autopilot_area(self, reader: Reader) -> m.AutopilotArea:
        now = datetime.now(timezone.utc)
        owner = self._apa_is_owner(reader)
        env = self._ap_env()  # type: ignore[attr-defined]
        state = self._apa_state()
        queue_path = self._ap_queue_path()  # type: ignore[attr-defined]
        notes: list[str] = []
        seen_all = self._ap_visible(reader, everyone=owner,  # type: ignore[attr-defined]
                                    states=("open", "done"))
        if seen_all is None:
            raw_items: list[dict[str, Any]] = []
            notes.append("The autopilot has not prepared anything on this server yet.")
        else:
            raw_items = [i for i in seen_all if i.get("state", "open") == "open"]
        glob, off, broken = self._apa_off(env, state)
        if broken:
            notes.append("The per-workflow switch file is unreadable: every workflow is "
                         "treated as off until the owner sets a switch again.")
        decisions = self._apa_decisions(state)
        from operating.proactive_commitments import owner_ids
        me = owner_ids(_principals(reader))
        registry = ap.load_registry()
        specs = {s.get("id"): s for s in registry.get("autopilots") or ()}
        role_members = self._apa_roles()
        dev = _gate_doc(Path(env.get(autopilot_engine.GATE_ENV) or (queue_path.parent / autopilot_engine.GATE_FILE))
                        if queue_path else Path("/nonexistent"), autopilot_engine.GATE_SCHEMA)
        # held-out scores: what the write gate counts (``aw.gate_counts``: fresh days and
        # clean past windows, re-checked against the rules at this read)
        try:
            rules = aw.load_rules()
        except aw.RulesInvalid:
            rules = None
        fresh = aw.gate_counts(state, rules) if state and rules else {}
        member_of: dict[str, set[str]] = {}
        for source in (role_members, {r: v.get("members") or () for r, v in dev.items()},
                       {r: v.get("members") or () for r, v in fresh.items()}):
            for role, members in source.items():
                for oid in members:
                    member_of.setdefault(str(oid).lower(), set()).add(role)
        today = now.astimezone(_LOCAL).date()
        # which open items an accept would perform live now (the server's view: the
        # owner's per-kind switch or a live gate), so the page asks for the confirm
        live_of = self._apa_live_of(env, state)
        items: list[m.AutopilotItem] = []
        last_at: dict[str, str] = {}
        used_cache: dict[str, Any] = {}
        for item in raw_items:
            happened = item["body"].get("happened") or {}
            flow = wf.of_item(item["autopilot"], happened)
            roles = set(ap.item_roles(specs.get(item["autopilot"]) or {}))
            roles |= {r for r in member_of.get(item["owner"], ()) if not r.startswith("person:")}
            dec = decisions.get(item["item_id"])
            item_state, decision = "waiting", None
            if dec:
                until = _instant(dec.get("until"))
                if dec["decision"] == "snooze" and until is not None and until <= now:
                    dec = None
                else:
                    item_state = {"accept": "accepted", "reject": "rejected",
                                  "snooze": "snoozed", "edit": "edited"}[dec["decision"]]
                    if ao.refused_accept(dec):
                        # the write path refused or failed it: nothing went out, the item
                        # still waits on its person (the decision shows why)
                        item_state = "waiting"
                    decision = m.AutopilotDecision(
                        decision=dec["decision"], at=dec["at"], by_you=dec.get("by") in me,
                        until=dec.get("until"), text=dec.get("text"), note=dec.get("note"),
                        outcome=dec.get("outcome"), reasons=list(dec.get("reasons") or ()),
                        steps=[m.AutopilotActionResult(
                            kind=str(step.get("kind") or ""),
                            system=str(step.get("system") or "other"),
                            status=str(step.get("status") or "unknown"),
                            reasons=[str(x) for x in step.get("reasons") or ()][:2])
                            for step in dec.get("steps") or () if isinstance(step, dict)],
                        sent=decision_sent(dec, item))
            created = _instant(item["created_at"])
            due = _instant(item["due_at"])
            is_today = bool((created and created.astimezone(_LOCAL).date() == today) or
                            (due and due.astimezone(_LOCAL).date() <= today))
            last_at[flow] = max(last_at.get(flow, ""), item["created_at"])
            # a sender shown by address or "Someone", never a bare object id
            shown = autopilot_engine.shown_item(item)
            items.append(m.AutopilotItem(
                item_id=item["item_id"], autopilot=item["autopilot"],
                label=item["body"].get("label") or item["autopilot"], title=shown["title"],
                workflow=flow, system=system_of(item), roles=sorted(roles),
                owner=item["owner"], mine=item["mine"],
                happened=shown["body"].get("happened") or {},
                prepared=shown["body"].get("prepared") or {}, citations=item["citations"],
                due_at=item["due_at"], created_at=item["created_at"], today=is_today,
                state=item_state, decision=decision,
                live_kinds=(kinds := live_of(item) if item_state in ("waiting", "edited")
                            else []), live=bool(kinds),
                used_for_draft=self._apa_used(reader, item, used_cache)))
        # what became of each prepared item this reader may see, settled ones too (the
        # same audience, marking and citation checks as the list; counts only)
        from operating import autopilot_outcomes as outcomes_of
        outcomes = m.AutopilotOutcomes(**outcomes_of.measure(seen_all, decisions, now)) \
            if seen_all else None
        rows = self._apa_rows(state)
        for row in rows:
            if row.get("workflow") and row.get("at"):
                last_at[row["workflow"]] = max(last_at.get(row["workflow"], ""), row["at"])
        # people: the person roles (named by their address) and every owner shown
        people: dict[str, m.AutopilotPerson] = {}
        for role, entry in list(dev.items()) + list(fresh.items()):
            if role.startswith("person:"):
                for oid in entry.get("members") or ():
                    oid = str(oid).lower()
                    if owner or oid in me:
                        people.setdefault(oid, m.AutopilotPerson(id=oid, name=_person_name(role)))
        # an owner the roster names only by id: the name a message of theirs this reader
        # may see states (a Teams sender's ``from.display_name``), never a guess
        stated: dict[str, str] = {}
        for raw in raw_items:
            happened = raw["body"].get("happened") or {}
            name = autopilot_engine.person_name(happened.get("sender_name"))
            if name and happened.get("sender"):
                stated.setdefault(str(happened["sender"]).lower(), name)
        for it in items:
            people.setdefault(it.owner, m.AutopilotPerson(id=it.owner, name=stated.get(
                str(it.owner).lower(), "")))
        # still unnamed (the roster holds ids): the staff list's name, for a person the
        # people exclusion clears now ("Quality colleague 2" otherwise)
        viewing = getattr(self, "view_as", None)
        unnamed = [oid for oid, p in people.items() if not p.name]
        if unnamed and viewing is not None and viewing.available:
            for oid, name in viewing.names_of(unnamed).items():
                for key in (oid, *(k for k in unnamed if k.lower() == oid)):
                    if key in people and not people[key].name:
                        people[key].name = name
        for oid, p in people.items():
            p.you = oid in me
            p.roles = sorted(member_of.get(oid, ()))
        # per role and workflow: scores, state, switches (a reader who is not the owner
        # sees only the roles they belong to)
        mine_roles = set().union(*(member_of.get(o, set()) for o in me)) if me else set()
        workflows: list[m.AutopilotWorkflowRow] = []
        gate = aw.WriteGate(state / aw.WRITE_GATE_FILE, rules) if state and rules else None
        if rules is None:
            notes.append("The write rules are unreadable: nothing is performed.")
        if gate is not None and state is not None and any(
                o["on"] and not aw.eligible(gate, p, w) for (p, w), o in aw.opt_ins(rows).items()):
            # an opt-in the gate no longer carries is suspended now, with why, not only at
            # the next scoring or run (it then stays off until its person switches it on);
            # the journal is locked only when there is one to suspend. Unwritable here: the
            # page still reads (the write path re-checks the gate and suspends it itself)
            try:
                with self._apa_journal(state).locked() as held:
                    if aw.suspend_lapsed(held, gate, now):
                        rows = list(held.rows)
            except OSError:
                notes.append("A switch whose score fell under the bar could not be recorded "
                             "as off just now; it does nothing either way.")
        passing = set(gate.passing()) if gate else set()
        master = env.get(aw.MASTER_ENV) == "1"
        sees_all = self._apa_sees_opt_ins(reader)
        chosen = aw.opt_ins(rows)
        stopped = aw.paused(rows)

        def acting(person: str, flow: str) -> bool:
            """``person`` switched ``flow`` on, has not paused and the gate carries it."""
            found = chosen.get((person, flow))
            return bool(found and found["on"] and person not in stopped and gate is not None
                        and aw.eligible(gate, person, flow))
        open_by: dict[str, int] = {}
        for it in items:
            if it.state in ("waiting", "edited"):
                open_by[it.workflow] = open_by.get(it.workflow, 0) + 1
        # Draft (``autopilot.load_draft``): per person role, the workflows prepared before
        # the gate passed; the counts the write gate admitted give the countdown
        # (a person role a gate document measured: a draft for someone never replayed
        # still prepares their items, it has no scores to show here)
        drafted = {r: f for r, f in draft_roles(ap.load_draft(env), {**dev, **fresh}).items()
                   if r in dev or r in fresh}
        held = _identities(reader)
        published = aw.gate_counts(state, rules) if gate is not None and rules else {}
        for role in sorted(set(dev) | set(fresh) | set(drafted)):
            if not owner and role not in mine_roles and not (
                    role in drafted and role[len("person:"):] in held):
                continue
            d, f = dev.get(role) or {}, fresh.get(role) or {}
            person = None
            if role.startswith("person:"):
                found = list((d or f).get("members") or ())
                person = str(found[0]).lower() if len(found) == 1 else None
            flows = set((d.get("workflows") or {})) | set((f.get("workflows") or {})) | \
                set(drafted.get(role) or ())
            members = {str(x).lower() for x in (d.get("members") or ())} | \
                {str(x).lower() for x in (f.get("members") or ())}
            if gate is not None and role in gate.roles:
                members |= gate.roles[role][0]
            # who of this row the reader may count: everyone for the owner and admins,
            # else only themselves
            counted = members if sees_all else members & me
            for flow in sorted(flows):
                ds, fs = _score((d.get("workflows") or {}).get(flow)), \
                    _score((f.get("workflows") or {}).get(flow))
                n_on = sum(1 for p in counted if acting(p, flow))
                missing: list[str] = []
                if glob or flow in off or broken:
                    st = "off"
                elif (role, flow) not in passing:
                    st, missing = "dry_run", ["gate_not_passed"]
                elif not n_on:
                    st, missing = "passed", ["not_switched_on_by_person"]
                elif not master:
                    st, missing = "live_held", ["master_switch_off"]
                else:
                    st = "live"
                window = f.get("window") or []
                workflows.append(m.AutopilotWorkflowRow(
                    role=role, person=person, workflow=flow, dev=ds, fresh=fs,
                    n=(fs or ds).judged if (fs or ds) else 0, state=st,
                    on=not (flow in off or broken), switched_on=n_on, missing=missing,
                    last_run=last_at.get(flow) or None,
                    scored_at=str(window[-1]) if window else None,
                    open_items=open_by.get(flow, 0),
                    draft=flow in (drafted.get(role) or ()) and st == "dry_run",
                    countdown=_countdown(published.get(role), flow, rules, gate)))
        if not dev and not fresh:
            notes.append("No replay has scored the workflows yet.")
        # what is prepared for whom: the rule the live feed reads
        # (``autopilot_engine.SwitchGate``): a person's own switch or the owner's Draft file
        # for them, their own off or pause winning over the Draft file. With nothing on for
        # this reader, that is why nothing is here
        prep = aw.prepared_for(rows, self._apa_drafted(env, reader, me, everyone=sees_all),
                               autopilot_engine.DRAFT_NEVER)
        if not items and not any(prep.get(o) for o in me):
            notes.insert(0, NO_DRAFTS_ON)
        mine_opt, paused_me = self._apa_my_opt_ins(gate, chosen, stopped, me)
        switched_on = None
        if sees_all:
            switched_on = [m.AutopilotOptInPerson(person=p, workflow=w, since=o.get("at"),
                                                  paused=p in stopped)
                           for (p, w), o in sorted(chosen.items()) if o["on"]]
            for entry in switched_on:
                people.setdefault(entry.person, m.AutopilotPerson(
                    id=entry.person, name="", you=entry.person in me,
                    roles=sorted(member_of.get(entry.person, ()))))
        roles_seen = {r for r in set(dev) | set(fresh) | set(role_members)
                      if not r.startswith("person:")}
        roles_seen |= {r for it in items for r in it.roles}
        if not owner:
            roles_seen &= mine_roles | {r for it in items for r in it.roles}
        # the owner is told once of each workflow's first gate pass (AP-22): pending until
        # they acknowledge it; information only, it switches nothing on
        gate_notices = [m.AutopilotGateNotice(role=str(n["role"]), workflow=str(n["workflow"]),
                                              passed_at=str(n["passed_at"]))
                        for n in aw.notices(rows)] if owner else None
        mine: dict[str, tuple[str, Any]] = {}
        for oid in sorted(me):
            for flow, how in (prep.get(oid) or {}).items():
                if flow not in mine or mine[flow][0] == aw.BY_OWNER:
                    mine[flow] = how
        conditions = _own_conditions(state, me)
        my_drafts = [_with_mode(_choice_row(cid, label, flows, mine), flows, mine, chosen, me,
                                gate, conditions.get(cid))
                     for cid, label, flows in aw.DRAFT_CHOICES]
        drafts_who = None
        if sees_all:
            drafts_who = [m.AutopilotDraftsPerson(person=p, choice=c.choice, label=c.label,
                                                  by_owner=c.by_owner, partly=c.partly)
                          for p, on in sorted(prep.items())
                          for c in (_choice_row(cid, label, flows, on)
                                    for cid, label, flows in aw.DRAFT_CHOICES) if c.on]
            for entry in drafts_who:
                people.setdefault(entry.person, m.AutopilotPerson(
                    id=entry.person, name="", you=entry.person in me,
                    roles=sorted(member_of.get(entry.person, ()))))
        from operating.autopilot_fast import last_ran
        return m.AutopilotArea(
            owner=owner, gate_notices=gate_notices, my_drafts=my_drafts,
            my_opt_ins=mine_opt, paused=paused_me, switched_on=switched_on,
            drafts_switched_on=drafts_who,
            switches=m.AutopilotSwitches(
                global_off=glob, master_switch=master,
                autopilot_off=bool(str(env.get(autopilot_queue.KILL_ENV, "")).lower() in
                                   ("1", "true", "yes", "on") or
                                   (queue_path is not None and
                                    (queue_path.parent / autopilot_queue.KILL_FILE).exists())),
                workflows_off=sorted(off) if owner else sorted(
                    off & {w.workflow for w in workflows})),
            people=sorted(people.values(), key=lambda p: (not p.you, not p.name, p.name, p.id)),
            roles=sorted(roles_seen), items=items, workflows=workflows, notes=notes,
            outcomes=outcomes, checked_at=last_ran(queue_path) if queue_path else None,
            known_workflows=_known_workflows() if owner else sorted(
                {w.workflow for w in workflows} | {it.workflow for it in items}))

    # ------------------------------------------------------------ decisions

    def _apa_find_item(self, reader: Reader, item_id: str, *, also: tuple[str, ...] = ()
                       ) -> Optional[dict[str, Any]]:
        """Look up one visible queue item without scanning the entire queue when possible."""
        one = getattr(self, "_ap_visible_one", None)
        if callable(one):
            return one(reader, item_id, also=also)
        for item in self._ap_visible(reader, also=also) or ():  # type: ignore[attr-defined]
            if item["item_id"] == item_id:
                return item
        return None

    def _apa_item_view(self, reader: Reader, item: Mapping[str, Any],
                       dec: Optional[Mapping[str, Any]], now: datetime,
                       *, offer: Optional[str] = None) -> m.AutopilotItem:
        """Build one ``AutopilotItem`` response without rebuilding the entire ``AutopilotArea``."""
        env = self._ap_env()  # type: ignore[attr-defined]
        state = self._apa_state()
        from operating.proactive_commitments import owner_ids
        me = owner_ids(_principals(reader))
        registry = ap.load_registry()
        specs = {s.get("id"): s for s in registry.get("autopilots") or ()}
        role_members = self._apa_roles()
        member_of: dict[str, set[str]] = {}
        for role, members in role_members.items():
            for oid in members:
                member_of.setdefault(str(oid).lower(), set()).add(role)
        today = now.astimezone(_LOCAL).date()
        happened = item["body"].get("happened") or {}
        flow = wf.of_item(item["autopilot"], happened)
        roles = set(ap.item_roles(specs.get(item["autopilot"]) or {}))
        roles |= {r for r in member_of.get(item["owner"], ()) if not r.startswith("person:")}
        item_state, decision = "waiting", None
        if dec:
            until = _instant(dec.get("until"))
            if dec["decision"] == "snooze" and until is not None and until <= now:
                dec = None
            else:
                item_state = {"accept": "accepted", "reject": "rejected",
                              "snooze": "snoozed", "edit": "edited"}[dec["decision"]]
                if ao.refused_accept(dec):
                    item_state = "waiting"
                decision = m.AutopilotDecision(
                    decision=dec["decision"], at=dec["at"], by_you=dec.get("by") in me,
                    until=dec.get("until"), text=dec.get("text"), note=dec.get("note"),
                    outcome=dec.get("outcome"), reasons=list(dec.get("reasons") or ()),
                    steps=[m.AutopilotActionResult(
                        kind=str(step.get("kind") or ""),
                        system=str(step.get("system") or "other"),
                        status=str(step.get("status") or "unknown"),
                        reasons=[str(x) for x in step.get("reasons") or ()][:2])
                        for step in dec.get("steps") or () if isinstance(step, dict)],
                    sent=decision_sent(dec, item))
        created = _instant(item["created_at"])
        due = _instant(item["due_at"])
        is_today = bool((created and created.astimezone(_LOCAL).date() == today) or
                        (due and due.astimezone(_LOCAL).date() <= today))
        shown = autopilot_engine.shown_item(item)
        live_kinds = self._apa_live_of(env, state)(item) if item_state in ("waiting", "edited") else []
        return m.AutopilotItem(
            item_id=item["item_id"], autopilot=item["autopilot"],
            label=item["body"].get("label") or item["autopilot"], title=shown["title"],
            workflow=flow, system=system_of(item), roles=sorted(roles),
            owner=item["owner"], mine=item["mine"],
            happened=shown["body"].get("happened") or {},
            prepared=shown["body"].get("prepared") or {}, citations=item["citations"],
            due_at=item["due_at"], created_at=item["created_at"], today=is_today,
            state=item_state, decision=decision,
            live_kinds=live_kinds, live=bool(live_kinds),
            confirm_token=offer,
            used_for_draft=self._apa_used(reader, item, {}))

    def autopilot_decide(self, reader: Reader, item_id: str,
                         body: m.AutopilotDecisionRequest) -> m.AutopilotItem:
        """Record the reader's decision on an item of their own; an accept goes through the
        write path (a dry run unless the workflow is live)."""
        now = datetime.now(timezone.utc)
        env = self._ap_env()  # type: ignore[attr-defined]
        state = self._apa_state()
        to_me = self._apa_to_me(self._apa_handed(state), self._apa_me(reader))
        found = self._apa_find_item(reader, item_id, also=to_me)
        if found is None or not (found["mine"] or found.get("handed")):
            raise UserFacing("not_found", "That item is not on your list.", 404)
        if not found["mine"] and body.decision not in ("reject", "snooze"):
            raise UserFacing("action_refused", "An alert passed to you is decided in its own "
                             "system; here it is only set aside.", 409)
        if state is None:
            raise UserFacing("action_refused", "The autopilot's state directory is not "
                             "configured on this server.", 409)
        text = (body.text or "").strip() or None
        row: dict[str, Any] = {"event": "item_decision", "item_id": item_id,
                               "decision": body.decision, "at": _iso(now),
                               "by": sorted(self._apa_me(reader))[0],
                               "note": (body.note or "").strip() or None}
        if body.decision == "snooze":
            until = _instant(body.until)
            if until is None or until <= now or until > now + timedelta(days=SNOOZE_DAYS):
                raise UserFacing("bad_request", "Snooze needs a time in the next "
                                 f"{SNOOZE_DAYS} days.", 422)
            row["until"] = _iso(until)
        if body.decision == "edit" and not text:
            raise UserFacing("bad_request", "An edit needs the draft's new text.", 422)
        draft = ((found["body"].get("prepared") or {}).get("draft") or {})
        chat = ((found["body"].get("prepared") or {}).get("teams_draft") or {}) \
            if not draft else {}
        if text and not draft and not chat:
            raise UserFacing("bad_request", "This item has no draft to edit.", 422)
        if body.decision == "accept" and not text:
            prior = self._apa_decisions(state).get(item_id) or {}
            text = prior.get("text") if prior.get("decision") == "edit" \
                or ao.refused_accept(prior) else None
        if body.decision == "accept" and not text and \
                (found["body"].get("prepared") or {}).get("your_call"):
            # a "your call" card has no reply until the person says what to say
            raise UserFacing("bad_request", "Say what you want to reply first.", 422)
        if text:
            row["text"] = text
        offer = None
        if body.decision == "accept" and todo_only(found["body"].get("prepared") or {}):
            # a to-do card: "done" is the person's word that they did it by hand; nothing
            # goes through the write path
            row.update({"outcome": "done", "closed": "person_done", "steps": []})
        elif body.decision == "accept":
            if self._apa_off(env, state)[0]:
                raise UserFacing("action_refused", "The autopilot is switched off.", 409)
            item = found
            if text:
                # a Teams reply is posted to the chat it answers, as a chat message
                sent = {**draft, "body": text} if draft else {
                    "channel": "teams", "chat_id": chat.get("conversation"), "body": text}
                prepared = {**(found["body"].get("prepared") or {}), "draft": sent}
                item = {**found, "body": {**found["body"], "prepared": prepared}}
            executor = self._apa_executor(state)
            live = bool(executor.live_kinds(item))
            if body.bulk and live:
                raise UserFacing("action_refused", BULK_LIVE, 409)
            # the owner accepting their own item is a person's approval of it: it goes
            # live only through the owner's per-kind switch (or the workflow's passed
            # gate), and only once they confirmed it goes live (a second accept carrying
            # the token the first one answered); money or a judgment call still needs
            # each reason acknowledged. It runs through the prepared-writes desk, the one
            # write path Ask uses too (same checks, journal, card and confirm).
            confirmed = not body.bulk and self._apa_confirmed(
                state, item_id, row["by"], text, body.confirm_token, now)
            results = self._apa_through_desk(reader, item, chat, now, live, confirmed,
                                             executor)
            if any(r.get("status") == "needs_confirm" for r in results):
                if body.bulk:
                    raise UserFacing("action_refused", BULK_LIVE, 409)
                offer = self._apa_offer(state, item_id, row["by"], text, now)
            row["outcomes"] = [r["status"] for r in results]
            row["outcome"] = item_outcome(row["outcomes"])
            # each action in order, so the item says which step went through and which
            # did not (WEB-036): kind, its system, how it ended and why (two reasons)
            row["steps"] = [{"kind": str(r.get("kind") or ""),
                             "system": WRITE_SYSTEM.get(str(r.get("kind") or ""), "other"),
                             "status": str(r.get("status") or ""),
                             "reasons": [str(x) for x in r.get("reasons") or ()][:2]}
                            for r in results]
            # only the send's own result says it went out (an entry before it can be done
            # while the confirmation failed, was refused or ran dry); a Teams reply is sent
            # as much as an email is
            row["sent"] = any(r.get("kind") in SENDS and r.get("status") == "done"
                              for r in results)
            row["reasons"] = [str(x) for r in results for x in r.get("reasons") or ()]
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if offer is None:            # an accept waiting for its confirm is no decision yet
            journal = Journal(state / DECISIONS_FILE)
            with journal.locked() as held:
                held.append(row)
        from .assist_routes import _bump_assist_drafts
        _bump_assist_drafts(self)
        if not found["mine"]:        # passed to the reader: set aside, and nothing else
            shown = autopilot_engine.shown_item(found)
            return m.AutopilotItem(
                item_id=item_id, autopilot=found["autopilot"],
                label=found["body"].get("label") or found["autopilot"], title=shown["title"],
                workflow=found["autopilot"], system=system_of(found), owner="", mine=False,
                happened=shown["body"].get("happened") or {}, due_at=found["due_at"],
                created_at=found["created_at"], today=False,
                state="rejected" if body.decision == "reject" else "snoozed",
                decision=m.AutopilotDecision(decision=body.decision, at=row["at"], by_you=True,
                                             until=row.get("until"), note=row.get("note")))
        dec_row = row if offer is None else self._apa_decisions(state).get(item_id)
        return self._apa_item_view(reader, found, dec_row, now, offer=offer)

    def autopilot_prepared_list(self, reader: Reader) -> m.AutopilotDrafts:
        """``_prepared_read`` (the backend serves it kept per reader: ``backend_screens``)."""
        return self._prepared_read(reader)

    def _prepared_read(self, reader: Reader) -> m.AutopilotDrafts:
        """The reader's own open items that carry a reply or a "your call" card, newest
        first. One display copy each (``shown_item``: no thread text or kept event); an item
        the person already answered (their own later mail in the conversation, or a decision
        taken) is not here."""
        state = self._apa_state()
        path = self._ap_queue_path()  # type: ignore[attr-defined]
        queue = ap.Queue(path) if path is not None and Path(path).exists() else None
        try:
            replaced: dict[str, str] = {}
            if queue is not None:
                try:
                    got_rep = queue.meta(autopilot_queue.SUPERSEDED_BY)
                    if isinstance(got_rep, Mapping):
                        replaced = {str(k): str(v) for k, v in got_rep.items()}
                except (OSError, ValueError, sqlite3.Error):
                    replaced = {}
            hands, mine_ids = self._apa_handed(state, replaced=replaced), self._apa_me(reader)
            from operating.assist_matters import mc_approval_workflows
            from operating.assist_readings import is_automated_system_alert
            no_mc = mc_approval_workflows()    # decided in Mission Control, never listed here
            # every queue trigger's own cards stay here until L1 deletes the triggers (ASSIST specs 5;
            # open-assist3-l1-draft-executor): a draft already prepared is never taken off the list.
            # A matter made from a queue item is listed as that item (``assist_routes``)
            def _is_auto_it_queue_item(qitem: Mapping[str, Any]) -> bool:
                body = qitem.get("body") if isinstance(qitem.get("body"), Mapping) else {}
                happened = body.get("happened") if isinstance(body.get("happened"), Mapping) else {}
                prepared = body.get("prepared") if isinstance(body.get("prepared"), Mapping) else {}
                return is_automated_system_alert(
                    sender=happened.get("sender") or "",
                    subject=happened.get("subject") or qitem.get("title") or "",
                    summary=prepared.get("summary") or "",
                    note=prepared.get("note") or prepared.get("warning") or "",
                    message=happened.get("snippet") or "",
                )
            _qc_autopilots = frozenset({"qc_hold", "lot_on_hold", "quality_hold", "batch_hold"})
            _qc_role_re = re.compile(r"\b(?:qa\s*/\s*qc|qa|qc|quality|lab(?:oratory)?|regulatory)\b", re.I)  # guard-ok: org-chart QA/QC/lab/regulatory role check for QC hold routing

            def _load_qc_staff_sets() -> tuple[set[str], set[str], set[str], set[str]]:
                from operating import autopilot_roster as roster
                rows: list[dict[str, Any]] = []
                if getattr(self, "pg_dsn", None) and callable(getattr(self, "pg", None)):
                    try:
                        with self.pg().connection() as conn:
                            if conn.execute("SELECT to_regclass('biz.staff') IS NOT NULL").fetchone()[0]:
                                for pid, email, dept, title, reports in conn.execute(
                                    "SELECT person_id, email, to_jsonb(s)->>'department', "
                                    "to_jsonb(s)->>'title', "
                                    "COALESCE((to_jsonb(s)->>'reports_count')::int, 0) "
                                    "FROM biz.staff s WHERE person_id IS NOT NULL"
                                ).fetchall():
                                    rows.append({
                                        "person_id": str(pid or "").strip().lower(),
                                        "email": str(email or "").strip().lower(),
                                        "department": str(dept or "").strip(),
                                        "title": str(title or "").strip(),
                                        "reports_count": int(reports or 0),
                                    })
                    except Exception:  # guard-ok: best-effort staff lookup falls back to org capture
                        rows = []
                if not rows:
                    rows = roster._load_staff_rows()
                known_ids: set[str] = set()
                qa_ids: set[str] = set()
                exec_ids: set[str] = set()
                non_op_ids: set[str] = set()
                for row in rows:
                    idents = [
                        str(row.get("person_id") or "").strip().lower(),
                        str(row.get("email") or "").strip().lower(),
                    ]
                    idents = [x for x in idents if x]
                    if not idents:
                        continue
                    known_ids.update(idents)
                    dept = str(row.get("department") or "").strip()
                    title = str(row.get("title") or "").strip()
                    reports_count = int(row.get("reports_count") or 0)
                    if dept.lower() in ("exec", "executive") or reports_count >= 5:
                        exec_ids.update(idents)
                    elif roster.is_non_operational_staff(department=dept, title=title):
                        non_op_ids.update(idents)
                    elif roster.staff_matches_role("quality", department=dept, title=title) or bool(
                        _qc_role_re.search(f"{dept} {title}")
                    ):
                        qa_ids.update(idents)
                return known_ids, qa_ids, exec_ids, non_op_ids

            qc_staff_loaded: list[Optional[tuple[set[str], set[str], set[str], set[str]]]] = [None]
            is_owner_reader = self._apa_is_owner(reader)

            def _is_misrouted_qc_hold(qitem: Mapping[str, Any]) -> bool:
                body = qitem.get("body") if isinstance(qitem.get("body"), Mapping) else {}
                is_qc = (
                    qitem.get("autopilot") in _qc_autopilots
                    or body.get("done_when") == "batch_released"
                )
                if not is_qc or qitem.get("handed"):
                    return False
                if is_owner_reader:
                    return False
                if qc_staff_loaded[0] is None:
                    qc_staff_loaded[0] = _load_qc_staff_sets()
                known_ids, qa_ids, exec_ids, non_op_ids = qc_staff_loaded[0]
                if not known_ids:
                    return False
                owner = str(qitem.get("owner") or "").strip().lower()
                reader_ids = {str(x).strip().lower() for x in mine_ids if x and x != "unknown"}
                email = str(getattr(reader, "email", "") or "").strip().lower()
                if email:
                    reader_ids.add(email)
                check_ids = set(reader_ids)
                if owner and not owner.startswith(autopilot_queue.POOL_PREFIX):
                    check_ids.add(owner)
                if check_ids & exec_ids:
                    return False
                if check_ids & non_op_ids:
                    return True
                if check_ids & known_ids:
                    return not bool(check_ids & qa_ids)
                return False

            items = [i for i in self._ap_visible(  # type: ignore[attr-defined]
                reader, also=self._apa_to_me(hands, mine_ids), pools=self._apa_pools(reader)) or ()
                if (i["mine"] or i.get("handed") or i.get("pooled"))
                and i["autopilot"] not in no_mc
                and not _is_auto_it_queue_item(i)
                and not _is_misrouted_qc_hold(i)]
            decisions = self._apa_decisions(state, replaced=replaced)
            never = self._apa_never(state, mine_ids)
            now = datetime.now(timezone.utc)
            used_cache: dict[str, Any] = {}
            jobs_cache: dict[str, Any] = {}
            auto_would = self._apa_auto_would(state, mine_ids)
            out: list[m.AutopilotPrepared] = []
            # one card per real-world thing: an older item about the lot (or other subject) a
            # newer item of the reader's is about is not listed, decided or not (the engine's
            # settle sets it aside; until then it would show the same thing twice)
            # (across every queue owner the reader reads for: their own and their role's)
            gone = autopilot_queue.superseded([{**i, "owner": ""} for i in items if i["mine"]])
            # before the settle sets the older ones aside: their snooze or pass carries already
            decisions = carried(decisions, gone, lambda r: r.get("decision") == "snooze")
            hands = carried(hands, gone)
            for item in items:
                if item["item_id"] in gone:
                    continue
                dec = decisions.get(item["item_id"])
                # kept: an edit, an accept the write path refused (nothing went out: the
                # reply stays to confirm again), a snooze that has run out
                again = bool(dec) and (dec["decision"] == "edit" or ao.refused_accept(dec))
                if dec and not again:
                    until = _instant(dec.get("until"))
                    if not (dec["decision"] == "snooze" and until is not None and until <= now):
                        continue
                if never and sender_of(item) in never:
                    continue
                kept = (item.get("body") or {}).get("prepared") or {}
                shown = autopilot_engine.shown_item(item)
                prepared = shown["body"].get("prepared") or {}
                if item.get("pooled"):   # the role's, untaken: what happened, never the draft
                    out.append(pooled_card(item, shown))
                    continue
                if not item["mine"]:     # passed to this reader: only ever as its alert
                    alert = alert_of(item, shown)
                    if alert is not None:
                        out.append(passed(alert, hands, mine_ids))
                    continue
                if prepared.get("kind") in ("job", "todo"):   # todo: a message's to-do card
                    job = self._apa_job_shown(reader, item, shown, jobs_cache)
                    if job is not None:
                        out.append(job)
                    continue
                card = prepared.get("your_call")
                envelope = prepared.get("draft") or prepared.get("teams_draft") or {}
                body = str(envelope.get("body") or "")
                if again and dec.get("text"):
                    body = str(dec["text"])
                if not body and not isinstance(card, Mapping):
                    alert = alert_of(item, shown)
                    if alert is not None:
                        out.append(passed(alert, hands, mine_ids))
                    continue
                if queue is not None and queue.answered_since(item):
                    continue
                happened = shown["body"].get("happened") or {}
                teams = envelope is prepared.get("teams_draft") or happened.get("channel") == "teams"
                out.append(m.AutopilotPrepared(
                    item_id=item["item_id"], sender=str(happened.get("sender") or ""),
                    channel="teams" if teams else "mail",
                    subject=str(envelope.get("subject") or happened.get("subject") or ""),
                    message=shown_message((prepared.get("message") or {}).get("text")
                                          or happened.get("snippet")),
                    kind="your_call" if isinstance(card, Mapping) and not body else "reply",
                    redo=bool(body) and isinstance(kept.get("redo"), Mapping),
                    to=[str(t.get("address") if isinstance(t, Mapping) else t)
                        for t in envelope.get("to") or ()],
                    cc=[str(t.get("address") if isinstance(t, Mapping) else t)
                        for t in envelope.get("cc") or ()],
                    body=body, edited=bool(again and dec.get("text")),
                    note=clean_note_spacing(str(prepared.get("warning") or "")),
                    ask=str((card or {}).get("ask") or "") if isinstance(card, Mapping) else "",
                    facts=[str(f) for f in (card or {}).get("facts") or ()]
                    if isinstance(card, Mapping) else [],
                    options=[m.AutopilotOption(label=str(o.get("label") or ""),
                                               reply=str(o["reply"]))
                             for o in (card or {}).get("options") or ()
                             if isinstance(o, Mapping) and isinstance(o.get("reply"), str)
                             and o["reply"].strip()] if isinstance(card, Mapping) else [],
                    used_for_draft=self._apa_used(reader, item, used_cache),
                    learned=learning.describe_edit(str(envelope.get("body") or ""), body)
                    if again and dec.get("text") else [],
                    auto_would=auto_would(item),
                    todos=todos_of(prepared), created_at=str(item["created_at"])))
        finally:
            if queue is not None:
                queue.close()
        out = self._apa_with_switch(state, mine_ids, {i["item_id"]: i for i in items}, out)
        out.sort(key=lambda d: d.created_at, reverse=True)
        return m.AutopilotDrafts(items=out, never_senders=never)

    # ------------------------------------------------------------ jobs

    def _apa_job_ids(self, state: Optional[Path], item_id: str) -> dict[int, str]:
        """Each reviewed job of an item: its index -> the prepared change it became."""
        if state is None or not (state / JOBS_FILE).exists():
            return {}
        return {int(r["index"]): str(r["prepared_id"]) for r in Journal(state / JOBS_FILE).rows()
                if r.get("item_id") == item_id and isinstance(r.get("index"), int)
                and isinstance(r.get("prepared_id"), str)}

    @staticmethod
    def _apa_job_stage(card: Optional[Mapping[str, Any]]) -> str:
        """Where a reviewed job's prepared change stands: ``open`` (to confirm), ``again``
        (expired, or confirmed and the write failed or was refused: it is reviewed again as
        a new change) or ``decided`` (confirmed and run, possibly run or recorded as a dry
        run where writes are not live; dismissed; undone)."""
        if card is None:
            return "again"
        state = str(card.get("state") or "")
        statuses = {str(o.get("status")) for o in card.get("outcome") or ()
                    if isinstance(o, Mapping)}
        if state in ("expired", "not_run") or (
                state == "confirmed" and statuses and statuses <= JOB_AGAIN):
            return "again"
        return "decided" if state in ("confirmed", "dismissed", "undone") else "open"

    def _apa_job_decided(self, reader: Reader, pid: str) -> None:
        """After the person confirmed, dismissed or undid a prepared change: when it was a
        job's and every job of that item is now decided, the item is closed as the
        person's decision (``person_decided``: an accept if any change was confirmed, else
        a reject), the engine's settle then marks it done. Never fails the step itself."""
        from operating import autopilot_jobs as aj
        from operating import prepared_writes as pw
        state = self._apa_state()
        if state is None or not (state / JOBS_FILE).exists():
            return
        try:
            item_id = next((str(r["item_id"]) for r in Journal(state / JOBS_FILE).rows()
                            if r.get("prepared_id") == pid and r.get("item_id")), None)
            if item_id is None or item_id in self._apa_decisions(state):
                return
            found = next((i for i in self._ap_visible(reader) or ()  # type: ignore[attr-defined]
                          if i["item_id"] == item_id and i["mine"]), None)
            if found is None:
                return
            proposals = aj.kept(((found.get("body") or {}).get("prepared") or {}
                                 ).get("jobs") or ())
            made = self._apa_job_ids(state, item_id)
            desk, person = self._pw_desk(reader), self._pw_person(reader)  # type: ignore[attr-defined]
            cards = []
            for n in range(len(proposals)):
                if n not in made:
                    return
                card = desk.card(person, made[n])
                if self._apa_job_stage(card) != "decided":
                    return
                cards.append(card)
        except (pw.Refused, OSError, ValueError, UserFacing):
            return
        if not cards:
            return
        confirmed = any(c.get("state") == "confirmed" for c in cards)
        row = {"event": "item_decision", "item_id": item_id,
               "decision": "accept" if confirmed else "reject",
               "at": _iso(datetime.now(timezone.utc)), "by": sorted(self._apa_me(reader))[0],
               "note": None, "closed": "person_decided",
               "outcome": "done" if confirmed else None,
               "steps": [{"kind": "job", "system": str(c.get("system") or "other"),
                          "status": str(c.get("state") or ""), "reasons": []} for c in cards]}
        with Journal(state / DECISIONS_FILE).locked() as held:
            if not any(r.get("item_id") == item_id for r in held.rows):
                held.append(row)

    def _apa_job_shown(self, reader: Reader, item: Mapping[str, Any],
                       shown: Mapping[str, Any], cache: dict[str, Any]
                       ) -> Optional[m.AutopilotPrepared]:
        """A job item as the person sees it: what happened, what it asks and each proposed
        change (checked again: ``autopilot_jobs.kept``) with the state of the prepared
        change it became, or the brief it holds (meeting prep). None once every change was
        confirmed or dismissed (done)."""
        from operating import autopilot_jobs as aj
        from operating import prepared_writes as pw
        body = shown.get("body") or {}
        prepared = body.get("prepared") or {}
        happened = body.get("happened") or {}
        proposals = aj.kept(prepared.get("jobs") or ())
        todos = todos_of(prepared)
        if not proposals and not prepared.get("brief"):
            if not todos:
                return None
            # only things to do by hand: a to-do card (done or not needed; nothing written)
            channel = str(happened.get("channel") or "")
            return m.AutopilotPrepared(
                item_id=item["item_id"], sender=str(happened.get("sender") or ""),
                channel=channel if channel in ("mail", "teams") else "system",
                subject=str(happened.get("subject") or (happened.get("fields") or {}
                                                        ).get("title") or ""),
                message=shown_message(happened.get("snippet")), kind="todo",
                summary=str(prepared.get("summary") or ""), todos=todos,
                created_at=str(item["created_at"]))
        if "desk" not in cache:
            try:
                cache["desk"] = (self._pw_desk(reader), self._pw_person(reader))  # type: ignore[attr-defined]
            except UserFacing:
                cache["desk"] = None
        if "jobs_by_item" not in cache:
            by_item: dict[str, dict[int, str]] = {}
            st_dir = self._apa_state()
            if st_dir is not None and (st_dir / JOBS_FILE).exists():
                for r in Journal(st_dir / JOBS_FILE).rows():
                    iid = r.get("item_id")
                    if iid and isinstance(r.get("index"), int) and isinstance(r.get("prepared_id"), str):
                        by_item.setdefault(str(iid), {})[int(r["index"])] = str(r["prepared_id"])
            cache["jobs_by_item"] = by_item
        made = cache["jobs_by_item"].get(str(item["item_id"]), {})
        jobs: list[m.AutopilotJob] = []
        stages: list[Optional[str]] = []
        for n, p in enumerate(proposals):
            args = p["arguments"]
            pid, state, stage = made.get(n), None, None
            if pid and cache["desk"] is not None:
                desk, person = cache["desk"]
                try:
                    card = desk.card(person, pid)
                except pw.Refused:
                    card = None
                stage = self._apa_job_stage(card)
                state = str(card.get("state") or "") if card is not None else None
                if stage == "again":
                    # expired or nothing written: offered for review again (a new change)
                    pid = None
            doc = p["tool"] == aj.DOC_TOOL_NAME
            jobs.append(m.AutopilotJob(
                index=n, tool=p["tool"], system="sharepoint" if doc else str(args.get("system") or ""),
                operation="document_edit" if doc else str(args.get("operation") or ""),
                reason=str(args.get("reason") or ""),
                target={"citation": args.get("citation")} if doc else dict(args.get("target") or {}),
                change={k: args[k] for k in ("find", "replace", "sheet", "cell", "value") if k in args}
                if doc else dict(args.get("change") or {}),
                prepared_id=pid, state=state))
            stages.append(stage)
        brief = str(prepared.get("brief") or "")
        if (stages and all(st == "decided" for st in stages)) or (not stages and not brief):
            return None
        facts, links = decision_view(prepared)
        action = prepared.get("action") if isinstance(prepared.get("action"), Mapping) else {}
        note = str(action.get("note") or "").strip() if facts else ""
        channel = str(happened.get("channel") or "")
        return m.AutopilotPrepared(
            item_id=item["item_id"], sender=str(happened.get("sender") or ""),
            channel=channel if channel in ("mail", "teams") else "system",
            subject=str(happened.get("subject") or (happened.get("fields") or {}).get("title") or ""),
            # a brief (meeting prep) is the item's own text: what the person reads
            message=brief if brief else shown_message(happened.get("snippet")),
            kind="job",
            summary=str(prepared.get("summary") or ""), jobs=jobs, todos=todos,
            # a decision the job came with (an MC approval): its recommendation, reasons
            # and where it is decided, on the same card as the proposed changes
            facts=facts, links=links, note=clean_note_spacing(note),
            created_at=str(item["created_at"]))

    def prepare_proposal(self, reader: Reader, proposal: Mapping[str, Any], *, origin: str,
                         citations: Any = ()) -> str:
        """One proposed change (``{"tool", "arguments"}``) prepared through the prepared-writes desk as the
        reader (the record read from its system, before and after, could they make it themselves, the
        people exclusion): the prepared id. The one step an autopilot job item and an Assist job piece
        share; nothing is written here, the card takes the two-step confirm."""
        from operating import autopilot_jobs as aj
        desk, person = self._pw_desk(reader), self._pw_person(reader)  # type: ignore[attr-defined]
        args = dict(proposal["arguments"])
        cites = [*[c for c in args.pop("citations", None) or () if isinstance(c, str)], *citations]
        if proposal["tool"] == aj.DOC_TOOL_NAME:
            got = self._pw_doc_tool(reader, desk, person).call(args)  # type: ignore[attr-defined]
            if not isinstance(got, Mapping) or not got.get("prepared_id"):
                raise UserFacing("action_refused", " ".join(
                    str(x) for x in (got or {}).get("refused") or ["Refused."]), 422)
            return str(got["prepared_id"])
        # the same step as the Ask's: a file only from the reader's own made files
        args, refused = self._pw_resolve(reader, args)  # type: ignore[attr-defined]
        if refused:
            raise UserFacing("action_refused", refused, 422)
        card = self._pw_do(lambda: desk.prepare(person, args, origin=origin,  # type: ignore[attr-defined]
                                                citations=cites))
        return str(card["prepared_id"])

    def autopilot_job_prepare(self, reader: Reader, item_id: str, index: int
                              ) -> m.PreparedWrite:
        """Review one proposed change of a job item of your own: it is prepared through the
        prepared-writes desk as you (the record read from its system, before and after,
        could you make it yourself, the people exclusion), the same as an Ask request; the
        card then takes the two-step confirm on ``/api/prepared/{id}``. A job reviewed
        before answers the change it became (never a second one)."""
        from operating import autopilot_jobs as aj
        from operating import prepared_writes as pw
        found = next((i for i in self._ap_visible(reader) or ()  # type: ignore[attr-defined]
                      if i["item_id"] == item_id and i["mine"]), None)
        if found is None:
            raise UserFacing("not_found", "That item is not on your list.", 404)
        state = self._apa_state()
        if state is None:
            raise UserFacing("action_refused", "Prepared changes are not configured on this "
                             "server.", 409)
        if self._apa_off(self._ap_env(), state)[0]:  # type: ignore[attr-defined]
            raise UserFacing("action_refused", "The autopilot is switched off.", 409)
        proposals = aj.kept(((found.get("body") or {}).get("prepared") or {}).get("jobs") or ())
        if not 0 <= index < len(proposals):
            raise UserFacing("not_found", "That change is not on this item.", 404)
        desk, person = self._pw_desk(reader), self._pw_person(reader)  # type: ignore[attr-defined]
        made = self._apa_job_ids(state, item_id).get(index)
        if made:
            try:
                card = desk.card(person, made)
            except pw.Refused:
                card = None
            if self._apa_job_stage(card) != "again":
                return self._pw_card(card)  # type: ignore[attr-defined]
            # expired, or confirmed with nothing written: prepared anew below (every check
            # again, the record read again), the job then answers the new change
        pid = self.prepare_proposal(reader, proposals[index], origin=str(found.get("autopilot") or "autopilot"),
                                    citations=[c for c in found.get("citations") or () if isinstance(c, str)])
        with Journal(state / JOBS_FILE).locked() as held:
            last = [r.get("prepared_id") for r in held.rows
                    if r.get("item_id") == item_id and r.get("index") == index]
            if (last[-1] if last else None) == made:     # nobody reviewed it meanwhile
                held.append({"event": "job_prepared", "at": _iso(datetime.now(timezone.utc)),
                             "item_id": item_id, "index": index, "prepared_id": pid,
                             "by": sorted(self._apa_me(reader))[0]})
        return self._pw_card(self._pw_do(lambda: desk.card(person, pid)))  # type: ignore[attr-defined]

    def autopilot_reply_from_line(self, reader: Reader, item_id: str,
                                  body: m.AutopilotLineRequest) -> m.AutopilotItem:
        """The full reply to a "your call" card of the reader's own, written by the model from
        their one line and the card's facts (figures only from the two; the writer's screens
        read again), kept as their edit of the item's draft. Nothing is sent: the person
        confirms it on the usual path."""
        from operating import autopilot_drafter as ad
        env = self._ap_env()  # type: ignore[attr-defined]
        state = self._apa_state()
        found = self._apa_find_item(reader, item_id)
        if found is None or not found["mine"]:
            raise UserFacing("not_found", "That item is not on your list.", 404)
        if state is None:
            raise UserFacing("action_refused", "The autopilot's state directory is not "
                             "configured on this server.", 409)
        prepared = found["body"].get("prepared") or {}
        # a "your call" card, or a model's reply kept with its material (``redo``): written
        # again from the line, the reply it had is replaced by the person's edit
        card = prepared.get("your_call")
        if not isinstance(card, Mapping):
            card = prepared.get("redo")
        if not isinstance(card, Mapping):
            raise UserFacing("bad_request", "This draft cannot be rewritten from a line; "
                             "edit it yourself.", 422)
        line = (body.line or "").strip()
        if not line:
            raise UserFacing("bad_request", "Say what you want to reply first.", 422)
        envelope = prepared.get("draft") or prepared.get("teams_draft")
        kept_event = card.get("event")
        try:
            if not isinstance(envelope, Mapping) or not isinstance(kept_event, Mapping):
                raise ValueError("card_before_line_replies")
            material = ad.Material.of(card.get("material") or {})
        except ValueError:
            raise UserFacing("action_refused", "This card was prepared before replies could "
                             "be written from it; write the reply yourself.", 409) from None
        by = sorted(self._apa_me(reader))[0]
        writer = self.autopilot_writer if self.autopilot_writer is not None \
            else ad.from_environ(env)
        if writer is None:
            raise UserFacing("action_refused", "No model is set up to write replies on this "
                             "server.", 409)
        # every attempt counts (one that failed or was refused called the model too), read
        # and recorded under one lock: two requests at once never both pass the last slot
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = datetime.now(timezone.utc)
        hour_ago = _iso(now - timedelta(hours=1))
        with Journal(state / LINE_ATTEMPTS_FILE).locked() as held:
            if sum(1 for r in held.rows if r.get("by") == by
                   and str(r.get("at") or "") > hour_ago) >= LINE_REPLIES_PER_HOUR:
                raise UserFacing("rate_limited", LINE_REFUSED["rate_limited"], 429)
            held.append({"event": "line_attempt", "by": by, "at": _iso(now)})
        people = self._apa_exclusion(env)
        if people is None:
            raise UserFacing("action_refused", "The people screen is not available, so no "
                             "reply is written.", 409)
        event = {**kept_event, "text": material.text}
        owner = str(found["owner"])
        queue_path = self._ap_queue_path()  # type: ignore[attr-defined]
        if queue_path is not None:
            mine = sorted(a for a in _identities(reader) if "@" in a)
            material.style = ap._writing_style(ap._DryQueue(Path(queue_path)), {}, owner,
                                               mine, event, prepared)
        text, why = writer.from_line(material, line, event, people, owner)
        if not text:
            raise UserFacing("action_refused", LINE_REFUSED.get(why, LINE_REFUSED[""]), 409
                             if why in ("draft_failed", "draft_model_unsafe") else 422)
        now = datetime.now(timezone.utc)
        row = {"event": "item_decision", "item_id": item_id, "decision": "edit",
               "at": _iso(now), "by": sorted(self._apa_me(reader))[0], "note": None,
               "text": text, "from_line": line[:ad.LINE_CHARS]}
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with Journal(state / DECISIONS_FILE).locked() as held:
            held.append(row)
        return self._apa_item_view(reader, found, row, now)

    def _apa_through_desk(self, reader: Reader, item: Mapping[str, Any],
                          chat: Mapping[str, Any], now: datetime, live: bool,
                          confirmed: bool, executor: Any) -> list[dict[str, Any]]:
        """An accept as the prepared-writes desk runs it (``Desk.adopt`` then
        ``Desk.confirm``): the executor's result per action. A live item not yet
        confirmed runs nothing (each action ``needs_confirm``: the page offers the
        confirm); a refusal of the desk's checks is each action ``refused``. An item with
        nothing to write is the executor's own answer (its ``no_action`` refusal: nothing
        runs, and the item stays prepared, never accepted); a confirm that recorded no
        outcome is ``unknown``, never an accept."""
        from operating import prepared_writes as pw
        actions = aw.actions_of(item)
        if not actions:
            return [dict(r) for r in executor.run(item, now, aw.Approval(
                by=tuple(sorted(self._apa_me(reader))), confirmed_live=confirmed))] or [
                {"kind": None, "status": "refused", "reasons": ["no_action"]}]
        desk = self._pw_desk(reader)  # type: ignore[attr-defined]
        person = self._pw_person(reader)  # type: ignore[attr-defined]
        # a Teams reply into the chat the person's own message came from is theirs to post
        own = frozenset({"send_teams"}) if chat.get("conversation") else frozenset()
        try:
            card = desk.adopt(person, item, origin=str(item.get("autopilot") or "autopilot"),
                              now=now, own=own)
            if live and not confirmed:
                return [{"kind": a.get("kind"), "status": "needs_confirm",
                         "reasons": ["confirm_live"]} for a in actions]
            shown = desk.preview(person, card["prepared_id"], now=now)
            done = desk.confirm(person, card["prepared_id"],
                                preview_hash=str(shown.get("preview_hash") or ""),
                                confirm=True, now=now, confirmed_live=confirmed,
                                acknowledged=())
        except pw.Refused as refused:
            return [{"kind": a.get("kind"), "status": "refused",
                     "reasons": [str(p) for p in refused.problems][:2]} for a in actions]
        return [dict(o) for o in done.get("outcome") or ()] or [
            {"kind": None, "status": "unknown", "reasons": ["no_outcome"]}]

    @staticmethod
    def _apa_accepted(item_id: str, by: str, text: Optional[str]) -> str:
        """What a confirm is bound to: the item, the person, the exact text accepted."""
        return hashlib.sha256(json.dumps([item_id, by, text or ""]).encode()).hexdigest()

    def _apa_offer(self, state: Path, item_id: str, by: str, text: Optional[str],
                   now: datetime) -> str:
        """A confirm token for this accept (the journal keeps its digest, never the token)."""
        token = secrets.token_urlsafe(24)
        with Journal(state / CONFIRMS_FILE).locked() as held:
            held.append({"event": "confirm_offered", "at": _iso(now),
                         "until": _iso(now + timedelta(seconds=CONFIRM_SECONDS)),
                         "token": hashlib.sha256(token.encode()).hexdigest(),
                         "accepted": self._apa_accepted(item_id, by, text)})
        return token

    def _apa_confirmed(self, state: Path, item_id: str, by: str, text: Optional[str],
                       token: Optional[str], now: datetime) -> bool:
        """The token an accept answered, sent back by the same person for the same item
        and text within its time, once (it is spent here)."""
        if not token or not (state / CONFIRMS_FILE).exists():
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        accepted = self._apa_accepted(item_id, by, text)
        with Journal(state / CONFIRMS_FILE).locked() as held:
            spent = {r.get("token") for r in held.rows if r.get("event") == "confirm_spent"}
            for r in held.rows:
                until = _instant(r.get("until"))
                if r.get("event") == "confirm_offered" and r.get("token") not in spent \
                        and hmac.compare_digest(str(r.get("token")), digest) \
                        and r.get("accepted") == accepted and until is not None \
                        and now <= until:
                    held.append({"event": "confirm_spent", "at": _iso(now), "token": digest})
                    return True
        return False

    def _apa_used(self, reader: Reader, item: Mapping[str, Any], cache: dict[str, Any]
                  ) -> Optional[m.AutopilotDraftUse]:
        """The memory entries that changed the item's draft, those read for it but not
        applied (``related``), and its template (``context_used``: ids),
        each resolved only as this reader may read it: a memory entry's text only for the
        person whose memory it is and only while they keep it; a template's name only when
        they may use it. Unreadable stores show the ids alone (fail closed)."""
        used = (item.get("body") or {}).get("context_used")
        if not isinstance(used, Mapping):
            return None
        ids = [i for i in used.get("memory") or () if type(i) is int]
        related = [i for i in used.get("related") or ()
                   if type(i) is int and i not in ids]
        template = used.get("template") if type(used.get("template")) is int else None
        if not ids and not related and template is None:
            return None
        view_as = bool(getattr(reader.session, "view_as", False))
        if "memory" not in cache:
            cache["memory"] = {}
            store = getattr(self, "memory", None)
            if store is not None and not view_as:
                try:
                    decide = getattr(self, "_memory_decide", None)
                    rows = store.items(reader, decide=decide(reader, wait=True)
                                       if decide else None)
                    cache["memory"] = {int(r["id"]): r for r in rows}
                except Exception:  # noqa: BLE001 - unreadable: ids only
                    cache["memory"] = {}
        own = cache["memory"] if item.get("mine") else {}
        def resolved(i: int) -> m.AutopilotMemoryUse:
            return (m.AutopilotMemoryUse(id=i, kind=own[i]["kind"], text=own[i]["text"])
                    if i in own else m.AutopilotMemoryUse(id=i))
        memory = [resolved(i) for i in ids]
        used_template = None
        if template is not None:
            got = None
            if not view_as:
                try:
                    from .templates import TemplateStore
                    templates = cache.setdefault("templates", TemplateStore.for_backend(self))
                    got = templates.get(reader, template) if templates is not None else None
                except Exception:  # noqa: BLE001 - unreadable: the id alone
                    got = None
            used_template = m.AutopilotTemplateUse(
                id=template, name=got.get("name") if got else None,
                doc_type=got.get("doc_type") if got else None)
        return m.AutopilotDraftUse(memory=memory, related=[resolved(i) for i in related],
                                   template=used_template)

    def _apa_me(self, reader: Reader) -> set[str]:
        from operating.proactive_commitments import owner_ids
        return set(owner_ids(_principals(reader))) or {"unknown"}

    # ------------------------------------------------------------ owner switches

    def _apa_switch_row(self, state: Path, reader: Reader, **fields: Any) -> None:
        with self._apa_journal(state).locked() as held:
            held.append({"event": "switch", "at": _iso(datetime.now(timezone.utc)),
                         "by": sorted(self._apa_me(reader))[0], **fields})

    def autopilot_global(self, reader: Reader, body: m.AutopilotSwitchRequest
                         ) -> m.AutopilotSwitches:
        """The owner sets the write path's global kill switch."""
        state = self._apa_owner_only(reader)
        path = state / aw.KILL_FILE
        if body.global_off:
            path.write_text("switched off by the owner\n", encoding="utf-8")
        elif path.exists():
            path.unlink()
        self._apa_switch_row(state, reader, switch="global_off", value=body.global_off)
        return self._area_read(reader).switches

    def autopilot_workflow(self, reader: Reader, workflow: str,
                           body: m.AutopilotWorkflowSwitch) -> m.AutopilotArea:
        """The owner switches a workflow off for everyone or back on (the write path's
        per-workflow kill switch). Back on makes it act for nobody: each person switches
        it on for themselves (``autopilot_opt_in``)."""
        state = self._apa_owner_only(reader)
        if not aw._ROLE_FLOW.fullmatch(workflow.lower()) or ":" in workflow:
            raise UserFacing("not_found", "No such workflow.", 404)
        journal = self._apa_journal(state)
        path = state / aw.WORKFLOW_KILL_FILE
        with journal.locked():
            try:
                off = set(json.loads(path.read_text(encoding="utf-8"))["workflows"]) \
                    if path.exists() else set()
            except (OSError, ValueError, KeyError, TypeError):
                off = set()     # unreadable: every workflow was off; rebuilt from here
                off |= {w.workflow for w in self._area_read(reader).workflows}
            (off.discard if body.on else off.add)(workflow)
            _write_json(path, {"workflows": sorted(off)})
        self._apa_switch_row(state, reader, switch="workflow_on", workflow=workflow,
                             value=body.on)
        return self._area_read(reader)

    def autopilot_ack_notice(self, reader: Reader, body: m.AutopilotNoticeAck
                             ) -> m.AutopilotArea:
        """The owner acknowledges a workflow's gate-pass notice (told once: it is not shown
        again unless the gate is lost and passed anew). Turns nothing on."""
        state = self._apa_owner_only(reader)
        try:
            aw.ack_notice(self._apa_journal(state), body.role, body.workflow,
                          datetime.now(timezone.utc), sorted(self._apa_me(reader))[0])
        except ValueError:
            raise UserFacing("not_found", "No such notice is waiting for you.", 404) from None
        return self._area_read(reader)

    # ------------------------------------------------------------ a person's own opt-in

    def _apa_own_ids(self, reader: Reader) -> list[str]:
        """The signed-in person's own ids, for switching their own autopilot. A view-as
        session or a session naming nobody switches nothing (403)."""
        session = getattr(reader, "session", None)
        if getattr(session, "view_as", False) or getattr(session, "warm_ahead", False):
            raise UserFacing("forbidden", NOT_YOURS, 403)
        from operating.proactive_commitments import owner_ids
        ids = sorted(owner_ids(_principals(reader)))
        if not ids:
            raise UserFacing("forbidden", "Your sign-in doesn't say who you are, so your "
                             "autopilot can't be switched from it.", 403)
        return ids

    def _apa_opt_in_state(self) -> tuple[Path, aw.WriteGate, Journal]:
        state = self._apa_state()
        if state is None:
            raise UserFacing("action_refused", "The autopilot's state directory is not "
                             "configured on this server.", 409)
        try:
            rules = aw.load_rules()
        except aw.RulesInvalid:
            raise UserFacing("action_refused", "The write rules can't be read, so nothing "
                             "can be switched on.", 409) from None
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        return state, aw.WriteGate(state / aw.WRITE_GATE_FILE, rules), self._apa_journal(state)

    def autopilot_opt_in(self, reader: Reader, body: m.AutopilotOptInRequest
                         ) -> m.AutopilotArea:
        """The reader switches one of their own workflows on (only while it passes its gate
        for them) or off. Always the reader's own: the request names no person, so the
        owner cannot switch it on for anyone else. Recorded on the audit log."""
        ids = self._apa_own_ids(reader)
        _state, gate, journal = self._apa_opt_in_state()
        try:
            workflow = aw._workflow(body.workflow)
        except ValueError:
            raise UserFacing("not_found", "No such workflow.", 404) from None
        now = datetime.now(timezone.utc)
        mine = [i for i in ids if aw.eligible(gate, i, workflow)] if body.on else ids
        if not mine:
            raise UserFacing("action_refused", NOT_ELIGIBLE, 409)
        for oid in mine:
            try:
                aw.set_opt_in(journal, gate, oid, workflow, body.on, oid, now)
            except ValueError:
                raise UserFacing("action_refused", NOT_ELIGIBLE, 409) from None
        return self._area_read(reader)

    def autopilot_drafts(self, reader: Reader, body: m.AutopilotDraftsRequest
                         ) -> m.AutopilotArea:
        """The reader switches one choice of work prepared for them (``aw.DRAFT_CHOICES``)
        on or off: only ever their own (the request names no person). On needs no score:
        nothing is sent or written without their confirm, and performing anything still
        takes the write gate and their own opt-in. Recorded on the audit log."""
        ids = self._apa_own_ids(reader)
        try:
            flows = aw.draft_choice(body.choice)
        except ValueError:
            raise UserFacing("not_found", "No such choice.", 404) from None
        _state, _gate, journal = self._apa_opt_in_state()
        now = datetime.now(timezone.utc)
        for oid in ids:
            for flow in flows:
                aw.set_drafts(journal, oid, flow, body.on, oid, now)
        return self._area_read(reader)

    def autopilot_mode(self, reader: Reader, body: m.AutopilotChoiceModeRequest
                       ) -> m.AutopilotArea:
        """The reader sets one category of their own work to off, confirm (prepared, one
        click performs it) or auto (performed by itself, only for the workflows their
        replay score earned: 409 when none is). Only ever their own; recorded on the
        audit log (``operating.assist_modes``)."""
        from operating import assist_modes as am
        ids = self._apa_own_ids(reader)
        try:
            _label, flows = am.category(body.choice)
        except ValueError:
            raise UserFacing("not_found", "No such choice.", 404) from None
        _state, gate, journal = self._apa_opt_in_state()
        try:
            opted = aw.opt_ins(journal.rows())
            am.apply(journal, gate, ids, flows, body.mode, datetime.now(timezone.utc),
                     opted=opted)
        except ValueError:
            raise UserFacing("action_refused", NOT_ELIGIBLE, 409) from None
        return self._area_read(reader)

    def autopilot_pause(self, reader: Reader, body: m.AutopilotPauseRequest
                        ) -> m.AutopilotArea:
        """The reader pauses (or resumes) everything they switched on, at once."""
        ids = self._apa_own_ids(reader)
        _state, _gate, journal = self._apa_opt_in_state()
        now = datetime.now(timezone.utc)
        for oid in ids:
            aw.set_paused(journal, oid, body.paused, oid, now)
        return self._area_read(reader)

    @staticmethod
    def _apa_my_opt_ins(gate: Optional[aw.WriteGate], chosen: Mapping[tuple[str, str], Any],
                        stopped: Mapping[str, str], me: Any
                        ) -> tuple[list[m.AutopilotOptIn], bool]:
        """The reader's workflows they may switch on (passed for them now) or switched on
        before (on, or suspended since), and whether they paused everything."""
        out: dict[str, m.AutopilotOptIn] = {}
        flows: set[str] = {w for (p, w), o in chosen.items()
                           if p in me and (o["on"] or o.get("suspended"))}
        if gate is not None:
            flows |= {w for r, (members, ok, _s) in gate.roles.items()
                      for w in ok if members & set(me)}
        for flow in sorted(flows):
            best: Optional[m.AutopilotOptIn] = None
            for person in sorted(me):
                roles = aw.eligible(gate, person, flow) if gate is not None else []
                found = chosen.get((person, flow)) or {}
                if not roles and not found:
                    continue
                score = None
                if gate is not None:     # what Auto rests on: the category's judged score
                    score = _judged_bar(gate.judged_for((person,), flow)[1], gate)
                susp = found.get("suspended") or None
                suspension = None
                if susp:
                    counted = (susp.get("judged"), susp.get("matched"))
                    suspension = m.AutopilotSuspension(
                        at=found.get("suspended_at"), reason=str(susp.get("reason") or
                                                                  "gate_lost"),
                        score=_bar(counted, gate) if gate is not None and all(
                            isinstance(c, int) for c in counted) else None)
                row = m.AutopilotOptIn(
                    workflow=flow, eligible=bool(roles),
                    on=bool(found.get("on")) and bool(roles),
                    since=found.get("at") if found.get("on") else None, score=score,
                    suspended=suspension)
                if best is None or (row.on, row.eligible) > (best.on, best.eligible):
                    best = row
            if best is not None:
                out[flow] = best
        return list(out.values()), bool(set(me) & set(stopped))

    # ------------------------------------------------------------ audit, undo

    def autopilot_audit(self, reader: Reader) -> m.AutopilotAudit:
        if not self._apa_is_owner(reader):
            raise UserFacing("forbidden", "Only the owner reads the autopilot's audit log.",
                             403)
        rows = self._apa_rows(self._apa_state())
        # API-019: an action is undone only when its undo's latest state (attempt, outcome
        # or a person's reconciliation) is done; pending, failed or unknown say so. Any
        # undo attempt blocks a second one either way.
        undo_key = {r["of"]: r.get("key") for r in rows
                    if r.get("event") in ("attempt", "outcome") and r.get("of")}
        latest = aw._outcomes(rows)
        undo_state = {of: latest.get(key) if key else "unknown"
                      for of, key in undo_key.items()}
        audit_rows = [r for r in reversed(rows) if r.get("event") in AUDIT_EVENTS]
        needed_ids = {str(r["item_id"]) for r in audit_rows if r.get("item_id")}
        titles: dict[str, str] = {}
        path = self._ap_queue_path()  # type: ignore[attr-defined]
        if needed_ids and path is not None and path.exists():
            for it in self._ap_visible(reader, everyone=True) or ():  # type: ignore[attr-defined]
                iid = str(it.get("item_id") or "")
                if iid in needed_ids:
                    titles[iid] = autopilot_engine.shown_item(it)["title"]
        out = []
        for r in audit_rows:
            target = r.get("target")
            done = r.get("event") == "outcome" and r.get("status") == "done" and not r.get("of")
            status = r.get("status")
            if r["event"] == "opt_in":           # a person's own switch: on or off
                status = "on" if r.get("on") is True else "off"
            elif r["event"] == "opt_in_paused":
                status = "paused" if r.get("paused") is True else "resumed"
            elif r["event"] == "opt_in_suspended":
                status = "suspended"
            out.append(m.AutopilotAuditRow(
                at=str(r.get("at") or ""), event=str(r["event"]), status=status,
                mode=r.get("mode"), workflow=r.get("workflow"), role=r.get("role"),
                system=r.get("system") or WRITE_SYSTEM.get(str(r.get("kind")))
                if r.get("kind") else r.get("system"),
                kind=r.get("kind"), operation=r.get("operation"),
                target=json.dumps(target, sort_keys=True)
                if isinstance(target, (dict, list)) else (str(target) if target else None),
                item_id=r.get("item_id"), title=titles.get(str(r.get("item_id"))),
                why=r.get("why") if isinstance(r.get("why"), str) else None,
                reasons=[str(x) for x in r.get("reasons") or ()] or (
                    [str(r["why"].get("reason"))] if isinstance(r.get("why"), dict) else []),
                key=r.get("key"), by=r.get("by"),
                undoable=bool(done and r.get("inverse") and r.get("key") not in undo_state),
                undone=bool(done and undo_state.get(r.get("key")) == "done"),
                undo_status=undo_state.get(r.get("key")) if done and undo_state.get(
                    r.get("key")) not in (None, "done") else None,
                undo_note=r.get("undo_note") if done else None))
        live = sum(1 for r in rows if r.get("event") == "outcome" and r.get("mode") == "live"
                   and r.get("status") == "done")
        return m.AutopilotAudit(rows=out, live=live)

    def autopilot_undo(self, reader: Reader, key: str) -> m.AutopilotAudit:
        """The owner undoes a live action once, through the write path's recorded inverse."""
        state = self._apa_owner_only(reader)
        try:
            self._apa_executor(state).undo(key, datetime.now(timezone.utc),
                                           by=sorted(self._apa_me(reader))[0])
        except (ValueError, KeyError) as refused:
            raise UserFacing("action_refused", f"Not undone: {refused}", 409)
        return self.autopilot_audit(reader)


NO_DRAFTS_ON = ("Nothing is prepared for you until you switch it on yourself under "
                "What to prepare for me.")


# Why no reply came from the person's line, in their words.
LINE_REPLIES_PER_HOUR = 20     # replies written from a line, per person

LINE_REFUSED = {
    "figure_not_in_material": "The reply needed a figure that is neither in your line nor in "
                              "the message. Put the number in your line.",
    "nothing_useful": "Nothing usable came back. Say a little more in your line.",
    "empty_line": "Say what you want to reply first.",
    "not_cleared": "This message can't go to the model (the people screen).",
    "pay_or_hr": "This message is about pay or HR, so no reply is written for it.",
    "copy_tics": "The reply read like a template, so it is not shown. Write it yourself, "
                 "or put more of your own words in the line.",
    "rate_limited": "That is 20 replies written in the last hour. Try again later.",
    "draft_failed": "The model didn't answer in time. Try again.",
    "draft_model_unsafe": "The model's setup on this server is not safe to use.",
    "": "No reply was written. Write it yourself.",
}


def _known_workflows() -> list[str]:
    """Every workflow name the catalogue holds; none when it cannot be read (the filter
    then offers the workflows the page shows)."""
    try:
        return sorted(wf.names())
    except Exception:  # noqa: BLE001 - an unreadable catalogue only narrows the filter
        return []


def draft_roles(draft: Mapping[str, Any], docs: Mapping[str, Mapping[str, Any]]
                ) -> dict[str, frozenset[str]]:
    """Person role -> its workflows in draft: an address names ``person:<address>``; a
    person id names the person role whose members are exactly that id."""
    out: dict[str, frozenset[str]] = {}
    for who, flows in draft.items():
        role = f"person:{who}"
        if "@" not in who:
            role = next((r for r, v in docs.items() if r.startswith("person:") and
                         [str(x).lower() for x in v.get("members") or ()] == [who]), "")
        if role:
            out[role] = frozenset(out.get(role, frozenset()) | set(flows))
    return out


def _choice_row(cid: str, label: str, flows: tuple[str, ...],
                on: Mapping[str, tuple[str, Any]]) -> m.AutopilotDraftChoice:
    """One choice of prepared work as it stands for one person (``on``: their
    ``autopilot_write.prepared_for`` entry): on when any workflow of it is prepared for
    them (so its off reaches what the Draft file turned on), ``partly`` when not all are
    (a workflow of ``DRAFT_NEVER`` is never one a switch turns on, so it does not decide
    the row); set by the owner when any of it is on only through the Draft file."""
    counted = [f for f in flows if f not in autopilot_engine.DRAFT_NEVER]
    live = [f for f in counted if f in on]
    is_on = bool(live)
    by_owner = any(on[f][0] == aw.BY_OWNER for f in live)
    since = max((str(on[f][1] or "") for f in live), default="") \
        if is_on and not by_owner else ""
    return m.AutopilotDraftChoice(choice=cid, label=label, on=is_on,
                                  partly=is_on and len(live) < len(counted),
                                  by_owner=by_owner, since=since or None)


def _with_mode(row: m.AutopilotDraftChoice, flows: tuple[str, ...],
               mine: Mapping[str, Any], chosen: Mapping[tuple[str, str], Any], me: Any,
               gate: Optional[aw.WriteGate], conditions: Any = None
               ) -> m.AutopilotDraftChoice:
    """The reader's own choice row with its mode (``operating.assist_modes``), whether
    auto is earned for any of it, its replay score against the bar and the limits the
    reader set on its auto (``operating.assist_switches``)."""
    from operating import assist_modes as am
    return row.model_copy(update={
        "mode": am.mode_of(flows, mine, chosen, me),
        "auto_ok": bool(am.earned(gate, me, flows)),
        "score": _judged_bar(am.score(gate, me, flows), gate) if gate is not None else None,
        "conditions": m.AutopilotSwitchConditions(**conditions.as_dict())
        if conditions is not None and not conditions.empty() else None})


class _Cleared:
    """The people exclusion as ``autopilot_engine.draft_owners`` asks it: unavailable when
    it is missing or cannot decide, and whether a person id is clear of it."""

    def __init__(self, guard: Any) -> None:
        self.guard = guard
        try:
            self.unavailable = "absent" if guard is None else guard.unavailable
        except Exception:  # noqa: BLE001 - undecidable: unavailable
            self.unavailable = "undecidable"

    def identity_clear(self, oid: str) -> bool:
        try:
            return not self.guard.is_excluded(oid)
        except Exception:  # noqa: BLE001 - undecidable: not clear
            return False


def _identities(reader: Reader) -> set[str]:
    """The signed-in session's own identities (addresses and ids), lower case."""
    session = getattr(reader, "session", None)
    held = {str(v).lower() for v in getattr(session, "identities", ()) or ()}
    employee = getattr(session, "employee", None)
    if getattr(employee, "object_id", None):
        held.add(str(employee.object_id).lower())
    return held


def _countdown(entry: Optional[Mapping[str, Any]], flow: str,
               rules: Optional[Mapping[str, Any]], gate: Optional[aw.WriteGate] = None
               ) -> Optional[m.AutopilotCountdown]:
    """The replay counts against the gate, and the category's judged score beside them:
    it passes only when both do, as the write gate decides."""
    if not rules:            # unreadable rules: no countdown, nothing is performed
        return None
    try:
        found = aw.countdown(entry, flow, rules)
    except Exception:  # noqa: BLE001 - unreadable rules: no countdown, never a guess
        return None
    if not found:
        return None
    from operating import autopilot_judged
    from operating.assist_switches import choice_of
    every = (gate.judged.get((autopilot_judged.ALL, choice_of(flow) or ""))
             if gate is not None else None)
    found["passes"] = bool(found["passes"] and gate is not None and gate.judged_all(flow))
    return m.AutopilotCountdown(**found, judged_score=_judged_bar(every, gate)
                                if gate is not None else None)


def _judged_bar(score: Any, gate: aw.WriteGate) -> Optional[m.AutopilotScoreBar]:
    """A category's judged score (``autopilot_judged.Score``) against the gate's bar, with
    where and when it was measured."""
    if score is None:
        return None
    bar = _bar((score.judged, score.matched), gate)
    return bar.model_copy(update={"source": score.source, "measured_on": score.measured_on,
                                  "window": list(score.window),
                                  "scope": score.scope}) if bar else None


def _bar(counts: Optional[tuple[int, int]], gate: aw.WriteGate) -> Optional[m.AutopilotScoreBar]:
    """Counts against the gate's bar, the percentage with one decimal (Decimal, half up)."""
    if not counts:
        return None
    judged, matched = int(counts[0]), int(counts[1])
    pct = float((Decimal(matched) * 100 / Decimal(judged)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP)) if judged > 0 else None
    return m.AutopilotScoreBar(judged=judged, matched=matched, percent=pct,
                               need_judged=gate.minimum,
                               need_percent=float(gate.precision * 100))


def _principals(reader: Reader) -> str:
    from .autopilot_api import _principals as found
    return found(reader)


def _write_json(path: Path, doc: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
