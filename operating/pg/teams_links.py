"""Record and party identifier linker for Teams messages (ASK-GENIE §20 #8).

Resolves business identifiers written in a Teams message's unmarked text (``said``,
``subject``, ``chat_topic``) into the row's ``customers``, ``suppliers`` and ``records``
handle arrays, using the same two general extractors and conservative resolution rules as
mail (one definition, imported):

* ``ontology.mentions.extract`` (prefixed/formatted numbers: PO, SO, batch, formula);
* ``operating.pg.bare_mentions.candidates`` (bare alphanumeric customer, item and order
  codes, resolved via ``ontology.identifiers`` with the same ``MAX_HOLDERS``, confirmed
  ``same_record`` twins and one-per-system rules as ``bare_mentions.resolve``);
* existing unmarked, non-contradicted ``mentions_batch`` links in ``ontology.links``
  resolved to ``biz.batches`` handles (``business_comms.RECORD_VIEWS`` omits ``batches``).

Runs right after ``teams_text.apply`` and ``teams_members.apply`` and BEFORE
``message_parties.apply`` (so ``comm_links.named_records`` immediately derives the
customer/supplier of every newly linked order or PO). Never reads marked text fields
(``teams_text.apply`` already withholds marked text to NULL) and never removes existing
stored links."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from operating.pg import bare_mentions as bm
from operating.pg import business_views as bv

# identifier_kind -> (target ontology object type, target biz view name)
KINDS: dict[str, tuple[str, str]] = {
    "customer_erp_code": ("Customer", "customers"),
    "erp_item_code": ("Product", "products"),
    "item_code": ("Product", "products"),
    "formula_code": ("Product", "products"),
    "so_number": ("SalesOrder", "sales_orders"),
    "petra_po": ("PurchaseOrder", "purchase_orders"),
    "batch_code": ("Batch", "batches"),
}
BATCH_LINK_TYPES = ("TeamsChatMessage.mentions_batch", "TeamsChannelMessage.mentions_batch")
BATCH = 20_000


def _conn(write: Any) -> Any:
    return write if hasattr(write, "cursor") else write.connection


def _resolve_identifiers(conn: Any, bare_tokens: Iterable[str],
                         typed_keys: Iterable[tuple[str, str]],
                         stats: dict[str, int]
                         ) -> tuple[dict[str, list[str]], dict[tuple[str, str], list[str]], set[str]]:
    """Resolve ``bare_tokens`` (any kind in ``KINDS``) and ``typed_keys`` (``(kind, value)``
    from ``mentions.extract``) to current target object ids under the same conservative
    ambiguity rule as ``bare_mentions.resolve`` (confirmed twins or at most one per system;
    never cross-type; at most ``bare_mentions.MAX_HOLDERS``)."""
    bare_set = set(bare_tokens)
    typed_set = {(k, v) for k, v in typed_keys if k in KINDS}
    all_vals = sorted(bare_set | {v for _, v in typed_set})
    if not all_vals:
        return {}, {}, set()

    held_bare: dict[str, set[tuple[str, str]]] = defaultdict(set)
    held_typed: dict[tuple[str, str], set[str]] = defaultdict(set)
    sys_keys: dict[str, set[str]] = defaultdict(set)

    for lo in range(0, len(all_vals), BATCH):
        part = all_vals[lo:lo + BATCH]
        for kind, val, obj, lineage, handle in conn.execute(
                "SELECT identifier_kind, normalized, object_id, lineage, l1_handle "
                "FROM ontology.identifiers WHERE normalized = ANY(%s) "
                "AND identifier_kind = ANY(%s) AND superseded_at IS NULL",
                (part, sorted(KINDS))).fetchall():
            target, _view = KINDS[kind]
            if obj.split(":", 1)[0] != target:
                continue
            sys_keys[obj] |= bm.systems(obj, lineage, handle)
            if val in bare_set:
                held_bare[val].add((target, obj))
            if (kind, val) in typed_set:
                held_typed[(kind, val)].add(obj)

    known = set(held_bare)
    several_objs: set[str] = set()
    for h in held_bare.values():
        if 1 < len(h) <= bm.MAX_HOLDERS:
            several_objs.update(o for _, o in h)
    for h in held_typed.values():
        if 1 < len(h) <= bm.MAX_HOLDERS:
            several_objs.update(h)
    group = bm.twin_groups(conn, several_objs) if several_objs else {}

    def accept(objs: set[str], multi_type: bool) -> bool:
        if not objs or len(objs) > bm.MAX_HOLDERS or multi_type:
            return False
        if len(objs) == 1:
            return True
        if len({group.get(o, o) for o in objs}) == 1:
            return True
        return bm._one_per_system(objs, group, sys_keys)

    bare_out: dict[str, list[str]] = {}
    for val, pairs in held_bare.items():
        objs = {o for _, o in pairs}
        types = {ty for ty, _ in pairs}
        if accept(objs, len(types) > 1):
            bare_out[val] = sorted(objs)
        else:
            stats["ambiguous"] += 1

    typed_out: dict[tuple[str, str], list[str]] = {}
    for key, objs in held_typed.items():
        if accept(objs, False):
            typed_out[key] = sorted(objs)
        else:
            stats["ambiguous"] += 1

    return bare_out, typed_out, known


def _oid_to_biz(conn: Any, oids: Iterable[str]) -> dict[str, list[tuple[str, str]]]:
    """Target ontology object_id -> [(bucket, biz_handle)] where bucket is 'customers',
    'suppliers', or 'records'."""
    wanted = sorted(set(oids))
    if not wanted:
        return {}
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    views = ("customers", "suppliers", "sales_orders", "sales_invoices",
             "purchase_orders", "products", "supplier_invoices", "batches")
    has_lineage = conn.execute("SELECT to_regclass(%s) IS NOT NULL",
                               (f"{bv.SCHEMA}.lineage",)).fetchone()[0]
    for view in views:
        if conn.execute("SELECT to_regclass(%s) IS NULL", (f"{bv.SCHEMA}.{view}",)).fetchone()[0]:
            continue
        bucket = view if view in ("customers", "suppliers") else "records"
        has_oids = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = %s "
            "AND table_name = %s AND column_name = 'object_ids')", (bv.SCHEMA, view)).fetchone()[0]
        for lo in range(0, len(wanted), BATCH):
            part = wanted[lo:lo + BATCH]
            if has_oids:
                rows = conn.execute(
                    f"SELECT o.oid, v.handle FROM {bv.SCHEMA}.{view} v "
                    "CROSS JOIN LATERAL unnest(v.object_ids) o(oid) WHERE o.oid = ANY(%s)",
                    (part,)).fetchall()
            elif has_lineage:
                rows = conn.execute(
                    f"SELECT o.oid, l.handle FROM {bv.SCHEMA}.lineage l "
                    "CROSS JOIN LATERAL unnest(l.object_ids) o(oid) "
                    "WHERE l.view = %s AND o.oid = ANY(%s)", (view, part)).fetchall()
            else:
                rows = []
            for oid, handle in rows:
                if handle:
                    out[oid].append((bucket, handle))
    return out


def _batch_links(conn: Any, msg_oids: list[str]) -> dict[str, set[str]]:
    """Message object_id -> target Batch object_ids from existing unmarked non-contradicted
    ``mentions_batch`` ontology links."""
    if not msg_oids:
        return {}
    out: dict[str, set[str]] = defaultdict(set)
    for lo in range(0, len(msg_oids), BATCH):
        part = msg_oids[lo:lo + BATCH]
        for frm, to in conn.execute(
                "SELECT from_object, to_object FROM ontology.links "
                "WHERE from_object = ANY(%s) AND link_type = ANY(%s) "
                "AND superseded_at IS NULL AND tier <> 'contradicted' "
                "AND markings = '[]' AND from_object <> to_object",
                (part, list(BATCH_LINK_TYPES))).fetchall():
            out[frm].add(to)
    return out


def apply(write: Any, table: str, where: str = "true") -> dict[str, int]:
    """Union identifiers extracted from unmarked Teams text (``said``, ``subject``,
    ``chat_topic``) and existing ``mentions_batch`` links into ``customers``, ``suppliers``
    and ``records`` on ``table`` rows matching ``where``."""
    from ontology import mentions
    from operating.pg import message_parties as mp

    stats = {"rows": 0, "linked_rows": 0, "records_added": 0,
             "customers_added": 0, "suppliers_added": 0, "ambiguous": 0}
    has_topic = write.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE "
        "(table_schema || '.' || table_name = %s OR table_name = %s) "
        "AND column_name = 'chat_topic')", (table, table.split(".")[-1])).fetchone()[0]
    topic_col = "t.chat_topic" if has_topic else "NULL::text"
    rows = write.execute(
        f"SELECT t.object_ids[1], t.subject, {topic_col}, t.said FROM {table} t "
        f"WHERE ({where})").fetchall()
    stats["rows"] = len(rows)
    if not rows:
        return stats

    own = bm.own_words()
    bare_by_msg: dict[str, set[str]] = {}
    ext_by_msg: dict[str, list[tuple[str, str]]] = {}
    plain_toks: set[str] = set()
    alt_toks: set[str] = set()
    typed_keys: set[tuple[str, str]] = set()
    msg_oids: list[str] = []

    for oid, subject, topic, said in rows:
        if not oid:
            continue
        msg_oids.append(oid)
        parts = [p for p in (subject, topic, mp.own_text(said) if said else None) if p]
        if not parts:
            continue
        text = "\n".join(parts)
        toks = bm.candidates(text, own)
        if toks:
            bare_by_msg[oid] = toks
            for t in toks:
                if bm.ALT in t:
                    alt_toks.add(t)
                else:
                    plain_toks.add(t)
        found = mentions.extract(text)
        if found:
            pairs = [(m["identifier_kind"], m["normalized"]) for m in found
                     if m.get("identifier_kind") in KINDS and m.get("normalized")]
            if pairs:
                ext_by_msg[oid] = pairs
                typed_keys.update(pairs)

    wanted_bare = plain_toks | {p for t in alt_toks for p in t.split(bm.ALT)}
    bare_resolved, typed_resolved, known = _resolve_identifiers(
        write, wanted_bare, typed_keys, stats)
    chosen_alt = {t: ([bm._alternatives(t)[0]] if bm._alternatives(t)[0] in known
                      else bm._alternatives(t)[1]) for t in alt_toks}

    batch_targets = _batch_links(write, msg_oids)
    per_msg_targets: dict[str, set[str]] = defaultdict(set)
    for oid, toks in bare_by_msg.items():
        for t in toks:
            for c in chosen_alt.get(t, [t]):
                per_msg_targets[oid].update(bare_resolved.get(c, ()))
    for oid, pairs in ext_by_msg.items():
        for key in pairs:
            per_msg_targets[oid].update(typed_resolved.get(key, ()))
    for oid, btargets in batch_targets.items():
        per_msg_targets[oid].update(btargets)

    all_targets = {to for ts in per_msg_targets.values() for to in ts}
    biz_map = _oid_to_biz(write, all_targets) if all_targets else {}

    # Participant email addresses on Teams chats/channels -> customer/supplier handles via
    # comm_links.addresses (keeps comm_links.py untouched so emails links_rule never moves).
    from operating.pg import comm_links
    has_addrs = write.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE "
        "(table_schema || '.' || table_name = %s OR table_name = %s) "
        "AND column_name = 'addresses')", (table, table.split(".")[-1])).fetchone()[0]
    party_tables_ready = has_addrs and all(
        write.execute("SELECT to_regclass(%s) IS NOT NULL", (r,)).fetchone()[0]
        for r in ("biz.staff", "biz.customers", "biz.suppliers"))
    addr_parties = comm_links.addresses(write, table, where) if party_tables_ready else {}

    all_oids = sorted(set(per_msg_targets) | set(addr_parties))
    if not all_oids:
        return stats

    staged: list[tuple[str, list[str], list[str], list[str]]] = []
    for oid in all_oids:
        custs: set[str] = set()
        supps: set[str] = set()
        recs: set[str] = set()
        for to in per_msg_targets.get(oid, ()):
            for bucket, handle in biz_map.get(to, ()):
                if bucket == "customers":
                    custs.add(handle)
                elif bucket == "suppliers":
                    supps.add(handle)
                else:
                    recs.add(handle)
        for kind, handle in addr_parties.get(oid, ()):
            if kind == "customer":
                custs.add(handle)
            elif kind == "supplier":
                supps.add(handle)
        if custs or supps or recs:
            staged.append((oid, sorted(custs), sorted(supps), sorted(recs)))

    if not staged:
        return stats

    write.execute("DROP TABLE IF EXISTS pg_temp.tl_add")
    write.execute("CREATE TEMP TABLE tl_add (oid text PRIMARY KEY, customers text[], "
                  "suppliers text[], records text[])")
    try:
        with _conn(write).cursor().copy(
                "COPY tl_add (oid, customers, suppliers, records) FROM STDIN") as cp:
            for row in staged:
                cp.write_row(row)

        def gain(col: str) -> str:
            return f"(cardinality(a.{col}) > 0 AND NOT t.{col} @> a.{col})"

        def union(col: str) -> str:
            return (f"CASE WHEN {gain(col)} THEN ARRAY(SELECT DISTINCT x FROM unnest("
                    f"t.{col} || a.{col}) x ORDER BY 1) ELSE t.{col} END")

        stats["customers_added"], stats["suppliers_added"], stats["records_added"] = write.execute(
            f"SELECT count(*) FILTER (WHERE {gain('customers')}), "
            f"count(*) FILTER (WHERE {gain('suppliers')}), "
            f"count(*) FILTER (WHERE {gain('records')}) "
            f"FROM {table} t JOIN pg_temp.tl_add a ON t.object_ids[1] = a.oid").fetchone()
        stats["linked_rows"] = write.execute(
            f"UPDATE {table} t SET customers = {union('customers')}, "
            f"suppliers = {union('suppliers')}, records = {union('records')} "
            "FROM pg_temp.tl_add a WHERE t.object_ids[1] = a.oid AND "
            f"({gain('customers')} OR {gain('suppliers')} OR {gain('records')})").rowcount
        return stats
    finally:
        write.execute("DROP TABLE IF EXISTS pg_temp.tl_add")
