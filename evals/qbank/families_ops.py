"""Question families over served records that have no curated table: Mission Control, Planner,
SharePoint lists and files, meetings and calendars, mail, Teams, extracted documents
(freight bills, certificates of analysis, customer POs, packing slips), lots, shipments,
assembly orders, the BC chart of accounts and the Portal's departments.

Truth reads the current version of each record (authority-first value per property) and
never a record held back by the people exclusion. Big types are sampled with a repeatable
TABLESAMPLE so one seed draws one sample.
"""

from __future__ import annotations

import json
from typing import Any

from .core import Family, num, spec
from . import grading_helpers as g

EXCL = "NOT EXISTS (SELECT 1 FROM ontology.people_excluded_objects px WHERE px.object_id = o.object_id)"


def O(type_: str, props: tuple[str, ...], where: str = "TRUE", sample: str = "") -> str:
    """Current records of ``type_`` with one column per property (authority-first value)."""
    cols = ", ".join(f"(array_agg(NULLIF(c.value, 'None') ORDER BY k.authority_rank NULLS LAST, c.field) "
                     f"FILTER (WHERE f.property = '{p}'))[1] AS \"{p}\"" for p in props)
    plist = ", ".join(f"'{p}'" for p in props)
    return (f"(SELECT o.object_id AS oid, {cols} FROM ontology.objects o {sample} "
            "JOIN ontology.object_versions v ON v.object_id = o.object_id AND v.superseded_at IS NULL "
            "JOIN ontology.value_sources s ON s.ver = v.ver AND s.superseded_at IS NULL "
            "JOIN ontology.value_cells c ON c.src = s.src "
            f"JOIN ontology.value_fields f ON f.field = c.field AND f.property IN ({plist}) "
            "JOIN ontology.value_kinds k ON k.kind = c.kind "
            f"WHERE o.object_type = '{type_}' AND o.missing_from_source_since IS NULL AND {EXCL} AND {where} "
            "GROUP BY o.object_id)")


SAMPLE = "TABLESAMPLE SYSTEM (%(pct)s) REPEATABLE (%(seed_n)s)"


def unique_pool(type_: str, key: str, props: tuple[str, ...], cond: str = "TRUE", sample: str = "", agree: str | None = None) -> str:
    """One record per value of ``key`` whose records all agree on the asked property
    (``agree``, default the last property): copies of one record in two bindings stay in,
    two different records sharing a title do not."""
    agree = agree or props[-1]
    others = ", ".join(f'min("{p}") AS "{p}"' for p in props if p != key)
    return (f'SELECT min(oid) AS oid, "{key}", {others}, count(*) AS w FROM {O(type_, props, sample=sample)} x '
            f'WHERE "{key}" IS NOT NULL AND {cond} GROUP BY "{key}" HAVING count(DISTINCT coalesce("{agree}", \'\')) = 1 '
            "ORDER BY md5(min(oid) || %(seed)s) LIMIT 4000")


def by_oid(type_: str, props: tuple[str, ...]) -> str:
    return f"SELECT * FROM {O(type_, props, 'o.object_id = %(oid)s')} x"


def person_names(value: Any) -> list[str]:
    """Ways a person value may be written: a display name, an address's local part."""
    v = str(value or "").strip()
    out: list[str] = []
    if v.startswith("{"):
        try:
            d = json.loads(v)
        except ValueError:
            d = {}
        for k in ("name", "display_name", "displayName"):
            if d.get(k):
                out.append(str(d[k]))
        v = str(d.get("address") or d.get("email") or "")
    if "@" in v:
        local = v.split("@", 1)[0]
        out += [local, local.replace(".", " ")]
    elif v:
        out.append(v)
    return [o for o in out if o.strip()]


def person(col: str, rule: str):
    def grade(rows, params):
        r = g.first(rows)
        names = person_names(r and r.get(col))
        return spec(rule, names=[names], min_names=1) if names else None
    return grade


def ts_date(col: str, rule: str):
    return g.date(col, rule, tol_days=1)


