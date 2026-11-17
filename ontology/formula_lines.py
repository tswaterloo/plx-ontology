"""Formula lines as links: a product -> each raw material its formula sheets list.

The Portal's formula lines (FileMaker ``formula_item``, ``app_formulation_ingredient``)
are not served, so the formula sheets are the only source (``operating.business_documents``
``formula_lines``, the per-product read view, and the product page's Formula section).
This step writes what those sheets say once for every product, as links the graph and
every reader can follow: ``Product.formula_line`` / ``FgItem.formula_line`` from the
product to the raw-material item a line names, and from that item on its supplier items
(``SupplierItem.for_item``, served already): Product -> FormulaLine -> raw-material item
-> supplier item. A FormulaLine is the link itself (one per sheet line), not an object: a
build step writes links only (``ontology.step_migrate``), and every field of the line
rides on the link's ``reason``, cited to the sheet.

One ``post_project`` step (``operating/build_steps.py`` ``with_formula_lines``; steps run
in name order, so after ``text_index`` and the sheets' text is there), link id prefix
``PREFIX``, self-contained: it reads the current store only, removes the rows it wrote
before and writes them again, so it runs in a full build or alone on a store in place (the
step migration; ``ontology.pg_step_inplace`` reads Postgres in place: every read is done
before the first write).

The same rules as the view, for every product at once:

* Sheets: current ``Attachment``, ``SharePointFile`` and ``ContainerMember`` records whose
  name or path (a file in a formulas folder) holds a formula number (``F-YY-NNNN``) or a formula
  word (``formula*``, ``sample``) as a whole word: one read of the name cells with three
  exact patterns, each candidate then tested by the word and number rules in code (a
  pattern alone never decides). Removed records are left out; a sheet without captured
  text, or whose text has no line, is counted.
* People first, fail closed: a sheet, product or item whose audience the people exclusion
  does not clear is never read into a link (``excluded_person``); without a usable
  exclusion nothing is written (``exclusion_unavailable``).
* A sheet is a product's when one formula-named name or path of it holds at least
  ``FORMULA_NAME_WORDS`` of the product's name words (all, when fewer), and its stated ``CODE`` is one of the product's
  codes (``basis: code``) or, stating none, its ``NAME`` has the product's name words
  (``basis: name``). A product without name words gets none.
* Each line (``ontology.composition_lines.read_sheet``) whose part code is exactly one or
  more items' identifier (``Product`` / ``FgItem``, their settled ``Product.same_record_as``
  twins joined) links to the least id of that group, as the view names it; an unresolved
  part is counted, never linked.
* Tier: ``corroborated`` on a sheet that states the product's code, ``lead`` on a sheet
  matched by name only. The link cites the sheet's version (``evidence_handles``: a reader
  who may not read the sheet never sees the line), the product's code or name value and the
  item's identifier; it carries the union of their markings and the text's.

Stdlib only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology import mentions

STEP = "with_formula_lines"
PREFIX = "formula:"
FROM_TYPES = ("FgItem", "Product")
LINK_TYPES = {t: f"{t}.formula_line" for t in FROM_TYPES}
ITEM_TYPES = ("Product", "FgItem")
ITEM_TWINS = "Product.same_record_as"
DOC_TYPES = ("Attachment", "ContainerMember", "SharePointFile")
NAME_PROPS = ("name", "path")
CODE_PROPS = ("code", "erp_code", "item_code", "no", "number", "lot_number", "lot_text")
TEXT_PATH = "row.content_text"
FORMULA_WORDS = ("formula*", "sample")
FORMULA_NAME_WORDS = 3
CODE_TIER, NAME_TIER = "corroborated", "lead"
BATCH = 2000
PART = 400
MAX_CHARS = 400_000
_FORMULA_NUMBER = re.compile(r"(?i)\bF-\d{2}-\d{4}\b")
# the three name patterns the candidate read uses (exact rules follow in code)
NAME_PATTERNS = ("%formula%", "%sample%", "%f-__-____%")
_NAME_STOP = frozenset({"the", "and", "for", "with", "of", "a", "an", "on", "in", "to"})
_SETTLED = ("verified", "reviewed", "corroborated")


def _end(prefix: str) -> str:
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _digest(*parts: Any) -> str:
    return sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()[:32]


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _q(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    return mentions._query(conn, sql, list(params)).fetchall()


def _parts(ids: Iterable[str]) -> Iterable[str]:
    want = sorted(set(ids))
    for start in range(0, len(want), PART):
        yield json.dumps(want[start:start + PART])


def norm_code(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def name_words(text: Any) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if w not in _NAME_STOP and len(w) > 1]


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def formula_named(name: str) -> bool:
    """A sheet's name: a formula number or a formula word, whole (the view's rule)."""
    if _FORMULA_NUMBER.search(name):
        return True
    toks = _tokens(name)
    return any(t.startswith("formula") for t in toks) or "sample" in toks


def declared(links: Mapping[str, Any]) -> bool:
    """Whether the schema declares this step's link types (a step binding)."""
    return any(b.get("kind") == mentions.BINDING_KIND and b.get("written_by_step") == STEP
               for lt in links.values() for b in (lt.get("bindings") or ()))


