"""Each person's saved templates for documents they write often (a quote, an order
confirmation, a supplier email, a PO note, a form's layout, their house phrasing), filled
from the records when used, and offered again when the same kind of document comes up.

* **A template** (``templates.template``, migration 0022): a name, a document type
  (``KINDS``), a body whose placeholders are bound to record fields, and style notes.
  A placeholder is ``{{field}}``: a field of the form-fill terms (``doc_fill_terms.json``:
  ``po_number``, ``customer_name``, ``ship_date`` ...), ``{{Type.property}}`` (one
  property of the record's type, ``{{SalesOrder.required_date}}``), ``{{items}}`` (the
  record's lines) or ``{{today}}``. Anything else is refused when saved (``placeholder_unknown``),
  never kept to be guessed at later. Each is typed by what it holds (``text``, ``date``,
  ``quantity``, ``lines``).
* **Private by default**: only the person who saved it (``owner``: their principals key)
  lists, uses, edits or deletes it. Its owner chooses who else may use it
  (``shared_with``): any person (``person:<address>``, a colleague's address the people
  exclusion decides and does not exclude) and any role (``role:<role>``, a role of the
  roles file). Those people list and use it, never edit it. A role list that cannot be
  read shows nothing shared through a role (fail closed).
* **Use** (``fill``): the record's facts as the reader reads them (``doc_fill.facts_from``
  through their own tools: the read policy applies there) are planned field by field by the
  form-fill engine (``doc_fill.plan``): a placeholder is filled only when exactly one value
  stands among the properties it may be read from, with that value's citation; otherwise
  it is written ``[check: <label>]`` and listed with why (no record holds it, the records
  disagree, a person's own). Nothing is invented. ``{{items}}``: one line per order line,
  read only when every line was read; else the whole list is ``[check: items]``.
* **Save from a draft** (``propose``): a draft's text with each value that one of its
  record's facts holds exactly replaced by that fact's placeholder, for the person to
  check before saving.
* **Suggestions** (``TemplateStore.suggest``, ``templates_api``): when a person starts a
  document of a type, their own and shared templates of that type, most used first, and
  the layout the Workbench learned from the documents of that kind they sent
  (``operating.doc_style_mine``: learning stays there, not repeated here).

**What is never kept.** Pay and finance (``memory.finance_text``: an amount of money, a
pay or finance word, personal financial content) in a template's name, body or notes is
refused, and no placeholder may name a money field: prices are typed by the person at use.
**The people exclusion** fails closed: unavailable, nothing is written or read; an
excluded person saves nothing; a text naming one (address, object id or name) is refused;
every row is checked again on read (an owner or text excluded since is never shown). A
view-as session reads and writes nothing.

Evidence: isolated-fixture (``operator_api/tests/test_templates.py`` over a scratch
Postgres).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from operating import doc_fill as df

from . import feedback as fb
from . import memory as mem
from . import work_log as wl

# document type -> its words in the app
KINDS: dict[str, str] = {
    "quote": "Quote", "order_confirmation": "Order confirmation",
    "supplier_email": "Supplier email", "customer_email": "Customer email",
    "po_note": "PO note", "form": "Form layout", "phrasing": "House phrasing",
    "price_list": "Price list", "proposal": "Proposal", "spec_sheet": "Spec sheet",
    "coa": "Certificate of analysis", "letter": "Letter", "other": "Other",
}
# the Ask's draft kinds and the learned document kinds, as template types
DRAFT_KIND = {"email": ("supplier_email", "customer_email", "order_confirmation", "po_note",
                        "phrasing")}
MAX_NAME, MAX_BODY, MAX_STYLE = 120, 8000, 2000
MAX_PLACEHOLDERS = 60
MAX_LINES = 50
MAX_LIST = 200
LINE_FIELDS = ("ordered_quantity", "unit_of_measure", "product_code", "product_name")
SPECIAL = {"items": "lines", "today": "date"}

_PLACEHOLDER = re.compile(r"\{\{\s*([^{}\n]{1,80}?)\s*\}\}")
_FIELD = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_TYPE_PROP = re.compile(r"^([A-Z][A-Za-z]{1,40})\.([a-z][a-z0-9_]{1,60})$")
_ROLE = re.compile(r"^[a-z_]{1,60}$")
_ADDRESS = re.compile(r"^[a-z0-9._%+'-]{1,64}@[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+$")
MAX_SHARES = 50
# a property holding money or pay, by its name's words: never a placeholder
_MONEY_NAME = re.compile(r"(?:^|[._])(?:amount|amt|price|cost|total|subtotal|value|rate|margin|"
                         r"balance|tax|freight|charge|fee|pay|salary|wage|discount|credit|"
                         r"debit|money|currency_amount|extended|invoice_value)(?:$|[._s])", re.I)
_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")

REFUSED = {
    "doc_type_unknown": "That document type is not one templates are kept for.",
    "placeholder_unknown": "A placeholder names no record field: {detail}. Use a field from "
                           "the list, or type the text in.",
    "too_many_placeholders": "A template holds at most 60 placeholders.",
    "finance": "Templates never keep pay or finance (amounts, prices, pay words). Take it "
               "out and type it in when you use the template: \"{detail}\"",
    "names_excluded": "This names someone whose data stays out of the Workbench, so it was "
                      "not saved.",
    "secret": "This holds something that looks like a password or key, so it was not saved.",
    "share_unknown": "A template can be shared with a colleague's address or a role: "
                     "{detail} is neither.",
    "share_refused": "That person's data stays out of the Workbench, so nothing is shared "
                     "with them.",
    "name_taken": "You already have a template with that name.",
    "too_long": "That is too long for a template.",
    "empty": "A template needs a name and a body.",
    "view_as": "Templates are not changed while viewing as someone else.",
    "people_exclusion_unavailable": "This is paused: the list of people whose data stays "
                                    "out is not loaded.",
    "not_kept": "Templates are not kept for you.",
    "not_found": "No template of yours by that id.",
    "unavailable": "Templates are not available on this server.",
}


class TemplateRefused(ValueError):
    """``code`` (``REFUSED``) says why; ``detail`` is the person's own words it is about."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail

    @property
    def message(self) -> str:
        return REFUSED[self.code].format(detail=self.detail[:200])


