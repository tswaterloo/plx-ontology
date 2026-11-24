"""The workspace backend's screens, briefing, record pages and feedback."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from operating.file_modes import keep_shared

from . import errors, models as m
from .errors import RecordNotFound, source_refused
from .autopilot_api import AutopilotMixin
from .autopilot_area import AutopilotAreaMixin
from types import SimpleNamespace

from .briefs import BriefsMixin
from .team_view import TeamViewMixin
from .auth import Reader
from .test_records import hidden_words
from .serving import (
    _iso_now, access_label, HOST_READ_SCHEMES, LATE_LIST_LIMIT, object_url, READ_TIMEOUT_SECONDS,
    SAME_NAME_ROWS, WARM_LATE_PAGES
)

# the slow sections of a late purchase order's page read ahead after its trail, in page order
LATE_SECTIONS_AHEAD = ("late", "people", "landed_cost")
WARM_OPEN_PAGES = 12   # open orders beyond the late ones whose slow sections are read ahead
from .answers import (LATE_RULE, aged_to_today, attention_items, late_rule_behind, normalize,
                      without_tests)
from .suggest import projection_suggest, sublabel_tell
from .briefing_view import alert_playbook_of
from .page_parts import source_payload, source_records, source_view

from .backend_common import HOME_EXAMPLES, STYLE_ANSWER_MARGIN


SIDEBAR_SUPPLIERS = 8   # the sidebar names the suppliers with the most late POs, then "N more"


class _Unread(errors.UserFacing):
    """A list read that could not be read (``DocList.error``): answered as is, never kept
    as a last good page (a ``UserFacing`` refusal ``_serving`` passes through)."""

    def __init__(self, page: m.DocList) -> None:
        super().__init__("list_unread", getattr(page, "error", None) or "", 200)
        self.page = page


def weekly_key(week: Optional[date], today: Optional[date] = None) -> str:
    """The Monday of the week a weekly review is of, as its kept copy is keyed: the page asks
    for last week's Monday by date and the warm-up for "last week" (no date), one review. A day
    inside a week is that week's. (Keyed apart they were never one copy: every visit read the
    review cold, 46 s p50 over 6 h.)"""
    from datetime import timedelta
    from operating import briefs as br
    today = today or date.today()
    return (br.week_of(week) if week else br.week_of(today) - timedelta(days=7)).isoformat()


class ScreensMixin:
    """Screens, briefing, record pages and feedback."""

    def my_work(self, reader: Reader) -> m.MyWork:
        return self._serving(reader, ("my_work",), lambda: AutopilotMixin.my_work(
            self, reader), self._screen_inputs(files=(self._grants_file,), autopilot=True))

    def autopilot_area(self, reader: Reader) -> m.AutopilotArea:
        return self._serving(reader, ("autopilot",), lambda: AutopilotAreaMixin.autopilot_area(
            self, reader), self._screen_inputs(files=(self._grants_file,), autopilot=True))

    def autopilot_prepared_list(self, reader: Reader) -> m.AutopilotDrafts:
        """Home's prepared-for-you list, kept per reader like the autopilot area (its
        queue's and write state's files and settings are its inputs: a decision, a hand-off
        or a "never" is read now) and read ahead (``SCREENS_AHEAD``): read cold it held
        Home's first load 32 s after a restart (11-22, warm 2.5 s)."""
        return self._serving(reader, ("autopilot_drafts",), lambda: AutopilotAreaMixin.
                             autopilot_prepared_list(self, reader),
                             self._screen_inputs(files=(self._grants_file,), autopilot=True))

    def workload(self, reader: Reader) -> m.Workload:
        return self._serving(reader, ("workload",), lambda: BriefsMixin.workload(
            self, reader), self._screen_inputs(files=(self._grants_file,)))

    def approvals_queue(self, reader: Reader) -> m.ApprovalsQueue:
        return self._serving(reader, ("approvals",), lambda: BriefsMixin.approvals_queue(
            self, reader), self._screen_inputs(), reading=lambda: m.ApprovalsQueue(
                as_of=_iso_now(), total=0, sent=0, writes=0, reading=True))

    def reply_triage(self, reader: Reader) -> m.ReplyTriage:
        return self._serving(reader, ("replies",), lambda: BriefsMixin.reply_triage(
            self, reader), self._screen_inputs(), reading=lambda: m.ReplyTriage(
                as_of=_iso_now(), total=0, sent=0, writes=0, reading=True))

    def team_view(self, reader: Reader) -> m.TeamView:
        # who it names is part of the access state (``_screen_access``), the activity
        # snapshot and the gap inbox's open rows its inputs
        return self._serving(reader, ("team",), lambda: TeamViewMixin.team_view(
            self, reader), self._screen_inputs(files=(self._team_file,)))

    def team_person(self, reader: Reader, key: str) -> m.TeamPersonDetail:
        """A person's drill-down, kept per reader and person as the Team list is (it was read
        again on every click: 0.5-1.4 s alone, 20-35 s when thirty were asked at once, each
        one a whole-inbox read on the old engine). The Team screen's access state decides
        it (``_screen_access``: the name ``team``), the activity snapshot and the gap
        inbox's open rows are its inputs. A key the reader may not open is a 404, never kept."""
        def read() -> m.TeamPersonDetail:
            got = TeamViewMixin.team_person(self, reader, key)
            if got.items_unread:
                raise _Unread(got)     # a panel without its open items is answered, never kept
            return got
        try:
            return self._serving(reader, ("team", "person", key), read,
                                 self._screen_inputs(files=(self._team_file,)))
        except _Unread as unread:
            return unread.page

    def answer_bank(self, reader: Reader, question: Optional[str] = None) -> m.DocList:
        """The Templates page's answer bank (``BriefsMixin.answer_bank``: a whole-company
        document read) served as the other screens are: per reader and question, kept and
        re-checked, ``reading`` while a first read goes on. A list that could not be read
        is answered as before and never kept; one whose documents are still being read
        for the company is answered ``reading`` (the app asks again), never kept.

        While that company read goes on (the read this same reader and question last got
        "still reading" from, ``Memo.running``) and no page is kept for them, the app's poll
        is answered ``reading`` from that status alone: no read job, nothing of the
        reader's data. The first poll after it ends reads (and gates) as before."""
        from operating.business_documents import MEMO
        from .record_documents import STILL_READING
        asked = (question or "").strip()[:700] or None
        key = (reader.key, reader.mode, ("answer_bank", asked))
        polls = self.__dict__.setdefault("_answer_bank_reading", {})
        with self._lock:
            token = polls.get(key)
        if token is not None and self._held_good(key) is None and MEMO.running(token):
            return m.DocList(view="answer_bank", title="Answers any customer may get",
                             complete=False, reading=True)
        if token is not None:
            with self._lock:
                polls.pop(key, None)

        def read() -> m.DocList:
            got = BriefsMixin.answer_bank(self, reader, asked)
            if got.error == STILL_READING:
                if got._reading_token:
                    with self._lock:
                        while len(polls) >= 256:
                            polls.pop(next(iter(polls)))
                        polls[key] = got._reading_token
                return got.model_copy(update={"reading": True})
            if got.error is not None:
                raise _Unread(got)
            return got

        try:
            return self._serving(reader, ("answer_bank", asked), read, self._screen_inputs(),
                                 reading=lambda: m.DocList(
                                     view="answer_bank", title="Answers any customer may get",
                                     complete=False, reading=True))
        except _Unread as unread:
            return unread.page

    def upcoming_meetings(self, reader: Reader) -> m.UpcomingMeetings:
        from .briefs import _follow_up_stamp
        return self._serving(reader, ("meetings",), lambda: BriefsMixin.upcoming_meetings(
            self, reader), self._screen_inputs(extra=lambda: _follow_up_stamp(self.state_dir)),
            reading=lambda: m.UpcomingMeetings(meetings=[], reading=True))

    def my_action_items(self, reader: Reader, person: Optional[str]) -> m.MyActionItems:
        from .briefs import _follow_up_stamp
        return self._serving(reader, ("action_items", person),
                             lambda: BriefsMixin.my_action_items(self, reader, person),
                             self._screen_inputs(
                                 extra=lambda: _follow_up_stamp(self.state_dir)))

    def weekly_review(self, reader: Reader, week: Optional[date], *,
                      ahead: bool = False) -> m.Brief:
        from .briefs import WEEKLY_FILE
        return self._serving(reader, ("weekly", weekly_key(week)),
                             lambda: BriefsMixin.weekly_review(self, reader, week, ahead=ahead),
                             self._screen_inputs(files=(
                                 self.data_dir / WEEKLY_FILE if self.data_dir else None,)),
                             reading=lambda: BriefsMixin.weekly_reading(self, reader))

    def home(self, reader: Reader) -> m.Home:
        from . import page_sql
        # read ahead for the reader on every Home visit, also when a kept page answers it
        # (a snapshot runs no read, so the call inside ``_home`` is not made then), and on
        # the door's and the SQL alerts' Home too (their other screens and Ask as well)
        self.warm(reader)
        from . import simple_ask
        simple_ask.ready(self, reader)   # the person's next question starts on a ready session
        if page_sql.alerts_on():
            return self._home_alerts(reader)
        if page_sql.on("purchase_order"):
            return self._home_door(reader)
        return self._home_served(reader)

    def _home_door(self, reader: Reader) -> m.Home:
        """Home's late purchase orders through the read door (``page_sql``, switch
        ``PLX_PAGES_DOOR``): the count and the longest late of them, read as the reader
        in one transaction. At-risk items wait for the alerts table (none listed here).
        A failed read says unavailable, never "no late POs"."""
        from . import page_sql
        with self._lock:
            recent = list(self._recent.get(reader.key, []))
        try:
            total, late, as_of = page_sql.late_pos(self, reader, limit=LATE_LIST_LIMIT)
            attention = m.NeedsAttention(status="read", late_pos=late, late_total=total,
                                         as_of=as_of)
        except page_sql.PageDoorUnavailable:
            attention = m.NeedsAttention(status="unavailable")
        return m.Home(greeting_name=reader.display_name.split(" ")[0],
                      examples=list(HOME_EXAMPLES), needs_attention=attention,
                      recent_questions=recent, freshness=self.freshness())

    def _home_alerts(self, reader: Reader) -> m.Home:
        """Home from the SQL alerts (``page_sql``, switch ``PLX_ALERTS_SQL``): the po_late
        alerts as the late POs (count and the longest late), the other on-by-default
        My work types as at risk, all read as the reader through the door. A failed read
        says unavailable, never "nothing needs attention".
        Kept per reader as a screen is (``_serving``, as the sidebar: served at once after
        a restart while the reader's access and the screen's inputs are the same, read again
        behind it): the door's Home read took 2.3 s on every visit, Home's and Ask's."""
        from . import page_sql
        from .errors import UserFacing
        with self._lock:
            recent = list(self._recent.get(reader.key, []))
        try:
            got = self._serving(reader, ("home", "alerts"),
                                lambda: self._home_alerts_read(reader),
                                self._screen_inputs(alerts=True),
                                reading=lambda: self._home_reading(reader))
        except (page_sql.PageDoorUnavailable, UserFacing, TimeoutError):
            # unreadable, or the late POs' check not current (``alerts_stale``), and nothing
            # kept: never "none late" from old or missing numbers
            got = m.Home(greeting_name=reader.display_name.split(" ")[0],
                         examples=list(HOME_EXAMPLES),
                         needs_attention=m.NeedsAttention(status="unavailable"),
                         recent_questions=recent, freshness=self.freshness())
        # a kept page that is being served stale keeps the data stamp it was read with:
        # never old numbers under a fresh "as of"
        return got.model_copy(update={"recent_questions": recent,
                                      "freshness": got.freshness if got.stale_since
                                      else self.freshness()})

    def _home_alerts_read(self, reader: Reader) -> m.Home:
        """``_home_alerts``' read through the door (raises when unreadable: never kept)."""
        from . import page_sql
        total, late, as_of, risk, stale, risk_total, stale_total = page_sql.alert_home(
            self, reader, limit=LATE_LIST_LIMIT)
        # a home type whose check is not current: left out, said (refreshing, since)
        attention = m.NeedsAttention(status="read", late_pos=late, at_risk=risk,
                                     late_total=total, stale_total=stale_total, as_of=as_of, refreshing=bool(stale),
                                     at_risk_total=risk_total, checked_on=page_sql._oldest(stale))
        return m.Home(greeting_name=reader.display_name.split(" ")[0],
                      examples=list(HOME_EXAMPLES), needs_attention=attention,
                      recent_questions=[], freshness=self.freshness())

    def _home_served(self, reader: Reader) -> m.Home:
        """Home through ``_serving`` with no read ahead started (the warm-up's own read)."""
        from . import page_sql
        if page_sql.alerts_on():
            return self._home_alerts(reader)   # the screen ``home`` serves under the alerts
        return self._serving(reader, ("home",), lambda: self._home(reader),
                             reading=lambda: self._home_reading(reader))

    def _home_reading(self, reader: Reader) -> m.Home:
        """Home while its first read goes on: the reader's own name, the examples and
        the questions they asked; no list, no count and no source (``reading``)."""
        with self._lock:
            recent = list(self._recent.get(reader.key, []))
        return m.Home(greeting_name=reader.display_name.split(" ")[0],
                      examples=list(HOME_EXAMPLES),
                      needs_attention=m.NeedsAttention(status="unavailable"),
                      recent_questions=recent, freshness=m.Freshness(), reading=True)

    def _home(self, reader: Reader) -> m.Home:
        inbox, labels, suppliers = without_tests(*self.reads.run(
            reader, self._attention_cached, timeout=READ_TIMEOUT_SECONDS),
            named=self.test_named())
        inbox = aged_to_today(inbox, self.today() if self.today else None)
        late, risk = attention_items(inbox, labels, suppliers=suppliers)
        status = inbox.get("status")
        with self._lock:
            recent = list(self._recent.get(reader.key, []))
        # Home's late line rests on the late-PO rule alone: re-running when that rule's
        # findings are of older data or code, as of that rule's own last run (another
        # rule re-running, or one no longer run, never holds it: 11-17 Home said
        # "Rechecking" for days, dated by a removed rule's last run)
        late_behind, _unknown = late_rule_behind(inbox)
        ran = (inbox.get("detector_run_at") or {}).get(LATE_RULE) \
            if isinstance(inbox.get("detector_run_at"), Mapping) else None
        checked = ran if isinstance(ran, str) else inbox.get("checked_on")
        return m.Home(
            greeting_name=reader.display_name.split(" ")[0],
            examples=list(HOME_EXAMPLES),
            needs_attention=m.NeedsAttention(
                status="read" if status == "read" else "not_configured"
                if status in ("not_configured", "not_scheduled") else "unavailable",
                late_pos=late, at_risk=risk, hidden=None,   # the inbox counts no hidden gap
                late_total=max(len(late), int(inbox.get("late_total") or 0))
                if status == "read" else None,
                test_records_hidden=hidden_words(int(inbox.get("tests_hidden") or 0)),
                as_of=inbox.get("run_at") if isinstance(inbox.get("run_at"), str) else None,
                refreshing=late_behind,
                checked_on=checked if late_behind and isinstance(checked, str) else None),
            recent_questions=recent, freshness=self.freshness(inbox))

    def data_health(self, reader: Reader, check: Optional[str] = None) -> m.DataHealth:
        """Admin, Data health (``operator_api.data_health``). No ``check``: every check's
        open count, read with no per-finding decision (the page's first read, well under a
        second). ``check``: that check's findings this reader may see and the count hidden
        from them (the trimmed read, kept per reader and check as Home and the late list are)."""
        from . import data_health

        def read() -> m.DataHealth:
            job = data_health.summary if check is None else (lambda v: data_health.page(v, check))
            return self.reads.run(reader, job, timeout=READ_TIMEOUT_SECONDS)
        # kept per reader (``_serving``): the last good page is served at once, marked stale,
        # while the business views refresh holds the read up (live 11-21: 60 s then 503)
        return self._serving(reader, ("data_health", check or "counts"), read,
                             self._screen_inputs(alerts=True),
                             reading=lambda: m.DataHealth(status="unread"))

    def data_health_all(self, reader: Reader) -> m.DataHealth:
        """Every on check's findings (the spreadsheet): one trimmed read per check."""
        from . import data_health
        first = self.data_health(reader)
        if first.status != "read":
            return first
        checks = []
        for c in first.checks:
            if not c.on:
                checks.append(c)
                continue
            got = self.data_health(reader, c.type)
            if got.status != "read":
                return got
            checks.extend(x for x in got.checks if x.type == c.type)
        return first.model_copy(update={"checks": checks})

    def late_list(self, reader: Reader, *, offset: int = 0, limit: int = 25,
                  stale: bool = False) -> m.LateList:
        from . import page_sql
        if page_sql.alerts_on() or page_sql.on("purchase_order"):
            # the read door (``page_sql``): count and page in one transaction (the po_late
            # alerts under PLX_ALERTS_SQL); a failed read is an error, never an empty list
            pos = page_sql.alert_late_pos if page_sql.alerts_on() else page_sql.late_pos

            def read_door() -> m.LateList:
                try:
                    total, items, as_of = (pos(self, reader, offset=offset, limit=limit, stale=True)
                                           if stale else
                                           pos(self, reader, offset=offset, limit=limit))
                    # the other group's size, said on every page, never hidden
                    other = pos(self, reader, offset=0, limit=1, stale=not stale)[0]
                except page_sql.PageDoorUnavailable:
                    from .briefs import alerts_not_read
                    raise alerts_not_read() from None
                after = offset + len(items)
                return m.LateList(total=total, offset=offset, items=items, more=after < total,
                                  next_offset=after, as_of=self.freshness().as_of or as_of,
                                  stale_total=None if stale else other)
            # kept per reader as Home and the sidebar are (``_serving``): served at once after
            # a restart or a visit while the reader's access and the alert rules are the same,
            # read again behind it (the door's read took 1.8-4.6 s on every visit)
            return self._serving(reader, ("late", offset, limit) + (("stale",) if stale else ()), read_door,
                                 self._screen_inputs(alerts=page_sql.alerts_on()),
                                 reading=lambda: m.LateList(total=0, offset=offset,
                                                            next_offset=offset, reading=True))
        if stale:   # the old engine has no 'no sign of life' read: none, said as an empty group
            return m.LateList(total=0, offset=offset, next_offset=offset)
        return self._serving(reader, ("late", offset, limit), lambda: self._late_list(
            reader, offset=offset, limit=limit), reading=lambda: m.LateList(
                total=0, offset=offset, next_offset=offset, reading=True))

    def sidebar(self, reader: Reader) -> m.Sidebar:
        """The sidebar's numbers on every page (``m.Sidebar``): the late POs' count and the
        suppliers with the most of them, kept per reader as a screen is (``_serving``:
        a kept page served at once after a restart while the reader's access is the same),
        never Home's lists. With the read door (``page_sql``) one grouped read; with the
        old engine they come from Home's own kept page (only its longest late: ``partial``).
        Not read: no count (null), never 0. Reads ahead for the reader as Home does (the
        sidebar is on every page; it asked Home for these before)."""
        from . import simple_ask
        self.warm(reader)
        simple_ask.ready(self, reader)
        return self._sidebar_served(reader)

    def _sidebar_served(self, reader: Reader) -> m.Sidebar:
        """``sidebar`` with no read ahead started (the warm-up's own read)."""
        from . import page_sql
        with self._lock:
            recent = list(self._recent.get(reader.key, []))
        if not (page_sql.alerts_on() or page_sql.on("purchase_order")):
            home = self._home_served(reader)
            na = home.needs_attention
            if na.status != "read" or home.reading:
                return m.Sidebar(recent_questions=recent, freshness=home.freshness,
                                 reading=home.reading, stale_since=home.stale_since)
            counts: dict[str, int] = {}
            for po in na.late_pos:
                if po.supplier:
                    counts[po.supplier] = counts.get(po.supplier, 0) + 1
            ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            total = na.late_total if na.late_total is not None else len(na.late_pos)
            return m.Sidebar(
                late_total=total, late_suppliers=[
                    m.SupplierLate(supplier=k, late=v) for k, v in ranked[:SIDEBAR_SUPPLIERS]],
                supplier_total=len(ranked), partial=total > len(na.late_pos),
                recent_questions=recent, freshness=home.freshness,
                stale_since=home.stale_since)
        try:
            got = self._serving(reader, ("sidebar",), lambda: self._sidebar_door(reader),
                                self._screen_inputs(alerts=True),
                                reading=lambda: m.Sidebar(reading=True))
        except (page_sql.PageDoorUnavailable, errors.UserFacing, TimeoutError):
            # not read now and nothing kept: no count, never "none late"
            got = m.Sidebar()
        # a kept page that is being served stale keeps the data stamp it was read with:
        # never old numbers under a fresh "as of"
        return got.model_copy(update={"recent_questions": recent,
                                      "freshness": got.freshness if got.stale_since
                                      else self.freshness()})

    def _late_trails_ahead(self, reader: Reader) -> int:
        """Read ahead (the warm-up) the evidence trail of the longest late purchase orders
        Home lists first through the read door (``WARM_LATE_PAGES``): Home's detail pane
        opens the first of them and reads its trail from the old engine (15-50 s cold);
        kept per reader, it opens at once after a restart. Without the door the late
        orders' whole pages are read ahead instead (``warm``). How many were read."""
        from . import page_sql
        if not (page_sql.alerts_on() or page_sql.on("purchase_order")):
            return 0
        pos = page_sql.alert_late_pos if page_sql.alerts_on() else page_sql.late_pos
        _total, late, _as_of = pos(self, reader, offset=0, limit=WARM_LATE_PAGES)
        # then the other open orders (late first, then newest): the sections only, so the
        # first open of any recent order is kept too
        try:
            more = [p for p in page_sql.open_po_ids(self, reader, WARM_LATE_PAGES + WARM_OPEN_PAGES)
                    if p not in {po.object_id for po in late}][:WARM_OPEN_PAGES]
        except Exception:  # noqa: BLE001 - the late ones are read; these when opened
            more = []
        read = 0
        for po in [*late, *(SimpleNamespace(object_id=o, open_url="", more=True) for o in more)]:
            otype = po.object_id.split(":", 1)[0]
            if otype != "PurchaseOrder":
                continue   # not a record the old engine has a trail for (a bare handle)
            try:
                if not getattr(po, "more", False):   # the lateness trail: late orders only
                    self.object_section(reader, otype, po.object_id, "trail")
                read += 1
            except Exception:  # noqa: BLE001 - read when opened
                continue
            # the page's slow sections too, in the page's order (each plan once: a later
            # section rests on the plans before it): a late order's first open took 9.6 s, 8.4 s
            # and 5.3 s for late, people and landed cost cold (11-23 walk), under 150 ms kept
            for section in LATE_SECTIONS_AHEAD:
                try:
                    self.object_section(reader, otype, po.object_id, section)
                except Exception:  # noqa: BLE001 - read when opened
                    break
            # (the order's page itself is one door read on open: ``served.biz_page``)
        return read

    def _sidebar_door(self, reader: Reader) -> m.Sidebar:
        """The sidebar's late POs through the read door (one transaction)."""
        from . import page_sql
        total, top, suppliers = page_sql.late_suppliers(self, reader, limit=SIDEBAR_SUPPLIERS)
        return m.Sidebar(late_total=total, supplier_total=suppliers, late_suppliers=[
            m.SupplierLate(supplier=name, late=n) for name, n in top])

    def _late_list(self, reader: Reader, *, offset: int = 0, limit: int = 25) -> m.LateList:
        """Every late purchase order the reader may see, a page at a time, in Home's order
        (longest late first): Home and the late-list answer show the first
        ``LATE_LIST_LIMIT``; "show all" pages through the rest."""
        def job(view: Any) -> tuple[list[m.LatePo], int, Optional[str]]:
            held = self._late_held(view)
            inbox, ordered, labels, suppliers = held
            if not isinstance(inbox, Mapping) or inbox.get("status") != "read":
                # no late-order findings read for this reader: never "No late POs" (the UX
                # walk of 11-13 showed that while the alert run had not run on the server)
                from .briefs import alerts_not_read
                raise alerts_not_read()
            window = ordered[offset:offset + limit]
            with view._request():
                handles = sorted({h for g in window for h in g.get("citations") or ()
                                  if isinstance(h, str)})
                decided = view.access.decide(handles) if handles else {}
            shown = [g for g in window
                     if all(decided.get(h) is None for h in g.get("citations") or ()
                            if isinstance(h, str))]
            items, _ = attention_items({"gaps": shown}, labels, limit=limit,
                                       suppliers=suppliers)
            run_at = inbox.get("run_at") if isinstance(inbox.get("run_at"), str) else None
            consumed = offset + len(window)
            return items, len(ordered), run_at, consumed, consumed < len(ordered)

        items, total, run_at, after, more = self.reads.run(reader, job,
                                                           timeout=READ_TIMEOUT_SECONDS)
        # the data cut-off every screen states (``freshness``), not the inbox run's time
        return m.LateList(total=total, offset=offset, items=items, more=more,
                          next_offset=after,
                          as_of=self.freshness().as_of or run_at)

    # ---- briefing ------------------------------------------------------------------------

    def briefing(self, reader: Reader, *, since_visit: bool = False) -> m.Briefing:
        """Your briefing (``ObjectWorkspace.briefing``): the events routed to this reader
        that its grant shows, ranked; the top ``bf.TOP_ITEMS`` one by one, each with its
        record's label, its sources (decided for the reader), a suggested next action and,
        for the first ``BRIEFING_TRAILS``, how we know (the PO lateness trail or the why
        trail) read as the reader, all trails within ``BRIEFING_TRAIL_SECONDS`` (each trail
        gets what is left and stops itself at it); every other item counted in its type's
        group; each check's state; the daily digest text as a preview (never sent)."""
        from . import page_sql
        if page_sql.alerts_on():
            from operating import briefing as bf
            # kept per reader as every screen is: the door's briefing read took 4-5 s on
            # every visit; the visit mark (``/api/briefing/seen``) makes the kept one stale
            return self._serving(reader, ("briefing", since_visit),
                                 lambda: page_sql.alert_briefing(
                                     self, reader, since_visit=since_visit, top=bf.TOP_ITEMS),
                                 self._screen_inputs(alerts=True),
                                 reading=lambda: m.Briefing(
                                     status="unavailable", count=0, new_since_visit=0,
                                     since_visit=since_visit, other_teams=0, rank_basis="",
                                     routing_basis="", complete=False, digest_preview="",
                                     reading=True))
        from .page_core import ENRICH_PRIORITY
        # the old engine's briefing is a long read (90-110 s cold, 11-18): it runs on all
        # but one read worker, after the short page reads queued (``serving._LongSlots``)
        return self._serving(reader, ("briefing", since_visit), lambda: self.reads.run(
            reader, lambda view: self._briefing_cached(
                view, reader, since_visit=since_visit), timeout=READ_TIMEOUT_SECONDS,
            priority=ENRICH_PRIORITY, long=True),
            reading=lambda: m.Briefing(
                status="unavailable", count=0, new_since_visit=0, since_visit=since_visit,
                other_teams=0, rank_basis="", routing_basis="", complete=False,
                digest_preview="", reading=True))

    def _briefing_pages_ahead(self, reader: Reader) -> int:
        """Read ahead (the warm-up) the first page of each group Home lists past its top
        items (``/api/briefing?pages=true``: ``home_group_pages``), kept per reader: Home's
        one request then finds them and is not held by their cold reads. How many were read."""
        from .briefing_view import HOME_GROUP_PAGE_ROWS, home_group_pages
        got = self.briefing(reader, since_visit=False)
        if getattr(got, "status", None) != "read":
            return 0
        read = 0
        for kind in home_group_pages(got):
            try:
                self.briefing_items(reader, kind, offset=0, limit=HOME_GROUP_PAGE_ROWS,
                                    since_visit=False)
                read += 1
            except Exception:  # noqa: BLE001 - read when Home opens it
                continue
        return read

    def _home_pages_ahead(self, reader: Reader, later: bool = False) -> int:
        """Read ahead (the warm-up) the quick page of each record Home opens first
        (``home_first_records``: the first item of each kind it lists), kept per reader on
        disk where the API's record request finds it: Home's detail pane opened its first
        row (a batch on quality hold) cold after every delta cycle, 1.5 s "reading" then a
        second request (11-22 walk: Home 4.4 s, 6.5-14 s at 11:20 AM). Not for a directory
        employee with no session (``later``). How many were read."""
        from .backend import object_page
        from .briefing_view import home_first_records
        if later:
            return 0
        got = self.briefing(reader, since_visit=False)
        if getattr(got, "status", None) != "read":
            return 0
        read = 0
        for otype, oid in home_first_records(got):
            try:
                self.reads.run(reader, lambda view, t=otype, o=oid: object_page(
                    self, view, t, o, quick=True), timeout=READ_TIMEOUT_SECONDS)
                read += 1
            except Exception:  # noqa: BLE001 - read when Home opens it
                continue
        return read

    def briefing_items(self, reader: Reader, event_type: str, *, offset: int = 0,
                       limit: int = 25, since_visit: bool = False) -> m.BriefingItems:
        """One event type's items of this reader's briefing, a page at a time (a group's
        "+N more": its items past the top ones), each with its record's label; no trail
        (the item's page has it)."""
        from . import page_sql
        if page_sql.alerts_on():
            return self._serving(reader, ("briefing_items", event_type, offset, limit),
                                 lambda: page_sql.alert_briefing_items(
                                     self, reader, event_type, offset=offset, limit=limit),
                                 self._screen_inputs(alerts=True))
        return self.reads.run(reader, lambda view: self._briefing_items_cached(
            view, event_type, offset=offset, limit=limit, since_visit=since_visit),
            timeout=READ_TIMEOUT_SECONDS)

    def briefing_unjudged(self, reader: Reader, check: str, reason: Optional[str], *,
                          offset: int = 0, limit: int = 50) -> m.BriefingUnjudged:
        """The records one check could not judge that this reader may read (the count its
        briefing's data notes give), each with its label as this reader reads it."""
        return self.reads.run(reader, lambda view: self._briefing_unjudged(
            view, check, reason, offset=offset, limit=limit), timeout=READ_TIMEOUT_SECONDS)

    def suggestions(self, reader: Reader) -> m.Suggestions:
        """What needs this reader's attention and what to do about it
        (``operating.proactive_suggestions``): its briefing's findings merged per record,
        ranked by the briefing's impact order, each with "ETA confirmed?", the chain of
        reasoning with its sources, a suggested next step and a draft message worded from
        cited values only (never sent), confidence and what is missing. Purchase order
        trails are read as the reader within ``SUGGESTION_TRAIL_SECONDS``; a card whose
        trail did not fit says so (low confidence), never guesses."""
        from operating import proactive_suggestions as ps

        from . import page_sql
        from .page_core import ENRICH_PRIORITY

        def read() -> m.Suggestions:
            # PLX_ALERTS_SQL: made from the SQL alerts Home lists, read first (their own
            # door, one transaction), never from the old engine's inbox
            alerts = page_sql.alert_suggestion_items(self, reader, ps.KIND_OF_TYPE) \
                if page_sql.alerts_on() else None
            return self.reads.run(
                reader, lambda view: self._suggestions_cached(view, alerts),
                timeout=READ_TIMEOUT_SECONDS, priority=ENRICH_PRIORITY,
                long=True)

        return self._serving(reader, ("suggestions",), read, reading=lambda: m.Suggestions(
            status="unavailable", total=0, note="", reading=True))

    def alert_playbook_dict(self, reader: Reader, alert_id: str) -> Optional[dict[str, Any]]:
        """One alert's playbook (``operating.alert_playbooks``) built from the alert as this
        reader's inbox shows it, every value read by this reader's own tool calls."""
        env = self._actions_env()
        return self.reads.run(reader, lambda view: alert_playbook_of(view, alert_id, env),
                              timeout=READ_TIMEOUT_SECONDS)

    def finding_card(self, reader: Reader, ref: str) -> Optional[dict[str, Any]]:
        """A data-health finding as a card for the action desk (assign only), built from
        the finding as this reader's trimmed read shows it: ``ref`` is ``<check>:<id>``; the
        check only narrows the read, the finding must be in it. None: not theirs to see."""
        from . import data_health
        check, _, gap_id = ref.partition(":")
        known = {str(t["type"]): t for t in data_health.registered() if t.get("default") == "on"}
        if check not in known or not gap_id:
            return None

        def read(view: Any) -> Optional[dict[str, Any]]:
            found = view.gap_inbox(kinds=[check], limit=data_health.READ_LIMIT)
            if found.get("status") != "read":
                return None
            return next((g for g in found.get("gaps") or () if g.get("id") == gap_id), None)
        gap = self.reads.run(reader, read, timeout=READ_TIMEOUT_SECONDS)
        if gap is None:
            return None
        words = str(gap.get("summary") or known[check].get("label") or "Finding")
        return {"id": f"dh:{ref}", "headline": words[:300], "subject": {"label": words[:200]},
                "action": {"text": ""}, "draft": {"channel": None, "facts": []},
                "teams_draft": None}

    def briefing_seen(self, reader: Reader) -> m.BriefingSeen:
        try:
            at = self._seen_direct(reader)
        except (FileNotFoundError, ValueError):
            return m.BriefingSeen(ok=False)
        if at is not None:
            from . import page_sql
            page_sql.forget_scope(self, reader)   # "new since your visit" counts from this one
            self.warm_briefing(reader)   # the next visit's briefing is ready
            return m.BriefingSeen(ok=True, at=at)
        try:
            # a write a reader waits on: on the check worker (short jobs only: the door's
            # reads on the access workers held it 1 s on Home), else the access workers,
            # else the read workers, where it waited behind 2-minute reads
            worker = None
            for name in ("check_worker", "access_worker"):
                worker = getattr(self, name, None)
                if worker is not None and not worker.stopped.is_set():
                    break
                worker = None
            if worker is None:
                worker = self.reads
            at = worker.run(reader, lambda view: view.briefing_seen(),
                            timeout=READ_TIMEOUT_SECONDS, priority=-1)
        except (FileNotFoundError, ValueError):
            return m.BriefingSeen(ok=False)
        from . import page_sql
        page_sql.forget_scope(self, reader)   # "new since your visit" counts from this one
        self.warm_briefing(reader)   # the next visit's briefing is ready
        return m.BriefingSeen(ok=True, at=at)

    def _seen_direct(self, reader: Reader) -> Optional[str]:
        """The reader's "seen" mark written straight to the served store's inbox (one row
        under the reader's own key: ``ObjectWorkspace.briefing_seen`` writes the same row
        through a view), so it never waits for a view to be made for a reader's first visit
        (7-9 s on a loaded check worker, ahead of that reader's access checks); None when
        this API names no store (the view's path then)."""
        path = getattr(self, "store_path", None)
        if path is None:
            return None
        from operating import proactive_engine as pe
        from operating import proactive_engine_inbox
        return proactive_engine_inbox.record_visit(pe.inbox_path(Path(path)),
                                                   pe.reader_key(reader.session.actor))

    def watcher_item_state(self, reader: Reader, item_id: str,
                           request: m.WatcherItemStateRequest) -> m.WatcherItemState:
        """Dismiss or snooze one AI watcher item of the reader's briefing
        (``ObjectWorkspace.watcher_item_state``); an item they may not see is not found."""
        try:
            done = self.reads.run(reader, lambda view: view.watcher_item_state(
                item_id, request.state, until=request.until, reason=request.reason),
                timeout=READ_TIMEOUT_SECONDS)
        except (KeyError, FileNotFoundError):
            raise errors.UserFacing("item_not_found", "That item isn't in your briefing.",
                                    404) from None
        except ValueError:
            raise errors.UserFacing("snooze_invalid", "Snooze until a day after today, "
                                    "within a year.", 400) from None
        return m.WatcherItemState(**done)

    def same_name(self, reader: Reader, page: m.ObjectPage) -> list[m.ObjectRef]:
        """The other records of the page's type and name this reader may see, as its
        search shows them (``suggest``: its scope, copies of one record collapsed), so the
        page can say "N customers share this name". None listed unless the search also
        shows the page's own record (a copy of it is never called another customer), nor
        any copy of it (``also_in``)."""
        name = (page.label or "").strip()
        if not name:
            return []
        members: dict[str, list[str]] = {}
        try:
            found = self._pg_suggest(reader, name[:100], SAME_NAME_ROWS, members=members)
            if found is None:
                found = self.reads.run(reader, lambda view: projection_suggest(
                    view, name[:100], SAME_NAME_ROWS), timeout=READ_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - not established: the page says nothing of it
            return []
        own = {page.object_id, *(r.object_id for r in (page.customer.also_in
                                                         if page.customer else ()))}
        same = [s for s in found if s.type == page.type
                and normalize(s.label) == normalize(name)]
        if not any(own & set(members.get(s.object_id, [s.object_id])) for s in same):
            return []
        return [m.ObjectRef(object_id=s.object_id, type=s.type,
                            label=(f"{s.label} ({tell})" if (tell := sublabel_tell(s))
                                   else s.label)[:200],
                            open_url=s.open_url)
                for s in same if not own & set(members.get(s.object_id, [s.object_id]))]

    def object_section(self, reader: Reader, object_type: str, object_id: str,
                       section: str) -> m.ObjectSection:
        """One section of the record's page (``object_section``), read on the section
        pool (``_run_section``); labelled and journalled as the page is."""
        if section not in m.PAGE_SECTIONS:
            raise ValueError("unknown_section")
        out = self._serving(reader, ("section", object_type, object_id, section),
                            lambda: self._run_section(reader, object_type, object_id,
                                                      section))
        return self._labelled(reader, out, "/api/object-section")

    def _labelled(self, reader: Reader, payload: Any, path: str) -> Any:
        """``payload`` (a copy) with the full-read label when the reader's session holds the
        grant, journalled; else unchanged."""
        label = access_label(reader.session)
        if not label or getattr(reader.session, "warm_ahead", False):
            return payload
        self.journal_full_read(reader, path)
        return payload.model_copy(update={"access_label": label})

    def source(self, reader: Reader, handle: str, text_offset: int) -> m.SourceView:
        """``_source``; one opened under a full-read grant is journalled."""
        return self._labelled(reader, self._source_view(reader, handle, text_offset),
                              "/api/source")

    def mail_conversation(self, reader: Reader, handle: str) -> Optional[str]:
        """The conversation id of a cited mail this reader may open (``view.source``, the
        same gates as the source page); None when it is not a mail, not theirs, or the
        read names none. What a card reply's follow-through loop is matched on."""
        if not isinstance(handle, str) or not handle.startswith("graph-mail://"):
            return None
        from .work_log import conversation_in
        try:
            got = self.reads.run(reader, lambda view: view.source(handle, text_offset=0),
                                 timeout=READ_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - unknown: the loop falls back to the recipients
            return None
        return conversation_in(got)

    def source_records(self, reader: Reader, handle: str) -> m.SourceRecords:
        """The records citing a source for this reader (``page_parts.source_records``),
        read as the reader with the record pages' grant and gates; journalled under a
        full-read grant. A source refused to the reader is not found."""
        return self._labelled(reader, self.reads.run(
            reader, lambda view: source_records(view, handle),
            timeout=READ_TIMEOUT_SECONDS), "/api/source/records")

    def _source_view(self, reader: Reader, handle: str, text_offset: int) -> m.SourceView:
        """A cited source as this reader may open it. A captured case original or a
        SharePoint file (``HOST_READ_SCHEMES``) the app's own runtime cannot open for an
        employee is read through
        that employee's agent host (``AgentAsk.read_source``: the capture store's
        per-reader gate, the one the Ask's reads passed); refused there, it is not found."""
        try:
            return self.reads.run(reader, lambda view: source_view(view, handle, text_offset),
                                  timeout=READ_TIMEOUT_SECONDS)
        except (LookupError, ValueError) as error:
            # only a refusal goes on to the host: a bug's KeyError stays the server's
            if not (source_refused(error) and isinstance(handle, str)
                    and handle.startswith(HOST_READ_SCHEMES)
                    and self.agent_ask is not None
                    and self.agent_ask.serves(reader.session)):
                raise
        with self._original_reads(reader):
            # RecordNotFound: the host does not serve it to this reader (refused, unknown
            # or unreadable alike); any other failure of the host is the server's (a 500)
            got = self.agent_ask.read_source(reader.session, handle, text_offset)
        return source_payload(handle, got, text_offset)

    def form_sources(self, reader: Reader, fields: list[Any], *, message: Optional[str],
                     words: str, named: bool
                     ) -> tuple[list[Any], list[str], Optional[m.ObjectRef]]:
        """A form's facts from its thread (its earlier versions there field by field), the
        records its identifiers name and the reader's own earlier copies of it
        (``operating.doc_gather.form_facts``), read through this reader's own tools; a
        copy or version is used only when their own read of it resolves
        (``doc_fill_copies.opened``, ``doc_gather.thread_facts``). Stops in time to answer, labelled partial."""
        from operating import doc_fill as df
        from operating import doc_fill_copies as dc
        from operating import doc_gather
        from operating.typed_graph.runtime import GraphRepository
        terms = df.load_terms()
        index = {} if self.state_dir is None else dc.load(
            Path(self.state_dir) / "doc-fill" / "copies.json")
        rows = dc.earlier(index, reader.email or "", fields)
        deadline = time.monotonic() + max(5.0, READ_TIMEOUT_SECONDS - STYLE_ANSWER_MARGIN)

        class _ViewReads:
            def __init__(self, view: Any) -> None:
                self.view = view

            def call(self, name: str, arguments: dict[str, Any], _actor: Any) -> Any:
                return self.view._call(name, arguments)

        def read(view: Any) -> tuple[list[Any], list[str], Optional[m.ObjectRef]]:
            with view._request():
                copies: list[Any] = []
                blob = None
                unread: list[str] = []
                if self.state_dir is not None and (rows or message):
                    from integrations.capture_mail import MailCaptureStore
                    from integrations.capture_mail_scope import CaptureError
                    try:
                        blob = MailCaptureStore.open_read_only(self.state_dir).blob
                    except CaptureError:
                        unread = ["the mail store is not readable: no earlier copy or "
                                  "version of the form read"]
                if rows and blob is not None:
                    target = doc_gather.sender_party(view._call, message, reader.email or "")
                    copies = dc.copy_facts(dc.opened(view._call, rows, blob), terms, target)
                repo = GraphRepository(_ViewReads(view), None, max_pages=4)
                facts, notes, records = doc_gather.form_facts(
                    view._call, fields, terms, message=message, words=words, repo=repo,
                    copies=copies, deadline=deadline, find_records=not named, blob=blob,
                    own=reader.email or "")
                notes += unread
            ref = None
            if records:
                kind = records[0].split(":", 1)[0]
                ref = m.ObjectRef(object_id=records[0], type=kind, label=records[0][:200],
                                  open_url=object_url(kind, records[0]))
            return facts, notes, ref
        return self.reads.run(reader, read, timeout=READ_TIMEOUT_SECONDS)

    def style_store(self) -> Optional[Path]:
        """The learned templates (``operating.doc_style_mine``) under the state directory."""
        return None if self.state_dir is None else Path(self.state_dir) / "doc-style"

    def style_facts(self, reader: Reader, words: str
                    ) -> tuple[list[Any], list[str], Optional[m.ObjectRef]]:
        """Facts for a document about ``words`` gathered across what this reader may read
        (``operating.doc_gather.gather``) through their own tools."""
        from operating import doc_fill as df
        from operating import doc_gather
        from operating.typed_graph.runtime import GraphRepository

        class _ViewReads:
            def __init__(self, view: Any) -> None:
                self.view = view

            def call(self, name: str, arguments: dict[str, Any], _actor: Any) -> Any:
                return self.view._call(name, arguments)

        # the gather stops in time to answer within the read's own limit: a partial
        # result, labelled, rather than none
        deadline = time.monotonic() + max(5.0, READ_TIMEOUT_SECONDS - STYLE_ANSWER_MARGIN)

        def read(view: Any) -> tuple[list[Any], list[str], Optional[m.ObjectRef]]:
            with view._request():
                repo = GraphRepository(_ViewReads(view), None, max_pages=4)
                facts, notes = doc_gather.gather(view._call, words, df.load_terms(),
                                                 repo=repo, deadline=deadline)
            first = next((f.citation.record for f in facts
                          if not f.key.startswith(("Text.", "Quoted."))), None)
            ref = None
            if first:
                kind = first.split(":", 1)[0]
                ref = m.ObjectRef(object_id=first, type=kind, label=words[:200],
                                  open_url=object_url(kind, first))
            return facts, notes, ref
        return self.reads.run(reader, read, timeout=READ_TIMEOUT_SECONDS)

    # ---- feedback -------------------------------------------------------------------------

    def answer_context(self, reader: Reader, answer_id: str) -> dict[str, Any]:
        """What was kept of an answer given to this reader (its question, words, facts with
        their citations, subjects); another reader's answer reads as none."""
        with self._lock:
            asked = self._answers.get(answer_id)
        if asked is None or asked["reader"] != reader.key:
            raise RecordNotFound("answer_not_found")
        return dict(asked)

    def feedback_grant(self, reader: Reader, handles: Sequence[str]
                       ) -> tuple[frozenset[str], dict[str, Any]]:
        """(the markings this reader holds, each handle's withheld marker or None), from
        the read policy every answer is decided by (``access.grant`` and ``access.decide``);
        a reader whose access holds no grant holds no marking. Raises when undecidable."""
        def job(view: Any) -> tuple[frozenset[str], dict[str, Any]]:
            with view._request():
                grant = getattr(view.access, "grant", None)
                held = frozenset(grant()[1]) if callable(grant) else frozenset()
                decided = view.access.decide(list(handles)) if handles else {}
                return held, dict(decided)
        return self.reads.run(reader, job, timeout=READ_TIMEOUT_SECONDS)

    def served_version(self) -> Optional[str]:
        """The served data's version: a short digest of the served store file's identity."""
        if not self.store_path:
            return None
        from operating.ontology_host import _file_identity
        return hashlib.sha256(repr(_file_identity(self.store_path)).encode()
                              ).hexdigest()[:12]

    def feedback(self, reader: Reader, request: m.FeedbackRequest) -> m.FeedbackResponse:
        with self._lock:
            asked = self._answers.get(request.answer_id)
        if asked is None or asked["reader"] != reader.key:
            raise RecordNotFound("answer_not_found")   # another reader's answer reads as none
        entry = {"at": _iso_now(), "actor": reader.key, "answer_id": request.answer_id,
                 "rating": request.rating, "note": request.note,
                 "question": asked["question"]}
        stored: list[str] = []
        feedback_id = secrets.token_hex(8)
        if self.pg_dsn:
            try:
                import psycopg
                with psycopg.connect(self.pg_dsn, connect_timeout=2, autocommit=True) as conn:
                    row = conn.execute(
                        "INSERT INTO search.feedback (at, actor, question, answer_id, rating, "
                        "note) VALUES (now(), %s, %s, %s, %s, %s) RETURNING id",
                        (entry["actor"], entry["question"], entry["answer_id"],
                         entry["rating"], entry["note"])).fetchone()
                    feedback_id = f"pg-{row[0]}"
                    stored.append("search.feedback")
            except Exception:  # noqa: BLE001 - the log below still keeps it
                pass
        if self.data_dir is not None:
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.data_dir / "feedback.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            keep_shared(path, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({**entry, "feedback_id": feedback_id},
                                        ensure_ascii=False) + "\n")
            stored.append("feedback_log")
        if not stored:
            raise RuntimeError("feedback_not_stored")
        return m.FeedbackResponse(ok=True, feedback_id=feedback_id, stored_in=stored)