# --------------------------------------------------------------------------- reads

def read_sheets(conn: Any) -> dict[str, dict[str, Any]]:
    """Current sheet records by id: type, display name (``name`` before ``path``'s last
    part) and every name or path text that is formula-named (``matched``: the view tests
    a product's name words against the same text, so a file under a formulas folder is a
    sheet), not removed."""
    from ontology import store as ostore
    hits: dict[str, dict[str, Any]] = {}
    where_type = " OR ".join("object_id LIKE ?" for _ in DOC_TYPES)
    where_name = " OR ".join("lower(value) LIKE ?" for _ in NAME_PATTERNS)
    rows = _q(conn, "SELECT object_id, property, value, l1_handle FROM object_values "
                    "WHERE superseded_at IS NULL AND property IN (?, ?) "
                    f"AND ({where_type}) AND ({where_name})",
              (*NAME_PROPS, *(t + ":%" for t in DOC_TYPES), *NAME_PATTERNS))
    for r in sorted(rows, key=lambda r: (r["object_id"], r["property"] != "name",
                                          str(r["value"]))):
        oid, prop = r["object_id"], r["property"]
        text = str(r["value"] or "")
        if not formula_named(text):
            continue
        hit = hits.setdefault(oid, {"type": oid.split(":", 1)[0], "name": None,
                                    "matched": []})
        hit["matched"].append(text)
        if hit["name"] is None:
            hit["name"] = text.rsplit("/", 1)[-1] if prop == "path" else text
    removed: set[str] = set()
    for part in _parts(hits):
        removed.update(r[0] for r in conn.execute(
            "SELECT o.object_id FROM objects o WHERE o.object_id IN "
            "(SELECT value FROM json_each(?)) AND " + ostore.removed_at_sql("o.object_id"),
            (part,)))
    return {o: h for o, h in hits.items() if o not in removed}