# ---- placeholders --------------------------------------------------------------------------

def field_labels(terms: df.Terms) -> dict[str, str]:
    """Each terms field's name -> a label the form-fill plan reads it under (its first)."""
    out: dict[str, str] = {}
    for label, (name, _props) in terms.fields.items():
        out.setdefault(name, label)
    return out


def _type_of(name: str) -> str:
    if name in SPECIAL:
        return SPECIAL[name]
    prop = name.rsplit(".", 1)[-1]
    if prop.endswith(("_date", "_at", "_on")) or prop in ("date", "eta", "etd"):
        return "date"
    if prop.endswith(("_quantity", "_weight", "_qty")) or prop in ("packages",
                                                                   "units_per_case"):
        return "quantity"
    return "text"


def _plan_label(name: str, labels: Mapping[str, str]) -> str:
    """The label the form-fill plan reads a placeholder under: the terms field's own label,
    or a ``Type.property`` as words (the plan's schema rule matches those)."""
    typed = _TYPE_PROP.match(name)
    if typed:
        return f"{df._words(typed.group(1))} {df._words(typed.group(2))}"
    return labels.get(name) or name.replace("_", " ")


def _label(name: str, labels: Mapping[str, str]) -> str:
    """A placeholder's words in the app (``po_number`` -> ``po number``)."""
    if name in SPECIAL or _TYPE_PROP.match(name):
        return _plan_label(name, labels) if name not in SPECIAL else name
    return name.replace("_", " ")


