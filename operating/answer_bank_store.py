"""The questionnaire answer bank kept on disk: built once for the company, gated per reader.

``business_documents.view_answer_bank`` (the Templates page's "Questionnaire answers" and
a customer page's list) needs every answer we gave a customer in a questionnaire. Reading
them (which document is a questionnaire, its text, its question/answer pairs, whose it is)
took 6-50 s per reader on the served store and was kept per reader in memory only
(``document_memo.MEMO``), so each person's first view after a restart waited for it.

The read is split in two:

* ``collect(conn, gate)``: the reader-independent part, as data (``Bank``): each
  questionnaire document found (its records, day, whether its text was captured, its
  pairs with each answer's customer-independent specific check), how its customer is
  decided (``CustomerOf``'s order, as steps: a fixed customer, or the votes of a sender
  domain's messages), each such domain's messages in the order the word index gave them
  with the customers each is tied to, and the name words of every customer a step can
  name. Records ``gate`` does not show are kept as ids only (their text never read).
* ``pairs(bank, gate, ans)``: the reader's part, with the reader's own ``gate``: a
  document all of whose records the reader may open (else counted
  ``document_not_readable``), the domain votes over the messages the reader may open
  (the first ``THREAD_MAIL`` of them), the customer, the specific check against that
  customer's name. Every row is gated again by ``Answer.add`` on the records it cites.

Built with the reader's gate, the two are the per-reader read as before (the fallback,
kept in ``MEMO``). Built with ``BuildGate`` (every record but those the people exclusion
withholds: a superset of any reader's) by ``python3 -m operating.answer_bank_store
build`` after each delta (``plx-answer-bank.timer``, low priority, stamp-gated), written
to ``<state>/answer-bank/bank.json`` (0640). A request uses the file only while it was
built by this code (``VERSION``), under the same people exclusion (``fingerprint``) and
within ``MAX_AGE_SECONDS``; otherwise it reads per reader as before. Stdlib only.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from typing import Any, Mapping

from . import document_tables as dt

VERSION = 1
MAX_AGE_SECONDS = 4 * 3600      # as stale as the per-reader memo served (MEMO_STALE_SECONDS)
BANK_ENV = "PLX_ANSWER_BANK"
BANK_FILE = Path("answer-bank") / "bank.json"


class BuildGate(dt.Gate):
    """Every record the store holds, but those the people exclusion withholds (and one
    whose markings cannot be read): a superset of what any reader's gate shows."""

    def visible(self, oid: str) -> bool:
        if oid in self._seen:
            return self._seen[oid]
        self.load([oid])
        info = self._info.get(oid)
        ok = False
        if info is not None and self.unavailable is None:
            key, marks = info
            try:
                ok = bool(self.guard.unit_clear(key)) and marks is not None
            except Exception:  # noqa: BLE001 - undecidable: withheld
                ok = False
        self._seen[oid] = ok
        return ok


# ------------------------------------------------------------------------------ collect

def collect(conn: Any, gate: dt.Gate) -> dict[str, Any]:
    """The reader-independent part of the answer bank (module doc), read through ``gate``."""
    from . import business_documents as bd

    customer_of = bd.CustomerOf(conn, gate, None)
    parties = customer_of.parties
    suppliers = dt.Parties(conn, "Supplier", "Supplier.same_record_as",
                           getattr(gate.guard, "internal", ()))
    found = dt.find_documents(conn, bd.QUESTIONNAIRE_WORDS,
                              require=lambda n: bd._words_in(n, bd.QUESTIONNAIRE_WORDS))
    gate.load(o for d in found.docs for o in d.cited)
    shown = [d for d in found.docs if all(gate.visible(o) for o in d.cited)]
    shown_ids = {d.object_id for d in shown}
    dt.texts(conn, shown)
    domains: dict[str, Any] = {}
    names: dict[str, list[str]] = {}
    docs: list[dict[str, Any]] = []
    for d in found.docs:
        entry: dict[str, Any] = {"id": d.object_id, "message": d.message,
                                 "day": d.day.isoformat() if d.day else None}
        docs.append(entry)
        if d.object_id not in shown_ids:
            continue   # its records' ids only: a reader that may open them all is no reader
        entry["shown"] = True
        entry["text"] = d.text is not None
        pairs = bd.qa_pairs(d.text or "") if d.text is not None else []
        if not pairs:
            continue
        outside = [d.sender] + d.recipients if d.sender else d.recipients
        entry["supplier"] = next((x for x in (suppliers.of(o) for o in outside
                                              if not suppliers.internal_address(o)) if x),
                                 None) is not None
        steps = _steps(d, customer_of, parties, conn, gate, domains)
        entry["steps"] = steps
        for step in steps:
            for who in ([step[1]] if step[0] == "customer" else
                        [c for _, tied in domains.get(step[1], {}).get("mail", ())
                         for c in tied]):
                if who not in names:
                    names[who] = sorted(customer_of.name_words(who, every=True))
        entry["pairs"] = [[q, a, cell, bd.fact_category(q) is not None,
                           bd._specific(a, customer_of, None)] for q, a, cell in pairs]
    return {"version": VERSION, "complete": bool(found.complete), "docs": docs,
            "domains": domains, "names": names}