def folder_of(rows, params):
    r = g.first(rows)
    path = str((r or {}).get("path") or "").rstrip("/")
    leaf = path.rsplit("/", 1)[-1] if path else ""
    return spec("the file's folder", names=[leaf]) if leaf else None


MC_OPEN = "stage IN ('backlog', 'progress', 'review')"
ON_DAY = "BETWEEN %(day)s AND %(day)s || 'T23:59:59.999999Z'"

FAMILIES = [
    # ---- Mission Control ---------------------------------------------------------------
    Family("mc_task_stage", "mission_control", "MCTask", "lookup", unique_pool("MCTask", "title", ("title", "stage"), "stage IS NOT NULL"),
           by_oid("MCTask", ("stage",)),
           ("What stage is the Mission Control task \"{title}\" in?", "Is the MC task '{title}' done?", "status of task \"{title}\" in mission control?"),
           g.state("stage", "the task's stage"), truth_source="ontology"),
    Family("mc_task_assignee", "mission_control", "MCTask", "lookup", unique_pool("MCTask", "title", ("title", "assignee"), "assignee IS NOT NULL"),
           by_oid("MCTask", ("assignee",)),
           ("Who is assigned the Mission Control task \"{title}\"?", "who's on '{title}' in MC?"),
           person("assignee", "the task's assignee"), truth_source="ontology"),
    Family("mc_task_done_when", "mission_control", "MCTask", "lookup",
           unique_pool("MCTask", "title", ("title", "completed_at"), "completed_at IS NOT NULL"), by_oid("MCTask", ("completed_at",)),
           ("When was the Mission Control task \"{title}\" completed?", "what day did MC task '{title}' get finished?"),
           ts_date("completed_at", "completion date"), truth_source="ontology"),
    Family("mc_open_by_assignee", "mission_control", "MCTask", "count",
           f"SELECT assignee, count(*) AS w FROM {O('MCTask', ('assignee',))} x WHERE assignee IS NOT NULL GROUP BY 1",
           f"SELECT count(*) AS n FROM {O('MCTask', ('assignee', 'stage'))} x WHERE assignee = %(assignee)s AND {MC_OPEN}",
           ("How many open Mission Control tasks does {assignee} have?", "{assignee}'s open MC tasks - how many?"),
           g.count("open (backlog, in progress, review) tasks of the assignee"), truth_source="ontology"),
    Family("mc_bucket_open", "mission_control", "MCTask", "count",
           f"SELECT bucket, count(*) AS w FROM {O('MCTask', ('bucket',))} x WHERE bucket IS NOT NULL GROUP BY 1 ORDER BY md5(bucket || %(seed)s) LIMIT 4000",
           f"SELECT count(*) AS n FROM {O('MCTask', ('bucket', 'stage'))} x WHERE bucket = %(bucket)s AND {MC_OPEN}",
           ("How many open tasks are in the {bucket} bucket in Mission Control?", "open MC tasks in bucket {bucket}?"),
           g.count("open tasks in the bucket"), truth_source="ontology"),
    *[Family(fid, "mission_control", "MCProject", "lookup", unique_pool("MCProject", "name", ("name", col), f"\"{col}\" IS NOT NULL"),
             by_oid("MCProject", (col,)), asks, grade, n=30, truth_source="ontology")
      for fid, col, asks, grade in (
          ("mc_project_status", "status", ("Is the {name} project in Mission Control still active?", "status of MC project {name}?"),
           g.state("status", "the project's status")),
          ("mc_project_health", "health", ("How is the {name} project tracking?", "is project {name} on track?"), g.state("health", "the project's health")),
          ("mc_project_owner", "owner", ("Who owns the {name} project in Mission Control?",), person("owner", "the project owner")))],
    Family("mc_approvals_pending", "mission_control", "MCApproval", "count", "SELECT 1 AS w",
           f"SELECT count(*) AS n FROM {O('MCApproval', ('status',))} x WHERE status = 'pending'",
           ("How many approvals are waiting in Mission Control?", "anything pending approval in MC?"),
           g.count("pending approvals"), n=1, truth_source="ontology"),
    Family("mc_done_week", "mission_control", "MCTask", "count",
           "SELECT (date_trunc('week', d) )::date::text AS day, to_char(date_trunc('week', d), 'FMMonth FMDD, YYYY') AS week, 1 AS w "
           "FROM generate_series(%(today)s::date - 120, %(today)s::date - 7, interval '7 days') AS d",
           f"SELECT count(*) AS n FROM {O('MCTask', ('completed_at',))} x WHERE completed_at >= %(day)s "
           "AND completed_at < (%(day)s::date + 7)::text",
           ("How many Mission Control tasks were completed in the week of {week}?", "MC tasks finished the week starting {week}?"),
           g.count("tasks completed in the week (UTC)"), n=15, truth_source="ontology"),
    # ---- Planner ---------------------------------------------------------------------------
    Family("pl_task_due", "planner", "PlannerTask", "lookup",
           unique_pool("PlannerTask", "title", ("title", "plan_title", "due_at"), "due_at IS NOT NULL"), by_oid("PlannerTask", ("due_at",)),
           ("When is the Planner task \"{title}\" due?", "due date for '{title}' in the {plan_title} plan?"),
           ts_date("due_at", "the task's due date"), truth_source="ontology"),
    Family("pl_task_pct", "planner", "PlannerTask", "lookup",
           unique_pool("PlannerTask", "title", ("title", "plan_title", "percent_complete"), "percent_complete IS NOT NULL"),
           by_oid("PlannerTask", ("percent_complete",)),
           ("Is the Planner task \"{title}\" finished?", "how far along is '{title}' in {plan_title}?"),
           lambda rows, p: (spec("task state from percent complete", must_any={"100": ("complete", "completed", "done", "finished", "100"),
                                                                                "0": ("not started", "0%", "hasn't started", "has not started", "not yet started")}
                                 .get(str(rows[0]["percent_complete"]), ("in progress", "50", "underway", "partially")))
                            if g.first(rows) else None), truth_source="ontology"),
    Family("pl_task_bucket", "planner", "PlannerTask", "lookup",
           unique_pool("PlannerTask", "title", ("title", "plan_title", "bucket_name"), "bucket_name IS NOT NULL"), by_oid("PlannerTask", ("bucket_name",)),
           ("Which bucket is the Planner task \"{title}\" in?", "what column is '{title}' under in {plan_title}?"),
           g.text("bucket_name", "the bucket"), truth_source="ontology"),
    Family("pl_plan_open", "planner", "PlannerTask", "count",
           f"SELECT plan_title, count(*) AS w FROM {O('PlannerTask', ('plan_title',))} x WHERE plan_title IS NOT NULL GROUP BY 1",
           f"SELECT count(*) AS n, array_agg(title ORDER BY title) AS titles FROM {O('PlannerTask', ('plan_title', 'percent_complete', 'title'))} x "
           "WHERE plan_title = %(plan_title)s AND coalesce(percent_complete, '0') <> '100'",
           ("How many tasks are still open in the {plan_title} plan?", "what's left to do in planner plan {plan_title}?"),
           g.count("incomplete tasks in the plan", names_col="titles"), truth_source="ontology"),
    # ---- SharePoint lists and files --------------------------------------------------------
    Family("spl_status", "sharepoint_lists", "SharePointListItem", "lookup",
           "SELECT oid, title, 1 AS w FROM (SELECT oid, f::json->>'Title' AS title, f::json->>'Status' AS status, count(*) OVER (PARTITION BY f::json->>'Title') AS dup "
           f"FROM (SELECT oid, fields AS f FROM {O('SharePointListItem', ('fields',))} x WHERE fields LIKE '{{%%') y) z "
           "WHERE dup = 1 AND title IS NOT NULL AND length(title) >= 6 AND status IS NOT NULL ORDER BY md5(oid || %(seed)s) LIMIT 4000",
           f"SELECT fields::json->>'Status' AS status FROM {O('SharePointListItem', ('fields',), 'o.object_id = %(oid)s')} x",
           ("What's the status of \"{title}\" on the SharePoint list?", "where does '{title}' stand in the list?"),
           g.state("status", "the list item's Status"), truth_source="ontology"),
    Family("spf_modified", "documents", "SharePointFile", "lookup",
           unique_pool("SharePointFile", "name", ("name", "last_modified"), "last_modified IS NOT NULL AND name ~ '[.][a-z]{3,4}$' AND length(name) >= 10",
                       sample=SAMPLE),
           by_oid("SharePointFile", ("last_modified",)),
           ("When was the file \"{name}\" last changed?", "last modified date of {name} on SharePoint?"),
           ts_date("last_modified", "last modified date"), truth_source="ontology"),
    Family("spf_folder", "documents", "SharePointFile", "lookup",
           unique_pool("SharePointFile", "name", ("name", "path"), "path IS NOT NULL AND name ~ '[.][a-z]{3,4}$' AND length(name) >= 10", sample=SAMPLE),
           by_oid("SharePointFile", ("path",)),
           ("Where is \"{name}\" saved on SharePoint?", "which folder has the file {name}?"), folder_of, truth_source="ontology"),
    # ---- meetings and calendar -------------------------------------------------------------
    Family("mtg_when", "meetings", "Meeting", "lookup",
           unique_pool("Meeting", "subject", ("subject", "start", "type"), "type = 'singleInstance' AND length(subject) >= 8"),
           by_oid("Meeting", ("start",)),
           ("When was the meeting \"{subject}\"?", "what day is/was '{subject}'?"), ts_date("start", "the meeting date"), truth_source="ontology"),
    Family("mtg_organizer", "meetings", "Meeting", "lookup",
           unique_pool("Meeting", "subject", ("subject", "organizer", "type"), "type = 'singleInstance' AND length(subject) >= 8"),
           by_oid("Meeting", ("organizer",)),
           ("Who organized the meeting \"{subject}\"?", "who set up '{subject}'?"), person("organizer", "the organizer"), truth_source="ontology"),
    Family("cal_location", "meetings", "CalendarEvent", "lookup",
           unique_pool("CalendarEvent", "subject", ("subject", "location"), "location IS NOT NULL AND location !~ '^https?:' AND length(location) >= 4 "
                       "AND length(subject) >= 8"),
           by_oid("CalendarEvent", ("location",)),
           ("Where is the \"{subject}\" event held?", "location of '{subject}'?"), g.text("location", "the event's location"), truth_source="ontology"),
    # ---- mail ---------------------------------------------------------------------------------
    Family("mail_sender", "mail", "Email", "lookup",
           "SELECT oid, subject, left(sent_at, 10) AS day, to_char(left(sent_at, 10)::date, 'FMMonth FMDD, YYYY') AS day_text, 1 AS w "
           f"FROM {O('Email', ('subject', 'sent_at'), sample=SAMPLE)} x WHERE sent_at >= (%(today)s::date - 120)::text "
           "AND length(subject) >= 12 AND subject !~* '^(re|fw|fwd|automatic reply|undeliverable)' ORDER BY md5(oid || %(seed)s) LIMIT 4000",
           by_oid("Email", ("sender",)),
           ("Who sent the email \"{subject}\" on {day_text}?", "who emailed us '{subject}' ({day_text})?"),
           person("sender", "the sender"), truth_source="ontology"),
    Family("mail_when", "mail", "Email", "lookup",
           "SELECT oid, subject, 1 AS w "
           f"FROM {O('Email', ('subject', 'sent_at'), sample=SAMPLE)} x WHERE sent_at >= (%(today)s::date - 120)::text "
           "AND length(subject) >= 16 AND subject !~* '^(re|fw|fwd|automatic reply|undeliverable)' ORDER BY md5(oid || %(seed)s) LIMIT 4000",
           f"SELECT min(left(sent_at, 10)) AS d, count(DISTINCT left(sent_at, 10)) AS days FROM {O('Email', ('subject', 'sent_at'), 'o.object_id = %(oid)s')} x",
           ("When did we get the email \"{subject}\"?", "what date was the '{subject}' email sent?"),
           lambda rows, p: g.date("d", "the email's date", tol_days=1)(rows, p) if rows and rows[0]["days"] == 1 else None,
           truth_source="ontology"),
    # ---- Teams --------------------------------------------------------------------------------
    Family("teams_chat_last", "teams", "TeamsChat", "lookup",
           unique_pool("TeamsChat", "topic", ("topic", "last_activity_at"), "length(topic) >= 6 AND last_activity_at IS NOT NULL"),
           by_oid("TeamsChat", ("last_activity_at",)),
           ("When was the last message in the Teams chat \"{topic}\"?", "last activity in the '{topic}' chat?"),
           ts_date("last_activity_at", "the chat's last activity"), truth_source="ontology"),
    Family("teams_channel_day", "teams", "TeamsChannelMessage", "count",
           f"SELECT ch.display_name AS channel, ch.channel_id, left(m.created_at, 10) AS day, to_char(left(m.created_at, 10)::date, 'FMMonth FMDD, YYYY') AS day_text, "
           f"count(*) AS w FROM {O('TeamsChannelMessage', ('channel_id', 'created_at'))} m JOIN {O('TeamsChannel', ('channel_id', 'display_name'))} ch "
           "ON ch.channel_id = m.channel_id WHERE m.created_at >= (%(today)s::date - 90)::text "
           "AND ch.display_name IN (SELECT display_name FROM " + O('TeamsChannel', ('display_name',)) + " d GROUP BY 1 HAVING count(*) = 1) "
           "GROUP BY 1, 2, 3, 4 ORDER BY md5(ch.channel_id || left(m.created_at, 10) || %(seed)s) LIMIT 4000",
           f"SELECT count(*) AS n FROM {O('TeamsChannelMessage', ('channel_id', 'created_at', 'message_type'))} m WHERE channel_id = %(channel_id)s "
           f"AND created_at {ON_DAY} AND coalesce(message_type, 'message') = 'message'",
           ("How many messages were posted in the {channel} channel on {day_text}?", "activity in Teams channel {channel} on {day_text} - how many posts?"),
           g.count("channel messages that day (UTC)"), truth_source="ontology"),
    # ---- extracted documents ---------------------------------------------------------------------
    *[Family(fid, "documents", typ, "lookup", unique_pool(typ, key, (key, col), f"\"{col}\" IS NOT NULL AND length(\"{key}\") >= 4", sample=smp),
             by_oid(typ, (col,)), asks, grade, truth_source="ontology")
      for fid, typ, key, col, smp, asks, grade in (
          ("fb_carrier", "FreightBill", "invoice_number", "carrier", "", ("Which carrier is on freight bill {invoice_number}?",
                                                                         "who shipped freight invoice {invoice_number}?"), g.text("carrier", "the carrier")),
          ("fb_port", "FreightBill", "invoice_number", "port_of_loading", "", ("Where did the shipment on freight bill {invoice_number} load?",),
           g.text("port_of_loading", "port of loading")),
          ("coa_supplier", "CertificateOfAnalysis", "lot", "supplier", "", ("Who supplied lot {lot} (per its certificate of analysis)?",
                                                                           "CoA for lot {lot} - which supplier?"), g.text("supplier", "the supplier")),
          ("coa_expiry", "CertificateOfAnalysis", "lot", "expiry_date", "", ("When does lot {lot} expire according to its CoA?",),
           g.date("expiry_date", "the expiry date")),
          ("cpo_required", "CustomerPoDocument", "po_number", "required_date", "", ("What date does the customer need PO {po_number} by?",
                                                                                    "required date on customer PO {po_number}?"),
           g.date("required_date", "the required date", tol_days=1)),
          ("ps_ship_date", "PackingSlip", "document_number", "ship_date", "", ("When did packing slip {document_number} ship?",),
           g.date("ship_date", "the ship date", tol_days=1)))],
    # ---- lots, shipments, assembly orders ------------------------------------------------------
    *[Family(fid, "quality", "Lot", "lookup", unique_pool("Lot", "lot_number", ("lot_number", col), f"\"{col}\" IS NOT NULL AND length(lot_number) >= 5"),
             by_oid("Lot", (col,)), asks, grade, truth_source="ontology")
      for fid, col, asks, grade in (
          ("lot_qc", "qc_status", ("What's the QC status of lot {lot_number}?", "is lot {lot_number} released?"), g.state("qc_status", "the lot's QC status")),
          ("lot_expiry", "expiry_date", ("When does lot {lot_number} expire?", "expiry on lot {lot_number}?"),
           lambda rows, p: g.date("expiry_date", "the expiry date")(rows, p) if rows and str(rows[0].get("expiry_date") or "")[:1].isdigit() else None))],
    *[Family(fid, "shipping", "Shipment", "lookup",
             unique_pool("Shipment", "shipment_number", ("shipment_number", col), f"\"{col}\" IS NOT NULL", sample=""),
             by_oid("Shipment", (col,)), asks, grade, truth_source="ontology")
      for fid, col, asks, grade in (
          ("ship_carrier", "carrier", ("Which carrier took shipment {shipment_number}?", "carrier for shipment {shipment_number}?"), g.text("carrier", "the carrier")),
          ("ship_tracking", "tracking_number", ("What's the tracking number for shipment {shipment_number}?",), g.text("tracking_number", "the tracking number")),
          ("ship_date", "ship_date", ("When did shipment {shipment_number} go out?",), g.date("ship_date", "the ship date", tol_days=1)))],
    *[Family(fid, "production", "AssemblyOrder", "lookup",
             unique_pool("AssemblyOrder", "code", ("code", col), f"\"{col}\" IS NOT NULL AND length(code) >= 4"),
             by_oid("AssemblyOrder", (col,)), asks, grade, truth_source="ontology")
      for fid, col, asks, grade in (
          ("ao_status", "status", ("What's the status of assembly order {code}?", "is AO {code} done?"), g.state("status", "the assembly order's status")),
          ("ao_scheduled", "scheduled_date", ("When is assembly order {code} scheduled?", "AO {code} scheduled date?"),
           g.date("scheduled_date", "the scheduled date", tol_days=1)))],
    # ---- finance master and org ------------------------------------------------------------------
    Family("gl_account_name", "finance", "GlAccount", "lookup", unique_pool("GlAccount", "number", ("number", "display_name"), "display_name IS NOT NULL"),
           by_oid("GlAccount", ("display_name",)),
           ("What is GL account {number} in Business Central?", "BC account {number} - what's it called?"),
           g.text("display_name", "the account name"), truth_source="ontology"),
    Family("gl_account_category", "finance", "GlAccount", "lookup", unique_pool("GlAccount", "number", ("number", "category"), "category IS NOT NULL"),
           by_oid("GlAccount", ("category",)),
           ("What category is GL account {number} in BC?",), g.text("category", "the account category"), truth_source="ontology"),
    Family("dept_members", "org", "Department", "count",
           unique_pool("RelPublicAppDepartment", "name", ("name", "id"), "id IS NOT NULL"),
           f"SELECT count(DISTINCT user_id) AS n FROM {O('RelPublicAppUserDepartmentMembership', ('department_id', 'user_id'))} x "
           f"WHERE department_id = (SELECT id FROM {O('RelPublicAppDepartment', ('id',), 'o.object_id = %(oid)s')} d)",
           ("How many people are in the {name} department?", "headcount of {name} dept in the Portal?"),
           g.count("members of the department (Portal)"), truth_source="ontology"),
    # ---- impossible over records ---------------------------------------------------------------------
    Family("imp_mc_task", "mission_control", "MCTask", "impossible",
           f"SELECT 'T-' || (max(task_id_n) + 1000 + k)::text AS task_id, 1 AS w FROM (SELECT NULLIF(regexp_replace(task_id, '[^0-9]', '', 'g'), '')::bigint AS task_id_n "
           f"FROM {O('MCTask', ('task_id',))} x) y, generate_series(1, 5) AS k GROUP BY k",
           f"SELECT count(*) AS n FROM {O('MCTask', ('task_id',))} x WHERE task_id = %(task_id)s",
           ("What's the status of Mission Control task {task_id}?",), g.missing("no such task: the answer says it cannot be found"),
           n=5, truth_source="ontology"),
]
