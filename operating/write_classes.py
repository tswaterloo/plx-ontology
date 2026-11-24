"""Write classes (ASSIST specs 1.3 and 2.10, contract C8): every operation a write path can
perform has one class, the single table here (``SEED``; ``assist.write_classes`` is loaded
from it and a test holds the migration equal to it).

* **A**: reversible or harmless (a note, a comment line, a task, a message with no date or
  number). May go out unattended only when the person's own switch, the earned bar and the
  system's readiness all hold (``autopilot_write``).
* **B**: can move money, a quantity or a date. Never unattended: a named person's approval.
* **C**: no write path at all (FileMaker, Sage): the system gets an entry sheet, we never
  perform it.

A row may carry a ``b_rule``: the action is class A unless the rule fires, then class B. The
three rules are the three functions below and nothing else. A rule that cannot be decided
(the rules, the item or the readers are not known here) fires: the conservative class.

``write_class(action)`` is the one resolution. An operation not in the table raises
``Refused(["unclassed operation"])``: nothing new is performed before it has a class."""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, NamedTuple, Optional, Sequence


class Row(NamedTuple):
    system: str
    operation: str
    cls: str
    b_rule: str
    why: str


def _rows(system: str, operations: Sequence[str], cls: str, why: str,
          rule: str = "") -> list[Row]:
    return [Row(system, op, cls, rule, why) for op in operations]


SEED: tuple[Row, ...] = tuple(
    _rows("mc", ["approval_decide"], "A",
          "B whenever autopilot_write.routine() says not routine (money, complex, flagged, no "
          "precedent); A otherwise", "mc_not_routine")
    + _rows("mc", ["task_note"], "A", "a note")
    + _rows("mc", ["task_update"], "B", "can move a due date or an assignee")
    + _rows("bc", ["purchase_order_create", "sales_order_create"], "B",
            "quantities, prices, dates")
    + _rows("bc", ["purchase_order_update", "sales_order_update"], "B",
            "header dates and terms")
    + _rows("bc", ["purchase_order_line_create", "purchase_order_line_update",
                   "sales_order_line_create", "sales_order_line_update"], "B",
            "quantity, price, date")
    + _rows("bc", ["sales_order_charge_line", "sales_invoice_charge_line"], "B", "money")
    + _rows("bc", ["purchase_order_delete", "sales_order_delete"], "B", "removes quantities")
    + _rows("bc", ["purchase_order_comment", "sales_order_comment"], "A", "a comment line")
    + _rows("bc", ["purchase_order_attach", "sales_order_attach", "sales_invoice_attach"], "A",
            "a file on a record")
    + _rows("bc", ["customer_note", "vendor_note"], "A", "a note")
    + _rows("portal", ["activity.note"], "A", "a note")
    + _rows("portal", ["customer-contact.create", "customer-contact.update",
                       "customer-location.create", "customer-location.update"], "A",
            "contact data; never Auto during the pilot (Auto is hidden)")
    + _rows("portal", ["purchase-order.update", "sales-order.update", "ship-release.create",
                       "ship-release.update"], "B", "quantities and dates")
    + _rows("planner", ["task_create", "task_update"], "A",
            "a task; its due date commits nobody outside")
    + _rows("calendar", ["event_create", "event_update", "event_respond"], "A",
            "B when any attendee is outside the company", "external_attendee")
    + _rows("sharepoint", ["list_item_create", "list_item_update", "file_upload"], "A",
            "reversible")
    + _rows("sharepoint", ["file_replace"], "B", "overwrites a document")
    + _rows("graph", ["send_email", "chat_message", "channel_message"], "A",
            "B when the text holds any date or number token, or any reader is outside the "
            "company; live send stays Vince only regardless", "message_has_figures")
    + _rows("filemaker", ["record_create", "record_edit"], "C",
            "no transport; entry sheet")
    # the nine sheet kinds are the Sage kinds of day_coverage.MINUTES: no write path
    + _rows("sage", ["ar_invoice", "oe_shipment", "ar_receipt", "ap_invoice", "ap_payment",
                     "ic_adjustment", "po_receipt", "gl_journal", "bank_entry"], "C",
            "no write path; entry sheet"))

RULES = ("mc_not_routine", "message_has_figures", "external_attendee")


def _outside(addresses: Iterable[Any], known: Iterable[str]) -> bool:
    domains = {str(d).lower().lstrip("@") for d in known}
    for a in addresses:
        a = a.get("address") if isinstance(a, Mapping) else a
        a = str(a or "").strip().lower()
        if "@" not in a or a.rsplit("@", 1)[1] not in domains:
            return True            # a reader we cannot place inside is outside
    return False