def placeholders(body: str, terms: df.Terms) -> list[dict[str, str]]:
    """The body's placeholders, each once, in order, with its type and label; an unknown
    or money placeholder refuses the template."""
    labels = field_labels(terms)
    out: dict[str, dict[str, str]] = {}
    unknown = []
    for match in _PLACEHOLDER.finditer(body):
        name = match.group(1).strip()
        if name in out:
            continue
        known = name in SPECIAL or (_FIELD.match(name) and name in labels) \
            or bool(_TYPE_PROP.match(name))
        if not known:
            unknown.append(name)
            continue
        if mem.finance_text(name.replace("_", " ").replace(".", " ")) or _MONEY_NAME.search(name):
            raise TemplateRefused("finance", "{{" + name + "}}")
        out[name] = {"name": name, "type": _type_of(name), "label": _label(name, labels)}
    if unknown:
        raise TemplateRefused("placeholder_unknown", ", ".join(unknown[:5]))
    if len(out) > MAX_PLACEHOLDERS:
        raise TemplateRefused("too_many_placeholders")
    return list(out.values())


def known_fields(terms: df.Terms) -> list[dict[str, str]]:
    """The fields a placeholder may name, for the app's picker."""
    labels = field_labels(terms)
    rows = [{"name": n, "type": _type_of(n), "label": _label(n, labels)} for n in sorted(labels)
            if not mem.finance_text(n.replace("_", " ")) and not _MONEY_NAME.search(n)]
    return rows + [{"name": n, "type": t, "label": n} for n, t in SPECIAL.items()]


def finance_sentence(text: str) -> Optional[str]:
    """The first sentence of ``text`` (placeholders aside) that carries pay or finance."""
    for sentence in _SENTENCE.split(_PLACEHOLDER.sub(" ", str(text or ""))):
        if sentence.strip() and mem.finance_text(sentence):
            return sentence.strip()
    return None


# ---- use -----------------------------------------------------------------------------------

def _check(label: str) -> str:
    return f"[check: {label}]"


def _cite(c: Optional[df.Citation]) -> Optional[dict[str, str]]:
    return c.as_dict() if c else None


def _plan_one(label: str, facts: Sequence[df.Fact], terms: df.Terms) -> df.Filled:
    return df.plan([df.FormField("p", label)], facts, terms)[0]


def fill(body: str, facts: Sequence[df.Fact], terms: df.Terms, *,
         lines: Optional[Sequence[Sequence[df.Fact]]] = None,
         today: Optional[dt.date] = None) -> dict[str, Any]:
    """The template's body filled from ``facts`` (module doc, **Use**): ``text``, and each
    placeholder's ``status`` (``filled`` or ``check``), value, reason and citation.
    ``lines``: each order line's facts, or None when the lines were not all read."""
    out_fields: list[dict[str, Any]] = []
    values: dict[str, str] = {}
    labels = field_labels(terms)
    for ph in placeholders(body, terms):
        name, label = ph["name"], ph["label"]
        row: dict[str, Any] = {"name": name, "label": label, "type": ph["type"],
                               "status": "check", "value": "", "reason": "",
                               "citation": None, "also": []}
        if name == "today":
            if today is None:
                row["reason"] = "no date given"
            else:
                row.update(status="filled", value=today.isoformat(),
                           reason="the day it is made")
        elif name == "items":
            text, reason, cites = _items(lines, terms)
            if text is None:
                row["reason"] = reason
            else:
                row.update(status="filled", value=text, also=cites,
                           reason=reason)
        else:
            got = _plan_one(_plan_label(name, labels), facts, terms)
            if got.status == "filled" and not mem.finance_text(got.after):
                row.update(status="filled", value=got.after, citation=_cite(got.citation),
                           also=[c.as_dict() for c in got.also])
            elif got.status == "filled":
                row["reason"] = "money is never filled into a template"
            else:
                row["reason"] = {"no_fact": "no record read holds it",
                                 "ambiguous": got.reason or "the records disagree",
                                 "person_only": "for you to write",
                                 "unknown_field": "no record field of this record"}.get(
                                     got.status, got.reason or got.status)
        values[name] = row["value"] if row["status"] == "filled" else _check(label)
        out_fields.append(row)
    text = _PLACEHOLDER.sub(lambda m: values.get(m.group(1).strip(), m.group(0)), body)
    return {"text": text, "fields": out_fields,
            "checks": sum(1 for f in out_fields if f["status"] == "check")}


