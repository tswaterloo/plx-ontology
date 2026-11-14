"""The forms each person filled and sent before, so a new copy of the same form can take the
answers that do not change from one copy to the next (their phone, account, rep line, a
standing PO, the samples a recurring test runs on).

    python3 -m operating.doc_fill_copies learn --state /data/plx-state [--workers N]

* **learn** reads every Word, Excel and PDF attachment a mailbox's owner sent whose file
  name reads as a form (``doc_fill_kinds.json``; card, bank, pay and safety-sheet kinds are
  not listed) and whose bytes hold fields ``doc_fill`` reads (``doc_style_mine
  .mail_documents``: one per message and file name; a document whose mailbox, sender or
  any recipient the people exclusion keeps out is skipped, and with the exclusion
  unavailable nothing is read). The index (``<state>/doc-fill/copies.json``, 0600) keeps,
  per author, each copy's source handle, sent time, digest and its fields' labels: its
  structure, never a value. Values are read from the copy itself when a fill uses it, as
  the filling reader, so they are never cached and never outlive the reader's access.
* **earlier** (``earlier``): an author's copies of the same form, newest first: same field
  labels (``SAME_FORM`` or more of them shared), sent before a time, never the form itself.
* **facts** (``copy_facts``): each copy's filled fields, keyed by the field's place and
  label (the same question in the same form), then only what ``MIN_COPIES`` or more copies
  answer alike (``doc_gather.copies_agree``), cited to the newest; only when those copies
  went to two or more counterparties: a value that stays the same only across copies to one
  customer (their consignee, their account) is theirs, not the filler's own, unless the form
  is for that customer (its email came from them) and the copies span a month or more. An index row
  without a counterparty (learned before it was kept) never counts toward that.

Reads the capture store only (read-only). Evidence: a run on the real store is
live-unverified.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from operating import doc_fill as df
from operating.doc_gather import MAX_COPIES, SAME_FORM
from ontology import parent_watch

SCHEMA = "plx-doc-fill-copies/v1"
KINDS_FILE = Path(__file__).with_name("doc_fill_kinds.json")
MIN_FIELDS = 2


def copy_key(field: df.FormField) -> str:
    """A field's place and label: the same question in another copy of the same form."""
    return f"Copy.{field.field_id}|{df.normalize(field.label)}"


def _labels(data: bytes, terms: df.Terms) -> Optional[list[str]]:
    try:
        fields = df.open_document(data, terms).fields()
    except Exception:  # noqa: BLE001 - a file that is not a form is not indexed
        return None
    labels = sorted({df.normalize(f.label) for f in fields if df.normalize(f.label)})
    return labels if len(labels) >= MIN_FIELDS else None


def _read_one(args: tuple[str, dict[str, Any]]) -> Optional[dict[str, Any]]:
    from operating import doc_style_mine as mine
    state, doc = Path(args[0]), args[1]
    try:
        if "mail" not in mine._STORE:
            from integrations.capture_mail import MailCaptureStore
            mine._STORE["mail"] = MailCaptureStore.open_read_only(state)
        data = mine._STORE["mail"].blob(doc["digest"])
    except Exception:  # noqa: BLE001 - a copy not read is not indexed
        return None
    labels = _labels(data, df.load_terms())
    if labels is None:
        return None
    return {"author": doc["author"], "source": doc["source"], "sent": doc["sent"],
            "digest": doc["digest"], "kind": doc["kind"], "labels": labels,
            "party": str(doc.get("counterparty") or "")}


def learn(state: Path, out: Path, *, workers: int = 0) -> dict[str, Any]:
    """Index every sent form (module doc). Returns counts only."""
    from concurrent.futures import ProcessPoolExecutor
    from operating import doc_style as ds
    from operating import doc_style_mine as mine
    guard = mine._guard(state)
    kinds = ds.load_kinds(KINDS_FILE)
    docs = [d for d in mine.mail_documents(state, guard, kinds) if d["kind"] != "skip"]
    with ProcessPoolExecutor(max_workers=workers or os.cpu_count() or 1,
                             **parent_watch.pool_init()) as pool:
        got = [g for g in pool.map(_read_one, [(str(state), d) for d in docs], chunksize=16)
               if g is not None]
    authors: dict[str, list[dict[str, Any]]] = {}
    for g in sorted(got, key=lambda x: x["sent"]):
        authors.setdefault(g.pop("author"), []).append(g)
    save(out / "copies.json", authors, dt.datetime.now(dt.timezone.utc).isoformat())
    return {"documents": len(docs), "forms": len(got), "authors": len(authors)}


