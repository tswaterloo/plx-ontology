"""``biz.staff``: the org chart as a business view ("a company
model that knows the people and who reports to whom"; option 3: no ontology
schema change).

Read at business-view build from the org chart capture's own store
(``integrations.capture_org``: ``<PLX_STATE_DIR>/org-capture/capture.db``, read-only) and
joined to the served staff ``Person`` objects by exact Entra object id (a staff Person's
key is its object id): a directory user with no served staff Person is not listed. One row
per staff person: name, title, department, office, address, manager (only a manager who is
also listed) and the people reporting to them.

Who reads it: every staff reader and every full reader (``staff_scope.ORG_CHART_KEY``, the org chart is not sensitive), never
a guest or an unresolved reader (their policy grants no ``org:`` key). A row carries no
record marking: its values are the org chart capture's, not the staff Person's (whose
version may be marked ``pii`` as a mail contact). The row's own
record is the staff Person (``object_ids``): the people exclusion drops the row
(``business_views_access``) and ``door.exclude`` drops it when the set grows; the manager
and reports cells name other Persons (``Row.col_names``): such a cell is emptied when one of
them is excluded. The capture itself never stores an excluded, undecided or HR / payroll
deny-listed user, nor names one as a manager (``withheld``). No store, an unreadable
store: no rows (nothing guessed)."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from operating.pg.business_views import Reader, Row, view
from operating.staff_scope import ORG_CHART_KEY

PERSON_BINDING = "graph_mail.people"
MAX_REPORTS = 500   # names listed per manager (the count is exact)


def org_db() -> Path:
    """The org chart capture's store on this host (``PLX_STATE_DIR``)."""
    from integrations.capture_org import DB_FILE, STORE_DIR
    return Path(os.environ.get("PLX_STATE_DIR") or "/data/plx-state") / STORE_DIR / DB_FILE


def person_id(oid: str) -> str:
    """The staff ``Person`` object id of an Entra object id (the projector's own)."""
    from ontology.project import object_id_of
    return object_id_of("Person", PERSON_BINDING, {"person_key": oid})


def staff_persons(rd: Reader, oids: Sequence[str]) -> dict[str, tuple[str, str, str]]:
    """object id -> (Person object id, its least-marked current version's handle, that
    version's markings) of the served, not removed staff Persons among ``oids``."""
    ids = {person_id(o): o for o in oids}
    gone = rd.removed("Person")
    out: dict[str, tuple[str, str, str]] = {}
    keys = sorted(ids)
    for at in range(0, len(keys), 5000):
        for r in rd.conn.execute(
                "SELECT o.object_id, v.l1_handle, v.markings FROM objects o JOIN "
                "object_versions v ON v.object_id = o.object_id AND v.superseded_at IS NULL "
                "WHERE o.object_type = 'Person' AND o.object_id IN (SELECT value FROM "
                "json_each(:ids)) ORDER BY o.object_id, v.l1_handle",
                {"ids": json.dumps(keys[at:at + 5000])}):
            obj, handle, marks = r[0], r[1], r[2] or "[]"
            if obj in gone or not handle:
                continue
            held = out.get(ids[obj])
            if held is None or len(marks) < len(held[2]):
                out[ids[obj]] = (obj, handle, marks)
    return out


def staff_rows(org: Mapping[str, Mapping[str, Any]],
               persons: Mapping[str, tuple[str, str, str]]) -> Iterator[Row]:
    """One row per object id both in the org chart (``org``: ``capture_org.current_rows``)
    and among the served staff Persons (``persons``: ``staff_persons``), by object id. A
    manager is named only when listed here too; reports are the listed people whose
    manager this person is (names sorted, at most ``MAX_REPORTS``; the count exact)."""
    listed = sorted(o for o in org if o in persons)
    kept = set(listed)
    reports: dict[str, list[str]] = {}
    for oid in listed:
        boss = org[oid].get("manager_object_id")
        if org[oid].get("manager_state") == "listed" and boss in kept and boss != oid:
            reports.setdefault(boss, []).append(oid)
    for oid in listed:
        held, (obj, handle, _marks) = org[oid], persons[oid]
        row = Row()
        row.objects, row.handle, row.copies = [obj], handle, 1
        row.systems = ["directory"]
        row.audience = {ORG_CHART_KEY}
        # every value is the org chart capture's (work name, title, department, office,
        # address, manager): the Person is the row's identity for the people exclusion,
        # none of its cells is shown, so its version's marking (a mail contact's "pii")
        # is not the row's; carrying it hid the org chart from every staff reader
        row.marks = {"[]"}
        cite = [f"org-user://{oid}", "row"]
        boss = held.get("manager_object_id")
        boss = boss if held.get("manager_state") == "listed" and boss in kept \
            and boss != oid else None
        under = sorted(reports.get(oid, ()), key=lambda o: (
            (org[o].get("display_name") or "").casefold(), o))
        row.values.update({
            "name": held.get("display_name"), "title": held.get("job_title"),
            "department": held.get("department"), "office": held.get("office"),
            "email": held.get("upn"),
            "person_id": oid.lower(),
            "manager": org[boss].get("display_name") if boss else None,
            "manager_email": org[boss].get("upn") if boss else None,
            "manager_state": "listed" if boss else (
                "none" if held.get("manager_state") == "none" else "not_known"),
            "reports": [org[o].get("display_name") or org[o].get("upn") or ""
                        for o in under[:MAX_REPORTS]],
            "reports_count": len(under)})
        for col in ("name", "title", "department", "office", "email", "person_id",
                    "manager_state"):
            row.cites[col] = cite
        if boss:
            row.cites["manager"] = row.cites["manager_email"] = cite
            row.col_names["manager"] = {persons[boss][0]}
            row.col_names["manager_email"] = {persons[boss][0]}
        if under:
            row.cites["reports"] = row.cites["reports_count"] = cite
            row.col_names["reports"] = {persons[o][0] for o in under}
            row.col_names["reports_count"] = {persons[o][0] for o in under}
        yield row


@view("staff", "Our staff and the org chart, one row per person (the tenant directory, "
      "read daily, joined to the staff people the mail capture knows): name, job title, "
      "department, office, work address, their manager and the people who report to "
      "them. Who reports to X: WHERE manager = X; X's team: reports. A manager is named "
      "only when they are listed here too.",
      [("name", "text", "the person's display name"),
       ("title", "text", "job title as the directory states it (may be empty)"),
       ("department", "text", "department as the directory states it (may be empty)"),
       ("office", "text", "office location as the directory states it (may be empty)"),
       ("email", "text", "work address (user principal name), lower-case"),
       ("person_id", "text", "their directory id: their own mailbox and calendar rows have "
        "emails.mailbox / meetings.mailbox = person_id, and their Teams messages carry it as "
        "the sender's \"id\""),
       ("manager", "text", "the manager's display name (empty: none or not known)"),
       ("manager_email", "text", "the manager's work address"),
       ("manager_state", "text", "listed (manager named), none (the directory names no "
        "manager) or not_known (withheld or not read: never guess one)"),
       ("reports", "list", "display names of the people who report directly to them"),
       ("reports_count", "integer", "how many people report directly to them")],
      reads=("Person",), covers=False)   # staff only: every Person stays in records
def _staff(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    from integrations.capture_org import current_rows
    org = current_rows(org_db())
    if not org:
        return
    yield from staff_rows(org, staff_persons(rd, sorted(org)))