def _items(lines: Optional[Sequence[Sequence[df.Fact]]], terms: df.Terms
           ) -> tuple[Optional[str], str, list[dict[str, str]]]:
    if lines is None:
        return None, "the record's lines could not all be read", []
    if not lines:
        return None, "no lines read for this record", []
    labels = field_labels(terms)
    out, cites, checks = [], [], 0
    for facts in list(lines)[:MAX_LINES]:
        parts = []
        for name in LINE_FIELDS:
            got = _plan_one(labels.get(name, name), facts, terms)
            if got.status == "filled" and not mem.finance_text(got.after):
                parts.append(got.after)
                if got.citation:
                    cites.append(got.citation.as_dict())
            elif name in ("ordered_quantity", "product_code"):
                parts.append(_check(name.replace("_", " ")))
                checks += 1
        out.append("- " + " ".join(parts))
    if len(lines) > MAX_LINES:
        out.append(_check(f"{len(lines) - MAX_LINES} more lines"))
    reason = f"{len(lines)} line{'s' if len(lines) != 1 else ''}" + (
        f", {checks} part{'s' if checks != 1 else ''} to check" if checks else "")
    return "\n".join(out), reason, cites[:MAX_LIST]


def propose(text: str, facts: Sequence[df.Fact], terms: df.Terms) -> dict[str, Any]:
    """A draft's text as a template: each value one terms field's facts hold exactly (one
    value, not disputed, three characters or more, not money) replaced by ``{{field}}``,
    longest first; what was replaced, for the person to check."""
    labels = field_labels(terms)
    found: dict[str, str] = {}
    for name, label in labels.items():
        got = _plan_one(label, facts, terms)
        if got.status == "filled" and len(got.after.strip()) >= 3 \
                and not mem.finance_text(got.after):
            found.setdefault(got.after.strip(), name)
    body, used = str(text or ""), []
    for value in sorted(found, key=len, reverse=True):
        pattern = re.compile(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])")
        if pattern.search(body):
            body = pattern.sub("{{" + found[value] + "}}", body)
            used.append({"name": found[value], "value": value})
    return {"body": body, "replaced": used}


# ---- the store -----------------------------------------------------------------------------

_COLUMNS = ("id", "owner", "name", "doc_type", "body", "placeholders", "style_notes",
            "shared_with", "source_ref", "created_at", "updated_at", "uses", "last_used_at")