def mc_not_routine(action: Mapping[str, Any], *, item: Optional[Mapping[str, Any]] = None,
                   rules: Optional[Mapping[str, Any]] = None, workflow: str = "",
                   known_domains: Iterable[str] = ()) -> bool:
    """Fires when ``autopilot_write.routine`` gives any reason the approval is not routine;
    without the item and the rules it cannot say, so it fires."""
    if item is None or rules is None:
        return True
    from operating import autopilot_write as aw
    return bool(aw.routine(rules, item, action, workflow, known_domains))


def message_has_figures(action: Mapping[str, Any], *, item: Optional[Mapping[str, Any]] = None,
                        rules: Optional[Mapping[str, Any]] = None, workflow: str = "",
                        known_domains: Iterable[str] = ()) -> bool:
    """Fires when the text holds any date or number token (``autopilot_drafter._tokens``),
    or any reader is outside the company, or the readers are not named (a chat or channel
    with no member list)."""
    from operating import autopilot_drafter as ad
    from operating.autopilot_commitments import _strings
    payload = action.get("payload") or {}
    text = "\n".join(t for t in _strings({k: v for k, v in payload.items()
                                          if k in ("subject", "body", "text")}) if t.strip())
    dates, numbers = ad._tokens(text)
    if dates or numbers:
        return True
    # an email's readers are its recipients; a chat or channel names its members (the
    # action's people: the members and the person the item answers)
    readers = ([*(payload.get("to") or ()), *(payload.get("cc") or ())]
               if action.get("operation") == "send_email" else list(action.get("people") or ()))
    return not readers or _outside(readers, known_domains)


def external_attendee(action: Mapping[str, Any], *, item: Optional[Mapping[str, Any]] = None,
                      rules: Optional[Mapping[str, Any]] = None, workflow: str = "",
                      known_domains: Iterable[str] = ()) -> bool:
    """Fires when any attendee of the event is outside the company (a response to an
    invitation names none: the organiser is not asked)."""
    attendees = (action.get("payload") or {}).get("attendees") or ()
    return _outside([a.get("emailAddress", a) if isinstance(a, Mapping) else a
                     for a in attendees], known_domains) if attendees else False


_RULE_FN: dict[str, Callable[..., bool]] = {
    "mc_not_routine": mc_not_routine, "message_has_figures": message_has_figures,
    "external_attendee": external_attendee}
_TABLE = {(r.system, r.operation): r for r in SEED}


def write_class(action: Mapping[str, Any], *, item: Optional[Mapping[str, Any]] = None,
                rules: Optional[Mapping[str, Any]] = None, workflow: str = "",
                known_domains: Iterable[str] = ()) -> str:
    """'A', 'B' or 'C' for ``action`` (its ``system`` and ``operation``): the table's class,
    raised to B when its rule fires. An operation not in the table raises ``Refused``."""
    row = _TABLE.get((str(action.get("system") or ""), str(action.get("operation") or "")))
    if row is None:
        from operating.prepared_writes import Refused
        raise Refused(["unclassed operation"])
    if row.b_rule and row.cls == "A" and _RULE_FN[row.b_rule](
            action, item=item, rules=rules, workflow=workflow, known_domains=known_domains):
        return "B"
    return row.cls


def seed_sql() -> tuple[str, list[tuple[str, str, str, str, str]]]:
    """The statement and rows that load ``assist.write_classes`` from ``SEED``: an upsert,
    so a changed class or reason replaces the stored one."""
    return ("INSERT INTO assist.write_classes (system, operation, class, b_rule, why) VALUES "
            "(%s, %s, %s, %s, %s) ON CONFLICT (system, operation) DO UPDATE SET class = "
            "EXCLUDED.class, b_rule = EXCLUDED.b_rule, why = EXCLUDED.why",
            [(r.system, r.operation, r.cls, r.b_rule, r.why) for r in SEED])


def migration_sql() -> str:
    """The seed as migration 0063 holds it (a test holds the file equal to this)."""
    def q(v: str) -> str:
        return "'" + v.replace("'", "''") + "'"
    values = ",\n".join(f"  ({q(r.system)}, {q(r.operation)}, {q(r.cls)}, {q(r.b_rule)}, "
                        f"{q(r.why)})" for r in SEED)
    return ("INSERT INTO assist.write_classes (system, operation, class, b_rule, why) VALUES\n"
            + values + "\nON CONFLICT (system, operation) DO UPDATE SET class = EXCLUDED.class, "
            "b_rule = EXCLUDED.b_rule, why = EXCLUDED.why;")


def seed(pg: Any) -> int:
    """Load the table (idempotent); returns the number of rows."""
    sql, rows = seed_sql()
    for row in rows:
        pg.execute(sql, row)
    return len(rows)
