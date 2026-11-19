"""Prepared writes: the one general actions layer Ask and Autopilot share.

Any write the Workbench can make, in any system with a write adapter
(``operating.autopilot_write_adapters``: BC, Mission Control, Planner, calendar,
SharePoint lists and files, Portal, mail, Teams), is first *prepared*: the model (through
``plx.prepare_write``, ``tool``) or an autopilot names the system, the operation, the
target, the change, the values as read before it and the reason. Nothing is written then.
The person sees the change (``preview``: each field before and after, who it reaches,
whether it would go live now and what a live write still lacks, the reasons that need
their acknowledgement) and confirms it in two steps (``confirm`` with the preview's hash
and an explicit ``confirm``). Only then does the one write executor
(``autopilot_write.Executor``) perform it, as that person (``Approval`` with their own
ids), under every guard it keeps: kill switches, the people exclusion of everyone the
write reaches, the routine rules, the owner's per-kind switch ``PLX_APPROVED_WRITES``,
the per-approver rate limit, idempotency, the adapter's own checks (etags, no system
fields, no overwrite) and the undo it records. There is no per-system flow here: a new
system is a new adapter.

What ``prepare`` refuses (nothing stored, the reasons returned to the model):

* a system or operation no adapter knows, or a change its adapter refuses (the exact
  request is built and its undo too, before anything is stored);
* FileMaker (read-only, retiring) and Sage (no write path): never prepared;
* a BC write whose company is not one the host configured (``target.company_id``) or
  whose name (``target.company_name``) is not BC's own name for one of them as the
  readiness probe found it: the name is resolved to the id here and the card shows both,
  so the company shown is the company written (never the default for a name it does not
  know); a target key the write does not use (``Adapter.TARGET_KEYS``) is refused too;
* anything that names Freightcom (never to the model, never acted on);
* a person the people exclusion does not clear (the preparer or anyone the write
  reaches); an unavailable exclusion list refuses everything (fail closed);
* a write the person could not make themselves (``may``: the caller's per-system check
  of the person's own grants; a system it cannot decide is refused). A full reader
  (owner, CEO) skips this check only; every other guard stays;
* more than ``PREPARE_PER_HOUR`` prepared by one person within an hour;
* a mail or Teams message whose recipients, subject or body are not all in ``change`` (the
  card shows the message whole), or an action field the card would not show;
* an edit whose current values cannot be read from the system (``read``) or differ there
  from what Ask read: the card's "before" is always the system's, never the model's.

What the person sees is what runs: the card's ``actions`` are the executor's own actions
(``autopilot_write.actions_of`` of the stored item), every field of each (system,
operation, the target's ids and the record's name as read from the system, each field
before and after, recipients, subject and full body), and the confirm hash covers exactly
that card and the stored item. ``confirm`` checks again that the person could make the
write themselves (a grant revoked since is refused) and refuses an item older than
``EXPIRES`` (prepare it again). A change that failed, was recorded as a dry run, was
dismissed or expired can be prepared again (``retry``); it gets a new id.

Only the person the write acts as (the owner of the prepared item) may see, confirm,
dismiss or undo it; an autopilot's item is owned by the person who opted in. Every step is
a row of one hash-chained journal (``JOURNAL``); a second confirm of the same item never
runs it twice (the journal lock picks one; the executor's idempotency key holds besides).

Evidence label of tests: ``injected-transport`` (fake adapters); live-unverified.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa
from operating.card_actions import Journal

JOURNAL = "prepared-writes/journal.jsonl"
TOOL_NAME = "plx.prepare_write"
PREPARE_PER_HOUR = 40
# the model-facing system names and the shape each becomes in the executor's item
DRAFT_SYSTEMS = {"mail": "email", "teams": "teams"}
NEVER = {"filemaker": "FileMaker is read-only (it is being retired): nothing is written "
                      "there.",
         "sage": "Sage has no write path: the person keys it in Sage."}
# kinds a person can do as themselves with no grant to check: their own mailbox and
# calendar (the executor forces the sender and the calendar to the item's owner)
OWN = frozenset({"send_mail", "calendar_write"})
STATES = ("prepared", "confirmed", "dismissed", "expired", "undone", "not_run")
EXPIRES = timedelta(hours=24)
# a confirmed item with no outcome this long after is shown as not run (a crash)
NOT_RUN_AFTER = timedelta(minutes=10)
# outcomes after which the same change may be prepared again
RETRYABLE = frozenset({"failed", "not_performed", "dry_run", "refused", "skipped"})
# the fields of a message the card shows whole; nothing else may be in it
DRAFT_FIELDS = {"mail": {"change": {"to", "cc", "subject", "body", "attachments"},
                         "target": set()},
                "teams": {"change": {"body", "members"},
                          "target": {"chat_id", "team_id", "channel_id"}}}
MC_FIELDS = {"target": {"task_id", "gate_id"}, "change": {"decision", "scope", "proposal"}}
# every key of an executor action; one outside it would run unseen, so it is refused
ACTION_KEYS = frozenset({"kind", "system", "operation", "target", "payload", "people",
                         "precondition", "reason", "recommendation", "unknown_domains"})
# read(person, action) -> {"name": the record as named in the system, "values": current
# values of the fields the write sets, "requested_by": an MC gate's requester} or None
Read = Callable[["Person", Mapping[str, Any]], Optional[Mapping[str, Any]]]
Check = Callable[["Person", Mapping[str, Any]], Optional[str]]


class Refused(ValueError):
    """The write cannot be prepared or confirmed; ``problems`` say why in plain words."""

    def __init__(self, problems: Sequence[str], status: int = 422) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)
        self.status = status


@dataclass(frozen=True)
class Person:
    """The signed-in person a write acts as: ``ids`` their lower-case email and Entra
    object id; ``full`` a full reader (owner, CEO): no grant check, every other guard."""
    ids: tuple[str, ...]
    full: bool = False

    @property
    def email(self) -> str:
        return next((i for i in self.ids if "@" in i), "")

    def owns(self, owner: str) -> bool:
        return bool(owner) and owner.lower() in self.ids


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


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


def _freightcom(value: Any) -> bool:
    from integrations.image_ocr_gpu import touches_freightcom
    return any(touches_freightcom(s) for s in _strings(value) if s)


def _only(request: Mapping[str, Any], allowed: Mapping[str, set], what: str) -> None:
    for part in ("target", "change"):
        extra = sorted(set(dict(request.get(part) or {})) - set(allowed[part]))
        if extra:
            raise Refused([f"{what}: {part} may hold only "
                           f"{', '.join(sorted(allowed[part])) or 'nothing'}, not "
                           f"{', '.join(str(e)[:40] for e in extra[:5])} (the card shows "
                           "everything that is sent)."])


def item_of(request: Mapping[str, Any], owner: str, item_id: str, origin: str,
            citations: Sequence[str] = (), requested_by: Optional[str] = None
            ) -> dict[str, Any]:
    """The executor's item (``autopilot_write.actions_of``) for one prepared write: a
    system write (``write``), a mail or Teams message (``draft``, built from ``change``
    alone: recipients, subject and body are all shown) or an MC approval decision
    (``action``; its requester as read from MC, ``requested_by``, never the model's)."""
    system = str(request.get("system") or "").strip().lower()
    operation = str(request.get("operation") or "")
    target = dict(request.get("target") or {})
    change = dict(request.get("change") or {})
    reason = str(request.get("reason") or "")[:600]
    if system in DRAFT_SYSTEMS:
        _only(request, DRAFT_FIELDS[system], system)
        if "attachments" in change:
            try:
                change["attachments"] = aw.attachments_of(change["attachments"])
            except ValueError as error:
                raise Refused([f"mail: {error}"]) from None
            if not change["attachments"]:
                change.pop("attachments")
        # the channel is the system's, never one the change names
        prepared: dict[str, Any] = {"draft": {**target, **change,
                                              "channel": DRAFT_SYSTEMS[system]}}
    elif system == "mc" and operation == "approval_decide":
        _only(request, MC_FIELDS, "mc")
        prepared = {"action": {"kind": "mc_approval", "task_id": target.get("task_id"),
                               "gate": target.get("gate_id"), "scope": change.get("scope"),
                               "proposal": change.get("proposal"),
                               "requested_by": requested_by},
                    "recommendation": change.get("decision")}
    else:
        prior = _prior_of(request.get("prior"), change)
        precondition = dict(request.get("precondition") or {})
        if prior:
            precondition["prior"] = prior
        prepared = {"write": {"system": system, "operation": operation, "target": target,
                              "payload": change, "precondition": precondition or None,
                              "people": [p for p in request.get("people") or ()
                                         if isinstance(p, str)],
                              "reason": reason}}
    return {"item_id": item_id, "autopilot": origin, "owner": owner, "title": str(request.get("title") or reason)[:200],
            "citations": [c for c in citations if isinstance(c, str)][:20],
            "body": {"label": reason, "happened": {}, "prepared": prepared}}


def _plain(value: Any, key: str = "") -> Any:
    """``value`` as the card shows it: a document's bytes as their size and digest."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if key.endswith("_b64") and isinstance(value, str):
        if value.startswith("blob:sha256:"):
            return f"[document bytes, sha256 {value.rsplit(':', 1)[1][:16]}]"
        return f"[{len(value) * 3 // 4} bytes, sha256 " \
               f"{hashlib.sha256(value.encode('ascii', 'replace')).hexdigest()[:16]}]"
    return value


def shown_action(action: Mapping[str, Any], read: Optional[Mapping[str, Any]] = None,
                 company: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """One executor action as the card shows it: every field the executor builds the
    request from (``ACTION_KEYS``), the record's name as the system holds it and, for BC,
    the company the request is sent to (``Desk._bc_company``: its id and BC's name)."""
    pre = action.get("precondition") if isinstance(action.get("precondition"), Mapping) \
        else {}
    payload = action.get("payload") if isinstance(action.get("payload"), Mapping) else {}
    out = {"kind": action.get("kind"), "system": action.get("system"),
           "operation": action.get("operation"),
           "target": _plain(dict(action.get("target") or {})),
           "record": (read or {}).get("name"),
           "fields": _plain(dict(payload)),
           "people": [p for p in action.get("people") or () if p],
           "condition": _plain({k: v for k, v in pre.items() if k != "prior"})}
    if action.get("kind") == "bc_write":
        out["company"] = dict(company) if company else None
    if action.get("recommendation") is not None:
        out["decision"] = action.get("recommendation")
    if action.get("kind") == "send_mail":
        out["message"] = {"sender": (action.get("target") or {}).get("sender"),
                          "to": list(payload.get("to") or ()),
                          "cc": list(payload.get("cc") or ()),
                          "subject": payload.get("subject"), "body": payload.get("body")}
        files = [{"name": a.get("name"), "size": a.get("size"), "file_id": a.get("file_id")}
                 for a in payload.get("attachments") or () if isinstance(a, Mapping)]
        if files:   # each attached file by name and size, opened from its link
            out["message"]["attachments"] = files
    if action.get("kind") == "send_teams":
        out["message"] = {"sender": (action.get("target") or {}).get("user"),
                          "to": [p for p in action.get("people") or () if p],
                          "subject": None, "body": payload.get("body")}
    return out


def _spelled(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _prior_of(prior: Any, change: Mapping[str, Any]) -> dict[str, Any]:
    """What Ask read of the fields the write changes, under the change's own field names:
    a key spelled another way (the record's column, ``expected_date`` for ``expectedDate``)
    is that field; the exact name wins over another spelling. A key for a field the write
    does not change is left out: the card shows, and the desk checks against the system,
    only the fields it changes (the record's version, ``precondition``, covers the rest).
    Before, such a key was compared with a value never read and refused every such edit
    as changed (3 of 14 Portal edits of a live dry run)."""
    if not isinstance(prior, Mapping):
        return {}
    named = {_spelled(f): f for f in change}
    out: dict[str, Any] = {}
    for key, value in prior.items():
        field = key if key in change else named.get(_spelled(key))
        if field is not None and (field not in out or key == field):
            out[field] = value
    return out


def diff_of(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each field the write sets with no current value (a new record or a message):
    before None, after the value. An edit's before is the system's (``Desk.prepare``)."""
    change = request.get("change") if isinstance(request.get("change"), Mapping) else {}
    return [{"field": str(f), "before": None, "after": _plain(v, str(f))}
            for f, v in change.items()]


def _instant(value: Any) -> Optional[datetime]:
    """A Graph ``{dateTime, timeZone}`` (an IANA zone or UTC) as an aware instant."""
    if not isinstance(value, Mapping) or set(value) - {"dateTime", "timeZone"}:
        return None
    try:
        from zoneinfo import ZoneInfo
        text = str(value.get("dateTime") or "")
        whole, _dot, fraction = text.partition(".")
        when = datetime.fromisoformat(whole + (f".{fraction[:6]}" if fraction else ""))
        zone = str(value.get("timeZone") or "")
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc if zone.upper() == "UTC"
                                else ZoneInfo(zone))
        return when
    except Exception:  # noqa: BLE001 - a zone or time not known: not compared as one
        return None


def _same(a: Any, b: Any) -> bool:
    if a == b:
        return True
    if isinstance(a, Mapping) or isinstance(b, Mapping):
        first, second = _instant(a), _instant(b)
        return first is not None and first == second
    try:
        from decimal import Decimal
        return Decimal(str(a)) == Decimal(str(b))
    except Exception:  # noqa: BLE001 - not numbers: different
        return False


def _blob_refs(value: Any) -> Iterable[str]:
    for text in _strings(value):
        if text.startswith("blob:sha256:"):
            yield text.rsplit(":", 1)[1]


class Desk:
    """The prepared writes of one state directory (module doc). ``executor``: builds the
    one write executor (its adapters, the people-exclusion guard, the host's switches),
    read anew for each step so a switch the owner set is seen at once. ``may``: the
    caller's per-system check that the person could make this write themselves (None:
    they could; a reason: refused); None checks nothing but the person's own mailbox and
    calendar, so every other system is refused. ``read``: the system's current record
    for an action (``Read``); None reads nothing, so every edit and MC decision is
    refused (its "before" would be the model's word)."""

    def __init__(self, state: Path, executor: Callable[[], aw.Executor], *,
                 may: Optional[Check] = None, read: Optional[Read] = None) -> None:
        self.state = Path(state)
        self.journal = Journal(self.state / JOURNAL)
        self.executor = executor
        self.may = may
        self.read = read

    def _read(self, person: "Person", action: Mapping[str, Any],
              cache: Optional[dict[str, Any]] = None) -> Optional[Mapping[str, Any]]:
        """The system's record for ``action`` (``Read``), once per record per prepare: None
        when there is nothing to read; ``{"unreadable": True}`` when the read failed."""
        if self.read is None:
            return None
        key = _canonical([action.get("kind"), action.get("operation"), action.get("target")])
        if cache is not None and key in cache:
            return cache[key]
        try:
            got = self.read(person, action)
            got = dict(got) if isinstance(got, Mapping) else None
        except Exception:  # noqa: BLE001 - a read that fails is not read
            got = {"unreadable": True}
        if cache is not None:
            cache[key] = got
        return got

    @staticmethod
    def _as_read(request: Mapping[str, Any], got: Optional[Mapping[str, Any]]
                 ) -> tuple[dict[str, Any], list[str]]:
        """``request`` with what its system holds now: the precondition (the etag the
        write sends as If-Match, a document's status) and the prior values of the fields it
        changes come from the read; where Ask said them too and the system holds others,
        the problems (the record changed since Ask read it)."""
        out = dict(request)
        if not got or got.get("unreadable"):
            return out, []
        system = str(request.get("system") or "the system")
        problems: list[str] = []
        condition = got.get("condition") if isinstance(got.get("condition"), Mapping) else {}
        pre = dict(request.get("precondition") or {}) \
            if isinstance(request.get("precondition"), Mapping) else {}
        for key, value in condition.items():
            if value is None:
                continue
            if key in pre and not _same(pre[key], value):
                problems.append(f"{system} holds a newer version of the record than Ask read "
                                f"({key}): read it again.")
            pre[key] = value
        values = got.get("values") if isinstance(got.get("values"), Mapping) else {}
        change = request.get("change") if isinstance(request.get("change"), Mapping) else {}
        prior = _prior_of(request.get("prior"), change)
        for field in change:
            if field not in values:
                continue
            if field in prior and not _same(prior[field], values[field]):
                continue              # the moved value is refused by ``_before``
            prior[field] = values[field]
        if pre:
            out["precondition"] = pre
        if prior:
            out["prior"] = prior
        return out, problems

    # ---- prepare -----------------------------------------------------------------------

    def prepare(self, person: Person, request: Mapping[str, Any], *,
                origin: str = "instruction", citations: Sequence[str] = (),
                now: Optional[datetime] = None,
                diff: Optional[Sequence[Mapping[str, Any]]] = None,
                read: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        """Prepare one write as ``person`` (module doc); returns its card (``card``).
        ``origin``: ``instruction`` (the person asked for it, in Ask) or an autopilot's id
        (it prepared the item for the person who opted in). ``diff`` and ``read``: a
        trusted caller that read the record from the system itself (the document edit:
        the file's bytes of the cited version) says the diff and the record it read.
        Raises ``Refused``."""
        now = now or _now()
        owner = person.email
        if not owner:
            raise Refused(["You have no mailbox identity here, so nothing can be prepared "
                           "as you."], 403)
        system = str(request.get("system") or "").strip().lower()
        if system in NEVER:
            raise Refused([NEVER[system]])
        if _freightcom([request, list(citations)]):
            raise Refused(["That carrier is never acted on through the Workbench (its data stays out)."])
        executor = self.executor()
        seed = _canonical({"owner": owner, "origin": origin, "request": request})
        asked = dict(request)            # as Ask said it (kept: a retry reads anew)
        if system == "bc":
            # the company by its id among the configured ones, before anything is read
            request = self._bc_named(request, executor, now)
        requester = None
        reads: dict[str, Any] = {}
        problems: list[str] = []
        if read is None and system not in DRAFT_SYSTEMS:
            # the record as its system holds it now: an edit's etag, status and prior
            # values are the system's, and an MC gate's requester is MC's (the own-request
            # and exclusion checks never take the model's word for it)
            probe = aw.actions_of(item_of(request, owner, "pw_probe", origin, citations))
            mc_gate = system == "mc"
            # nothing is read for a person who could not make the change themselves (the
            # loop below refuses them); an MC gate is read first: its requester decides
            got = self._read(person, probe[0], reads) if len(probe) == 1 and probe[0].get(
                "kind") in aw.KINDS and (mc_gate or person.full or probe[0].get("kind") in OWN
                                         or (self.may is not None
                                             and not self.may(person, probe[0]))) else None
            if system == "mc" and str(request.get("operation") or "") == "approval_decide":
                requester = str((got or {}).get("requested_by") or "").strip().lower() or None
                if requester is None:
                    raise Refused(["Mission Control's gate cannot be read now (who requested "
                                   "it), so no decision is prepared."])
                # the scope the decision renders back is the gate's own as MC states it
                # now, never the model's or a stored copy (the served record holds none)
                scope = (got or {}).get("scope")
                if not isinstance(scope, Mapping) or not scope.get("kind"):
                    raise Refused(["Mission Control's gate shows no scope to approve, so no "
                                   "decision is prepared."])
                request = {**request, "change": {**dict(request.get("change") or {}),
                                                 "scope": dict(scope)}}
            else:
                request, problems = self._as_read(request, got)
        attempt = self._attempt(seed, now)
        pid = "pw_" + hashlib.sha256(f"{seed}#{attempt}".encode("utf-8")).hexdigest()[:24]
        item = item_of(request, owner, pid, origin, citations, requested_by=requester)
        actions = aw.actions_of(item)
        if not actions or any(a.get("kind") not in aw.KINDS for a in actions):
            raise Refused([f"No write path for {system or 'that system'} "
                           f"{str(request.get('operation') or '')[:40]}".strip()
                           + ": say what to change in a system the Workbench writes to "
                             "(BC, Mission Control, Planner, calendar, SharePoint, Portal, "
                             "mail, Teams)."])
        return self._record(person, item, executor, pid=pid, seed=seed, origin=origin,
                            now=now, problems=problems, reads=reads, read=read, diff=diff,
                            asked=asked)

    def adopt(self, person: Person, item: Mapping[str, Any], *, origin: str,
              now: Optional[datetime] = None, own: frozenset = frozenset()
              ) -> dict[str, Any]:
        """An autopilot's prepared item (the person's own, ``autopilot_queue``) as one of
        their prepared writes: the same checks, journal, card, preview and confirm as an
        Ask request, so Ask and Autopilot share one write path. The item keeps its own id
        (the executor's idempotency and the autopilot's audit key on it). Raises
        ``Refused``."""
        now = now or _now()
        owner = person.email
        if not owner or not person.owns(str(item.get("owner") or "")):
            raise Refused(["That item is not yours."], 404)
        whole = {str(k): v for k, v in item.items() if k != "mine"}
        if _freightcom([whole.get("title"), whole.get("body")]):
            raise Refused(["That carrier is never acted on through the Workbench (its data "
                           "stays out)."])
        actions = aw.actions_of(whole)
        if not actions or any(a.get("kind") not in aw.KINDS for a in actions):
            raise Refused(["This item holds nothing the Workbench can write."])
        seed = _canonical({"owner": owner, "origin": origin, "item_id": whole.get("item_id"),
                           "body": whole.get("body")})
        attempt = self._attempt(seed, now)
        pid = "pw_" + hashlib.sha256(f"{seed}#{attempt}".encode("utf-8")).hexdigest()[:24]
        return self._record(person, whole, self.executor(), pid=pid, seed=seed,
                            origin=origin, now=now, problems=[], reads={}, read=None,
                            diff=None, asked=None, own=own)

    def _record(self, person: Person, item: Mapping[str, Any], executor: aw.Executor, *,
                pid: str, seed: str, origin: str, now: datetime, problems: list[str],
                reads: dict[str, Any], read: Optional[Mapping[str, Any]],
                diff: Optional[Sequence[Mapping[str, Any]]],
                asked: Optional[Mapping[str, Any]], own: frozenset = frozenset()
                ) -> dict[str, Any]:
        """Check every action of ``item`` as ``person`` (the write's own checks, could they
        make it themselves, the people exclusion, the record as read, Freightcom) and record
        it prepared; its card. One path for an Ask request and an autopilot's item.
        ``own``: kinds that act in the person's own place for this item (an autopilot's
        reply into the chat the person's message came from). Raises ``Refused``."""
        owner = person.email
        actions = aw.actions_of(item)
        rows: list[dict[str, Any]] = []
        shown: list[dict[str, Any]] = []
        for action in actions:
            hidden = sorted(set(action) - ACTION_KEYS)
            if hidden:
                problems.append("The change holds fields the card would not show "
                                f"({', '.join(hidden[:5])}): nothing is prepared.")
            adapter = executor.adapters.get(str(action.get("kind")))
            # the write's own checks: an Ask request is refused here; an autopilot's item
            # is checked by the executor as it runs (each action's own refused, excluded or
            # killed result, as the Autopilot page shows them)
            own_checks: list[str] = []
            if adapter is None:
                own_checks.append(f"No {action.get('kind')} adapter on this server.")
            else:
                try:
                    request_built = adapter.request(action)
                    adapter.undo_request(action, request_built)
                except wa.AdapterRefused as refused:
                    own_checks += [str(p) for p in refused.problems]
                except (KeyError, TypeError, ValueError) as error:
                    own_checks.append(f"{action.get('system')}: the change is not complete "
                                      f"({type(error).__name__})")
            company = None
            if action.get("kind") == "bc_write":
                # the company the request goes to, as the card shows it (an autopilot's
                # item writes the configured default); shown wrong is refused on any path
                company, wrong = self._bc_company(executor, action, now)
                problems += wrong
            if asked is not None:
                problems += own_checks
                if adapter is None:
                    continue
            denied = None
            if not person.full and action.get("kind") not in OWN | own:
                denied = (self.may(person, action) if self.may is not None
                          else "the Workbench cannot check your own access there")
                if denied:
                    problems.append(f"You cannot make this {action.get('system')} change yourself: {denied}.")
            people = aw._addresses(item, action) + [
                str(p) for p in adapter.people(action)] if adapter is not None else []
            why = aw.excluded(executor.guard, owner, people, executor._known_domains())
            if why and asked is not None:
                problems.append({"exclusion_unavailable": "The list of people whose data "
                                 "stays out is not loaded, so nothing is prepared.",
                                 "person_excluded": "It reaches a person the Workbench "
                                 "keeps out.",
                                 "person_undecided": "It reaches an address the Workbench "
                                 "cannot check."}.get(why, f"Not cleared: {why}."))
            # a record is read only for a person who could change it (an MC gate was read
            # above, for its requester)
            got = read if read is not None else (
                self._read(person, action, reads)
                if not denied or action.get("kind") == "mc_approval" else None)
            if (got or {}).get("unreadable"):
                problems.append(f"The record cannot be read from {action.get('system')} now, so the change "
                                "cannot be shown against it: nothing is prepared.")
            if (got or {}).get("blocked"):
                problems.append(str(got["blocked"]))
            if action.get("kind") == "mc_approval" and [
                    str(p).lower() for p in action.get("people") or ()][:1] != [
                    str((got or {}).get("requested_by") or "").lower()]:
                problems.append("Mission Control does not show this gate as requested by "
                                "the person on the card (or cannot be read): nothing is "
                                "prepared.")
            seen = [str(p) for p in (got or {}).get("people") or () if p]
            if seen and adapter is not None and not why:
                # the people the record names are cleared like the write's own
                why = aw.excluded(executor.guard, owner, seen, executor._known_domains())
                if why:
                    problems.append("The record names a person the Workbench keeps out "
                                    "(or cannot check): nothing is prepared.")
            problems += self._before(action, got, diff is not None, rows)
            shown.append(shown_action(action, got, company))
        if _freightcom([shown, rows, diff or []]):
            problems.append("That carrier is never acted on through the Workbench (its data "
                            "stays out).")
        if problems:
            raise Refused(list(dict.fromkeys(problems)))
        with self.journal.locked() as held:
            rows_now = held.rows
            mine = [r for r in self._rows(rows_now) if r.get("event") == "prepared"
                    and r.get("owner") == owner
                    and now - aw._instant(r["at"]) < timedelta(hours=1)]
            existing = next((r for r in self._rows(rows_now) if r.get("event") == "prepared"
                             and r.get("id") == pid), None)
            if existing is None:
                if len(mine) >= PREPARE_PER_HOUR:
                    raise Refused([f"More than {PREPARE_PER_HOUR} writes prepared in an hour:"
                                   " confirm or dismiss some first."], 429)
                held.append({"event": "prepared", "at": aw._iso(now), "id": pid,
                             "owner": owner, "origin": origin, "item": self._stash(item),
                             "seed": hashlib.sha256(seed.encode("utf-8")).hexdigest(),
                             "request": None if asked is None else self._stash(dict(asked)),
                             "own": sorted(own),
                             "shown": shown,
                             # a caller that read the record (a document edit) says the diff
                             # it made; else each field's before is the system's
                             "diff": [dict(d) for d in diff] if diff is not None else rows,
                             "trusted": diff is not None,
                             "undo": [self._undo_words(executor, a) for a in actions]})
        self.prune(now)
        return self.card(person, pid, now=now)

    @staticmethod
    def _before(action: Mapping[str, Any], got: Optional[Mapping[str, Any]], trusted: bool,
                rows: list[dict[str, Any]]) -> list[str]:
        """Add the action's diff rows (before as the system holds it) to ``rows``; the
        problems when an edit's current values cannot be read or differ from Ask's."""
        payload = action.get("payload") if isinstance(action.get("payload"), Mapping) else {}
        pre = action.get("precondition") if isinstance(action.get("precondition"), Mapping) \
            else {}
        prior = pre.get("prior") if isinstance(pre.get("prior"), Mapping) else None
        if trusted or (got or {}).get("unreadable"):
            return []
        values = (got or {}).get("values") if isinstance((got or {}).get("values"), Mapping) \
            else None
        system = str(action.get("system") or "the system")
        if prior:
            if values is None:
                return [f"The current values cannot be read from {system} now, so the change "
                        "cannot be shown against them: nothing is prepared."]
            moved = [k for k in prior if k not in values or not _same(values[k], prior[k])]
            if moved:
                return [f"{system} holds other values now for {', '.join(map(str, moved[:5]))}"
                        " than Ask read: read the record again."]
        mine = []
        for field, after in payload.items():
            before = values.get(field) if values is not None and prior else None
            mine.append({"field": str(field), "before": _plain(before, str(field)),
                         "after": _plain(after, str(field))})
        rows += mine
        if prior and mine and all(field in prior and _same(values[field], after)
                                  for field, after in payload.items()):
            # an edit that sets every field to what the record holds changes nothing
            return [f"{system} already holds these values: there is nothing to change."]
        return []

    def _attempt(self, seed: str, now: datetime) -> int:
        """The attempt number of a request: the same open item for an open one; a new one
        once every earlier item of it is closed (confirmed, dismissed, expired)."""
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        rows = self._rows(self.journal.rows())
        ids = [r["id"] for r in rows if r.get("event") == "prepared"
               and r.get("seed") == digest]
        for n, pid in enumerate(ids):
            if self._state(pid, rows, now) == "prepared":
                return n
        return len(ids)

    def _state(self, pid: str, rows: Sequence[Mapping[str, Any]], now: datetime) -> str:
        mine = [r for r in rows if r.get("id") == pid]
        first = next((r for r in mine if r.get("event") == "prepared"), None)
        state, confirmed_at, ran = "prepared", None, False
        for r in mine:
            if r.get("event") in ("confirmed", "dismissed"):
                state = str(r["event"])
                confirmed_at = r.get("at")
            if r.get("event") == "outcome":
                ran = True
            if r.get("event") == "undone":
                state = "undone"
        if state == "prepared" and first is not None \
                and now - aw._instant(first["at"]) >= EXPIRES:
            return "expired"
        if state == "confirmed" and not ran and confirmed_at \
                and now - aw._instant(confirmed_at) >= NOT_RUN_AFTER:
            return "not_run"
        return state

    def prune(self, now: Optional[datetime] = None) -> int:
        """Remove the document bytes no open item needs any more (dismissed, run,
        undone, expired; a retry within ``EXPIRES`` still finds them). Undo keeps its own
        copy in the executor's undo store. Returns how many files were removed."""
        now = now or _now()
        folder = self.state / "prepared-writes" / "blobs"
        if not folder.is_dir():
            return 0
        rows = self._rows(self.journal.rows())
        keep: set[str] = set()
        for r in rows:
            if r.get("event") != "prepared":
                continue
            state = self._state(str(r.get("id")), rows, now)
            young = now - aw._instant(r["at"]) < EXPIRES
            if state == "prepared" or (young and state in ("confirmed", "dismissed")):
                keep.update(_blob_refs([r.get("item"), r.get("request")]))
        removed = 0
        for path in folder.iterdir():
            # a blob stashed by a prepare still writing its row is young: kept
            if path.name in keep or now.timestamp() - path.stat().st_mtime < 3600:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def retry(self, person: Person, pid: str, now: Optional[datetime] = None
              ) -> dict[str, Any]:
        """Prepare a change again that was dismissed, expired, failed or recorded as a
        dry run: a new item, checked anew from the start. A change that ran (or may have:
        no outcome) is never re-prepared here: check the system, then ask again."""
        now = now or _now()
        first, _rows = self._own(person, pid)
        card = self.card(person, pid, now=now)
        statuses = {str(o.get("status")) for o in card["outcome"]}
        if not (card["state"] in ("dismissed", "expired") or (
                card["state"] == "confirmed" and statuses and statuses <= RETRYABLE)):
            raise Refused(["Only a change that was dismissed, expired, failed or not written "
                           "can be prepared again; check the system first."], 409)
        if first.get("request") is None:
            raise Refused(["An autopilot's item is accepted again on the Autopilot page."],
                          409)
        request = self._unstash(first.get("request") or {})
        return self.prepare(person, request, origin=str(first.get("origin")),
                            citations=list(first["item"].get("citations") or ()), now=now,
                            diff=first.get("diff") if first.get("trusted") else None,
                            read={"name": (first.get("shown") or [{}])[0].get("record")}
                            if first.get("trusted") else None)

    # ---- bytes kept beside the journal ---------------------------------------------------

    def _blobs(self) -> Path:
        path = self.state / "prepared-writes" / "blobs"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def _stash(self, value: Any, key: str = "") -> Any:
        """``value`` with each long ``*_b64`` string (a document's bytes) kept in an
        owner-only file by its digest, so the journal stays small; ``_unstash`` undoes it."""
        if isinstance(value, Mapping):
            return {k: self._stash(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self._stash(v) for v in value]
        if key.endswith("_b64") and isinstance(value, str) and len(value) > 512:
            import os
            digest = hashlib.sha256(value.encode("ascii", "replace")).hexdigest()
            path = self._blobs() / digest
            if not path.exists():
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as out:
                    out.write(value)
            return f"blob:sha256:{digest}"
        return value

    def _unstash(self, value: Any, key: str = "") -> Any:
        if isinstance(value, Mapping):
            return {k: self._unstash(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self._unstash(v) for v in value]
        if key.endswith("_b64") and isinstance(value, str) and value.startswith("blob:sha256:"):
            digest = value.rsplit(":", 1)[1]
            text = (self._blobs() / digest).read_text(encoding="ascii") \
                if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest) else ""
            if hashlib.sha256(text.encode("ascii")).hexdigest() != digest:
                raise Refused(["The prepared document's content is missing or changed: "
                               "prepare it again."], 409)
            return text
        return value

    @staticmethod
    def _bc_companies(executor: aw.Executor, now: Optional[datetime] = None
                      ) -> Optional[dict[str, dict[str, Any]]]:
        """The host's BC companies as the readiness probe of the last 45 minutes found
        them (id -> BC's name and display name), None when there is no such probe."""
        from operating import autopilot_write_readiness as readiness
        try:
            return readiness.verified_companies(executor.state, executor.environ, now)
        except Exception:  # noqa: BLE001 - an unreadable record: no names are known
            return None

    def _bc_named(self, request: Mapping[str, Any], executor: aw.Executor, now: datetime
                  ) -> dict[str, Any]:
        """``request`` with its BC company by id: ``target.company_id`` one the host
        configured, or ``target.company_name`` BC's name (or display name) for exactly one
        of them as the probe found it; the target then holds the id and BC's name. A name
        that is not one of them, two companies of that name, or a name with no probe on
        record is refused: nothing is ever written to the default for a name. Raises
        ``Refused``."""
        from operating import autopilot_write_readiness as readiness
        target = dict(request.get("target") or {}) \
            if isinstance(request.get("target"), Mapping) else {}
        configured = readiness.bc_company_ids(executor.environ)
        found = self._bc_companies(executor, now) or {}
        names = ", ".join(f"{v['name']} ({k})" for k, v in found.items()) or \
            ", ".join(configured) or "none"
        given_id = target.get("company_id")
        given_name = target.get("company_name")
        if given_id is None and given_name is None:
            raise Refused([f"Name the BC company the change is for (one of: {names})."])
        if given_id is not None:
            if not isinstance(given_id, str) or given_id.strip().lower() not in configured:
                raise Refused([f"That BC company id is not one the Workbench writes to (one "
                               f"of: {names})."])
            company = given_id.strip().lower()
        else:
            wanted = " ".join(str(given_name).split()).casefold()
            hits = [k for k, v in found.items() if wanted and wanted in {
                " ".join(str(n).split()).casefold() for n in (v["name"], v["display_name"])
                if n}]
            if not found:
                raise Refused(["BC's companies have not been checked in the last 45 minutes "
                               "(the write readiness probe), so a company named by its name "
                               "cannot be matched: give its id."])
            if len(hits) != 1:
                raise Refused([f"BC has no company the Workbench writes to named "
                               f"{str(given_name)[:60]!r} (one of: {names})." if not hits else
                               f"More than one BC company is named {str(given_name)[:60]!r}:"
                               " give its id."])
            company = hits[0]
        known = found.get(company)
        if given_name is not None and given_id is not None and (known is None or " ".join(
                str(given_name).split()).casefold() not in {" ".join(str(n).split()).casefold()
                                                           for n in (known["name"],
                                                                     known["display_name"])
                                                           if n}):
            raise Refused([f"The BC company id and name do not name the same company (the "
                           f"id is {known['name'] if known else 'not one BC was checked for'})"
                           "."])
        target["company_id"] = company
        if known is not None:
            target["company_name"] = known["name"]
        else:
            target.pop("company_name", None)
        return {**request, "target": target}

    def _bc_company(self, executor: aw.Executor, action: Mapping[str, Any],
                    now: Optional[datetime] = None
                    ) -> tuple[Optional[dict[str, Any]], list[str]]:
        """The company a BC action's request goes to (``BCAdapter._company``: the id the
        target names, else the host's default) with BC's name for it, and why the card
        would show it wrong: a name on the target that is not BC's name for that id."""
        adapter = executor.adapters.get("bc_write")
        try:
            company = adapter._company(action) if adapter is not None else None
        except wa.AdapterRefused:
            # no company it may write: the request itself is refused (``adapter.request``)
            return None, []
        if company is None:
            return None, []
        known = (self._bc_companies(executor, now) or {}).get(company)
        shown = {"id": company, "name": known["name"] if known else None}
        named = (action.get("target") or {}).get("company_name")
        if named is not None and (known is None or named != known["name"]):
            return shown, ["The BC company named on the change is not the one it would be "
                           "written to: nothing is prepared."]
        return shown, []

    @staticmethod
    def _undo_words(executor: aw.Executor, action: Mapping[str, Any]) -> str:
        adapter = executor.adapters.get(str(action.get("kind")))
        if adapter is None:
            return ""
        try:
            undo = adapter.undo_request(action, adapter.request(action))
        except Exception:  # noqa: BLE001 - checked at prepare; an undo note only
            undo = None
        return "It can be undone from here." if undo else adapter.undo_note()

    # ---- reading -----------------------------------------------------------------------

    @staticmethod
    def _rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return [r for r in rows if isinstance(r, Mapping)]

    def _history(self, pid: str) -> tuple[Optional[Mapping[str, Any]], list[Mapping[str, Any]]]:
        rows = [r for r in self._rows(self.journal.rows()) if r.get("id") == pid]
        first = next((r for r in rows if r.get("event") == "prepared"), None)
        return first, rows

    def _own(self, person: Person, pid: str) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
        first, rows = self._history(pid)
        if first is None or not person.owns(str(first.get("owner") or "")):
            # another person's prepared write is not found, never shown
            raise Refused(["That prepared change is not one of yours."], 404)
        return first, rows

    def mine(self, person: Person, *, open_only: bool = True,
             now: Optional[datetime] = None) -> list[dict[str, Any]]:
        ids = [r["id"] for r in self._rows(self.journal.rows())
               if r.get("event") == "prepared" and person.owns(str(r.get("owner") or ""))]
        cards = [self.card(person, pid, now=now) for pid in dict.fromkeys(ids)]
        return [c for c in cards if c["state"] == "prepared" or not open_only]

    def card(self, person: Person, pid: str, *, now: Optional[datetime] = None
             ) -> dict[str, Any]:
        """What the app shows of a prepared write: every action as the executor runs it
        (``shown_action``: system, operation, target ids, the record's name as read,
        every field, a message whole), the diff (before as the system holds it), its state
        and, once run, the executor's outcome per action. Nothing here re-checks;
        ``preview`` does."""
        first, rows = self._own(person, pid)
        state = self._state(pid, rows, now or _now())
        outcome: list[Mapping[str, Any]] = []
        for r in rows:
            if r.get("event") == "outcome":
                outcome = list(r.get("results") or ())
        item = first["item"]
        return {"prepared_id": pid, "state": state, "origin": first.get("origin"),
                "title": item.get("title"), "reason": item["body"].get("label"),
                "citations": list(item.get("citations") or ()),
                "actions": [dict(a) for a in first.get("shown") or ()],
                "diff": list(first.get("diff") or ()), "undo": list(first.get("undo") or ()),
                "expires": aw._iso(aw._instant(first["at"]) + EXPIRES),
                "outcome": [{k: r.get(k) for k in ("kind", "status", "mode", "reasons", "key")}
                            for r in outcome]}

    # ---- preview and confirm -----------------------------------------------------------

    def _shown(self, person: Person, first: Mapping[str, Any], executor: aw.Executor,
               now: Optional[datetime] = None) -> dict[str, Any]:
        item = first["item"]
        whole = self._unstash(item)
        live = executor.live_kinds(whole)
        missing: list[str] = []
        acknowledge: list[str] = []
        try:
            rules = aw.load_rules(executor.rules_path)
        except aw.RulesInvalid:
            rules = None
            missing.append("The write rules cannot be read: nothing runs.")
        actions = aw.actions_of(whole)
        # what runs is what the card shows: the stored item's actions, shown again, must
        # be the card's (else nothing runs)
        stored = [dict(a) for a in first.get("shown") or ()]
        again = [shown_action(a, {"name": s.get("record")},
                              self._bc_company(executor, a, now)[0]
                              if a.get("kind") == "bc_write" else None)
                 for a, s in zip(actions, stored)]
        if len(actions) != len(stored) or _canonical(again) != _canonical(stored) \
                or any(set(a) - ACTION_KEYS for a in actions):
            raise Refused(["What would be written is not what the card shows: prepare it "
                           "again."], 409)
        for action in actions:
            kind = str(action.get("kind"))
            adapter = executor.adapters.get(kind)
            why = adapter.unavailable() if adapter is not None else "no adapter"
            if kind not in live:
                missing.append(f"{action.get('system')} is not opened to approved writes "
                               f"(PLX_APPROVED_WRITES lacks {kind}).")
            if why:
                missing.append(str(why))
            if rules is not None:
                acknowledge += aw.routine(rules, whole, action, str(
                    first.get("origin") or "instruction"), executor._known_domains())
        shown = {"live": bool(live) and not missing, "missing": list(dict.fromkeys(missing)),
                 "acknowledge": list(dict.fromkeys(acknowledge))}
        # the hash covers the card exactly as shown and the stored item it stands for
        # (a document's bytes by their digest)
        card = {k: v for k, v in self.card(person, str(first["id"])).items()
                if k not in ("state", "outcome", "expires")}
        shown["preview_hash"] = hashlib.sha256(_canonical(
            {"card": card, "item": item, **shown}).encode("utf-8")).hexdigest()
        return shown

    def preview(self, person: Person, pid: str, *, now: Optional[datetime] = None
                ) -> dict[str, Any]:
        """The card with what confirming it does now: ``live`` (it is written at once) or
        recorded as a dry run with ``missing``; the reasons to acknowledge; the hash the
        confirm must carry."""
        first, _rows = self._own(person, pid)
        card = self.card(person, pid, now=now)
        if card["state"] != "prepared":
            return card
        return {**card, **self._shown(person, first, self.executor(), now)}

    def _may_now(self, person: Person, item: Mapping[str, Any], own: Iterable[str] = ()
                 ) -> list[str]:
        """Could the person make it themselves now (a grant or role revoked since the
        prepare is seen: the route builds the check anew per request)."""
        if person.full:
            return []
        problems = []
        for action in aw.actions_of(item):
            if action.get("kind") in OWN or action.get("kind") in set(own):
                continue
            why = (self.may(person, action) if self.may is not None
                   else "the Workbench cannot check your own access there")
            if why:
                problems.append(f"You cannot make this {action.get('system')} change "
                                f"yourself now: {why}.")
        return problems

    def confirm(self, person: Person, pid: str, *, preview_hash: str, confirm: bool,
                now: Optional[datetime] = None, confirmed_live: Optional[bool] = None,
                acknowledged: Optional[Sequence[str]] = None) -> dict[str, Any]:
        """The person's second step: the preview they saw (its hash, re-computed now) and
        an explicit confirm. One request of several runs it; the others read its card."""
        now = now or _now()
        if confirm is not True:
            raise Refused(["Nothing is done without your confirm."], 409)
        first, rows = self._own(person, pid)
        if self._state(pid, rows, now) == "expired":
            raise Refused(["This prepared change is more than a day old: prepare it again "
                           "so it is checked against the system as it is now."], 409)
        executor = self.executor()
        shown = self._shown(person, first, executor, now)
        if shown["preview_hash"] != preview_hash:
            raise Refused(["The change or what it would do changed since you saw it: "
                           "preview it again."], 409)
        refused = self._may_now(person, first["item"], first.get("own") or ())
        if refused:
            raise Refused(refused, 403)
        with self.journal.locked() as held:
            rows = [r for r in self._rows(held.rows) if r.get("id") == pid]
            if any(r.get("event") in ("confirmed", "dismissed") for r in rows):
                mine = False
            else:
                held.append({"event": "confirmed", "at": aw._iso(now), "id": pid,
                             "by": person.email, "preview_hash": preview_hash,
                             "live": shown["live"]})
                mine = True
        if mine:
            origin = "instruction" if first.get("origin") == "instruction" else "autopilot"
            # the Autopilot page's accept says what its person confirmed (its live kinds,
            # with its own confirm token; nothing acknowledged it did not show)
            results = executor.run(self._unstash(first["item"]), now, aw.Approval(
                by=tuple(sorted(person.ids)), origin=origin,
                confirmed_live=shown["live"] if confirmed_live is None else confirmed_live,
                acknowledged=tuple(shown["acknowledge"] if acknowledged is None
                                   else acknowledged)))
            with self.journal.locked() as held:
                held.append({"event": "outcome", "at": aw._iso(now), "id": pid,
                             "results": [{k: r.get(k) for k in (
                                 "kind", "status", "mode", "reasons", "key")}
                                 for r in results]})
        return self.card(person, pid, now=now)

    def dismiss(self, person: Person, pid: str, now: Optional[datetime] = None
                ) -> dict[str, Any]:
        self._own(person, pid)
        with self.journal.locked() as held:
            rows = [r for r in self._rows(held.rows) if r.get("id") == pid]
            if not any(r.get("event") in ("confirmed", "dismissed") for r in rows):
                held.append({"event": "dismissed", "at": aw._iso(now or _now()), "id": pid,
                             "by": person.email})
        self.prune(now)
        return self.card(person, pid, now=now)

    def undo(self, person: Person, pid: str, now: Optional[datetime] = None
             ) -> dict[str, Any]:
        """Undo a done write the system can undo (the executor's recorded inverse), by the
        person it acted as; last action first."""
        now = now or _now()
        card = self.card(person, pid, now=now)
        done = [o for o in card["outcome"] if o.get("status") == "done" and o.get("key")]
        if card["state"] != "confirmed" or not done:
            raise Refused(["Only a change that was written can be undone."], 409)
        executor = self.executor()
        undone = []
        for o in reversed(done):
            try:
                undone.append(executor.undo(str(o["key"]), now, by=person.email))
            except ValueError as error:
                raise Refused([str(error)], 409) from None
        with self.journal.locked() as held:
            held.append({"event": "undone", "at": aw._iso(now), "id": pid,
                         "by": person.email,
                         "results": [{k: u.get(k) for k in ("status", "reasons")}
                                     for u in undone]})
        self.prune(now)
        return self.card(person, pid, now=now)


# ---- the model's tool --------------------------------------------------------------------

TOOL_SPEC = {
    "name": TOOL_NAME,
    "description": (
        "Prepare a change in a business system for the person to confirm in the app. "
        "Nothing is written or sent by this tool: the person sees the change and confirms "
        "it in two steps. Use it whenever the person asks you to do, change, create, send, "
        "approve or update something. system: bc | mc | planner | calendar | sharepoint | "
        "portal | mail | teams. operation: the system's operation (e.g. bc "
        "sales_order_create, sales_order_update, sales_order_line_update, "
        "purchase_order_line_update; mc approval_decide (change.decision approve), "
        "task_note; planner task_create, task_update; sharepoint list_item_update, "
        "file_upload; mail send_email). target: only the ids the write uses, as read (bc: "
        "company_id or the company's name as BC names it in company_name, document_id, "
        "line_id; planner: task_id; sharepoint: site_id, list_id, item_id; portal "
        "sales-order.update / purchase-order.update: id, activity.note: record_type, "
        "record_id, ship-release.create: order_id); a name the write "
        "does not use is refused. change: the fields to set (mail: to, cc, subject, body, "
        "attachments: [file_id of a file you made]; sharepoint file_upload: name, file_id, "
        "target.beside: the sharepoint:// handle of a file in the folder to save into; "
        "planner task_create: planId, title). prior "
        "(optional): the values you read; the system's current ones are read anyway. "
        "reason: one line why, in the person's terms. citations: the handles you read."),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "required": ["system", "operation", "target", "change", "reason"],
        "properties": {
            "system": {"type": "string"}, "operation": {"type": "string"},
            "target": {"type": "object"}, "change": {"type": "object"},
            "prior": {"type": "object"}, "precondition": {"type": "object"},
            "people": {"type": "array", "items": {"type": "string"}},
            "title": {"type": "string"}, "reason": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "string"}}}},
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}
TOOL_FIELDS = frozenset(TOOL_SPEC["inputSchema"]["properties"])  # type: ignore[index]

# ``plx.edit_document`` (``operator_api.prepared_area`` binds it to a reader's own
# document bytes; an autopilot job proposes with it)
DOC_TOOL_SPEC = {
    "name": "plx.edit_document",
    "description": (
        "Prepare an edit of a SharePoint or OneDrive document's own content for the person "
        "to confirm in the app (nothing is written by this tool). citation: the document's "
        "sharepoint:// handle as read. Word or text: find (the exact text, quoted enough to "
        "occur once) and replace. Excel: sheet, cell (like B7) and value. reason: one line "
        "why. It answers the diff (before/after) or why it was refused."),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "required": ["citation", "reason"],
        "properties": {"citation": {"type": "string"}, "find": {"type": "string"},
                       "replace": {"type": "string"}, "sheet": {"type": "string"},
                       "cell": {"type": "string"}, "value": {"type": ["string", "number"]},
                       "reason": {"type": "string"}}},
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}


def tool(desk: Desk, person: Person, *, origin: str = "instruction") -> Any:
    """``plx.prepare_write`` bound to one person (``agent_session.LocalTool``): the model's
    one general write tool. It answers the prepared card or the reasons it was refused;
    it never writes, sends or confirms."""
    from operating.agent_session import LocalTool

    def call(arguments: dict[str, Any]) -> Any:
        if not isinstance(arguments, Mapping) or set(arguments) - TOOL_FIELDS:
            return {"refused": ["Unknown fields: "
                                + ", ".join(sorted(set(arguments) - TOOL_FIELDS)[:5])]
                    if isinstance(arguments, Mapping) else ["Arguments must be an object."]}
        request = {k: v for k, v in arguments.items() if k != "citations"}
        cites = [c for c in arguments.get("citations") or () if isinstance(c, str)]
        try:
            card = desk.prepare(person, request, origin=origin, citations=cites)
        except Refused as refused:
            # a near-miss field name: where the system keeps it (``field_hints``)
            from operating.autopilot_jobs import field_hints
            return {"refused": list(refused.problems) + field_hints(refused.problems)[:3]}
        return {"prepared_id": card["prepared_id"], "diff": card["diff"],
                "actions": card["actions"],
                "note": "Prepared, not done: the person confirms it in the app. Tell them "
                        "what you prepared; never say it was done."}

    return LocalTool(spec=tool_spec(), call=call)


_SPEC_WITH_FIELDS: dict[str, Any] = {}


def tool_spec() -> dict[str, Any]:
    """``TOOL_SPEC`` with every operation and the exact target ids and fields it takes,
    read from the write adapters (``autopilot_jobs.catalogue``): the model names a field
    as the system does (BC's requestedReceiptDate, Planner's dueDateTime), not as it
    reads in the question ("expected date", "due")."""
    if not _SPEC_WITH_FIELDS:
        from operating.autopilot_jobs import catalogue
        _SPEC_WITH_FIELDS.update({**TOOL_SPEC, "description": str(TOOL_SPEC["description"])
                                  + " Each operation's target ids and the change fields it "
                                  "sets (use these names exactly; a date is ISO 8601): "
                                  + " | ".join(catalogue())})
    return _SPEC_WITH_FIELDS
