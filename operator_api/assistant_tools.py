"""The Ask's assistant tools: what the person asks the assistant to keep, their templates
and the documents it makes for them. Each is bound to one reader (``agent_session.
LocalTool``) beside the write tools (``simple_ask.write_tools``):

* ``plx.remember``: the person's own memory (``operator_api.memory``): keep, correct,
  forget or list items, Autopilot rules included (``operating.learning.autopilot_rules``:
  no draft for a sender, domain or side; who their email drafts copy or never copy). Each
  save answers what was kept and what the Autopilot will now do, so the answer says
  exactly that. Every memory check applies (``Memory.write``: viewing as someone and the
  Ask canary keep nothing, the people exclusion fails closed, pay and finance are never
  kept); a forget or a correction reaches only the person's own items.
* ``plx.template``: the person's templates (``operator_api.templates``): list and read the
  ones they may use, save their own (from a description, or from a document they read: its
  ``source`` handle kept with it) and delete their own. A placeholder names a record field
  (``{{po_number}}``); an unknown one is refused with the fields there are.
* ``plx.read_document``: one document read whole from its original (a SharePoint file the
  reader's host opens, or the person's own file), paged and searchable
  (``operator_api.doc_read_tool``).
* ``plx.make_document``: a Word, Excel, PowerPoint, PDF or CSV file of what the model wrote, made as the
  instruction engine makes its documents (``InstructMixin._prepare_document``: cleaned of
  excluded people and secret-looking text) and kept for this reader only: its download
  link, and its ``file_id`` for an email attachment or a SharePoint upload prepared
  through ``plx.prepare_write`` (``operating.prepared_writes``), which the person confirms.

Nothing here sends or writes outside the person's own memory, templates and files.
Evidence: isolated-fixture (``operator_api/tests/test_assistant_tools.py``).
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence

from operating import learning


REMEMBER = "plx.remember"
TEMPLATE = "plx.template"
DOCUMENT = "plx.make_document"
# a prepared document edit keeps its edited copy for the reader (``prepared_area``)
EDITED = "plx.edit_document"
WATCH = "plx.watch_measure"
READ_DOCUMENT = "plx.read_document"
MY_WORK = "plx.my_work"
HAND_OFF = "plx.hand_off"
OPEN_WORK = "plx.my_open_work"   # ``open_work_tool``
NAMES = (REMEMBER, TEMPLATE, DOCUMENT, WATCH, READ_DOCUMENT, MY_WORK, HAND_OFF, OPEN_WORK)
# the kinds of file plx.make_document makes (``operating.instruct_files.KINDS``)
KINDS = ("docx", "xlsx", "pdf", "pptx", "csv")
MOST_LISTED = 40

REMEMBER_SPEC = {
    "name": REMEMBER,
    "description": (
        "The person's own memory, which shapes every answer and every draft the Autopilot "
        "prepares for them. action save: keep text as kind instruction | preference | "
        "commitment | fact, in the person's own words; replaces: the id of an item it "
        "corrects. An Autopilot rule needs the exact address or domain, so look it up "
        "(their mail) when they name a person or company: 'Stop drafting replies to "
        "<address or domain>', 'Don't draft anything for suppliers', 'Always cc <address> "
        "on supplier emails', 'Never cc <address>'. action forget: id. action list: their "
        "items with ids. A line starting always / never / from now on / remember may "
        "already be kept for them: list before saving it again. When their words could mean "
        "no drafts at all for someone or only something narrower (a topic, a time), ask "
        "which before saving. The result says what was kept and what the Autopilot will now "
        "do, or why nothing was kept: tell the person exactly that, never more."),
    "inputSchema": {
        "type": "object", "additionalProperties": False, "required": ["action"],
        "properties": {
            "action": {"type": "string", "enum": ["save", "forget", "list"]},
            "kind": {"type": "string",
                     "enum": ["instruction", "preference", "commitment", "fact"]},
            "text": {"type": "string"}, "id": {"type": "integer"},
            "replaces": {"type": "integer"}}},
    "annotations": {"readOnlyHint": False, "destructiveHint": False},
}

TEMPLATE_SPEC = {
    "name": TEMPLATE,
    "description": (
        "The person's document templates. action list: the templates they may use "
        "(id, name, type). action get: one template's body by id. action save: keep a new "
        "template (name, doc_type, body) from what they described, or from a document they "
        "named (read it first; source: its handle): write the body with {{field}} "
        "placeholders where a record's details go (po_number, supplier_name, "
        "customer_name, required_date, ...; an unknown field is refused with the list). "
        "Templates never hold amounts or prices. id with save: change one of theirs. "
        "action delete: id. To make a document from a template, get it, fill each "
        "placeholder from the records you read, then plx.make_document. doc_type: quote | "
        "order_confirmation | supplier_email | customer_email | po_note | form | phrasing "
        "| price_list | proposal | spec_sheet | coa | letter | other."),
    "inputSchema": {
        "type": "object", "additionalProperties": False, "required": ["action"],
        "properties": {
            "action": {"type": "string", "enum": ["list", "get", "save", "delete"]},
            "id": {"type": "integer"}, "name": {"type": "string"},
            "doc_type": {"type": "string"}, "body": {"type": "string"},
            "source": {"type": "string"}}},
    "annotations": {"readOnlyHint": False, "destructiveHint": False},
}

DOCUMENT_SPEC = {
    "name": DOCUMENT,
    "description": (
        "Make a file the person can download, laid out and formatted for them (headings, "
        "tables, numbers aligned, dates and amounts typed in Excel). content is Markdown, one "
        "paragraph per line: '# '/'## ' headings, '- ' bullets, **bold**, *italic*, tables as "
        "'| a | b |' rows with a '|---|---|' row under the header, '---' a divider, and for "
        "a marked-up version {++added++} / {--removed--} (Word tracked changes). kind docx "
        "(Word) or pdf (the same Markdown), xlsx (Excel: one row per line, cells split by "
        "|; write dates as 2026-12-14 and amounts as numbers; **bold** a total), pptx "
        "(PowerPoint: a slide per '# ' heading, its lines and one table below it; a "
        "'[chart N]' line under a heading with up to 3 lines puts the chart on that slide), "
        "csv (rows only, cells split by | as xlsx; no charts). A file for a customer or "
        "supplier holds only what is on record or asked for: no terms, dates, conditions or "
        "notes you made up, no internal ids, statuses or tool words: a figure no result of "
        "yours holds stays out of the file or goes in a plain note, and you say so in the "
        "answer; a figure from a result is written as its {{...}} reference, as in the answer. charts: up to 8 "
        "charts, each {\"sql\": <a SELECT you ran with plx.sql>, \"type\", \"title\", "
        "\"x\", \"y\": [...], options as in a chart block}: the statement is read again as "
        "the person and the chart drawn from its rows (a native chart in Word, Excel and "
        "PowerPoint); a line '[chart N]' in content places chart N, the rest go at the end. "
        "tables: up to 8 tables, each {\"sql\": <a SELECT you ran with plx.sql>, "
        "\"title\"}: name each column as the reader should read it (AS \"PO number\"), "
        "EVERY row of the statement is read again as the person and put in the "
        "file (up to 50,000 rows in xlsx/csv, 5,000 in docx/pdf, 30 in pptx), so a list "
        "of all orders, customers or lines is a table, never rows you type into content; "
        "a line '[table N]' in content places table N (the rest go at the end); a csv "
        "of one table needs only content '[table 1]'. The result says each table's row "
        "count: state that count. "
        "citations: statements behind prose figures not in a table or chart, each "
        "{\"label\" or \"title\", \"sql\", \"as_of\", \"count\", \"measure\"}; these go in "
        "a Sources section, slide or sheet. "
        "name: the file name. It answers file_id and download_url: to email it, call "
        "plx.prepare_write (system mail, operation send_email) with change.attachments: "
        "[file_id]; to save it to SharePoint, operation file_upload with change.file_id "
        "and change.name. Nothing is sent or saved anywhere by this tool."),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "required": ["name", "kind", "content"],
        "properties": {"name": {"type": "string"},
                       "kind": {"type": "string", "enum": ["docx", "xlsx", "pdf", "pptx", "csv"]},
                       "content": {"type": "string"},
                       "charts": {"type": "array", "maxItems": 8,
                                  "items": {"type": "object", "required": ["sql", "type", "y"]}},
                       "tables": {"type": "array",
                                  "items": {"type": "object", "required": ["sql"],
                                            "properties": {"sql": {"type": "string"},
                                                           "title": {"type": "string"}}}},
                       "citations": {
                           "type": "array",
                           "items": {
                               "type": "object",
                               "properties": {
                                   "label": {"type": "string"},
                                   "title": {"type": "string"},
                                   "sql": {"type": "string"},
                                   "as_of": {"type": "string"},
                                   "count": {"type": "integer"},
                                   "measure": {
                                       "type": "object",
                                       "properties": {
                                           "id": {"type": "string"},
                                           "name": {"type": "string"},
                                           "owner": {"type": "string"},
                                           "version": {"type": "string"},
                                           "definition": {"type": "string"},
                                           "period": {"type": "string"}}}}}}}},
    "annotations": {"readOnlyHint": False, "destructiveHint": False},
}


def _words(text: Any) -> str:
    return " ".join(str(text or "").split())


def autopilot_effect(text: Any) -> list[str]:
    """What the Autopilot does because of one memory item, in plain words (its stated
    rules, ``learning.autopilot_rules`` per sentence); [] when it states none."""
    return [r["text"] for r in learning.stated_rules(text)
            if str(r.get("key", "")).startswith(learning.RULE_PREFIXES)]


MY_WORK_SPEC = {
    "name": MY_WORK,
    "description": (
        "The person's own work log in the Workbench, newest first: the questions they asked "
        "here, alerts they saw, dismissed or snoozed, Autopilot drafts they accepted, edited "
        "or rejected, messages and changes prepared, confirmed or sent from the Workbench, "
        "records they followed or saved, notes. Mail they sent from Outlook and approvals "
        "they gave in another system are not in it (those are in the emails view and the "
        "systems' own records). since / until: ISO dates or times (the business time zone "
        "is America/Toronto); words: a record number or words its entries name; kind: "
        "ask | alert | autopilot | action | write | watch | note | save; sends: only "
        "messages that went out; waiting: only messages still waiting for a reply. Each "
        "entry: at, what (as the log says it), records it names."),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "since": {"type": "string", "maxLength": 40},
            "until": {"type": "string", "maxLength": 40},
            "words": {"type": "string", "maxLength": 200},
            "kind": {"type": "string",
                     "enum": ["ask", "alert", "autopilot", "action", "write", "watch",
                              "note", "save"]},
            "sends": {"type": "boolean"}, "waiting": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}
MY_WORK_MOST = 50

HAND_OFF_SPEC = {
    "name": HAND_OFF,
    "description": (
        "Gives the person's line to the Workbench's drafting desk, which then answers it in "
        "your place with a draft for them to review and approve: a reply to or forward of "
        "an email or message (sent in its thread), a meeting's follow-up, an Autopilot "
        "item's work, a record created from another record, a form they attached filled "
        "in, or a job that repeats on a schedule. Nothing you write after the call is "
        "shown. No arguments."),
    "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}


def handed_off(events: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the session gave the line to the drafting desk (a ``plx.hand_off`` call that
    did not fail; the catalog name or Claude Code's mcp__<server>__plx_hand_off)."""
    return any(e.get("type") == "tool_call" and not e.get("is_error")
               and str(e.get("name") or "").split("__")[-1].replace(".", "_")
               == HAND_OFF.replace(".", "_") for e in events)