def _steps(d: dt.Doc, customer_of: Any, parties: Any, conn: Any, gate: dt.Gate,
           domains: dict[str, Any]) -> list[list[Any]]:
    """``CustomerOf.__call__`` as steps, the first that names a customer winning:
    ``["customer", id, basis]`` or ``["threads", domain, basis]`` (the domain's votes,
    decided per reader); ``["nobody"]`` ends it naming no one."""
    linked = sorted(set(d.parties.get("customer") or []))
    if linked:
        roots = {customer_of._root(c) for c in linked}
        return [["customer", next(iter(roots)), "sender_link"]] if len(roots) == 1 \
            else [["nobody"]]
    tries: list[tuple[str, str]] = []
    if d.sender and not parties.internal_address(d.sender):
        tries.append((d.sender, "sender"))
    elif d.sender:
        forwarded = customer_of._forwarded(d)
        if forwarded:
            tries.append((forwarded, "forwarded_sender"))
        outside = sorted({a for a in d.recipients if not parties.internal_address(a)})
        if len({dt.domain_of(a) for a in outside}) == 1:
            tries.append((outside[0], "recipient"))
    steps: list[list[Any]] = []
    for address, basis in tries:
        who = parties.of(address)
        if who:
            steps.append(["customer", who, basis + "_domain"])
            return steps
        domain = dt.domain_of(address)
        if domain and not parties._skip(domain):
            if domain not in domains:
                domains[domain] = _domain_mail(conn, customer_of, gate, domain)
            steps.append(["threads", domain, basis + "_threads"])
    who = customer_of._named(d)
    if who:
        steps.append(["customer", who, "named_in_document"])
    return steps


def _domain_mail(conn: Any, customer_of: Any, gate: dt.Gate, domain: str) -> dict[str, Any]:
    """``CustomerOf._read_threads``' messages of one sender domain, in the word index's
    order, each with the customers (cluster roots, not retired) it is tied to; the
    reader's ``THREAD_MAIL`` and votes are taken at read (``_votes``)."""
    from . import business_documents as bd
    import sqlite3

    token = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
    if len(token) < 3:
        return {"mail": []}
    try:
        senders = dt.word_values(conn, token, ["sender"], prefix="Email:",
                                 limit=bd.THREAD_MAIL * 4).rows
    except sqlite3.OperationalError as exc:
        if "interrupt" in str(exc):
            raise
        return {"mail": [], "unread": True}
    mails = [oid for oid, _prop, value in senders
             if dt.domain_of(value) == domain and gate.visible(oid)]
    links: dict[str, list[str]] = {}
    for start in range(0, len(mails), 500):
        part = mails[start:start + 500]
        for frm, to in conn.execute(
                "SELECT from_object, to_object FROM link_rows WHERE from_object IN (%s) "
                "AND superseded_at IS NULL AND tier <> 'contradicted'"
                % ",".join("?" * len(part)), tuple(part)):
            links.setdefault(frm, []).append(to)
    out = []
    for mail in mails:
        whose: set[str] = set()
        for to in links.get(mail, ()):
            kind = to.split(":", 1)[0]
            if kind == "Customer":
                whose.add(to)
            elif kind == "SalesOrder":
                whose.update(dt.links_from(conn, [to], bd.ORDERED_BY).get(to, []))
            elif kind == "SalesInvoice":
                whose.update(dt.links_from(conn, [to], bd.BILLED_TO).get(to, []))
            elif kind == "CustomerPurchaseOrder":
                for so in dt.links_to(conn, [to], "SalesOrder.references_customer_po"
                                      ).get(to, []):
                    whose.update(dt.links_from(conn, [so], bd.ORDERED_BY).get(so, []))
        roots = sorted({customer_of._root(c) for c in whose} - customer_of.parties.retired)
        out.append([mail, roots])
    return {"mail": out}