class TemplateStore:
    """Templates over the backend's Postgres (``connect``: a context manager giving a
    connection). ``rules``: the memory store, whose people exclusion (with the excluded
    people's names) decides every write and read here as it does there."""

    def __init__(self, connect: Callable[[], Any], rules: Any,
                 clock: Callable[[], dt.datetime] = mem._now) -> None:
        self.connect, self.rules, self.clock = connect, rules, clock

    @classmethod
    def for_backend(cls, backend: Any) -> Optional["TemplateStore"]:
        memory = getattr(backend, "memory", None)
        pool = getattr(memory, "pool", None)
        return cls(pool.connection, memory) if pool is not None else None

    # -- rules --

    def _guard(self, reader: Any, *, write: bool) -> Any:
        if getattr(getattr(reader, "session", None), "view_as", False):
            if write:
                raise TemplateRefused("view_as")
            return None
        try:
            guard = self.rules._guard()
        except Exception:  # noqa: BLE001 - undecidable: closed
            guard = None
        if guard is None:
            if write:
                raise TemplateRefused("people_exclusion_unavailable")
            return None
        if wl._actor_excluded(guard, reader):
            if write:
                raise TemplateRefused("not_kept")
            return None
        return guard

    @staticmethod
    def _text(guard: Any, text: str, limit: int) -> str:
        text = str(text or "").strip()
        if len(text) > limit:
            raise TemplateRefused("too_long")
        if not text:
            return ""
        said = finance_sentence(text)
        if said:
            raise TemplateRefused("finance", said)
        cleaned = fb.clean(text, guard, limit + 1) or ""
        if fb.WITHHELD in cleaned or guard.names_one(cleaned):
            raise TemplateRefused("names_excluded")
        if cleaned != text:
            raise TemplateRefused("secret")
        return text

    @staticmethod
    def _shares(guard: Any, reader: Any, shared_with: Iterable[str], roles: set[str]
                ) -> list[str]:
        """Who a template is shared with, as kept: each a role there is or a colleague's
        address (in the tenant's mail domains) the exclusion decides and does not exclude;
        anything else refuses the save, never dropped in silence."""
        out: list[str] = []
        own = str(getattr(reader, "email", "") or "").lower()
        for entry in shared_with:
            entry = str(entry or "").strip()
            kind, _, value = entry.partition(":")
            value = value.strip().lower()
            if kind == "role" and _ROLE.match(value) and value in roles:
                key = f"role:{value}"
            elif kind == "person" and _ADDRESS.match(value):
                domains = {d.lower() for d in guard.known_domains()}
                if value.rsplit("@", 1)[1] not in domains:
                    raise TemplateRefused("share_unknown", entry[:80])
                if wl._person_excluded(guard, value) or guard.names_one(value):
                    raise TemplateRefused("share_refused")
                if value == own:
                    continue
                key = f"person:{value}"
            else:
                raise TemplateRefused("share_unknown", entry[:80] or "an empty entry")
            if key not in out:
                out.append(key)
        if len(out) > MAX_SHARES:
            raise TemplateRefused("share_unknown", f"more than {MAX_SHARES} people and roles")
        return out

    @staticmethod
    def _visible(guard: Any, row: Mapping[str, Any]) -> bool:
        """Still clear of the exclusion as it is now: its owner and every text."""
        if wl._actor_excluded(guard, mem.KeyReader(row["owner"])):
            return False
        texts = [row.get("name"), row.get("body"), row.get("style_notes")]
        if guard.names_one(*texts):
            return False
        return all(fb.WITHHELD not in (fb.clean(t, guard, len(t) + 1) or "")
                   for t in texts if t)

    # -- writes --

    def save(self, reader: Any, *, name: str, doc_type: str, body: str,
             style_notes: str = "", shared_with: Iterable[str] = (),
             source_ref: Optional[str] = None, template_id: Optional[int] = None,
             shareable: Optional[Iterable[str]] = None, terms: Optional[df.Terms] = None
             ) -> dict[str, Any]:
        """Save a new template, or change one of the reader's own (``template_id``).
        ``shared_with``: who else may use it (``person:<address>``, ``role:<role>``);
        ``shareable``: the roles there are (None or empty: no role can be named)."""
        guard = self._guard(reader, write=True)
        if doc_type not in KINDS:
            raise TemplateRefused("doc_type_unknown")
        name = self._text(guard, name, MAX_NAME)
        body = self._text(guard, body, MAX_BODY)
        style_notes = self._text(guard, style_notes, MAX_STYLE)
        if not name or not body:
            raise TemplateRefused("empty")
        found = placeholders(body, terms or df.load_terms())
        shares = self._shares(guard, reader, shared_with, set(shareable or ()))
        ref = fb.clean_handle(source_ref, guard) if source_ref else None
        if source_ref and ref is None:
            raise TemplateRefused("names_excluded")
        now = self.clock()
        try:
            return self._write(reader, template_id, name, doc_type, body, found, style_notes,
                               shares, ref, now)
        except Exception as error:  # noqa: BLE001 - only a same-name race is ours to word
            if type(error).__name__ == "UniqueViolation":
                raise TemplateRefused("name_taken") from None
            raise

    def _write(self, reader: Any, template_id: Optional[int], name: str, doc_type: str,
               body: str, found: list[dict[str, str]], style_notes: str, shares: list[str],
               ref: Optional[str], now: dt.datetime) -> dict[str, Any]:
        with self.connect() as conn:
            taken = conn.execute(
                "SELECT id FROM templates.template WHERE owner = %s AND lower(name) = lower(%s) "
                "AND deleted_at IS NULL AND id IS DISTINCT FROM %s",
                (reader.key, name, template_id)).fetchone()
            if taken:
                raise TemplateRefused("name_taken")
            if template_id is None:
                row = conn.execute(
                    "INSERT INTO templates.template (owner, name, doc_type, body, placeholders, "
                    "style_notes, shared_with, source_ref, created_at, updated_at) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING " + ", ".join(_COLUMNS),
                    (reader.key, name, doc_type, body, json.dumps(found), style_notes,
                     shares, (ref or None) and ref[:400], now, now)).fetchone()
            else:
                row = conn.execute(
                    "UPDATE templates.template SET name = %s, doc_type = %s, body = %s, "
                    "placeholders = %s, style_notes = %s, shared_with = %s, updated_at = %s "
                    "WHERE id = %s AND owner = %s AND deleted_at IS NULL RETURNING "
                    + ", ".join(_COLUMNS),
                    (name, doc_type, body, json.dumps(found), style_notes, shares, now,
                     template_id, reader.key)).fetchone()
                if row is None:
                    raise TemplateRefused("not_found")
            conn.commit()
        return self._public(dict(zip(_COLUMNS, row)), reader)

    def delete(self, reader: Any, template_id: int) -> bool:
        """Delete one of the reader's own templates (another's reads as not found)."""
        if self._guard(reader, write=False) is None:
            return False
        with self.connect() as conn:
            row = conn.execute(
                "UPDATE templates.template SET deleted_at = %s WHERE id = %s AND owner = %s "
                "AND deleted_at IS NULL RETURNING id",
                (self.clock(), template_id, reader.key)).fetchone()
            conn.commit()
        return row is not None

    def used(self, reader: Any, template_id: int, *, roles: Optional[Iterable[str]] = None
             ) -> None:
        """Count one use of a template the reader may use (the suggestions put the most
        used first)."""
        if self._guard(reader, write=False) is None:
            return
        with self.connect() as conn:
            conn.execute("UPDATE templates.template SET uses = uses + 1, last_used_at = %s "
                         "WHERE id = %s AND deleted_at IS NULL AND (owner = %s OR "
                         "shared_with && %s)", (self.clock(), template_id, reader.key,
                                                _keys(reader, roles)))
            conn.commit()

    def purge_excluded(self) -> Optional[int]:
        """Delete every template the exclusion now covers (its owner or its text) and drop
        every share with a person it covers; the number deleted. None: the list cannot be
        read, nothing is done."""
        try:
            guard = self.rules._guard()
        except Exception:  # noqa: BLE001
            guard = None
        if guard is None:
            return None
        gone = 0
        with self.connect() as conn:
            for row in conn.execute("SELECT " + ", ".join(_COLUMNS)
                                    + " FROM templates.template").fetchall():
                row = dict(zip(_COLUMNS, row))
                if not self._visible(guard, row):
                    conn.execute("DELETE FROM templates.template WHERE id = %s", (row["id"],))
                    gone += 1
                    continue
                kept = [e for e in row["shared_with"] or () if not (
                    e.startswith("person:") and wl._person_excluded(guard, e[7:]))]
                if kept != list(row["shared_with"] or ()):
                    conn.execute("UPDATE templates.template SET shared_with = %s WHERE id = %s",
                                 (kept, row["id"]))
            conn.commit()
        return gone

    # -- reads --

    def list(self, reader: Any, *, roles: Optional[Iterable[str]] = None,
             doc_type: Optional[str] = None) -> Optional[list[dict[str, Any]]]:
        """The reader's own templates and those shared with a role of theirs (``roles``:
        None, no shared ones), most used first; None: nothing may be read now."""
        guard = self._guard(reader, write=False)
        if guard is None:
            return None
        if doc_type is not None and doc_type not in KINDS:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT " + ", ".join(_COLUMNS) + " FROM templates.template WHERE "
                "deleted_at IS NULL AND (owner = %s OR shared_with && %s) AND "
                "(%s::text IS NULL OR doc_type = %s) ORDER BY (owner = %s) DESC, uses DESC, "
                "updated_at DESC LIMIT %s",
                (reader.key, _keys(reader, roles), doc_type, doc_type, reader.key,
                 MAX_LIST)).fetchall()
        out = []
        for r in (dict(zip(_COLUMNS, x)) for x in rows):
            if not self._visible(guard, r):
                continue
            # a person excluded since it was shared: never shown, never a reader of it
            r["shared_with"] = [e for e in r.get("shared_with") or ()
                                if not (e.startswith("person:")
                                        and wl._person_excluded(guard, e[7:]))]
            out.append(self._public(r, reader))
        return out

    def get(self, reader: Any, template_id: int, *, roles: Optional[Iterable[str]] = None
            ) -> Optional[dict[str, Any]]:
        """One template the reader may use (theirs, or shared with them or a role of
        theirs), checked as ``list`` checks; None otherwise."""
        guard = self._guard(reader, write=False)
        if guard is None:
            return None
        with self.connect() as conn:
            got = conn.execute(
                "SELECT " + ", ".join(_COLUMNS) + " FROM templates.template WHERE id = %s AND "
                "deleted_at IS NULL AND (owner = %s OR shared_with && %s)",
                (template_id, reader.key, _keys(reader, roles))).fetchone()
        if got is None:
            return None
        row = dict(zip(_COLUMNS, got))
        if not self._visible(guard, row):
            return None
        row["shared_with"] = [e for e in row.get("shared_with") or ()
                              if not (e.startswith("person:")
                                      and wl._person_excluded(guard, e[7:]))]
        return self._public(row, reader)

    @staticmethod
    def _public(row: Mapping[str, Any], reader: Any) -> dict[str, Any]:
        out = {k: row[k] for k in _COLUMNS if k not in ("owner", "source_ref")}
        out["mine"] = row["owner"] == reader.key
        out["placeholders"] = row["placeholders"] if isinstance(row["placeholders"], list) \
            else json.loads(row["placeholders"] or "[]")
        for k in ("created_at", "updated_at", "last_used_at"):
            out[k] = out[k].isoformat() if out[k] else None
        if not out["mine"]:
            # who else it is shared with is its owner's business: a reader it is shared
            # with sees only that it is shared with them
            out["source_ref"], out["shared_with"] = None, []
        else:
            out["source_ref"], out["shared_with"] = row.get("source_ref"), list(
                row.get("shared_with") or [])
        return out