# when a monitor runs (``insights_runs``: business time zone)
RUNS = {"hourly": "every hour", "daily": "every morning", "weekly": "Monday mornings"}
WATCH_SPEC = {
    "name": WATCH,
    "description": (
        "Watch a number for the person and tell them on their Home when it crosses a line "
        "or moves. It runs by itself as them, on their own access, at the schedule given "
        "(default daily), and the alert offers the why question so the reason is read when "
        "it fires. Either a measure of the catalogue (plx.sql's measures: its id or name): "
        "default, tell them when the latest days (default 7) are unusual, 2 spreads or more "
        "from the usual level of the 12 periods before; move_pct: when it moves by that "
        "percent or more against the same days just before; above / below: a number the "
        "measure's value crosses (the only way for a snapshot measure like open or late "
        "orders; read its value first to propose a line). Or one of their pinned charts "
        "(insight: its id, s<n>; a question asked on the page /insights#insight-s<n> is "
        "about that chart): call with insight alone first to see its columns and latest "
        "rows, then with column, op (above/below), value, row (latest: the last row along "
        "the chart's axis, the usual choice for a chart over time, earlier periods never "
        "fire; total: the column's sum; any: some row, never for a chart over time) and "
        "where (column and value: one currency when the rows hold more than one). That "
        "call keeps nothing: it returns the watch read back as a sentence with its number "
        "now and a token; tell them that sentence and, only once they say yes, set it with "
        "the same line, confirmed: true and that token. "
        "Use it when they ask to watch, monitor, track or be alerted on a number; never "
        "for a record (a customer, an order). The result says what was set up and where to "
        "see it, or why nothing was kept: tell them exactly that."),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "measure": {"type": "string"},
            "days": {"type": "integer", "minimum": 1, "maximum": 365},
            "move_pct": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000},
            "above": {"type": "number"},
            "below": {"type": "number"},
            "schedule": {"type": "string", "enum": ["hourly", "daily", "weekly"]},
            "insight": {"type": "string", "pattern": "^s[0-9]{1,18}$"},
            "column": {"type": "string", "maxLength": 63},
            "op": {"type": "string", "enum": ["above", "below"]},
            "value": {"type": "number"},
            "row": {"type": "string", "enum": ["latest", "total", "any"]},
            "where": {"type": "object", "additionalProperties": False,
                      "required": ["column", "value"],
                      "properties": {"column": {"type": "string", "maxLength": 63},
                                     "value": {"type": "string", "maxLength": 200}}},
            "confirmed": {"type": "boolean"},
            "token": {"type": "string", "maxLength": 200},
        },
    },
}
CHART_ONLY = ("insight", "column", "op", "value", "row", "where", "confirmed", "token")
MEASURE_ONLY = ("measure", "days", "move_pct", "above", "below")
SHOWN_ROWS = 6     # a pinned chart's last rows shown to choose a line from