def save(path: Path, authors: Mapping[str, Sequence[Mapping[str, Any]]], learned_at: str
         ) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"schema": SCHEMA, "learned_at": learned_at, "authors": authors}, handle)
    tmp.chmod(0o600)
    tmp.replace(path)


def load(path: Path) -> dict[str, list[dict[str, Any]]]:
    """The index; a file this code does not understand gives none (``FillRefused
    ("copies_invalid")``), never a guess."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if raw.get("schema") != SCHEMA or not isinstance(raw.get("authors"), dict):
            raise ValueError("schema")
        for rows in raw["authors"].values():
            for r in rows:
                if not (r["source"] and r["sent"] and r["digest"]
                        and isinstance(r["labels"], list)):
                    raise ValueError("row")
        return raw["authors"]
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise df.FillRefused("copies_invalid") from error


def earlier(index: Mapping[str, Sequence[Mapping[str, Any]]], author: str,
            fields: Iterable[df.FormField], *, before: Optional[str] = None,
            not_source: Optional[str] = None, limit: int = 12) -> list[Mapping[str, Any]]:
    """The author's earlier copies of this form, newest first (module doc)."""
    mine = {df.normalize(f.label) for f in fields if df.normalize(f.label)}
    if len(mine) < MIN_FIELDS:
        return []
    out = []
    for row in reversed(index.get(str(author or "").lower(), ())):
        if before and str(row["sent"]) >= before:
            continue
        if row["source"] == not_source:
            continue
        theirs = set(row["labels"])
        if len(mine & theirs) / len(mine | theirs) >= SAME_FORM:
            out.append(row)
            if len(out) >= limit:
                break
    return out


def opened(call: Callable[[str, dict[str, Any]], Any], rows: Sequence[Mapping[str, Any]],
           blob: Callable[[str], bytes]) -> list[tuple[str, bytes, str, str]]:
    """The copies the reader opens (their own ``plx.read`` of each resolves), with their
    bytes (``blob`` by the digest the index holds, checked against the read's own digest
    when it gives one) and the counterparty it went to ("" when the index does not say),
    newest first, at most ``MAX_COPIES``; a copy refused, changed or unreadable is left
    out, never read around."""
    out: list[tuple[str, bytes, str, str]] = []
    for row in rows:
        if len(out) >= MAX_COPIES:
            break
        try:
            read = call("plx.read", {"citation": row["source"], "max_chars": 200})
        except Exception:  # noqa: BLE001 - refused or failed alike: not theirs
            continue
        if not isinstance(read, Mapping) or read.get("status") != "resolved":
            continue
        digest = (read.get("fields") or {}).get("row.content_sha256")
        digest = digest.get("untrusted_text") if isinstance(digest, Mapping) else digest
        if digest and digest != row["digest"]:
            continue
        try:
            out.append((row["source"], blob(row["digest"]), str(row.get("party") or ""),
                        str(row["sent"])))
        except Exception:  # noqa: BLE001 - a copy whose bytes are gone is not used
            continue
    return out


def copy_facts(copies: Sequence[tuple[str, bytes, str, str]], terms: df.Terms,
               target: str = "") -> list[df.Fact]:
    """What ``MIN_COPIES`` or more of the copies (``(source handle, bytes, counterparty,
    sent)``, newest first) answer alike for one field, those copies having gone to ``MIN_PARTIES``
    or more counterparties, or all to ``target`` (the party the form's email came from) over
    ``MIN_SPAN`` days or more (module doc)."""
    from operating import doc_gather as dg
    per_copy = []
    parties = []
    sent = []
    for handle, data, party, when in copies[:MAX_COPIES]:
        try:
            fields = df.open_document(data, terms).fields()
        except df.FillRefused:
            continue
        parties.append(party)
        sent.append(when)
        per_copy.append([df.Fact(copy_key(f), f.current, df.Citation(handle, f.label[:80],
                                                                      handle), tier=dg.COPIES)
                         for f in fields if f.current.strip() and f.current != "="])
    return dg.copies_agree(per_copy, parties, target, sent)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("learn",))
    parser.add_argument("--state", "--state-dir", dest="state", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--workers", type=int, default=0)
    args = parser.parse_args(argv)
    print(json.dumps(learn(args.state, args.out or args.state / "doc-fill",
                           workers=args.workers)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MAX_COPIES", "SAME_FORM", "copy_facts", "copy_key", "earlier", "learn", "load",
           "opened", "save"]
