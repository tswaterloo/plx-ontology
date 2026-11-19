"""Mined questions: what staff asked each other in Teams and mail in the last 60 days.

    python3 -m evals.qbank.mine [--days 60] [--workers N] [--dry]

1. Messages: Teams chat and channel messages (their text) and internal mail (sender on the
   company's own domain, the domain most senders use; its preview text), never one the
   people exclusion holds back. Mail copies in several mailboxes count once.
2. Question detection by sentence form: a sentence is a question when it ends in "?" or opens
   interrogatively (a wh-word, or an auxiliary or modal followed by a subject).
3. The question must name a record: an identifier token found in the business tables (order,
   customer PO, invoice, PO, batch or lot, item, receivable or payable document).
4. A reply (a later message of the same thread by someone else within 72 h) must state a
   checkable fact of that record: one of its values (a date within a day, a figure within 1%,
   a name or a related number; a state word only for a yes/no status question). The fact
   picks which field the question was about; the truth is that field's value read from the
   records at refresh time, never the reply's text (``evidence: reply_fact``).
5. Second tier (``evidence: question_intent``, counted apart): no reply states a fact, but the
   question names exactly one record and its own wording asks for exactly one field class of
   it (a date, an amount, a state, a name, a set); the truth is again read from the records.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import re
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Mapping

from . import db
from . import grading_helpers as g
from .core import D, num, spec, split_of
from .families_ops import EXCL, O, person_names
from .grade import dates_in, norm, numbers_in

WH = {"what", "when", "where", "which", "who", "whom", "whose", "why", "how"}
AUX = {"is", "are", "was", "were", "do", "does", "did", "has", "have", "had", "can", "could", "will", "would",
       "should", "shall", "may", "might", "must", "any", "isn't", "aren't", "didn't", "doesn't", "hasn't", "haven't",
       "can't", "won't", "wouldn't", "shouldn't", "couldn't"}
SUBJ = {"i", "you", "we", "they", "he", "she", "it", "this", "that", "these", "those", "there", "the", "a", "an",
        "our", "your", "their", "his", "her", "its", "my", "anyone", "anybody", "someone", "somebody", "everyone"}
ID_TOKEN = re.compile(r"(?<![\w/@.-])#?([A-Za-z]{0,6}[-_ ]?\d[\w-]{2,}\d?|\d{5,})(?![\w@])")

# Identifier kinds: the column a token is found in, and the fields asked about.
KINDS = {
    "order": ("biz.sales_orders WHERE ai_ok AND of_record AND order_type = 'order'", "order_number",
              {"status": "state", "customer": "text", "order_date": "date", "required_date": "date", "value": "money:value_currency"}),
    "cpo": ("biz.sales_orders WHERE ai_ok AND of_record AND order_type = 'order'", "customer_po",
            {"order_number": "text", "status": "state", "customer": "text", "required_date": "date"}),
    "invoice": ("biz.sales_invoices WHERE ai_ok AND NOT pilot", "invoice_number",
                {"total": "money:total_currency", "invoice_date": "date", "due_date": "date", "order_number": "text", "customer": "text"}),
    "po": ("biz.purchase_orders WHERE ai_ok", "po_number",
           {"status": "state", "supplier": "text", "order_date": "date", "promised_date": "date", "requested_date": "date",
            "total": "money:total_currency"}),
    "batch": ("biz.batches WHERE ai_ok", "batch_code",
              {"status": "state", "quality_status": "state", "product": "text", "realized_date": "date", "scheduled_date": "date",
               "produced_quantity": "number", "lot_number": "text"}),
    "lot": ("biz.batches WHERE ai_ok", "lot_number",
            {"status": "state", "quality_status": "state", "product": "text", "realized_date": "date", "batch_code": "text"}),
    "item": ("biz.stock WHERE ai_ok", "item_code", {"name": "text", "on_hand": "number"}),
    "ar_doc": ("biz.customer_balances WHERE ai_ok", "document_number",
               {"amount_due": "money:amount_due_currency", "due_date": "date", "customer": "text"}),
    "ap_doc": ("biz.supplier_invoices WHERE ai_ok AND NOT pilot", "document_number",
               {"original_amount": "money:original_amount_currency", "due_date": "date", "supplier": "text", "paid": "paid"}),
}
# Named entities: one virtual row per customer / supplier / product, its facts as columns
# (the outer WHERE is pushed into the derived table, so only the asked row is computed).
SO_OPEN = "biz.sales_orders o WHERE o.ai_ok AND o.of_record AND o.order_type = 'order' AND o.status = 'open'"
KINDS.update({
    "customer": ("(SELECT c.customer_code, c.salesperson, "
                 f"(SELECT array_agg(o.order_number ORDER BY o.order_date DESC) FROM {SO_OPEN} AND o.customer_code = c.customer_code) AS open_orders, "
                 "(SELECT sum(b.amount_due) FROM biz.customer_balances b WHERE b.ai_ok AND b.customer_code = c.customer_code "
                 "HAVING count(DISTINCT b.amount_due_currency) = 1) AS ar_owed, (SELECT max(b.amount_due_currency) FROM biz.customer_balances b "
                 "WHERE b.ai_ok AND b.customer_code = c.customer_code) AS ar_currency, (SELECT max(o.order_date) FROM biz.sales_orders o "
                 "WHERE o.ai_ok AND o.of_record AND o.order_type = 'order' AND o.customer_code = c.customer_code) AS last_order_date "
                 "FROM biz.customers c WHERE c.ai_ok) v WHERE TRUE", "customer_code",
                 {"open_orders": "set", "ar_owed": "money:ar_currency", "last_order_date": "date", "salesperson": "text"}),
    "supplier": ("(SELECT s.supplier_code, (SELECT array_agg(p.po_number ORDER BY p.order_date DESC) FROM biz.purchase_orders p "
                 "WHERE p.ai_ok AND p.status = 'open' AND p.supplier_handle = s.handle) AS open_pos, (SELECT sum(b.amount_due) "
                 "FROM biz.supplier_balances b WHERE b.ai_ok AND b.supplier_code = s.supplier_code HAVING count(DISTINCT b.amount_due_currency) = 1) "
                 "AS ap_owed, (SELECT max(b.amount_due_currency) FROM biz.supplier_balances b WHERE b.ai_ok AND b.supplier_code = s.supplier_code) "
                 "AS ap_currency, (SELECT max(p.order_date) FROM biz.purchase_orders p WHERE p.ai_ok AND p.status <> 'draft' "
                 "AND p.supplier_handle = s.handle) AS last_po_date FROM biz.suppliers s WHERE s.ai_ok) v WHERE TRUE", "supplier_code",
                 {"open_pos": "set", "ap_owed": "money:ap_currency", "last_po_date": "date"}),
    "product": ("biz.stock WHERE ai_ok", "name", {"on_hand": "number", "item_code": "text"}),
})
NAMED = {"customer": "SELECT name, customer_code AS k FROM biz.customers WHERE ai_ok AND name IS NOT NULL",
         "supplier": "SELECT name, supplier_code AS k FROM biz.suppliers WHERE ai_ok AND name IS NOT NULL",
         "product": "SELECT name, name AS k FROM biz.stock WHERE ai_ok AND name IS NOT NULL"}
LEGAL_WORDS = {"inc", "ltd", "llc", "corp", "corporation", "co", "company", "limited", "ltee", "the", "of", "and", "usa", "canada"}

SYSTEM = {"customer": "sales", "supplier": "purchasing", "product": "stock", "order": "sales", "cpo": "sales", "invoice": "sales", "po": "purchasing", "batch": "quality", "lot": "quality",
          "item": "stock", "ar_doc": "ar", "ap_doc": "ap"}
TYPE = {"customer": "Customer", "supplier": "Supplier", "product": "Stock", "order": "SalesOrder", "cpo": "SalesOrder", "invoice": "SalesInvoice", "po": "PurchaseOrder", "batch": "Batch",
        "lot": "Batch", "item": "Stock", "ar_doc": "CustomerBalance", "ap_doc": "SupplierInvoice"}
INTENT = (  # question words -> the field classes it asks for
    (re.compile(r"\b(when|what date|eta|by when|how long|what day|due)\b", re.I), {"date"}),
    (re.compile(r"\b(how much|price|cost|total|value|amount|\$|balance|owe)\b", re.I), {"money", "number"}),
    (re.compile(r"\b(open orders?|open pos?|any orders?|what orders?|which orders?|outstanding orders?|pos? open)\b", re.I), {"set"}),
    (re.compile(r"\b(how many|qty|quantity|units|stock|on hand|inventory|cases|pcs)\b", re.I), {"number"}),
    (re.compile(r"\b(who|whose|which (customer|supplier|vendor|client))\b", re.I), {"text"}),
    (re.compile(r"\b(paid|pay|payment)\b", re.I), {"paid", "money"}),
    (re.compile(r"\b(status|ship|shipped|released|approved|hold|done|ready|open|closed|received|arrived|complete|cancel\w*|made|produced)\b", re.I),
     {"state"}),
)


def sentences(text: str) -> list[str]:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return [s.strip() for s in re.split(r"(?<=[?.!])\s+(?=[A-Z@#\d\"'(])|\n", text) if s.strip()]


def is_question(sentence: str) -> bool:
    s = sentence.strip()
    if s.endswith("?"):
        return len(s) >= 8
    words = re.findall(r"[a-z']+", s.lower())[:3]
    if not words:
        return False
    if words[0] in WH and len(words) > 1 and words[1] not in ("i", "we") + (("you",) if words[0] == "how" else ()):
        return True
    return words[0] in AUX and len(words) > 1 and words[1] in SUBJ


def id_tokens(text: str) -> list[str]:
    out = []
    for m in ID_TOKEN.finditer(text or ""):
        raw = m.group(1)
        if re.fullmatch(r"(19|20)\d\d", raw) or re.fullmatch(r"\d{1,2}[-/]\d{1,2}([-/]\d{2,4})?", raw):
            continue
        out += [raw.replace(" ", ""), raw.replace(" ", "-")] + ([raw.split(" ", 1)[1]] if " " in raw else [])
    return list(dict.fromkeys(out))


def classes(kind: str) -> dict[str, str]:
    return {f: c.split(":")[0] for f, c in KINDS[kind][2].items()}


# ---- reading -------------------------------------------------------------------------------

def _chunks(sql: str, params: Mapping[str, Any], lo: int, hi: int, step: int, workers: int) -> list[dict[str, Any]]:
    """Rows of ``sql`` over row_no ranges; a range that hits the statement cap is split in two."""
    def fetch(r: tuple[int, int]) -> list[dict[str, Any]]:
        try:
            return db.query(sql, dict(params) | {"lo": r[0], "hi": r[1]})
        except db.TruthError as exc:
            if "QueryCanceled" not in str(exc) or r[1] - r[0] < 2000:
                raise
            mid = (r[0] + r[1]) // 2
            return fetch((r[0], mid)) + fetch((mid, r[1]))

    ranges = [(a, min(a + step, hi + 1)) for a in range(lo, hi + 1, step)]
    with ThreadPoolExecutor(workers) as ex:
        return [row for part in ex.map(fetch, ranges) for row in part]


def _row_range(*types: str) -> dict[str, int]:
    rows = db.query("SELECT object_type, min(row_no) AS lo, max(row_no) AS hi FROM ontology.objects "
                    "WHERE object_type = ANY(%(t)s) GROUP BY 1", {"t": list(types)})
    return {"lo": min(r["lo"] for r in rows), "hi": max(r["hi"] for r in rows)}


def _text(blobs: Iterable[bytes] | None) -> str:
    out = []
    for b in blobs or ():
        try:
            out.append(zlib.decompress(bytes(b)).decode("utf-8", "replace"))
        except zlib.error:
            continue
    return "".join(out)


def teams_messages(cutoff: str, workers: int) -> list[dict[str, Any]]:
    rng = _row_range("TeamsChatMessage", "TeamsChannelMessage")
    props = ("chat_id", "channel_id", "created_at", "from", "id", "reply_to_id", "message_type")
    sql = (f"SELECT x.*, (SELECT array_agg(c.body ORDER BY tf.field_id, c.start) FROM ontology.text_fields tf "
           "JOIN ontology.text_chunks c ON c.field_id = tf.field_id WHERE tf.object_id = x.oid AND tf.field_path = 'row.body_text') AS body "
           f"FROM {O('TeamsChatMessage', props, 'o.row_no >= %(lo)s AND o.row_no < %(hi)s')} x WHERE x.created_at >= %(cutoff)s "
           "UNION ALL SELECT x.*, (SELECT array_agg(c.body ORDER BY tf.field_id, c.start) FROM ontology.text_fields tf "
           "JOIN ontology.text_chunks c ON c.field_id = tf.field_id WHERE tf.object_id = x.oid AND tf.field_path = 'row.body_text') "
           f"FROM {O('TeamsChannelMessage', props, 'o.row_no >= %(lo)s AND o.row_no < %(hi)s')} x WHERE x.created_at >= %(cutoff)s")
    out = []
    for r in _chunks(sql, {"cutoff": cutoff}, rng["lo"], rng["hi"], 150_000, workers):
        if (r.get("message_type") or "message") != "message":
            continue
        thread = r["chat_id"] or ("ch:" + str(r["channel_id"]) + ":" + str(r["reply_to_id"] or r["id"]))
        sender = (person_names(r["from"]) or [""])[0]
        out.append({"oid": r["oid"], "channel": "teams", "thread": thread, "at": r["created_at"], "sender": sender,
                    "text": _text(r["body"])})
    return out


def mail_messages(cutoff: str, workers: int) -> list[dict[str, Any]]:
    rng = _row_range("Email")
    props = ("sender", "sent_at", "subject", "conversation_id", "body_preview", "mailbox", "internet_message_id")
    sql = f"SELECT * FROM {O('Email', props, 'o.row_no >= %(lo)s AND o.row_no < %(hi)s')} x WHERE x.sent_at >= %(cutoff)s"
    rows = _chunks(sql, {"cutoff": cutoff}, rng["lo"], rng["hi"], 60_000, workers)
    addr_of = lambda r: (re.search(r"[\w.+-]+@[\w.-]+", str(r.get("sender") or "")) or [""])[0].lower()
    domains = collections.Counter(addr_of(r).rsplit("@", 1)[-1] for r in rows if addr_of(r))
    own = domains.most_common(1)[0][0] if domains else ""   # staff write most of the mail
    seen, out = set(), []
    for r in rows:
        key = r.get("internet_message_id") or r["oid"]
        addr = addr_of(r)
        if key in seen or not own or not addr.endswith("@" + own):
            continue
        seen.add(key)
        text = re.split(r"\b(From:|-----Original|On .{4,60} wrote:|Sent from my)", str(r.get("body_preview") or ""))[0]
        out.append({"oid": r["oid"], "channel": "mail", "thread": r.get("conversation_id") or key, "at": r["sent_at"],
                    "sender": addr, "text": text, "subject": r.get("subject") or ""})
    return out


def id_index() -> dict[str, list[tuple[str, str]]]:
    """lower-cased identifier -> [(kind, identifier as recorded)]."""
    index: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for kind, (table, col, _f) in KINDS.items():
        for r in db.query(f"SELECT DISTINCT {col} AS k FROM {table} AND {col} IS NOT NULL AND length({col}) >= 4"):
            index[str(r["k"]).strip().lower()].append((kind, str(r["k"])))
    return index


def name_index() -> dict[str, list[tuple[str, str]]]:
    """Normalised name (legal words dropped, at least two words or one of 6+ letters) ->
    [(kind, key)], only names that belong to exactly one record of one kind."""
    index: dict[str, set[tuple[str, str]]] = collections.defaultdict(set)
    for kind, sql in NAMED.items():
        for r in db.query(sql):
            words = [w for w in norm(r["name"]).split() if w not in LEGAL_WORDS]
            if len(words) >= 2 or (len(words) == 1 and len(words[0]) >= 6 and not words[0].isdigit()):
                index[" ".join(words)].add((kind, str(r["k"])))
    return {k: sorted(v) for k, v in index.items() if len(v) == 1}


def names_in(text: str, index: Mapping[str, list[tuple[str, str]]]) -> list[tuple[str, str]]:
    words = [w for w in norm(text).split() if w not in LEGAL_WORDS]
    out = []
    for n in (4, 3, 2, 1):
        for i in range(len(words) - n + 1):
            out += index.get(" ".join(words[i:i + n]), [])
    return list(dict.fromkeys(out))


def facts(kind: str, key: str) -> dict[str, Any] | None:
    table, col, fields = KINDS[kind]
    sel = ", ".join(sorted(set(fields) | {c.split(":")[1] for c in fields.values() if ":" in c}))
    rows = db.query(f"SELECT {sel} FROM {table} AND {col} = %(k)s", {"k": key})
    return rows[0] if len(rows) == 1 else None


# ---- matching ------------------------------------------------------------------------------------

def reply_matches(kind: str, rec: Mapping[str, Any], reply: str, status_question: bool) -> set[str]:
    hit = set()
    t = " " + norm(reply) + " "
    found = numbers_in(reply)
    said = dates_in(reply, dt.date.today().year)
    for field, cls in classes(kind).items():
        v = rec.get(field)
        if v is None or str(v).strip() in ("", "None"):
            continue
        if cls == "date":
            d = v if isinstance(v, dt.date) else None
            if d and any(abs((s - d).days) <= 1 for s in said):
                hit.add(field)
        elif cls in ("money", "number"):
            x = abs(D(v))
            if x >= 10 and any(abs(abs(f) - x) <= x * D("0.01") for f, _h in found):
                hit.add(field)
        elif cls == "text":
            n = norm(v)
            if len(n) >= 4 and (f" {n} " in t or (len(n) > 12 and n[:12] in t)):
                hit.add(field)
        elif cls == "set":
            if any(f" {norm(x)} " in t for x in v or ()):
                hit.add(field)
        elif cls in ("state", "paid") and status_question:
            words = g.state_words(v) if cls == "state" else (g.YES_PAID if v else g.NOT_PAID)
            if any(f" {norm(w)} " in t for w in words):
                hit.add(field)
    return hit


def intent(question: str) -> set[str]:
    out = set()
    for rx, cls in INTENT:
        if rx.search(question):
            out |= cls
    return out


def grade_for(kind: str, field: str):
    cls = KINDS[kind][2][field]
    if cls == "state":
        return g.state(field, f"the record's {field}")
    if cls == "set":
        return lambda rows, p: (spec(f"the {field}: how many and which", numbers=[num(len(rows[0][field] or ()), "count")],
                                     names=list(rows[0][field] or ())[:15], min_names=min(3, len(rows[0][field] or ())))
                                if g.first(rows) and rows[0][field] else None)
    if cls == "text":
        return g.text(field, f"the record's {field}")
    if cls == "date":
        return g.date(field, f"the record's {field}", tol_days=1)
    if cls == "number":
        return g.number(field, f"the record's {field}", tol=0.02)
    if cls.startswith("money"):
        return g.number(field, f"the record's {field}", cur=cls.split(":")[1])
    return lambda rows, p: (spec("paid or not", must_any=g.YES_PAID if rows[0][field] else g.NOT_PAID) if g.first(rows) else None)


def truth_sql(kind: str, field: str) -> str:
    table, col, fields = KINDS[kind]
    cls = fields[field]
    extra = f", {cls.split(':')[1]}" if ":" in cls else ""
    return f"SELECT {field}{extra} FROM {table} AND {col} = %(key)s"


def regrade(item: Mapping[str, Any]):
    kind, field = item["params"].get("kind"), item["params"].get("field")
    return grade_for(kind, field) if kind in KINDS and field in KINDS[kind][2] else None


def mine(days: int, workers: int, log=print) -> list[dict[str, Any]]:
    from .bank import settle, today
    day = today()
    cutoff = (day - dt.timedelta(days=days)).isoformat()
    msgs = teams_messages(cutoff, workers) + mail_messages(cutoff, workers)
    log(f"messages: {collections.Counter(m['channel'] for m in msgs)}")
    index, names = id_index(), name_index()
    threads: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for m in msgs:
        threads[m["thread"]].append(m)
    cands, asked = [], 0
    for ms in threads.values():
        ms.sort(key=lambda m: m["at"])
        for i, m in enumerate(ms):
            sents = sentences(m["text"])
            qs = [k for k, s in enumerate(sents) if is_question(s)]
            if not qs:
                continue
            at = dt.datetime.fromisoformat(m["at"].replace("Z", "+00:00"))
            replies = [r for r in ms[i + 1:i + 8] if r["sender"] != m["sender"]
                       and dt.datetime.fromisoformat(r["at"].replace("Z", "+00:00")) - at <= dt.timedelta(hours=72)][:4]
            if not replies:
                continue
            asked += 1
            for k in qs:
                ctx = " ".join(sents[max(0, k - 2):k + 1])
                refs = [ref for t in id_tokens(ctx) for ref in index.get(t.lower(), ())] + names_in(ctx, names)
                if refs:
                    cands.append((m, ctx, sents[k], list(dict.fromkeys(refs)), replies))
    log(f"question messages with a reply: {asked}; question sentences naming a record: {len(cands)}")

    why: collections.Counter = collections.Counter()

    def judge(c) -> list[dict[str, Any]]:
        m, ctx, q, refs, replies = c
        out = []
        status_q = "state" in intent(q) or q.split(" ", 1)[0].lower() in AUX
        for kind, key in refs:
            rec = facts(kind, key)
            if rec is None:
                why[f"{kind}: no single record"] += 1
                continue
            hit = set()
            for r in replies:
                hit |= reply_matches(kind, rec, r["text"], status_q)
            want = intent(q)
            pick = [f for f in hit if classes(kind)[f] in want] or sorted(hit)
            evidence = "reply_fact"
            if not pick:
                # second tier: the question's own wording names one field class of the record
                asked_for = [f for f, cls in classes(kind).items() if cls in want and rec.get(f) not in (None, "", [])]
                if len({classes(kind)[f] for f in asked_for}) != 1 or len(refs) != 1:
                    why[f"{kind}: reply states none of its facts"] += 1
                    continue
                pick, evidence = asked_for[:1], "question_intent"
            field = sorted(pick)[0] if evidence == "reply_fact" else pick[0]
            inst = "mined:" + hashlib.sha256(f"{m['oid']}|{key}|{field}".encode()).hexdigest()[:12]
            item = {"id": inst, "instance": inst, "source": "mined", "family": f"mined_{kind}_{field}", "system": SYSTEM[kind],
                    "type": TYPE[kind], "kind": "mined", "variant": "real", "question": ctx[-500:], "turns": [ctx[-500:]],
                    "params": {"kind": kind, "field": field, "key": key}, "truth_sql": truth_sql(kind, field),
                    "truth_source": "biz", "channel": m["channel"], "asked_at": m["at"][:10], "reply_fields": sorted(hit),
                    "evidence": evidence}
            settled = settle(item, grade_for(kind, field), day)
            why[f"{kind}: " + (f"kept ({evidence})" if settled and not settled.get("stale") else "truth does not grade")] += 1
            if settled and not settled.get("stale"):
                settled["split"] = split_of(inst)
                out.append(settled)
            break
        return out

    with ThreadPoolExecutor(workers) as ex:
        items = [it for part in ex.map(judge, cands) for it in part]
    log(json.dumps(dict(why.most_common())))
    uniq = {}
    for it in items:   # one question asked twice (mail copies, re-posts) counts once
        uniq.setdefault((norm(it["question"]), it["params"]["key"], it["params"]["field"]), it)
    return list(uniq.values())


def main(argv: list[str] | None = None) -> int:
    from .bank import all_items, merge, save
    ap = argparse.ArgumentParser(prog="evals.qbank.mine")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    items = mine(a.days, a.workers)
    print(json.dumps({"mined": len(items), "by_evidence": dict(collections.Counter(i["evidence"] for i in items)), "by_channel": dict(collections.Counter(i["channel"] for i in items)),
                      "by_family": dict(collections.Counter(i["family"] for i in items).most_common(20))}))
    if not a.dry:
        print(json.dumps(save(merge(all_items(), items))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