def read_records(conn: Any, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Each record's audience, current version handle, markings and recorded time."""
    out: dict[str, dict[str, Any]] = {}
    for part in _parts(ids):
        for r in _q(conn, "SELECT o.object_id, o.audience_key, v.l1_handle, v.markings, "
                          "v.recorded_at FROM objects o JOIN object_versions v "
                          "ON v.object_id = o.object_id AND v.superseded_at IS NULL "
                          "WHERE o.object_id IN (SELECT value FROM json_each(?))", (part,)):
            out[r["object_id"]] = {"audience": r["audience_key"], "handle": r["l1_handle"],
                                   "markings": r["markings"] or "[]",
                                   "recorded_at": r["recorded_at"]}
    return out


def read_texts(conn: Any, handles: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    """Each sheet's captured text (``operating.document_tables.texts``' choice: the
    version's whole field when there is one, else its pages in order) and the union of
    the text fields' markings; ``handles``: sheet id -> its version handle."""
    from ontology.text_index import chunk_text
    found: dict[str, dict[tuple[str, str], list[tuple[int, str]]]] = defaultdict(
        lambda: defaultdict(list))
    marks: dict[str, dict[str, str]] = defaultdict(dict)
    for part in _parts(handles):
        for r in _q(conn, "SELECT f.object_id, f.citation, f.markings, k.start, k.body "
                          "FROM text_fields f JOIN text_chunks k ON k.field_id = f.field_id "
                          "WHERE f.object_id IN (SELECT value FROM json_each(?)) "
                          "AND f.field_path = ?", (part, TEXT_PATH)):
            oid, cite = r["object_id"], r["citation"]
            handle = handles[oid]
            if cite != handle and _page_of(cite, handle) is None:
                continue   # another version's text
            found[oid][(TEXT_PATH, cite)].append((int(r["start"]), chunk_text(r["body"])))
            marks[oid][cite] = r["markings"] or "[]"
    out: dict[str, tuple[str, str]] = {}
    for oid, fields in found.items():
        handle = handles[oid]
        whole = {k: v for k, v in fields.items() if k[1] == handle}
        chosen = sorted(whole.items()) if whole else sorted(
            fields.items(), key=lambda kv: _page_of(kv[0][1], handle) or 0)
        pieces, total = [], 0
        for _key, parts in chosen:
            end = 0
            for start, body in sorted(parts):
                if start + len(body) <= end:
                    continue
                piece = body[max(0, end - start):]
                pieces.append(piece)
                total += len(piece)
                end = start + len(body)
                if total >= MAX_CHARS:
                    break
            pieces.append("\n")
            if total >= MAX_CHARS:
                break
        used = [marks[oid][k[1]] for k, _ in chosen]
        out[oid] = ("".join(pieces).rstrip("\n"), mentions._marks(*used))
    return out


def _page_of(citation: str, handle: str) -> int | None:
    prefix = handle + "#page="
    if not citation.startswith(prefix):
        return None
    tail = citation[len(prefix):]
    return int(tail) if tail.isdigit() else None


def read_products(conn: Any) -> dict[str, dict[str, Any]]:
    """Every current product and finished-good item: its codes (as the view reads them:
    four or more letters and digits, never NA/N/A/NONE) and its name, each with its row."""
    out: dict[str, dict[str, Any]] = defaultdict(lambda: {"codes": [], "name": None})
    where_type = " OR ".join("object_id LIKE ?" for _ in FROM_TYPES)
    props = (*CODE_PROPS, "name")
    rows = _q(conn, "SELECT object_id, property, value, markings, l1_handle FROM object_values "
                    "WHERE superseded_at IS NULL AND property IN (SELECT value FROM "
                    f"json_each(?)) AND ({where_type})",
              (json.dumps(list(props)), *(t + ":%" for t in FROM_TYPES)))
    for r in sorted(rows, key=lambda r: (r["object_id"], r["property"], str(r["value"]),
                                          r["l1_handle"])):
        if r["object_id"].split(":", 1)[0] not in FROM_TYPES:
            continue
        p = out[r["object_id"]]
        row = {"value": r["value"], "markings": r["markings"] or "[]",
               "handle": r["l1_handle"]}
        if r["property"] == "name":
            if p["name"] is None:
                p["name"] = row
            continue
        v = str(r["value"] or "").strip()
        if len(norm_code(v)) >= 4 and v.upper() not in ("NA", "N/A", "NONE"):
            p["codes"].append(row)
    return dict(out)


def read_parts(conn: Any, parts: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """Part code (as the sheet prints it) -> the item identifiers it exactly is."""
    by_token: dict[str, list[str]] = defaultdict(list)
    for part in parts:
        token = re.sub(r"\s+", "", str(part or "")).upper()
        if 3 <= len(token) <= 60:
            by_token[token].append(part)
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in _parts(by_token):
        for r in _q(conn, "SELECT object_id, normalized, markings, l1_handle FROM identifiers "
                          "WHERE normalized IN (SELECT value FROM json_each(?)) "
                          "AND superseded_at IS NULL", (chunk,)):
            if r["object_id"].split(":", 1)[0] not in ITEM_TYPES:
                continue
            for raw in by_token[r["normalized"]]:
                out[raw].append({"object_id": r["object_id"], "markings": r["markings"] or "[]",
                                 "handle": r["l1_handle"]})
    return out


def read_twins(conn: Any, ids: Iterable[str], cap: int = 50) -> dict[str, set[str]]:
    """Each id's settled ``Product.same_record_as`` group (``document_tables.cluster``),
    walked a frontier at a time."""
    groups: dict[str, set[str]] = {}
    edges: dict[str, set[str]] = defaultdict(set)
    seen: set[str] = set()
    todo = set(ids)
    while todo:
        seen |= todo
        nxt: set[str] = set()
        for part in _parts(todo):
            for a, b in conn.execute(
                    "SELECT from_object, to_object FROM link_rows WHERE link_type = ? AND "
                    "superseded_at IS NULL AND tier IN (?, ?, ?) AND (from_object IN "
                    "(SELECT value FROM json_each(?)) OR to_object IN (SELECT value FROM "
                    "json_each(?)))", (ITEM_TWINS, *_SETTLED, part, part)):
                edges[a].add(b)
                edges[b].add(a)
                nxt |= {a, b}
        todo = nxt - seen
    for oid in ids:
        group, stack = {oid}, [oid]
        while stack and len(group) < cap:
            for other in sorted(edges.get(stack.pop(), ())):
                if other not in group:
                    group.add(other)
                    stack.append(other)
        groups[oid] = group
    return groups


# --------------------------------------------------------------------------- links

def formula_links(conn: Any, guard: Any) -> tuple[list[dict[str, Any]], Counter]:
    """The step's link rows and counts (module doc)."""
    from ontology.composition_lines import read_sheet
    counts: Counter = Counter()
    sheets = read_sheets(conn)
    counts["sheets_named"] = len(sheets)
    products = read_products(conn)
    docs = read_records(conn, sheets)
    clear: dict[str, bool] = {}

    def cleared(oid: str, info: Mapping[str, Any] | None) -> bool:
        if oid not in clear:
            try:
                clear[oid] = info is not None and bool(guard.unit_clear(info["audience"]))
            except Exception:  # noqa: BLE001 - undecidable: never read
                clear[oid] = False
        return clear[oid]
    readable = {}
    for oid in sorted(sheets):
        if oid not in docs:
            counts["sheet_no_version"] += 1
        elif not cleared(oid, docs[oid]):
            counts["excluded_person"] += 1
        else:
            readable[oid] = docs[oid]["handle"]
    texts = read_texts(conn, readable)
    # the products a sheet may be: by code and by name words
    by_code: dict[str, set[str]] = defaultdict(set)
    words_of: dict[str, list[str]] = {}
    for pid, p in products.items():
        words_of[pid] = list(dict.fromkeys(name_words((p["name"] or {}).get("value"))))
        for c in p["codes"]:
            by_code[norm_code(c["value"])].add(pid)
    by_name: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for pid, p in products.items():
        if p["name"] is not None:
            by_name[tuple(name_words(p["name"]["value"]))].add(pid)
    read: list[tuple[str, dict[str, Any], list[tuple[str, str, dict[str, Any]]]]] = []
    for oid in sorted(readable):
        if oid not in texts or not texts[oid][0]:
            counts["text_not_captured"] += 1
            continue
        sheet = read_sheet(texts[oid][0])
        if not sheet["lines"]:
            counts["sheet_no_lines_read"] += 1
            continue
        matched_toks = [_tokens(t) for t in sheets[oid]["matched"]]
        stated = (sheet["header"].get("product_code") or {}).get("value")
        if stated is not None:
            cands, basis = by_code.get(norm_code(stated), set()), "code"
        else:
            said = (sheet["header"].get("name") or {}).get("value")
            cands = by_name.get(tuple(name_words(said)), set()) if said else set()
            basis = "name"
        matched = []
        for pid in sorted(cands):
            words = words_of[pid]
            need = min(FORMULA_NAME_WORDS, len(words))
            if not words or not any(sum(w in toks for w in words) >= need
                                    for toks in matched_toks):
                continue
            p = products[pid]
            row = (next(c for c in p["codes"] if norm_code(c["value"]) == norm_code(stated))
                   if basis == "code" else p["name"])
            matched.append((pid, basis, row))
        if not matched:
            counts["sheet_other_product" if stated is not None else "sheet_product_not_stated"] += 1
            continue
        counts["sheet_" + sheet["status"]] += 1
        read.append((oid, sheet, matched))
    parts = read_parts(conn, {ln["part"] for _o, s, _m in read for ln in s["lines"]})
    twins = read_twins(conn, {i["object_id"] for found in parts.values() for i in found})
    ends = read_records(conn, {pid for _o, _s, m in read for pid, _b, _r in m}
                        | {i for g in twins.values() for i in g})
    out: list[dict[str, Any]] = []
    for oid, sheet, matched in read:
        doc = docs[oid]
        formula = (sheet["header"].get("formula_code") or {}).get("value")
        stated = (sheet["header"].get("product_code") or {}).get("value")  # this sheet's own code
        for line in sheet["lines"]:
            found = parts.get(line["part"], [])
            members = sorted({m for i in found for m in twins.get(i["object_id"], {i["object_id"]})})
            if not members:
                counts["part_unresolved"] += len(matched)
                continue
            item = members[0]
            ident = sorted(found, key=lambda i: (i["object_id"] != item, i["object_id"],
                                                i["handle"]))[0]
            for pid, basis, prow in matched:
                if not (cleared(pid, ends.get(pid)) and cleared(item, ends.get(item))):
                    counts["excluded_person"] += 1
                    continue
                if pid == item:
                    counts["part_is_the_product"] += 1
                    continue
                counts["part_resolved"] += 1
                line_of = {"feature": "formula_line", "agrees": True, "sheet": oid, "sheet_name": sheets[oid]["name"][:160],
                          "basis": basis, "formula_code": formula,
                          "sheet_status": sheet["status"],
                          "share_total": sheet["share_total"],
                          "sheet_lines": len(sheet["lines"]), "line": line["line"],
                          "no": line["no"], "part": line["part"],
                          "description": line["description"],
                          "share_percent": line["share"], "quantity": line["quantity"],
                          "unit": line["unit"], "check": line["check"],
                          "item_group": members[:50],
                          "citations": {"sheet": doc["handle"], "product": prow["handle"],
                                        "item": ident["handle"]}}
                # the store's reason is a feature list: the line first, then the evidence
                reason = [line_of,
                          {"feature": "product_" + basis, "agrees": True, "level": "exact",
                           "value": stated if basis == "code" else prow["value"]},
                          {"feature": "part_identifier", "agrees": True, "level": "exact",
                           "value": line["part"], "items": len(members)}]
                handles = list(dict.fromkeys([doc["handle"], prow["handle"], ident["handle"]]))
                out.append({
                    "link_id": PREFIX + _digest(pid, oid, line["line"], line["part"]),
                    "link_type": LINK_TYPES[pid.split(":", 1)[0]], "from_object": pid,
                    "to_object": item, "tier": CODE_TIER if basis == "code" else NAME_TIER,
                    "binding_kind": mentions.BINDING_KIND, "link_contract": None,
                    "reason": _canon(reason), "why_not": _canon(
                        [] if basis == "code" else
                        [{"feature": "product_code", "agrees": None,
                          "detail": "the sheet states no product code: matched by its "
                                    "NAME; a person confirms"}]),
                    "evidence_handles": _canon(handles),
                    "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
                    "markings": mentions._marks(doc["markings"], texts[oid][1],
                                                prow["markings"], ident["markings"]),
                    "recorded_at": max(doc["recorded_at"], ends[pid]["recorded_at"],
                                       ends[item]["recorded_at"]),
                    "superseded_at": None})
    # one row per link id (a sheet listing one part on two rows of one text line)
    uniq = {r["link_id"]: r for r in out}
    counts["duplicate_line"] += len(out) - len(uniq)
    return [uniq[k] for k in sorted(uniq)], counts


def step_for(load_guard: Callable[[], Any]) -> Any:
    """The ``with_formula_lines`` step of a build; ``load_guard()``: the people exclusion
    bound to what the captures recorded (``operating/build_steps.py`` passes the readers'
    ``operating.chunking.permissions.load_exclusion`` of the state directory), None when
    it is unavailable (nothing is written)."""
    def step(builder: Any, projector: Any) -> dict[str, Any]:
        try:
            guard = load_guard()
        except Exception:  # noqa: BLE001 - an unreadable capture: nothing is decided by it
            guard = None
        if guard is not None and getattr(guard, "unavailable", None):
            guard = None
        return formula_lines_step(builder, projector, guard=guard)
    return step


def formula_lines_step(builder: Any, projector: Any, *, guard: Any) -> dict[str, Any]:
    """The ``with_formula_lines`` step (module doc); ``guard`` None: nothing is written."""
    conn = builder.connection
    rows: list[dict[str, Any]] = []
    counts: Counter = Counter()
    if not declared(getattr(projector, "links", {}) or {}):
        pass
    elif guard is None:
        counts["exclusion_unavailable"] = 1
    else:
        rows, counts = formula_links(conn, guard)   # every read before the first write
    conn.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?",
                 (PREFIX, _end(PREFIX)))
    for start in range(0, len(rows), BATCH):
        builder.add("links", rows[start:start + BATCH])
    return {"counts": dict(counts), "link_tiers": dict(Counter(r["tier"] for r in rows))}


__all__ = ["FORMULA_NAME_WORDS", "LINK_TYPES", "PREFIX", "STEP", "declared",
           "formula_links", "formula_lines_step", "formula_named", "read_sheets", "step_for"]
