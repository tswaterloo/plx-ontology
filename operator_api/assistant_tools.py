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

from typing import Any, Mapping, Sequence

from operating import learning


REMEMBER = "plx.remember"
TEMPLATE = "plx.template"
DOCUMENT = "plx.make_document"
# a prepared document edit keeps its edited copy for the reader (``prepared_area``)
EDITED = "plx.edit_document"
WATCH = "plx.watch_measure"
READ_DOCUMENT = "plx.read_document"
NAMES = (REMEMBER, TEMPLATE, DOCUMENT, WATCH, READ_DOCUMENT)
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
        "notes you made up, no internal ids, statuses or tool words, and never the "
        "answer's '(unverified)' mark: a figure you could not check stays out of the file "
        "or goes in a plain note, and you say so in the answer. charts: up to 8 "
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
                       "tables": {"type": "array", "maxItems": 8,
                                  "items": {"type": "object", "required": ["sql"],
                                            "properties": {"sql": {"type": "string"},
                                                           "title": {"type": "string"}}}}}},
    "annotations": {"readOnlyHint": False, "destructiveHint": False},
}


def _words(text: Any) -> str:
    return " ".join(str(text or "").split())


def autopilot_effect(text: Any) -> list[str]:
    """What the Autopilot does because of one memory item, in plain words (its stated
    rules, ``learning.autopilot_rules`` per sentence); [] when it states none."""
    return [r["text"] for r in learning.stated_rules(text)
            if str(r.get("key", "")).startswith(learning.RULE_PREFIXES)]


# "0": a line asking for something done goes to the instruction engine, as before
DO_ENV = "PLX_ASK_SIMPLE_DO"
# where the instruction engine's own flows start: an email, file, meeting or autopilot item
# the person has open (a reply in its thread, a meeting's follow-up, the item's work)
ENGINE_CONTEXTS = ("email", "source", "meeting", "autopilot")
# a reply or forward goes in the thread of the person's own message (the engine's desk)
ENGINE_VERBS = ("reply", "respond", "forward", "answer")


# when a monitor runs (``insights_runs``: business time zone)
RUNS = {"hourly": "every hour", "daily": "every morning", "weekly": "Monday mornings"}
WATCH_SPEC = {
    "name": WATCH,
    "description": (
        "Watch a measure of the catalogue for the person (plx.sql's measures: use its id or "
        "name) and tell them on their Home when it moves. It runs by itself as them, on "
        "their own access, at the schedule given (default daily), and the alert offers the "
        "why question so the reason is read when it fires. Default: tell them when the "
        "latest days (default 7) are unusual, 2 spreads or more from the usual level of the "
        "12 periods before. move_pct: when it moves by that percent or more against the "
        "same days just before instead. above / below: a number the measure's value "
        "crosses (the only way for a snapshot measure like open or late orders; read its "
        "value first to propose a line). Use it when they ask to watch, monitor, track or "
        "be alerted on a number; never for a record (a customer, an order). The result "
        "says what was set up and where to see it, or why nothing was kept: tell them "
        "exactly that."),
    "inputSchema": {
        "type": "object", "additionalProperties": False, "required": ["measure"],
        "properties": {
            "measure": {"type": "string"},
            "days": {"type": "integer", "minimum": 1, "maximum": 365},
            "move_pct": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000},
            "above": {"type": "number"},
            "below": {"type": "number"},
            "schedule": {"type": "string", "enum": ["hourly", "daily", "weekly"]},
        },
    },
}


