"""The app's autopilot area (``/api/autopilot*``): today's prepared items the reader may
see, each with its evidence, drafted action and the reader's decision; per role and
workflow the replay scores (dev and fresh), the write state and the owner's switches; the
write path's audit log with undo where the system supports it.

Reads the autopilot's queue and gate files and the write path's state directory
(``operating.autopilot_write``; ``$state_dir/autopilot-write``). Writes only the
Workbench's own files there: item decisions (a hash-chained journal), and for the
owner-open reader only (decided server-side) the write path's own switch files (global
kill, per-workflow off, per role and workflow live flag). An accept runs the item through
``autopilot_write.Executor``, the one gated write path: it records a dry run with the exact
request unless the workflow passed its fresh gate, the owner was told and switched it on
and the host's master switch is set. Nothing else here sends or writes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

from operating import autopilot as ap
from operating import autopilot_write as aw
from operating import workflows as wf
from operating.card_actions import Journal

from . import models as m
from .auth import Reader
from .errors import UserFacing

DECISIONS_FILE = "autopilot-writes/decisions.jsonl"
SNOOZE_DAYS = 30
SYSTEM_OF_SCHEME = {"graph-mail": "mail", "teams-chat": "teams", "teams-channel": "teams",
                    "mc": "mc", "bc": "bc", "api": "portal", "sharepoint": "sharepoint",
                    "sharepoint-list": "sharepoint", "calendar-event": "meetings",
                    "meeting-transcript": "meetings", "planner-task": "planner"}
WRITE_SYSTEM = {"bc_write": "bc", "sage_write": "sage", "portal_write": "portal",
                "filemaker_write": "filemaker", "send_mail": "mail", "send_teams": "teams",
                "mc_approval": "mc"}
AUDIT_EVENTS = ("decision", "attempt", "outcome", "reconciled", "gate_passed", "gate_lost",
                "owner_notified", "switch")
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
    if str(happened.get("object_type") or "") in ap.TEAMS_TYPES:
        return "teams"
    if ":sage" in str(happened.get("object_id") or "").lower():
        return "sage"
    for cite in item.get("citations") or ():
        scheme = str(cite).split("://", 1)[0]
        if scheme in SYSTEM_OF_SCHEME:
            return SYSTEM_OF_SCHEME[scheme]
    return "other"


def _person_name(role: str) -> str:
    """A ``person:<address>`` role's person, by the address's first word."""
    local = role[len("person:"):].split("@", 1)[0]
    return local.replace(".", " ").replace("_", " ").strip().title()


class AutopilotAreaMixin:
    """Needs ``AutopilotMixin``. Tests may set ``autopilot_write_state`` (the write path's
    state dir), ``autopilot_adapters`` (kind -> adapter; a test never reaches a real
    system), ``autopilot_guard`` (the people exclusion) and ``autopilot_owner``
    (``reader -> bool``); without that hook the owner is the access layer's owner-open
    reader (``_is_owner``), and a backend without one has no owner (fail closed)."""

    autopilot_write_state: Optional[Path] = None
    autopilot_adapters: Any = None
    autopilot_guard: Any = None
    autopilot_owner: Any = None

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

    def _apa_decisions(self, state: Optional[Path]) -> dict[str, dict[str, Any]]:
        if state is None or not (state / DECISIONS_FILE).exists():
            return {}
        last: dict[str, dict[str, Any]] = {}
        for row in Journal(state / DECISIONS_FILE).rows():
            last[str(row.get("item_id"))] = row
        return last

    def _apa_roles(self) -> dict[str, list[str]]:
        """The roles file's members (``autopilot._roles``) beside the queue."""
        path = self._ap_queue_path()  # type: ignore[attr-defined]
        return ap._roles(self._ap_env(), path.parent) if path else {}  # type: ignore[attr-defined]

    def _apa_executor(self, state: Path) -> aw.Executor:
        env = dict(self._ap_env())  # type: ignore[attr-defined]
        guard = self.autopilot_guard
        if guard is None:
            from operating.card_actions import exclusion_for
            base = getattr(self, "state_dir", None)
            guard = exclusion_for(env, Path(base) if base else None)
        return aw.Executor(state, environ=env, adapters=self.autopilot_adapters, guard=guard)

    @staticmethod
    def _apa_off(env: Mapping[str, str], state: Optional[Path]) -> tuple[bool, set[str], bool]:
        """(global kill, workflows off, the workflow file unreadable)."""
        if state is None:
            return bool(env.get(aw.KILL_ENV) or env.get(ap.KILL_ENV)), set(), False
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

    def autopilot_area(self, reader: Reader) -> m.AutopilotArea:
        now = datetime.now(timezone.utc)
        owner = self._apa_is_owner(reader)
        env = self._ap_env()  # type: ignore[attr-defined]
        state = self._apa_state()
        queue_path = self._ap_queue_path()  # type: ignore[attr-defined]
        notes: list[str] = []
        raw_items = self._ap_visible(reader, everyone=owner)  # type: ignore[attr-defined]
        if raw_items is None:
            raw_items = []
            notes.append("The autopilot has not prepared anything on this server yet.")
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
        dev = _gate_doc(Path(env.get(ap.GATE_ENV) or (queue_path.parent / ap.GATE_FILE))
                        if queue_path else Path("/nonexistent"), ap.GATE_SCHEMA)
        fresh = _gate_doc(state / aw.WRITE_GATE_FILE, aw.WRITE_GATE_SCHEMA) if state else {}
        member_of: dict[str, set[str]] = {}
        for source in (role_members, {r: v.get("members") or () for r, v in dev.items()},
                       {r: v.get("members") or () for r, v in fresh.items()}):
            for role, members in source.items():
                for oid in members:
                    member_of.setdefault(str(oid).lower(), set()).add(role)
        today = now.astimezone(_LOCAL).date()
        items: list[m.AutopilotItem] = []
        last_at: dict[str, str] = {}
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
                    decision = m.AutopilotDecision(
                        decision=dec["decision"], at=dec["at"], by_you=dec.get("by") in me,
                        until=dec.get("until"), text=dec.get("text"), note=dec.get("note"),
                        outcome=dec.get("outcome"), reasons=list(dec.get("reasons") or ()),
                        sent=dec.get("sent") is True)
            created = _instant(item["created_at"])
            due = _instant(item["due_at"])
            is_today = bool((created and created.astimezone(_LOCAL).date() == today) or
                            (due and due.astimezone(_LOCAL).date() <= today))
            last_at[flow] = max(last_at.get(flow, ""), item["created_at"])
            items.append(m.AutopilotItem(
                item_id=item["item_id"], autopilot=item["autopilot"],
                label=item["body"].get("label") or item["autopilot"], title=item["title"],
                workflow=flow, system=system_of(item), roles=sorted(roles),
                owner=item["owner"], mine=item["mine"], happened=happened,
                prepared=item["body"].get("prepared") or {}, citations=item["citations"],
                due_at=item["due_at"], created_at=item["created_at"], today=is_today,
                state=item_state, decision=decision))
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
        for it in items:
            people.setdefault(it.owner, m.AutopilotPerson(id=it.owner, name=""))
        for oid, p in people.items():
            p.you = oid in me
            p.roles = sorted(member_of.get(oid, ()))
        # per role and workflow: scores, state, switches (a reader who is not the owner
        # sees only the roles they belong to)
        live = aw.live_flags(env, state) if state else set()
        mine_roles = set().union(*(member_of.get(o, set()) for o in me)) if me else set()
        workflows: list[m.AutopilotWorkflowRow] = []
        try:
            rules = aw.load_rules()
            gate = aw.WriteGate(state / aw.WRITE_GATE_FILE, rules) if state else None
        except aw.RulesInvalid:
            gate = None
            notes.append("The write rules are unreadable: nothing is performed.")
        passing = set(gate.passing()) if gate else set()
        master = env.get(aw.MASTER_ENV) == "1"
        open_by: dict[str, int] = {}
        for it in items:
            if it.state in ("waiting", "edited"):
                open_by[it.workflow] = open_by.get(it.workflow, 0) + 1
        for role in sorted(set(dev) | set(fresh)):
            if not owner and role not in mine_roles:
                continue
            d, f = dev.get(role) or {}, fresh.get(role) or {}
            person = None
            if role.startswith("person:"):
                found = list((d or f).get("members") or ())
                person = str(found[0]).lower() if len(found) == 1 else None
            flows = set((d.get("workflows") or {})) | set((f.get("workflows") or {}))
            for flow in sorted(flows):
                ds, fs = _score((d.get("workflows") or {}).get(flow)), \
                    _score((f.get("workflows") or {}).get(flow))
                flag = f"{role}:{flow}".lower() in live
                missing: list[str] = []
                if glob or flow in off or broken:
                    st = "off"
                elif (role, flow) not in passing:
                    st, missing = "dry_run", ["gate_not_passed"]
                elif not aw._notified(rows, role, flow) or not flag:
                    st, missing = "passed_awaiting_owner", ["owner_flag_off"]
                elif not master:
                    st, missing = "live_held", ["master_switch_off"]
                else:
                    st = "live"
                window = f.get("window") or []
                workflows.append(m.AutopilotWorkflowRow(
                    role=role, person=person, workflow=flow, dev=ds, fresh=fs,
                    n=(fs or ds).judged if (fs or ds) else 0, state=st,
                    on=not (flow in off or broken), live_flag=flag, missing=missing,
                    last_run=last_at.get(flow) or None,
                    scored_at=str(window[-1]) if window else None,
                    open_items=open_by.get(flow, 0)))
        if not dev and not fresh:
            notes.append("No replay has scored the workflows yet.")
        # items are prepared only for a workflow that passed its replay gate
        # (``autopilot.gate_from``): with none passed, that is why nothing is here
        if not items and not ap.gate_of({"schema": ap.GATE_SCHEMA, "roles": dev})[1]:
            notes.insert(0, NO_GATE_PASSED)
        roles_seen = {r for r in set(dev) | set(fresh) | set(role_members)
                      if not r.startswith("person:")}
        roles_seen |= {r for it in items for r in it.roles}
        if not owner:
            roles_seen &= mine_roles | {r for it in items for r in it.roles}
        return m.AutopilotArea(
            owner=owner,
            switches=m.AutopilotSwitches(
                global_off=glob, master_switch=master,
                autopilot_off=bool(str(env.get(ap.KILL_ENV, "")).lower() in
                                   ("1", "true", "yes", "on") or
                                   (queue_path is not None and
                                    (queue_path.parent / ap.KILL_FILE).exists())),
                workflows_off=sorted(off) if owner else sorted(
                    off & {w.workflow for w in workflows})),
            people=sorted(people.values(), key=lambda p: (not p.you, not p.name, p.name, p.id)),
            roles=sorted(roles_seen), items=items, workflows=workflows, notes=notes,
            known_workflows=_known_workflows())

    # ------------------------------------------------------------ decisions

    def autopilot_decide(self, reader: Reader, item_id: str,
                         body: m.AutopilotDecisionRequest) -> m.AutopilotItem:
        """Record the reader's decision on an item of their own; an accept goes through the
        write path (a dry run unless the workflow is live)."""
        now = datetime.now(timezone.utc)
        env = self._ap_env()  # type: ignore[attr-defined]
        state = self._apa_state()
        found = None
        for item in self._ap_visible(reader) or ():  # type: ignore[attr-defined]
            if item["item_id"] == item_id and item["mine"]:
                found = item
        if found is None:
            raise UserFacing("not_found", "That item is not on your list.", 404)
        if state is None:
            raise UserFacing("action_refused", "The autopilot's state directory is not "
                             "configured on this server.", 409)
        text = (body.text or "").strip() or None
        row: dict[str, Any] = {"event": "item_decision", "item_id": item_id,
                               "decision": body.decision, "at": _iso(now),
                               "by": sorted(self._apa_me(reader))[0],
                               "note": (body.note or "").strip()[:500] or None}
        if body.decision == "snooze":
            until = _instant(body.until)
            if until is None or until <= now or until > now + timedelta(days=SNOOZE_DAYS):
                raise UserFacing("bad_request", "Snooze needs a time in the next "
                                 f"{SNOOZE_DAYS} days.", 422)
            row["until"] = _iso(until)
        if body.decision == "edit" and not text:
            raise UserFacing("bad_request", "An edit needs the draft's new text.", 422)
        draft = ((found["body"].get("prepared") or {}).get("draft") or {})
        if text and not draft:
            raise UserFacing("bad_request", "This item has no draft to edit.", 422)
        if body.decision == "accept" and not text:
            prior = self._apa_decisions(state).get(item_id) or {}
            text = prior.get("text") if prior.get("decision") == "edit" else None
        if text:
            row["text"] = text
        if body.decision == "accept":
            if self._apa_off(env, state)[0]:
                raise UserFacing("action_refused", "The autopilot is switched off.", 409)
            item = found
            if text:
                prepared = {**(found["body"].get("prepared") or {}),
                            "draft": {**draft, "body": text}}
                item = {**found, "body": {**found["body"], "prepared": prepared}}
            results = self._apa_executor(state).run(item, now)
            row["outcome"] = results[0]["status"] if results else None
            row["outcomes"] = [r["status"] for r in results]
            # only the email's own result says it went out (an entry before it can be done
            # while the confirmation failed, was refused or ran dry)
            row["sent"] = any(r.get("kind") == "send_mail" and r.get("status") == "done"
                              for r in results)
            row["reasons"] = [str(x) for r in results for x in r.get("reasons") or ()][:8]
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        journal = Journal(state / DECISIONS_FILE)
        with journal.locked() as held:
            held.append(row)
        for it in self.autopilot_area(reader).items:
            if it.item_id == item_id:
                return it
        raise UserFacing("not_found", "That item is not on your list.", 404)

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
        return self.autopilot_area(reader).switches

    def autopilot_workflow(self, reader: Reader, workflow: str,
                           body: m.AutopilotWorkflowSwitch) -> m.AutopilotArea:
        """The owner switches a workflow off or on (the write path's per-workflow kill
        switch), or a role's workflow live (only once it passed its fresh gate; switching
        it on is the owner's acknowledgement of the pass) or back to dry run."""
        state = self._apa_owner_only(reader)
        if not aw._ROLE_FLOW.fullmatch(workflow.lower()) or ":" in workflow:
            raise UserFacing("not_found", "No such workflow.", 404)
        if body.on is None and body.live is None:
            raise UserFacing("bad_request", "Say on or live.", 422)
        now = datetime.now(timezone.utc)
        journal = self._apa_journal(state)
        if body.on is not None:
            path = state / aw.WORKFLOW_KILL_FILE
            with journal.locked():
                try:
                    off = set(json.loads(path.read_text(encoding="utf-8"))["workflows"]) \
                        if path.exists() else set()
                except (OSError, ValueError, KeyError, TypeError):
                    off = set()     # unreadable: every workflow was off; rebuilt from here
                    off |= {w.workflow for w in self.autopilot_area(reader).workflows}
                (off.discard if body.on else off.add)(workflow)
                _write_json(path, {"workflows": sorted(off)})
            self._apa_switch_row(state, reader, switch="workflow_on", workflow=workflow,
                                 value=body.on)
        if body.live is not None:
            role = str(body.role or "")
            if not role or not aw._ROLE_FLOW.fullmatch(role.lower()):
                raise UserFacing("bad_request", "Say which role's workflow goes live.", 422)
            flag = f"{role}:{workflow}".lower()
            path = state / aw.LIVE_FILE
            if body.live:
                gate = aw.WriteGate(state / aw.WRITE_GATE_FILE, aw.load_rules())
                if (role, workflow) not in set(gate.passing()):
                    raise UserFacing("action_refused", "This workflow has not passed its "
                                     "fresh gate for this role: it stays a dry run.", 409)
                if any(n["role"] == role and n["workflow"] == workflow
                       for n in aw.notices(journal.rows())):
                    aw.ack_notice(journal, role, workflow, now,
                                  "owner:" + sorted(self._apa_me(reader))[0])
            with journal.locked():
                try:
                    flags = set(json.loads(path.read_text(encoding="utf-8")).get("live") or ()) \
                        if path.exists() else set()
                except (OSError, ValueError, AttributeError):
                    flags = set()
                flags = {str(f).lower() for f in flags}
                (flags.add if body.live else flags.discard)(flag)
                _write_json(path, {"live": sorted(flags)})
            self._apa_switch_row(state, reader, switch="live", role=role, workflow=workflow,
                                 value=body.live)
        return self.autopilot_area(reader)

    # ------------------------------------------------------------ audit, undo

    def autopilot_audit(self, reader: Reader) -> m.AutopilotAudit:
        if not self._apa_is_owner(reader):
            raise UserFacing("forbidden", "Only the owner reads the autopilot's audit log.",
                             403)
        rows = self._apa_rows(self._apa_state())
        undone = {r.get("of") for r in rows if r.get("event") in ("attempt", "outcome")
                  and r.get("of")}
        titles: dict[str, str] = {}
        path = self._ap_queue_path()  # type: ignore[attr-defined]
        if path is not None and path.exists():
            for it in self._ap_visible(reader, everyone=True) or ():  # type: ignore[attr-defined]
                titles[it["item_id"]] = it["title"]
        out = []
        for r in reversed(rows):
            if r.get("event") not in AUDIT_EVENTS:
                continue
            target = r.get("target")
            done = r.get("event") == "outcome" and r.get("status") == "done" and not r.get("of")
            out.append(m.AutopilotAuditRow(
                at=str(r.get("at") or ""), event=str(r["event"]), status=r.get("status"),
                mode=r.get("mode"), workflow=r.get("workflow"), role=r.get("role"),
                system=r.get("system") or WRITE_SYSTEM.get(str(r.get("kind")))
                if r.get("kind") else r.get("system"),
                kind=r.get("kind"), operation=r.get("operation"),
                target=json.dumps(target, sort_keys=True)[:200]
                if isinstance(target, (dict, list)) else (str(target)[:200] if target else None),
                item_id=r.get("item_id"), title=titles.get(str(r.get("item_id"))),
                why=r.get("why"), reasons=[str(x) for x in r.get("reasons") or ()][:8],
                key=r.get("key"), by=r.get("by"),
                undoable=bool(done and r.get("inverse") and r.get("key") not in undone),
                undone=bool(done and r.get("key") in undone),
                undo_note=r.get("undo_note") if done else None))
        live = sum(1 for r in rows if r.get("event") == "outcome" and r.get("mode") == "live"
                   and r.get("status") == "done")
        return m.AutopilotAudit(rows=out[:500], live=live)

    def autopilot_undo(self, reader: Reader, key: str) -> m.AutopilotAudit:
        """The owner undoes a live action once, through the write path's recorded inverse."""
        state = self._apa_owner_only(reader)
        try:
            self._apa_executor(state).undo(key, datetime.now(timezone.utc))
        except (ValueError, KeyError) as refused:
            raise UserFacing("action_refused", f"Not undone: {refused}"[:300], 409)
        return self.autopilot_audit(reader)


NO_GATE_PASSED = ("No workflow has passed its gate yet. Items appear here once one passes "
                  "its replay check.")


def _known_workflows() -> list[str]:
    """Every workflow name the catalogue holds; none when it cannot be read (the filter
    then offers the workflows the page shows)."""
    try:
        return sorted(wf.names())
    except Exception:  # noqa: BLE001 - an unreadable catalogue only narrows the filter
        return []


def _principals(reader: Reader) -> str:
    from .autopilot_api import _principals as found
    return found(reader)


def _write_json(path: Path, doc: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