# -------------------------------------------------------------------------------- pairs

def pairs(bank: Mapping[str, Any], gate: dt.Gate, ans: Any) -> list[tuple]:
    """The answer pairs ``gate``'s reader may see (``_answer_pairs``' tuples), counted
    onto ``ans`` as the per-reader read counted them (module doc)."""
    from . import business_documents as bd

    docs = list(bank.get("docs") or ())
    if not bank.get("complete", False):
        ans.complete = False
        ans.counts["documents_cut"] += 1
    gate.load(o for e in docs for o in _cited(e))
    readable = []
    for e in docs:
        if not all(gate.visible(o) for o in _cited(e)):
            ans.withheld["document_not_readable"] += 1
            continue
        if not e.get("shown"):
            # the build withheld it and this reader may open it: the bank is not of this
            # reader's view (another exclusion than its fingerprint said): never guessed
            raise BankMismatch(e["id"])
        if not e.get("text"):
            ans.counts["text_not_captured"] += 1
            continue
        readable.append(e)
    ans.counts["documents"] += len(readable)
    domains = bank.get("domains") or {}
    names = bank.get("names") or {}
    votes: dict[str, tuple[str | None, list[str]]] = {}
    out: list[tuple] = []
    for e in readable:
        found = e.get("pairs") or ()
        if not found:
            continue
        customer, basis, evidence = _decide(e.get("steps") or (), domains, gate, ans, votes)
        if customer is None or e.get("supplier"):
            ans.counts["supplier_questionnaire" if e.get("supplier")
                       else "party_unknown"] += len(found)
            continue
        own = set(names.get(customer) or ())
        d = dt.Doc(e["id"], e["id"].split(":", 1)[0], "", "",
                   day=date.fromisoformat(e["day"]) if e.get("day") else None,
                   message=e.get("message"))
        for q, a, cell, fact, specific in found:
            specific = (not fact) or bool(specific) or bool(
                set(re.findall(r"[a-z0-9]+", str(a).lower())) & own)
            out.append((d, q, a, cell, customer, basis, evidence, specific))
    return out


class BankMismatch(Exception):
    """The kept bank does not cover what this reader may open: read per reader instead."""


def _cited(entry: Mapping[str, Any]) -> list[str]:
    return [entry["id"]] + ([entry["message"]] if entry.get("message") else [])


def _decide(steps: Any, domains: Mapping[str, Any], gate: dt.Gate, ans: Any,
            votes: dict[str, tuple[str | None, list[str]]]
            ) -> tuple[str | None, str | None, list[str]]:
    for step in steps:
        if step[0] == "nobody":
            return None, None, []
        if step[0] == "customer":
            return step[1], step[2], []
        domain = step[1]
        if domain not in votes:
            votes[domain] = _votes(domains.get(domain) or {}, gate, ans)
        who, evidence = votes[domain]
        if who:
            return who, step[2], evidence
    return None, None, []


def _votes(domain: Mapping[str, Any], gate: dt.Gate, ans: Any
           ) -> tuple[str | None, list[str]]:
    """``CustomerOf._read_threads``' vote over the reader's own first ``THREAD_MAIL``."""
    from . import business_documents as bd

    if domain.get("unread"):
        ans.counts["sender_threads_not_read"] += 1
        ans.complete = False
        return None, []
    held = list(domain.get("mail") or ())
    gate.load(m for m, _ in held)
    tally: dict[str, list[str]] = {}
    seen = 0
    for mail, roots in held:
        if seen >= bd.THREAD_MAIL:
            break
        if not gate.visible(mail):
            continue
        seen += 1
        for c in roots:
            tally.setdefault(c, []).append(mail)
    if not tally:
        return None, []
    best = max(tally, key=lambda c: (len(tally[c]), c))
    tied = sum(len(v) for v in tally.values())
    if len(tally[best]) >= 2 and len(tally[best]) >= bd.THREAD_SHARE * tied:
        return best, sorted(tally[best])[:3]
    return None, []