class AssistantToolsMixin:
    """Mixed into the backends: needs ``memory`` (``operator_api.memory.Memory`` or None),
    ``template_store`` (``TemplatesMixin``) and ``_prepare_document`` / ``_instruct_guard``
    (``InstructMixin``)."""

    def ask_does(self, reader: Any, request: Any) -> bool:
        """Whether a line asking for something done (or one the router cannot tell, a
        question or a job) goes to the simple Ask, which reads every record and has these
        tools and the write tools, rather than the instruction engine: when the simple
        Ask serves this reader (``simple_ask.enabled``, the agent path) and the line is
        none of the engine's own flows: a continued instruction, an email, file, meeting
        or autopilot item open (``ENGINE_CONTEXTS``), attached files, a reply or a forward
        (sent in the thread of the person's own message, ``ENGINE_VERBS``), a record to
        create from another, or a recurring job. "Use my <name> template for <record>" is
        the Ask's: it reads the template (``plx.template``) and fills it from every record."""
        import os
        from operating import instruct as ins
        from operating.entry_assist import create_kind
        from . import simple_ask
        agent = getattr(self, "agent_ask", None)
        if os.environ.get(DO_ENV, "1") == "0" or not simple_ask.enabled() or agent is None \
                or not agent.serves(reader.session):
            return False
        context = getattr(request, "context", None)
        text = " ".join(str(request.question or "").split())
        return not (request.instruction is not None or request.source or request.attachments
                    or getattr(context, "kind", None) in ENGINE_CONTEXTS
                    or (ins.verb(text) or "").startswith(ENGINE_VERBS)
                    or create_kind(text) or ins.recurring(text))

    def assistant_tools(self, reader: Any) -> list[Any]:
        """The reader's assistant tools (``plx.remember``, ``plx.template``,
        ``plx.make_document``) for the Ask; none that cannot be made, and none for a session
        that is not the person's own (viewing as someone, the Ask canary): it keeps nothing
        and makes nothing in their name."""
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
        return out

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
                               replaces=replaces)
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
            return {"refused": "This is paused: the list of people whose data stays out is "
                    "not loaded."}
        kind = args.get("kind") if args.get("kind") in KINDS else "docx"
        if kind == "csv" and args.get("charts"):
            return {"refused": "A CSV file holds rows only, never a chart: make an xlsx "
                    "(Excel) for the chart, or the CSV without it."}
        from operating import instruct_files as files
        if kind in ("xlsx", "csv") and files.data_rows(str(args.get("content") or "")) \
                > files.MAX_ROWS:
            return {"refused": f"More than {files.MAX_ROWS:,} rows typed into content: put "
                    "a list of rows in tables (its SELECT; every row is read for the file)."}
        from integrations.image_ocr_gpu import touches_freightcom
        if touches_freightcom(str(args.get("content") or ""), str(args.get("name") or "")):
            # the carrier whose data never reaches the AI or an action: no file of it
            return {"refused": "That carrier is never written about through the Workbench."}
        try:
            charts = self._document_charts(reader, args.get("charts"))
            tables, held = self._document_tables(reader, args.get("tables"), kind)
            doc = self._prepare_document(  # type: ignore[attr-defined]
                reader, {"kind": kind, "content": str(args.get("content") or ""),
                         "name": _words(args.get("name")) or "Document"}, [], guard,
                charts=charts, tables=tables)
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
        from integrations.image_ocr_gpu import touches_freightcom
        from . import doc_tables
        from .errors import UserFacing
        from .insights import Refused, read_sql
        if raw is None:
            return [], []
        if not isinstance(raw, list) or len(raw) > doc_tables.MAX_TABLES:
            raise UserFacing("table", f"A document holds up to {doc_tables.MAX_TABLES} "
                             "tables.", 422)
        tables, held = [], []
        for n, item in enumerate(raw, 1):
            sql = item.get("sql") if isinstance(item, Mapping) else None
            if not isinstance(sql, str) or not sql.strip() or set(item) - {"sql", "title"}:
                raise UserFacing("table", f"Table {n} is not a table of a statement "
                                 "(sql, title).", 422)
            try:
                got = doc_tables.read_all(lambda q: read_sql(self, reader, q), sql,
                                          max_rows=files.MAX_TABLE_ROWS[kind])
            except doc_tables.TableRefused as why:
                raise UserFacing("table", f"Table {n} is not made: {why}", 422) from None
            except Refused as why:
                raise UserFacing("table", f"Table {n} can't be read: {why.message}", 422) from None
            t = files.table(item.get("title"), got["columns"], got["rows"])
            if touches_freightcom(t["title"], t["lines"]):
                raise UserFacing("table", "That carrier is never written about through "
                                 "the Workbench.", 422)
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
    to the newer copy: the person gets the file with every change, once."""
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
        out.append({"name": str(result.get("name") or "Document")[:200],
                    "kind": result.get("kind") if result.get("kind") in KINDS else "docx",
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