WATCH_TOKEN_SECONDS = 1800    # a read-back's token is good this long
_PROCESS_KEY = __import__("secrets").token_bytes(32)


def _watch_mac(key: bytes, owner: str, sid: str, schedule: str, line: Mapping[str, Any],
               until: int) -> str:
    import hashlib
    import hmac
    import json
    said = json.dumps([owner, sid, schedule, dict(line), until], sort_keys=True,
                      separators=(",", ":"))
    return hmac.new(key, b"watch-read-back|" + said.encode(), hashlib.sha256).hexdigest()


def sentence(title: str, line: Mapping[str, Any], runs: str) -> str:
    """A chart's watched line read back in plain words."""
    col = line["column"].replace("_", " ")
    of = f" in {line['where']['value']}" if line.get("where") else ""
    what = {"latest": f"the latest {col}{of}" + (f" (along {line['order'].replace('_', ' ')})"
                                                  if line.get("order") else ""),
            "total": f"the total {col}{of}"}.get(line.get("row") or "any",
                                                  f"any row's {col}{of}")
    value = f"{line['value']:,.2f}".rstrip("0").rstrip(".")
    return (f"On \"{title}\", tell you on Home when {what} goes {line['op']} {value}; "
            f"checked {runs}.")


class AssistantToolsMixin:
    """Mixed into the backends: needs ``memory`` (``operator_api.memory.Memory`` or None),
    ``template_store`` (``TemplatesMixin``) and ``_prepare_document`` / ``_instruct_guard``
    (``InstructMixin``)."""

    def assistant_tools(self, reader: Any) -> list[Any]:
        """The reader's assistant tools (``plx.remember``, ``plx.template``,
        ``plx.make_document``, ``plx.my_work``, ``plx.hand_off``, ...) for the Ask; none
        that cannot be made, and none for a session that is not the person's own (viewing
        as someone, the Ask canary): it keeps nothing, makes nothing and reads no work log
        in their name."""
        from operating.agent_session import LocalTool
        session = getattr(reader, "session", None)
        if getattr(session, "view_as", False) or getattr(session, "canary", False):
            return []
        out = [LocalTool(spec=REMEMBER_SPEC, call=lambda a: self._remember_call(reader, a))]
        if callable(getattr(self, "template_store", None)):
            out.append(LocalTool(spec=TEMPLATE_SPEC,
                                 call=lambda a: self._template_call(reader, a)))
        if callable(getattr(self, "_prepare_document", None)):
            out.append(LocalTool(spec=DOCUMENT_SPEC,
                                 call=lambda a: self._document_call(reader, a)))
        if callable(getattr(self, "_pw_withheld", None)) \
                and callable(getattr(self, "_instruct_guard", None)):
            from . import doc_read_tool
            out.append(doc_read_tool.tool(self, reader))
        if callable(getattr(self, "insight_store", None)):
            out.append(LocalTool(spec=WATCH_SPEC, call=lambda a: self._watch_call(reader, a)))
        if getattr(self, "work_log", None) is not None:
            out.append(LocalTool(spec=MY_WORK_SPEC,
                                 call=lambda a: self._my_work_call(reader, a)))
        if callable(getattr(self, "_apa_rows", None)):
            from . import open_work_tool
            out.append(LocalTool(spec=open_work_tool.SPEC,
                                 call=lambda a: open_work_tool.call(self, reader, a)))
        if callable(getattr(self, "instruct_ask", None)):
            # the call itself does nothing: the app sees it in the answer
            # (``handed_off``) and gives the line to ``instruct_ask``
            out.append(LocalTool(spec=HAND_OFF_SPEC, call=lambda a: {
                "handed_off": True, "note": "The drafting desk answers the person now."}))
        return out

    # -- plx.my_work --------------------------------------------------------------------

    def _my_work_call(self, reader: Any, args: Mapping[str, Any]) -> Any:
        """The reader's own work-log entries (``work_log.recall``): theirs only, each row
        re-checked against the people exclusion; unreadable while it cannot decide."""
        from . import work_log as wl
        store = getattr(self, "work_log", None)
        if store is None:
            return {"error": "The work log is not set up on this server."}
        kind = args.get("kind")
        if kind is not None and kind not in wl.KINDS:
            return {"error": f"kind is one of {', '.join(wl.KINDS)}"}
        limit = args.get("limit") if type(args.get("limit")) is int else 20
        found = wl.recall(store, reader.key, since=_words(args.get("since")) or None,
                          until=_words(args.get("until")) or None,
                          words=_words(args.get("words")) or None, kind=kind,
                          sends=args.get("sends") is True,
                          open_loops=args.get("waiting") is True,
                          limit=max(1, min(limit, MY_WORK_MOST)))
        if found["total"] is None:
            return {"error": "The work log cannot be read right now (the list of people "
                             "kept out of the Workbench is not loaded): nothing is shown."}
        return {"total": found["total"], "entries": [
            {"at": e["at"], "what": wl.say(e),
             "records": [{k: r[k] for k in ("object_id", "type", "label") if r.get(k)}
                         for r in e.get("records") or ()]}
            for e in found["events"]]}

    # -- plx.watch_measure --------------------------------------------------------------

    def _watch_call(self, reader: Any, args: Mapping[str, Any]) -> Any:
        """Save and schedule a monitor over one measure for ``reader`` (``insights.
        measure_watch``): their own saved insight, run as them, its alert on their Home."""
        from . import insights as ins
        from .errors import UserFacing
        store = self.insight_store()  # type: ignore[attr-defined]
        if store is None:
            return {"not_kept": "Saved insights are not set up on this server."}
        schedule = args.get("schedule") or "daily"
        if schedule not in ("hourly", "daily", "weekly"):
            return {"not_kept": "schedule is hourly, daily or weekly"}
        chart = [k for k in CHART_ONLY if args.get(k) is not None]
        if chart or args.get("insight") is not None:
            if any(args.get(k) is not None for k in MEASURE_ONLY):
                return {"not_kept": "watch a measure or a pinned chart (insight), not both"}
            return self._watch_chart(reader, store, args, schedule)
        if args.get("measure") is None:
            return {"not_kept": "name a measure, or a pinned chart (insight: s<n>)"}
        try:
            spec = ins.measure_watch(args.get("measure"), args.get("days"),
                                     args.get("move_pct"), args.get("above"),
                                     args.get("below"))
            saved = store.save(reader, spec)
            got = store.set_schedule(reader, saved.id, schedule, "crosses", spec.watch)
        except UserFacing as why:
            return {"not_kept": why.message}
        if got is None:
            return {"not_kept": "The monitor could not be scheduled."}
        return {"watching": got.title, "runs": RUNS[schedule],
                "tells_them": "on their Home when it crosses the line, with the why "
                              f"question to ask: {got.question}",
                "definition": spec.about, "open": f"/insights#insight-{got.id}"}

    def _watch_chart(self, reader: Any, store: Any, args: Mapping[str, Any],
                     schedule: str) -> Any:
        """A line on one of ``reader``'s pinned charts (``insights_runs.reading``): its
        columns and latest rows (insight alone), the line read back with its number now
        (kept only with ``confirmed``), or why it cannot be watched. The chart is read now
        through their own door (``insights.read_sql``), as its runs will be."""
        from . import insights as ins
        from . import insights_runs as runs
        from .charts import _period, _text
        from .errors import UserFacing
        spec = store.get(reader.key, str(args.get("insight") or ""))
        if spec is None:
            return {"not_kept": "That is not one of their pinned charts (its id is s<n>, "
                                "in its Insights address /insights#insight-s<n>)."}
        try:
            got = ins.read_sql(self, reader, spec.sql)
        except UserFacing as why:
            return {"not_kept": why.message}
        columns = [str(c) for c in got.get("columns") or ()]
        rows = [r.get("cells", []) for r in got.get("rows") or () if isinstance(r, Mapping)]
        x = str((spec.chart or {}).get("x") or "") or None
        x = x if x in columns else None
        title = spec.title
        if args.get("column") is None:
            return {"chart": title, "columns": columns, "axis": x,
                    "currency_columns": runs.currency_columns(columns),
                    "last_rows": [[(_text(c) or "") if isinstance(c, str) else c
                                   for c in r] for r in rows[-SHOWN_ROWS:]],
                    "row_count": len(rows), "next": "send column, op, value, row and, for "
                    "one currency, where; nothing is kept until they say yes"}
        row = args.get("row") or ("latest" if x else "any")
        line = ins.watch_of({"column": args.get("column"), "op": args.get("op"),
                             "value": args.get("value"), "row": row,
                             "where": args.get("where"), "order": x})
        if not line:
            return {"not_kept": "name the column, above or below, and a number"}
        def timed(cell: Any) -> bool:
            t = (_text(cell) or "").strip()
            return _period(t) is not None or re.match(r"^\d{4}-\d{2}-\d{2}", t) is not None
        at_x = columns.index(x) if x else -1
        if row == "any" and x is not None and rows and all(
                timed(r[at_x] if at_x < len(r) else None) for r in rows):
            return {"not_kept": "This chart runs along time: any row would bring back "
                                "earlier periods. Watch its latest row (or the total)."}
        try:
            now = runs.reading(line, columns, rows)
        except runs.WatchUnread as why:
            return {"not_kept": f"It cannot be watched: {why}."}
        if row == "latest" and len({e["group"] for e in now}) > 1:
            return {"not_kept": "The rows hold more than one currency: name one (where)."}
        said = sentence(title, line, RUNS[schedule])
        state = [{"at": e["at"], "currency": e["group"] or (line.get("where") or {}).get(
                      "value", ""), "value": str(e["value"]),
                  "past_the_line_now": e["past"]} for e in now[:SHOWN_ROWS]]
        bound = (reader.key, spec.id, schedule, line)
        if args.get("confirmed") is not True:
            return {"read_back": said, "now": state, "kept": False,
                    "token": self._watch_token(*bound),
                    "next": "tell them this sentence; once they say yes, call again with "
                            "the same line, confirmed: true and this token"}
        if not self._watch_token_ok(str(args.get("token") or ""), *bound):
            return {"not_kept": "Nothing was set: a monitor is kept only with the token its "
                                "read-back gave, for that same line, within "
                                f"{WATCH_TOKEN_SECONDS // 60} minutes. Read it back again."}
        try:
            kept = store.set_schedule(reader, spec.id, schedule, "crosses", line)
        except UserFacing as why:
            return {"not_kept": why.message}
        if kept is None:
            return {"not_kept": "The monitor could not be scheduled."}
        return {"watching": said, "now": state, "runs": RUNS[schedule],
                "tells_them": "on their Home when it crosses the line"
                              + (f", with the why question to ask: {kept.question}"
                                 if kept.question else ""),
                "open": f"/insights#insight-{kept.id}"}

    def _watch_key(self) -> bytes:
        """The read-back tokens' key: the app's run key (the same for every process of this
        server), else one made for this process."""
        from pathlib import Path
        from .insights_runs import run_key
        data_dir = getattr(self, "data_dir", None)
        if data_dir:
            try:
                return run_key(Path(data_dir))
            except (OSError, ValueError):
                pass
        return _PROCESS_KEY

    def _watch_token(self, owner: str, sid: str, schedule: str, line: Mapping[str, Any],
                     now: Optional[float] = None) -> str:
        """A read-back's token: bound to the person, the chart, the schedule and the exact
        line, good for ``WATCH_TOKEN_SECONDS``."""
        import time
        until = int((time.time() if now is None else now) + WATCH_TOKEN_SECONDS)
        return f"{until}.{_watch_mac(self._watch_key(), owner, sid, schedule, line, until)}"

    def _watch_token_ok(self, token: str, owner: str, sid: str, schedule: str,
                        line: Mapping[str, Any]) -> bool:
        import hmac
        import time
        until, _, mac = token.partition(".")
        if not until.isdigit() or int(until) < time.time() \
                or int(until) > time.time() + WATCH_TOKEN_SECONDS:
            return False
        return hmac.compare_digest(mac, _watch_mac(self._watch_key(), owner, sid, schedule,
                                                   line, int(until)))

    # -- plx.remember -------------------------------------------------------------------

    def _remember_call(self, reader: Any, args: Mapping[str, Any]) -> Any:
        from . import memory as mem
        store = getattr(self, "memory", None)
        if store is None:
            return {"not_kept": "Memory is not set up on this server."}
        action = args.get("action")
        if action == "list":
            items = store.items(reader, limit=MOST_LISTED)
            return {"items": [{"id": i["id"], "kind": i["kind"], "text": i["text"],
                               **({"autopilot": autopilot_effect(i["text"])}
                                  if autopilot_effect(i["text"]) else {})}
                              for i in items]}
        if action == "forget":
            if type(args.get("id")) is not int or not store.forget(reader, args["id"]):
                return {"not_kept": mem.WRITE_REFUSED["not_found"]}
            return {"forgotten": args["id"], "note": "Gone from their memory: answers and "
                    "Autopilot drafts no longer follow it."}
        if action != "save":
            return {"not_kept": "action is save, forget or list."}
        kind = args.get("kind") or "instruction"
        replaces = args.get("replaces") if type(args.get("replaces")) is int else None
        new, why = store.write(reader, kind=kind, text=_words(args.get("text")),
                               replaces=replaces, personal=False)   # model-composed words
        if new is None:
            return {"not_kept": mem.WRITE_REFUSED.get(why or "", "It was not kept.")}
        text = _words(args.get("text"))
        effect = autopilot_effect(text)
        whole_side = any(r.startswith("Autopilot drafts nothing for ")
                         and " from " not in r for r in effect)
        if whole_side:
            effect = effect + ["(no draft at all for anyone on that side: if they meant "
                               "something narrower, correct it with replaces)"]
        return {"kept": new, "kind": kind, "text": text,
                "autopilot": effect or None,
                "note": ("Kept: it applies to their answers from now on, and the Autopilot "
                         "does exactly what 'autopilot' lists.") if effect else
                ("Kept: it applies to their answers and drafts from now on. It names no "
                 "address, domain or side, so the Autopilot cannot apply it as a rule by "
                 "itself (its drafts read it as guidance).")
                if kind in ("instruction", "preference") else "Kept."}

    # -- plx.template -------------------------------------------------------------------

    def _template_call(self, reader: Any, args: Mapping[str, Any]) -> Any:
        from operating import doc_fill as df
        from . import templates as tp
        store = self.template_store()  # type: ignore[attr-defined]
        if store is None:
            return {"refused": tp.REFUSED["unavailable"]}
        member, shareable = self.template_roles(reader)  # type: ignore[attr-defined]
        action = args.get("action")
        try:
            if action == "list":
                rows = store.list(reader, roles=member)
                if rows is None:
                    return {"refused": tp.REFUSED["people_exclusion_unavailable"]}
                return {"templates": [{"id": r["id"], "name": r["name"],
                                       "doc_type": r["doc_type"], "mine": r["mine"]}
                                      for r in rows[:MOST_LISTED]]}
            if action == "get":
                row = store.get(reader, int(args.get("id") or 0), roles=member)
                if row is None:
                    return {"refused": tp.REFUSED["not_found"]}
                return {"id": row["id"], "name": row["name"], "doc_type": row["doc_type"],
                        "body": row["body"],
                        "placeholders": [p["name"] for p in row["placeholders"]]}
            if action == "delete":
                if not store.delete(reader, int(args.get("id") or 0)):
                    return {"refused": tp.REFUSED["not_found"]}
                return {"deleted": int(args["id"])}
            if action != "save":
                return {"refused": "action is list, get, save or delete."}
            terms = df.load_terms()
            mine = args.get("id") if type(args.get("id")) is int else None
            kept = store.get(reader, mine, roles=member) if mine else None
            if mine and (kept is None or not kept.get("mine")):
                return {"refused": tp.REFUSED["not_found"]}
            row = store.save(reader, name=_words(args.get("name")),
                             doc_type=str(args.get("doc_type") or "other"),
                             body=str(args.get("body") or "").strip(),
                             source_ref=args.get("source") or (kept or {}).get("source_ref")
                             or None, shared_with=(kept or {}).get("shared_with") or (),
                             template_id=mine, shareable=shareable, terms=terms)
        except tp.TemplateRefused as refused:
            out: dict[str, Any] = {"refused": refused.message}
            if refused.code == "placeholder_unknown":
                out["fields"] = [f["name"] for f in tp.known_fields(df.load_terms())]
            return out
        except df.FillRefused:
            return {"refused": "Templates are paused: the list of record fields could not "
                    "be read."}
        return {"saved": row["id"], "name": row["name"], "doc_type": row["doc_type"],
                "placeholders": [p["name"] for p in row["placeholders"]],
                "note": "Saved in their Templates page; they can use it from there or ask "
                        "you to fill it for a record."}

    # -- plx.make_document --------------------------------------------------------------

    def _document_call(self, reader: Any, args: Mapping[str, Any]) -> Any:
        from .errors import UserFacing
        guard = self._instruct_guard()  # type: ignore[attr-defined]
        if guard is None:
            return {"refused": "This is paused right now; nothing was made. Try again in a few "
                    "minutes."}
        kind = args.get("kind") if args.get("kind") in KINDS else "docx"
        if kind == "csv" and args.get("charts"):
            return {"refused": "A CSV file holds rows only, never a chart: make an xlsx "
                    "(Excel) for the chart, or the CSV without it."}
        from operating import instruct_files as files
        if kind in ("xlsx", "csv") and files.data_rows(str(args.get("content") or "")) \
                > files.MAX_ROWS:
            return {"refused": f"More than {files.MAX_ROWS:,} rows typed into content: put "
                    "a list of rows in tables (its SELECT; every row is read for the file)."}
        try:
            charts = self._document_charts(reader, args.get("charts"))
            tables, held = self._document_tables(reader, args.get("tables"), kind)
            raw_cites = args.get("citations") if isinstance(args.get("citations"), list) else []
            doc = self._prepare_document(  # type: ignore[attr-defined]
                reader, {"kind": kind, "content": str(args.get("content") or ""),
                         "name": _words(args.get("name")) or "Document"}, [], guard,
                charts=charts, tables=tables, citations=raw_cites)
        except UserFacing as refused:
            return {"refused": refused.message}
        file_id = doc.download_url.rsplit("/", 1)[-1]
        out = {"file_id": file_id, "name": doc.name, "kind": doc.kind, "size": doc.size,
               "download_url": doc.download_url,
               "note": "Made and kept for them only. The app shows the file and its "
                       "download link under your answer: say what it holds, never its file "
                       "name or link."}
        if held:
            out["tables"] = held
        return out

    def _document_tables(self, reader: Any, raw: Any, kind: str
                         ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The tables a document asks for, each every row of its statement read again now
        as ``reader`` (``doc_tables.read_all`` over ``insights.read_sql``: their door), and
        what each holds (rows, columns, order) for the answer. One that cannot be read
        whole refuses the document with why: never a file with part of the rows."""
        from operating import instruct_files as files
        from . import doc_tables
        from .errors import UserFacing
        from .insights import Refused, read_sql
        if raw is None:
            return [], []
        if not isinstance(raw, list):
            raise UserFacing("table", "tables is a list of statements.", 422)
        tables, held = [], []
        for n, item in enumerate(raw, 1):
            sql = item.get("sql") if isinstance(item, Mapping) else None
            if not isinstance(sql, str) or not sql.strip() or set(item) - {"sql", "title"}:
                raise UserFacing("table", f"Table {n} is not a table of a statement "
                                 "(sql, title).", 422)
            try:
                got = doc_tables.read_all(lambda q: read_sql(self, reader, q), sql)
            except doc_tables.TableRefused as why:
                raise UserFacing("table", f"Table {n} is not made: {why}", 422) from None
            except Refused as why:
                raise UserFacing("table", f"Table {n} can't be read: {why.message}", 422) from None
            t = files.table(item.get("title"), got["columns"], got["rows"],
                            sql=sql, as_of=got.get("as_of"))
            tables.append(t)
            held.append({"table": n, "rows": len(got["rows"]), "columns": got["columns"],
                         "ordered": got["ordered"]} | ({"partial": "some cells are values "
                                                       "this person may not read: blank"}
                                                      if got["partial"] else {})
                        | ({"cells_cut": f"{got['cells_cut']} long text cells are cut at "
                                         "400 characters in the file"}
                           if got.get("cells_cut") else {}))
        return tables, held


    def _document_charts(self, reader: Any, raw: Any) -> list[dict[str, Any]]:
        """The charts a document asks for, each drawn from the rows of its statement read
        again now as ``reader`` (their door; ``insights.read_sql``), never from numbers the
        model wrote. One that cannot be read or drawn refuses the document with why."""
        from operating.instruct_files import MAX_DOC_CHARTS
        from . import charts as ch
        from .errors import UserFacing
        from .insights import read_sql
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > MAX_DOC_CHARTS:
            raise UserFacing("chart", f"A document holds up to {MAX_DOC_CHARTS} charts.", 422)
        out = []
        for n, item in enumerate(raw, 1):
            spec = ch.parse(item)
            sql = item.get("sql") if isinstance(item, Mapping) else None
            if spec is None or not isinstance(sql, str) or not sql.strip():
                raise UserFacing("chart", f"Chart {n} is not a chart of a statement.", 422)
            got = read_sql(self, reader, sql)
            rows = [r.get("cells", []) for r in got.get("rows") or () if isinstance(r, Mapping)]
            try:
                out.append(ch.build(spec, got["columns"], rows, title=str(
                    item.get("title") or ""), as_of=got.get("as_of"), sql=sql,
                    truncated=bool(got.get("truncated"))))
            except ch.NotDrawn as why:
                raise UserFacing("chart", f"Chart {n} can't be drawn: {why}", 422) from None
        return out


def documents(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The files the session made (``plx.make_document`` results, and the edited copy a
    ``plx.edit_document`` kept, in order, each once): name, kind, size and download link,
    for the answer to show. A copy edited again (``edited_from`` names it) gives its place
    to the newer copy: the person gets the file with every change, once; so does a file
    made again under the same name and kind (the session's revision of it)."""
    from .simple_ask import _parsed
    out: list[dict[str, Any]] = []
    for event in events:
        name = str(event.get("name") or "").split("__")[-1].replace(".", "_")
        if event.get("type") != "tool_call" or event.get("is_error") \
                or name not in (DOCUMENT.replace(".", "_"), EDITED.replace(".", "_")):
            continue
        result = _parsed(event.get("result"))
        changes: list[dict[str, str]] = []
        if name == EDITED.replace(".", "_"):
            source = result.get("edited_from") if isinstance(result, Mapping) else None
            changes = _changes(result.get("diff") if isinstance(result, Mapping) else None)
            result = result.get("edited_copy") if isinstance(result, Mapping) else None
            if isinstance(source, str) and source.startswith("file:f_") \
                    and isinstance(result, Mapping):
                older = f"/api/instruct/files/{source[len('file:'):]}"
                # the copy edited again: its changes come first in the newer copy's
                changes = [c for d in out if d["download_url"] == older
                           for c in d["changes"]] + changes
                out = [d for d in out if d["download_url"] != older]
        url = result.get("download_url") if isinstance(result, Mapping) else None
        if not isinstance(url, str) or not url.startswith("/api/instruct/files/f_") \
                or any(d["download_url"] == url for d in out):
            continue
        shown = str(result.get("name") or "Document")[:200]
        kind = result.get("kind") if result.get("kind") in KINDS else "docx"
        if name == DOCUMENT.replace(".", "_"):
            # a file made again under the same name and kind is the session's revision of
            # it: the newer copy takes the older one's place (the person gets it once)
            out = [d for d in out if (d["name"], d["kind"]) != (shown, kind)]
        out.append({"name": shown, "kind": kind,
                    "size": int(result.get("size") or 0), "download_url": url,
                    "changes": changes[:MOST_CHANGES]})
    return out


MOST_CHANGES = 60


def _changes(diff: Any) -> list[dict[str, str]]:
    """An edit's diff (``doc_edit``: field, before, after) as the answer shows it."""
    if not isinstance(diff, list):
        return []
    def text(v: Any) -> str:
        return "" if v is None else str(v)[:600]
    return [{"field": text(d.get("field"))[:120] or "change", "before": text(d.get("before")),
             "after": text(d.get("after"))} for d in diff if isinstance(d, Mapping)]