# --------------------------------------------------------------------------- the file

def state_of(store_path: Any) -> Path:
    """The state directory a served store's path sits in (``proactive_engine_inbox._people``)."""
    parent = Path(store_path).parent
    return parent.parent.parent.parent if parent.parent.name == "generations" \
        else parent.parent


def bank_path(store_path: Any) -> Path | None:
    env = os.environ.get(BANK_ENV)
    if env:
        return Path(env)
    return None if store_path is None else state_of(store_path) / BANK_FILE


_LOCK = threading.Lock()
_HELD: dict[str, tuple[tuple, dict[str, Any]]] = {}


def load(path: Path | None, guard: Any, *, now: float | None = None
         ) -> dict[str, Any] | None:
    """The kept bank when it may answer for a reader under ``guard`` now (module doc),
    else None (the per-reader read). Parsed once per file version."""
    if path is None or guard is None or not callable(getattr(guard, "fingerprint", None)):
        return None
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        with _LOCK:
            held = _HELD.get(str(path))
        if held is None or held[0] != stamp:
            bank = json.loads(path.read_text(encoding="utf-8"))
            with _LOCK:
                _HELD[str(path)] = (stamp, bank)
        else:
            bank = held[1]
        fingerprint = str(guard.fingerprint())
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(bank, dict) or bank.get("version") != VERSION \
            or bank.get("fingerprint") != fingerprint:
        return None
    built = bank.get("built_epoch")
    if not isinstance(built, (int, float)) \
            or (time.time() if now is None else now) - built > MAX_AGE_SECONDS:
        return None
    return bank


def build(conn: Any, guard: Any) -> dict[str, Any]:
    """The company's bank through ``BuildGate`` (module doc)."""
    gate = BuildGate(conn, may_read=lambda _k: True, held=(), guard=guard)
    if gate.unavailable:
        raise RuntimeError("people exclusion unavailable: " + str(gate.unavailable))
    began = time.time()
    bank = collect(conn, gate)
    bank.update(fingerprint=str(guard.fingerprint()), built_epoch=began,
                built_at=datetime.fromtimestamp(began, timezone.utc).isoformat(
                    timespec="seconds"))
    return bank


def write(path: Path, bank: Mapping[str, Any]) -> None:
    """Atomically, 0640 (the API's group reads it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".bank-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(bank, fh, separators=(",", ":"))
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python3 -m operating.answer_bank_store")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build the bank over the served store and write it")
    b.add_argument("--state-dir", type=Path, required=True)
    b.add_argument("--store", type=Path, default=None,
                   help="the served store path (default <state>/projection/ontology.sqlite)")
    b.add_argument("--if-stale", action="store_true",
                   help="do nothing when the kept bank is of the data served now")
    a = p.parse_args(argv)
    from .proactive_engine_inbox import _people
    from .proactive_engine_connections import _connect

    store = a.store or a.state_dir / "projection" / "ontology.sqlite"
    path = bank_path(store)
    assert path is not None
    stamp = _data_stamp()
    if a.if_stale and stamp is not None and path.is_file():
        try:
            kept = json.loads(path.read_text(encoding="utf-8"))
            if kept.get("data_stamp") == stamp and kept.get("version") == VERSION \
                    and time.time() - float(kept.get("built_epoch") or 0) \
                    < MAX_AGE_SECONDS / 2:
                print("answer_bank_store: current", file=sys.stderr)
                return 0
        except (OSError, ValueError, TypeError):
            pass
    guard = _people(Path(store))
    if guard is None:
        print("answer_bank_store: people exclusion unavailable; nothing built",
              file=sys.stderr)
        return 2
    conn = _connect(Path(store))
    try:
        bank = build(conn, guard)
    finally:
        conn.close()
    bank["data_stamp"] = stamp
    write(path, bank)
    print(f"answer_bank_store: {len(bank['docs'])} documents, "
          f"{sum(len(e.get('pairs') or ()) for e in bank['docs'])} answers in "
          f"{time.time() - bank['built_epoch']:.1f} s", file=sys.stderr)
    return 0


def _data_stamp() -> str | None:
    try:
        from operating.pg import ontology_reader
        return ontology_reader.change_stamp() if ontology_reader.pg_only() else None
    except Exception:  # noqa: BLE001 - unknown: built anew
        return None


if __name__ == "__main__":
    sys.exit(main())
