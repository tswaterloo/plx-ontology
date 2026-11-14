"""Learn every person's document templates from what they sent (``operating.doc_style``),
and measure them on documents held out.

    python3 -m operating.doc_style_mine learn --state /data/plx-state [--before DAY]
    python3 -m operating.doc_style_mine heldout --state /data/plx-state --report OUT.json

* **Documents**: Word, Excel and PDF attachments a mailbox's owner sent (the message's
  sender is the mailbox), one per message and file name, and SharePoint files by who last
  saved them; the kind from the file name (``doc_style_kinds.json``), none: not read.
  A document is skipped when its mailbox, sender or any recipient is a person the people
  exclusion keeps out; with the exclusion unavailable nothing is read (fail closed).
* **learn** writes ``<state>/doc-style/templates.json`` and each template's exemplar
  bytes (``exemplars/<sha256>``), both owner-only (0600). Reading the documents uses every
  core (``--workers``).
* **heldout**: for each template group of three or more, its latest document is hidden,
  the template learned from the ones sent before it, a draft made (facts: none, or
  ``facts_for`` when a caller gives one) and compared with what was sent
  (``doc_style.compare``). The report holds scores and counts per author and kind, never
  document text.

Reads the capture stores only (read-only connections). Evidence: a run on the real stores
is live-unverified until a verifier result exists.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import datetime as dt
import glob
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence

from operating import doc_style as ds

EXT = re.compile(r"\.(docx|xlsx|pdf)$", re.I)
MAX_BYTES = 8 * 1024 * 1024
MIN_HELDOUT = 3


def _excluded(guard: Any, addresses: Iterable[str]) -> bool:
    """Whether any address is a person kept out; one of the tenant the guard cannot decide
    counts as kept out (``truth_check.excluded_hit``)."""
    tenant = guard.known_domains()
    for a in addresses:
        a = str(a or "").lower()
        if "@" not in a:
            continue
        if guard.decides(a):
            if guard.is_excluded(a):
                return True
        elif a.split("@", 1)[1] in tenant:
            return True
    return False


def mail_documents(state: Path, guard: Any, kinds: Sequence[Any], *,
                   before: Optional[str] = None) -> Iterator[dict[str, Any]]:
    """Sent documents in the mail capture (module doc)."""
    db = sqlite3.connect((state / "mail-capture" / "capture.db").as_uri() + "?mode=ro",
                         uri=True)
    seen: set[tuple[str, str]] = set()
    rows = db.execute(
        "SELECT m.record, a.metadata, a.l1_handle, a.content_sha256, m.mailbox_id "
        "FROM attachments a JOIN messages m ON m.l1_handle = a.message_handle "
        "WHERE m.superseded_at IS NULL AND a.content_status = 'captured'")
    for record, metadata, handle, digest, mailbox in rows:
        meta = json.loads(metadata)
        name = str(meta.get("name") or "")
        if meta.get("is_inline") or not EXT.search(name) or int(meta.get("size") or 0) \
                > MAX_BYTES:
            continue
        kind = ds.kind_of(name, kinds)
        if kind is None or not digest:
            continue
        rec = json.loads(record)
        sender = str(rec.get("from") or "").lower()
        if not sender or sender != str(rec.get("mailbox_upn") or "").lower():
            continue
        sent = str(rec.get("sent_at") or "")
        if before and sent[:10] >= before:
            continue
        key = (str(rec.get("internet_message_id") or handle), name.lower())
        if key in seen:
            continue
        seen.add(key)
        if guard.is_excluded(mailbox) or _excluded(
                guard, [sender, *(rec.get("to") or []), *(rec.get("cc") or [])]):
            continue
        to = [str(a) for a in [*(rec.get("to") or []), *(rec.get("cc") or [])]]
        yield {"author": sender, "kind": kind, "source": handle, "sent": sent,
               "digest": digest, "store": "mail",
               "counterparty": ds.party_of(to, guard.known_domains()),
               "names": to, "subject": str(rec.get("subject") or "")[:300]}


def sharepoint_documents(state: Path, guard: Any, kinds: Sequence[Any], *,
                         before: Optional[str] = None) -> Iterator[dict[str, Any]]:
    """SharePoint files by the person who last saved them (module doc)."""
    for path in sorted(glob.glob(str(state / "sharepoint-capture" / "*.db"))):
        db = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
        for record, handle, digest in db.execute(
                "SELECT record, l1_handle, content_sha256 FROM items WHERE superseded_at IS "
                "NULL AND content_status = 'captured' AND content_sha256 IS NOT NULL"):
            rec = json.loads(record)
            name = str(rec.get("name") or "")
            author = str(rec.get("last_modified_by") or "").lower()
            if not EXT.search(name) or "@" not in author or int(rec.get("size") or 0) \
                    > MAX_BYTES:
                continue
            kind = ds.kind_of(name, kinds)
            sent = str(rec.get("last_modified") or "")
            if kind is None or (before and sent[:10] >= before) or _excluded(guard, [author]):
                continue
            yield {"author": author, "kind": kind, "source": handle, "sent": sent,
                   "digest": digest, "store": "sharepoint", "db": path}


def _blob(state: Path, doc: Mapping[str, Any]) -> bytes:
    if doc["store"] == "mail":
        from integrations.capture_mail import MailCaptureStore
        return MailCaptureStore.open_read_only(state).blob(doc["digest"])
    from integrations import blob_pack
    from integrations.captured_text import COMPRESSED_SUFFIX, blob_path, inflate
    root = Path(doc["db"]).parent / "blobs" / "sha256"
    try:
        return blob_path(root, doc["digest"]).read_bytes()
    except FileNotFoundError:
        return inflate(blob_pack.read(root, doc["digest"], COMPRESSED_SUFFIX))


_STORE: dict[str, Any] = {}


def _read_one(args: tuple[str, dict[str, Any]]) -> Optional[tuple[dict[str, Any], Any]]:
    state, doc = Path(args[0]), args[1]
    try:
        if doc["store"] == "mail":
            if "mail" not in _STORE:
                from integrations.capture_mail import MailCaptureStore
                _STORE["mail"] = MailCaptureStore.open_read_only(state)
            data = _STORE["mail"].blob(doc["digest"])
        else:
            data = _blob(state, doc)
        reading = ds.read(data)
    except Exception:  # noqa: BLE001 - a document not read is counted, never guessed
        return None
    # a SharePoint file names no counterparty: its repeated text is never fixed
    return doc, ds.Member(doc["source"], doc["sent"], doc["digest"], reading.units,
                          reading.format, doc.get("counterparty", ""),
                          tuple(doc.get("names") or ()), doc.get("subject", ""))


def read_all(state: Path, docs: Sequence[dict[str, Any]], workers: int
             ) -> list[tuple[dict[str, Any], Any]]:
    with ProcessPoolExecutor(max_workers=workers) as pool:
        got = pool.map(_read_one, [(str(state), d) for d in docs], chunksize=8)
        return [g for g in got if g is not None]


def _guard(state: Path) -> Any:
    from operating.truth_check import people_guard
    guard, why = people_guard(state, os.environ)
    if guard is None:
        raise SystemExit(f"people exclusion unavailable ({why}): nothing read")
    return guard


def learn(state: Path, out: Path, *, before: Optional[str] = None, workers: int = 0
          ) -> dict[str, Any]:
    """Learn and store every template (module doc). Returns counts only."""
    guard, kinds = _guard(state), ds.load_kinds()
    docs = list(mail_documents(state, guard, kinds, before=before)) + list(
        sharepoint_documents(state, guard, kinds, before=before))
    read = read_all(state, docs, workers or os.cpu_count() or 1)
    templates = ds.learn_all((d["author"], d["kind"], member) for d, member in read)
    exemplars = out / "exemplars"
    exemplars.mkdir(parents=True, exist_ok=True, mode=0o700)
    by_digest = {d["digest"]: d for d, _m in read}
    for t in templates:
        target = exemplars / t["exemplar"]["digest"]
        if not target.exists():
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(_blob(state, by_digest[t["exemplar"]["digest"]]))
    ds.save(out / "templates.json", templates, dt.datetime.now(dt.timezone.utc).isoformat())
    return {"documents": len(docs), "read": len(read), "templates": len(templates)}


def heldout(groups: Mapping[tuple[str, str], Sequence[Any]],
            exemplar: Callable[[Any], bytes], terms: Any, *,
            facts_for: Optional[Callable[[Any], list[Any]]] = None) -> dict[str, Any]:
    """Each group's latest document hidden and made again from the earlier ones (module
    doc). ``groups``: (author, kind) -> its members (one cluster); ``exemplar`` gives a
    member's bytes. Scores per (author, kind), never text."""
    rows = []
    for (author, kind), members in sorted(groups.items()):
        members = sorted(members, key=lambda x: x.sent)
        if len(members) < MIN_HELDOUT:
            continue
        hidden, earlier = members[-1], [m for m in members[:-1] if m.sent < members[-1].sent]
        template = ds.learn(author, kind, earlier)
        if template is None:
            continue
        facts = facts_for(hidden) if facts_for else []
        day = dt.date.fromisoformat(hidden.sent[:10])
        try:
            made = ds.generate(template, exemplar(earlier[-1]), facts, terms, as_of=day)
        except ds.StyleRefused as refused:
            rows.append({"author": author, "kind": kind, "error": refused.code})
            continue
        score = ds.compare(template, made.units, hidden.units)
        rows.append({"author": author, "kind": kind, "format": template["format"],
                     "support": template["support"], **score})
    return {"documents": len(rows), "rows": rows}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("learn", "heldout"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--before")
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.command == "learn":
        got = learn(args.state, args.out or args.state / "doc-style", before=args.before,
                    workers=args.workers)
        print(json.dumps(got))
        return 0
    from operating import doc_fill as df
    guard, kinds = _guard(args.state), ds.load_kinds()
    docs = list(mail_documents(args.state, guard, kinds, before=args.before))
    read = read_all(args.state, docs, args.workers or os.cpu_count() or 1)
    by_author: dict[tuple[str, str], list[Any]] = {}
    for d, member in read:
        by_author.setdefault((d["author"], d["kind"]), []).append(member)
    groups = {}
    for key, members in by_author.items():
        for n, group in enumerate(ds.cluster(members)):
            groups[(key[0], f"{key[1]}#{n}")] = group
    digests = {d["source"]: d for d, _m in read}
    report = heldout(groups, lambda mem: _blob(args.state, digests[mem.source]),
                     df.load_terms())
    for row in report["rows"]:
        row["kind"] = row["kind"].split("#", 1)[0]
    if args.report:
        args.report.write_text(json.dumps(report, indent=1))
    print(json.dumps({"documents": report["documents"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