def _keys(reader: Any, roles: Optional[Iterable[str]]) -> list[str]:
    """The share entries that name this reader: their address and their roles."""
    keys = sorted(f"role:{r}" for r in (roles or ()) if _ROLE.match(str(r)))
    email = str(getattr(reader, "email", "") or "").strip().lower()
    return keys + ([f"person:{email}"] if _ADDRESS.match(email) else [])


def template_request(text: str) -> Optional[dict[str, str]]:
    """The one-box's "use my <name> template for <record>": the name and the words after
    for (empty when none). None: not such a line."""
    match = _USE.match(" ".join(str(text or "").split()))
    if not match:
        return None
    return {"name": match.group("name").strip(" '\"").strip(),
            "for": (match.group("for") or "").strip()}


_USE = re.compile(
    r"^(?:please\s+)?(?:use|apply|start from|draft (?:it |this )?(?:from|with|in)|"
    r"write (?:it |this )?(?:from|with|in)|fill (?:in )?)\s+(?:my|our|the)?\s*"
    r"(?P<name>[^{}]{1,80}?)\s+template\b(?:\s+(?:for|on|with|to)\s+(?P<for>.{1,300}))?"
    r"\s*[.!]?$", re.I)


def named(rows: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    """The templates ``name`` means: the one with that name, else those of that document
    type (``quote`` or its words), own first."""
    key = " ".join(name.lower().split())
    exact = [r for r in rows if " ".join(str(r["name"]).lower().split()) == key]
    if exact:
        return exact[:1]
    kind = next((k for k, words in KINDS.items()
                 if key in (k.replace("_", " "), words.lower())), None)
    of_kind = [r for r in rows if r["doc_type"] == kind] if kind else []
    mine = [r for r in of_kind if r.get("mine")]
    return mine or of_kind


__all__ = ["KINDS", "REFUSED", "TemplateRefused", "TemplateStore", "fill", "known_fields",
           "named", "placeholders", "propose", "template_request"]
