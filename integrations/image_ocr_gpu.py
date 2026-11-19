"""``python3 -m integrations.image_ocr_gpu``: read the mail store's OCR-read images again
with a local vision-language model on the GPU hosts (the DGX Sparks).

Tesseract reads 0.684 of the hand-labelled image fields (50 images, 383 fields; its best
variant per image ~0.76); a local Qwen3-VL model on a Spark reads ~0.93 of them.
This pass makes
those readings; it never changes the capture:

* ``plan``: every distinct image (by content hash) the mail store holds an OCR reading of
  (``extracted_ocr`` / ``ocr_no_text``, an OCR image type), read from the store opened
  read-only, into this pass's own ``image-ocr-gpu.sqlite`` (its one writer).
* ``read``: each planned image not yet read by the configured engine is checked again,
  just before its bytes leave the box (``Gate``), and only then shipped over an ``ssh``
  pipe to ``integrations/image_ocr_gpu_worker.py`` on each host (the embed pass's pattern,
  ``operating.embed.remote``). An image is sent only when every attachment row carrying
  it passes: a mailbox the people exclusion does not withhold, every sender and
  recipient on the tenant's mail domains decided and not excluded (the capture's recorded
  directory and tenant listing attached; the list re-read during the run), no address
  the capture's purge left undecided, and no Freightcom in the file name, the whole
  message record, the whole body or any OCR text stored of it (its data never goes
  through an AI model). Any doubt refuses (fail closed; counted, never sent). The
  answers are stored by content hash under the engine's key (model, revision, prompt,
  bounds): resumable, an image read twice is stored once.

What the readings change is decided by a later apply step (the capture's own write path,
its own review): the reading is the GPU text; a ``truncated`` or ``unreadable`` answer
never replaces anything. No credential reaches the hosts; nothing but image bytes and
the worker leaves the box. Prints JSON lines of counts only (never a text, name, address
or hash). Exit 0 done, 2 refused, 1 failed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from operating import declared_schema as _schemas
from operating.declared_schema import image_ocr_gpu as _IMAGE_OCR_GPU

from . import image_ocr_gpu_worker as worker
from .address_forms import normalize_address, split_addresses

DB_NAME = "image-ocr-gpu.sqlite"
OCR_READ = ("extracted_ocr", "ocr_no_text")
from .freightcom_rule import FREIGHTCOM, names as _names_freightcom  # noqa: E402  the one rule
# the message fields whose addresses the exclusion decides
_ADDRESSED = ("from", "sender", "toRecipients", "ccRecipients", "bccRecipients", "to", "cc",
              "bcc", "replyTo")


def engine_key(hello: Mapping[str, Any]) -> str:
    """The key of the readings one engine makes: a reading under another key is another
    engine's (a changed prompt or bound is a new key)."""
    keep = {k: hello.get(k) for k in ("engine", "model", "revision", "prompt_sha256",
                                      "max_pixels", "max_new_tokens", "repetition_penalty", "worker")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:16]


def expected_hello() -> dict[str, Any]:
    return worker.hello_of("cuda")


def check_hello(hello: Mapping[str, Any]) -> str | None:
    """Why a host's worker is not the configured engine; None when it is."""
    want = expected_hello()
    for key in ("engine", "model", "revision", "prompt_sha256", "max_pixels",
                "max_new_tokens", "repetition_penalty", "worker"):
        if hello.get(key) != want[key]:
            return f"host_{key}_differs"
    return None


class Store:
    """``<dir>/image-ocr-gpu.sqlite``: the plan, the readings by content hash and engine
    key, and why an image was refused (no reason text beyond a code)."""

    def __init__(self, root: str | Path) -> None:
        path = Path(root).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path / DB_NAME, timeout=60, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.db:
            self.db.execute("PRAGMA journal_mode=WAL")
        _schemas.apply(self.db, _IMAGE_OCR_GPU)

    def plan(self, mail_db: sqlite3.Connection, is_ocr_image: Callable[[str, str], bool]
             ) -> dict[str, int]:
        counts = {"seen": 0, "not_image": 0, "listed": 0}
        now, batch = time.time(), set()
        rows = mail_db.execute(
            "SELECT content_sha256, metadata FROM attachments WHERE content_status='captured' "
            "AND text_status IN (?, ?) AND content_sha256 IS NOT NULL", OCR_READ)
        for sha, meta in rows:
            counts["seen"] += 1
            try:
                m = json.loads(meta)
            except ValueError:
                counts["not_image"] += 1
                continue
            if not is_ocr_image(str(m.get("name") or ""), str(m.get("content_type") or "")):
                counts["not_image"] += 1
                continue
            batch.add(sha)
        with self.lock, self.db:
            before = self.db.execute("SELECT COUNT(*) FROM plan").fetchone()[0]
            self.db.executemany("INSERT OR IGNORE INTO plan VALUES (?, ?)",
                                [(s, now) for s in sorted(batch)])
            counts["listed"] = self.db.execute("SELECT COUNT(*) FROM plan").fetchone()[0] - before
        counts["planned"] = before + counts["listed"]
        return counts

    def pending(self, key: str, after: str, limit: int) -> list[str]:
        with self.lock:
            return [r[0] for r in self.db.execute(
                "SELECT p.content_sha256 FROM plan p WHERE p.content_sha256 > ? AND NOT EXISTS "
                "(SELECT 1 FROM readings r WHERE r.content_sha256=p.content_sha256 AND "
                "r.engine_key=?) ORDER BY p.content_sha256 LIMIT ?", (after, key, limit))]

    def put(self, key: str, shas: Sequence[str], results: Sequence[Mapping[str, Any]]) -> None:
        now = time.time()
        with self.lock, self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?,?)",
                [(s, key, r["status"], r.get("text"), int(r.get("pages") or 0),
                  int(bool(r.get("truncated"))), now) for s, r in zip(shas, results)])
            self.db.executemany("DELETE FROM refused WHERE content_sha256=? AND engine_key=?",
                                [(s, key) for s in shas])

    def refuse(self, key: str, sha: str, reason: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO refused VALUES (?,?,?,?)",
                            (sha, key, reason, time.time()))

    def counts(self, key: str) -> dict[str, int]:
        with self.lock:
            out = {"planned": self.db.execute("SELECT COUNT(*) FROM plan").fetchone()[0]}
            for status, n in self.db.execute("SELECT status, COUNT(*) FROM readings WHERE "
                                             "engine_key=? GROUP BY status", (key,)):
                out[f"read_{status}"] = n
            out["truncated"] = self.db.execute("SELECT COUNT(*) FROM readings WHERE "
                                               "engine_key=? AND truncated=1", (key,)).fetchone()[0]
            out["refused"] = self.db.execute("SELECT COUNT(*) FROM refused WHERE engine_key=?",
                                             (key,)).fetchone()[0]
        return out


# --- who may see an image -----------------------------------------------------------

def _addresses(value: Any, *, name: bool = False) -> Iterable[tuple[str, bool]]:
    """Every string in a recipient field (the stored record's bare strings, a Graph
    ``emailAddress`` mapping, or a list of them), each with whether it is a display name
    (a Graph ``name`` key) rather than an address value. A value of another type is
    yielded as it is (``Gate`` refuses it: nothing can decide it)."""
    if isinstance(value, str):
        yield value, name
    elif isinstance(value, Mapping):
        for k, v in value.items():
            if k in ("address", "emailAddress", "email", "name") or isinstance(v, (Mapping, list)):
                yield from _addresses(v, name=k == "name")
    elif isinstance(value, list):
        for v in value:
            yield from _addresses(v, name=name)
    elif value is not None:
        yield value, name


def touches_freightcom(*texts: Any) -> bool:
    """Whether any of ``texts`` (a file name, a whole message record, a body, an OCR
    text) names Freightcom (``freightcom_rule``). Over-matching ("Freight Comments")
    refuses: fail closed."""
    return _names_freightcom(*texts)


class Refused(Exception):
    """An image may not leave the box; ``code`` says why (no name, no address)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class EnvFileExclusion:
    """The pass's people exclusion: a ``ReloadingExclusion`` (re-read when the list file
    changes or ``ttl`` passes, the mail capture's directory and listing attached) built from
    ``environ`` merged with the passes' env file (``environ_with_exclusion``), built again
    whenever that env file changes (inode, size, mtime; checked on every ``current()``), so
    a person added to its inline ``PLX_EXCLUDED_PEOPLE_OIDS`` applies within the run. Only
    when ``environ`` names neither the list nor its file itself (as the merge). A failed
    build is the unavailable guard (fail closed). Only the exclusion's own variables are
    kept from ``environ``."""

    def __init__(self, environ: Mapping[str, str], *, state_dir: Any = None,
                 **options: Any) -> None:
        from .people_exclusion import (DEFAULT_ENV_FILE, ENV_FILE, ENV_FILE_SETTING,
                                       ENV_IDS)
        self._source = {n: environ[n] for n in (ENV_IDS, ENV_FILE, ENV_FILE_SETTING)
                        if n in environ}
        self._path: str | None = None
        if environ.get(ENV_IDS) is None and not environ.get(ENV_FILE):
            self._path = self._source.get(ENV_FILE_SETTING, DEFAULT_ENV_FILE) or None
        self._state_dir, self._options = state_dir, options
        self._stamp: Any = ()
        self._inner: Any = None
        self.current()

    def _file_stamp(self) -> Any:
        if self._path is None:
            return None
        try:
            info = Path(self._path).expanduser().stat()
        except OSError:
            return None
        return info.st_ino, info.st_size, info.st_mtime_ns

    def current(self) -> Any:
        from .people_exclusion import (PeopleExclusion, ReloadingExclusion,
                                       environ_with_exclusion)
        stamp = self._file_stamp()
        if self._inner is None or stamp != self._stamp:
            try:
                merged = environ_with_exclusion(self._source)
                self._inner = ReloadingExclusion.from_env(merged, state_dir=self._state_dir,
                                                          **self._options)
            except Exception:  # noqa: BLE001 - an unreadable env file refuses everything
                self._inner = PeopleExclusion(unavailable="excluded_people_file_unreadable")
            self._stamp = stamp
        current = getattr(self._inner, "current", None)
        return current() if callable(current) else self._inner


class Gate:
    """Whether an image may go to a GPU host, decided just before its bytes would leave.

    The people exclusion is the capture's own offline guard: the list (re-read when its
    file changes or every ``ttl`` seconds, ``ReloadingExclusion``) with the directory and
    tenant listing the mail capture recorded attached (``with_directory``). Every
    sender/recipient on the tenant's mail domains is decided; one it cannot decide refuses
    (as ``ontology.project._UndecidedWithheld``), and so does any address the capture's
    last purge left undecided (``undecided_addresses``) and every carrier in a mailbox the
    exclusion withholds. No usable guard or no known tenant domain refuses every image.
    Freightcom: the file name, the whole message record, the whole body and every OCR
    text the store holds of the attachment (current and earlier versions) are read."""

    def __init__(self, exclusion: Any, mail: Any, *, ttl: float = 300.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._exclusion = exclusion   # a ReloadingExclusion (or a guard: never re-read)
        self._mail = mail
        self._ttl, self._clock = ttl, clock
        self._seen: Any = None
        self._at = -1e18
        self.guard: Any = None
        self.domains: frozenset[str] = frozenset()
        self.withheld: set[str] | None = None
        self.undecided: set[str] = set()
        self._bodies: dict[str, bool] = {}
        self.refresh()

    @classmethod
    def from_env(cls, env: Mapping[str, str], mail: Any, state_dir: str | Path, **options: Any
                 ) -> "Gate":
        # the passes' env file is read again whenever it changes: a person added to its
        # inline list during the run applies within the run (``EnvFileExclusion``)
        return cls(EnvFileExclusion(env, state_dir=state_dir), mail, **options)

    def refresh(self) -> None:
        """Re-read the guard (a changed list applies within the run) and, when it changed
        or ``ttl`` passed, the withheld mailboxes, tenant domains and undecided addresses."""
        current = getattr(self._exclusion, "current", None)
        guard = current() if callable(current) else self._exclusion
        now = self._clock()
        if guard is self._seen and now - self._at < self._ttl:
            return
        self._seen, self._at, self.guard = guard, now, guard
        self.withheld, self.domains, self.undecided = None, frozenset(), set()
        try:
            if guard is None or getattr(guard, "unavailable", None):
                return
            withheld = self._mail.withheld_mailboxes(guard)
            domains = {str(d).lower() for d in guard.known_domains()}
            for (identities,) in self._mail.db.execute("SELECT identities FROM mailboxes"):
                for a in json.loads(identities or "[]"):
                    a = normalize_address(a)
                    if "@" in a:
                        domains.add(a.rsplit("@", 1)[1])
            try:
                undecided = {normalize_address(a) for (a,) in self._mail.db.execute(
                    "SELECT address FROM undecided_addresses")} - {""}
            except sqlite3.OperationalError as error:
                if "no such table" not in str(error):
                    raise
                undecided = set()
        except Exception:  # noqa: BLE001 - nothing readable: refuse everything
            return
        if withheld is not None:
            self.withheld = {str(m).lower() for m in withheld}
        self.domains, self.undecided = frozenset(domains), undecided

    def why_not(self, mail_db: sqlite3.Connection, sha: str) -> str | None:
        """None when the image may go to a GPU host, else the refusal's code. Every
        attachment row carrying these bytes (any status, any version) must pass; none is a
        refusal; anything unreadable refuses."""
        try:
            self.refresh()
            self._check(mail_db, sha)
        except Refused as refused:
            return refused.code
        except Exception:  # noqa: BLE001 - a broken check refuses
            return "check_failed"
        return None

    def _require(self) -> Any:
        if self.guard is None or getattr(self.guard, "unavailable", None) \
                or self.withheld is None:
            raise Refused("exclusion_unavailable")
        if not self.domains:
            raise Refused("tenant_domains_unknown")
        return self.guard

    def _check(self, mail_db: sqlite3.Connection, sha: str) -> None:
        guard = self._require()
        rows = mail_db.execute(
            "SELECT a.mailbox_id, a.metadata, a.l1_handle, a.text_sha256, a.message_handle "
            "FROM attachments a WHERE a.content_sha256 = ?", (sha,)).fetchall()
        if not rows:
            raise Refused("no_carrier")
        for mailbox, meta, handle, text_sha, message in rows:
            try:
                name = (json.loads(meta) or {}).get("name")
            except (ValueError, TypeError, AttributeError):
                raise Refused("record_unreadable") from None
            if touches_freightcom(name):
                raise Refused("freightcom")
            texts = {text_sha} if text_sha else set()
            if handle:
                texts.update(t for (t,) in mail_db.execute(
                    "SELECT text_sha256 FROM attachment_text_versions WHERE l1_handle=? AND "
                    "text_sha256 IS NOT NULL", (handle,)))
            for text in sorted(texts):
                if touches_freightcom(self._text(text)):
                    raise Refused("freightcom")
            self.check_message(mail_db, mailbox, message, guard)

    def check_message(self, mail_db: sqlite3.Connection, mailbox: Any, message: Any,
                      guard: Any = None) -> None:
        """Raise ``Refused`` unless the captured message ``message`` (every stored version
        under that handle) in ``mailbox`` may be read by the model."""
        guard = guard or self._require()
        if mailbox is None or str(mailbox).lower() in (self.withheld or ()):
            raise Refused("withheld_mailbox")
        try:
            if guard.is_excluded(str(mailbox)):
                raise Refused("withheld_mailbox")
        except Refused:
            raise
        except Exception:  # noqa: BLE001 - a mailbox it cannot decide is withheld
            raise Refused("exclusion_undecided") from None
        versions = mail_db.execute("SELECT record, body_sha256 FROM messages WHERE "
                                   "l1_handle = ?", (message,)).fetchall()
        if not versions:
            raise Refused("message_missing")
        for record, body in versions:
            try:
                rec = json.loads(record)
            except (ValueError, TypeError):
                raise Refused("record_unreadable") from None
            if not isinstance(rec, Mapping):
                raise Refused("record_unreadable")
            if touches_freightcom(rec):
                raise Refused("freightcom")
            # a copy withheld in another mailbox (an excluded person sent or got it, e.g.
            # as Bcc): this copy waits for the capture's purge and is never sent meanwhile
            if self._mail.is_withheld(rec.get("internet_message_id")
                                      or rec.get("internetMessageId")):
                raise Refused("people_withheld")
            if body and self._body_freightcom(body):
                raise Refused("freightcom")
            self._people(rec, guard)

    def _tenant_domain(self, domain: str) -> bool:
        return domain in self.domains or any(domain.endswith("." + d) for d in self.domains)

    def _people(self, rec: Mapping[str, Any], guard: Any) -> None:
        """Every sender/recipient value, normalized (``normalize_address``). An Exchange
        X.500 identity (``/o=...``, even one holding an ``@``, or Exchange's encoded
        ``IMCEAEX-...@host`` form of one, decoded; one that does not decode, or any other
        ``IMCEA`` value, refuses) and an address on a tenant
        mail domain (or a subdomain of one) are decided by the guard (directory, listing;
        an X.500 one only through an object id the directory maps it to, ``_x500``); one it
        cannot decide refuses. Any other value without ``@`` (an identity no directory can map) refuses.
        A value holding several addresses (``;`` or ``,`` between them) is judged address
        by address; one still holding two refuses. A display name is only checked when it
        holds an address or an X.500 identity."""
        for field in _ADDRESSED:
            for value, is_name in _addresses(rec.get(field)):
                if not isinstance(value, str):
                    raise Refused("exclusion_undecided")
                if is_name and "@" not in value and "/o=" not in value.lower():
                    continue   # a display name, not an identity
                for part in split_addresses(value):
                    self._one(part, is_name, guard)

    def _one(self, value: str, is_name: bool, guard: Any) -> None:
        address = normalize_address(value)
        if not address:
            if is_name or not value.strip():
                return   # an empty value names nobody
            raise Refused("exclusion_undecided")
        if address in self.undecided:
            raise Refused("exclusion_undecided")
        if address.startswith("imcea"):
            # an Exchange-encapsulated identity normalize_address could not decode to one
            # X.500 identity (IMCEAEX), or another IMCEA type: nothing maps it
            raise Refused("exclusion_undecided")
        if "/o=" in address:
            if not address.startswith("/o="):
                raise Refused("exclusion_undecided")   # text around an X.500 identity
            self._x500(address, guard)
            return
        if "@" not in address or address.count("@") > 1:
            raise Refused("exclusion_undecided")   # nothing maps it: undecidable
        elif not self._tenant_domain(address.rsplit("@", 1)[1]):
            return   # outside the tenant's mail domains: no tenant person's
        try:
            if guard.is_excluded(address):
                raise Refused("excluded_person")
        except Refused:
            raise
        except Exception:  # noqa: BLE001 - a tenant address it cannot decide
            raise Refused("exclusion_undecided") from None

    @staticmethod
    def _x500(identity: str, guard: Any) -> None:
        """An Exchange X.500 identity is decided only by the object id the guard's attached
        directory maps it to (``object_id_of``), then by that id; one nothing maps refuses.
        Never ``is_excluded`` on the identity itself: a bound guard answers "no excluded
        person's" for a value its listing does not hold, which here would let it out.
        (The exclusion keys SMTP addresses only today, so every X.500 identity refuses.)"""
        try:
            oid = guard.object_id_of(identity)
        except Exception:  # noqa: BLE001 - a directory it cannot read maps nothing
            oid = None
        if not isinstance(oid, str) or not oid:
            raise Refused("exclusion_undecided")
        try:
            excluded = guard.is_excluded(oid)
        except Exception:  # noqa: BLE001 - an id it cannot decide
            raise Refused("exclusion_undecided") from None
        if excluded:
            raise Refused("excluded_person")

    def _text(self, digest: str) -> str:
        try:
            return self._mail.blob(digest).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - an OCR text it cannot read: unknown content
            raise Refused("text_unreadable") from None

    def _body_freightcom(self, digest: str) -> bool:
        if digest not in self._bodies:
            if len(self._bodies) > 200_000:
                self._bodies.clear()
            try:
                body = self._mail.blob(digest).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - a body it cannot read: unknown content
                raise Refused("body_unreadable") from None
            self._bodies[digest] = touches_freightcom(body)
        return self._bodies[digest]


# --- the hosts ------------------------------------------------------------------------

class HostFailed(RuntimeError):
    pass


class Host:
    """One worker behind a pipe (``ssh host python image_ocr_gpu_worker.py``)."""

    def __init__(self, name: str, argv: Sequence[str], log: Path | None = None, *,
                 answer_seconds: float = 1800.0) -> None:
        self.name = name
        self.answer_seconds = answer_seconds
        self._log = open(log, "ab") if log else subprocess.DEVNULL
        self.proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self._log,
                                     env=_scrubbed(os.environ))
        self.hello: dict[str, Any] = {}

    def _frame(self) -> dict[str, Any]:
        try:
            body = worker.read_frame(self.proc.stdout)
        except (OSError, ValueError, EOFError) as error:
            raise HostFailed(f"{self.name}:{type(error).__name__}") from error
        if body is None:
            raise HostFailed(f"{self.name}:pipe_ended")
        try:
            out = json.loads(body.decode("utf-8"))
        except ValueError as error:
            raise HostFailed(f"{self.name}:bad_frame") from error
        if not isinstance(out, dict):
            raise HostFailed(f"{self.name}:bad_frame")
        return out

    def start(self) -> dict[str, Any]:
        timer = threading.Timer(self.answer_seconds, self.proc.kill)
        timer.daemon = True
        timer.start()
        try:
            self.hello = self._frame()
        finally:
            timer.cancel()
        why = check_hello(self.hello)
        if why:
            raise HostFailed(f"{self.name}:{why}")
        return self.hello

    def read(self, images: Sequence[bytes]) -> list[dict[str, Any]]:
        """The host's results for ``images``; a host that has not answered within
        ``answer_seconds`` (hung) is killed, which fails it like a pipe that ended."""
        timer = threading.Timer(self.answer_seconds, self.proc.kill)
        timer.daemon = True
        timer.start()
        try:
            return self._read(images)
        finally:
            timer.cancel()

    def _read(self, images: Sequence[bytes]) -> list[dict[str, Any]]:
        try:
            worker.write_frame(self.proc.stdin, json.dumps({"n": len(images)}).encode())
            for image in images:
                worker.write_frame(self.proc.stdin, image)
            self.proc.stdin.flush()
        except (OSError, ValueError) as error:
            raise HostFailed(f"{self.name}:{type(error).__name__}") from error
        answer = self._frame()
        results = answer.get("results")
        if "error" in answer or answer.get("n") != len(images) or not isinstance(results, list) \
                or len(results) != len(images):
            raise HostFailed(f"{self.name}:bad_answer")
        for r in results:
            if not isinstance(r, dict) or r.get("status") not in ("text", "no_text",
                                                                   "unreadable") \
                    or (r["status"] == "text") != isinstance(r.get("text"), str):
                raise HostFailed(f"{self.name}:bad_result")
        return [clean_result(r) for r in results]

    def close(self) -> None:
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.write(b"\0\0\0\0")
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()
        if self._log is not subprocess.DEVNULL:
            self._log.close()


# What the box keeps of a host's text: control characters other than tab, line feed and
# form feed (the page break) stripped, and at most this much per page; a cut reading is
# ``truncated`` (it then never replaces a reading, ``capture_mail.apply_gpu_ocr_pass``).
MAX_TEXT_PER_PAGE = 64 * 1024
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0d-\x1f\x7f-\x9f]")


# The model's "no text" answer said inside a reading instead of as the whole answer
# (stored readings: after a transcription, after one made-up word on a logo, as "NO TEXT"):
# the underscore form anywhere (no real text spells it), the spaced form only as a line.
_NO_TEXT_TOKEN = re.compile(r"(?<![^\W_])no_text(?![^\W_])", re.I)
_NO_TEXT_LINE = re.compile(r"^[^\w\n]*no[ -]text[^\w\n]*$", re.I | re.M)
_WORD = re.compile(r"[^\W_]{2,}")
# a page that also said "no text" keeps its text only with this many real words left
MIN_WORDS_BESIDE_NO_TEXT = 3


def _without_no_text(page: str) -> str:
    """One page with the model's NO_TEXT marker taken out; a page that said NO_TEXT and
    keeps fewer than ``MIN_WORDS_BESIDE_NO_TEXT`` words read nothing (the made-up word)."""
    kept = _NO_TEXT_LINE.sub("", _NO_TEXT_TOKEN.sub("", page))
    if kept == page:
        return page
    return kept.strip() if len(_WORD.findall(kept)) >= MIN_WORDS_BESIDE_NO_TEXT else ""


def clean_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """A host's result as the box stores it (``MAX_TEXT_PER_PAGE``, no control
    characters, no NO_TEXT marker, a cut page's loop trimmed); text that is empty after
    that is ``no_text``. Idempotent: the apply cleans stored readings again, so a reading
    stored before a rule here was added gets it without a re-read."""
    out = {"status": result["status"], "text": result.get("text"),
           "pages": int(result.get("pages") or 0) if isinstance(result.get("pages"), int)
           else 0, "truncated": bool(result.get("truncated"))}
    if out["status"] != "text":
        out["text"] = None
        return out
    pages = [_without_no_text(page) for page in _CONTROL.sub("", out["text"]).split("\f")]
    if out["truncated"]:   # as the worker does: only a page cut at the bound can loop
        pages = [worker.trim_loops(page).strip() for page in pages]
    text = "\f".join(pages)
    cap = MAX_TEXT_PER_PAGE * min(max(out["pages"], 1), worker.MAX_FRAMES)
    if len(text) > cap:
        text, out["truncated"] = text[:cap], True
    text = text.strip()
    out["text"] = text or None
    if not text:
        out["status"] = "no_text"
    return out


def _scrubbed(env: Mapping[str, str]) -> dict[str, str]:
    """The ssh child's environment: no credential-shaped variable reaches it."""
    bad = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|DSN|AZURE|PLX_", re.I)
    return {k: v for k, v in env.items() if not bad.search(k)}


def _ssh_base(args: argparse.Namespace) -> list[str]:
    base = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30"]
    return base + (["-F", args.ssh_config] if args.ssh_config else [])


def ship_worker(host: str, args: argparse.Namespace) -> None:
    done = subprocess.run(
        [*_ssh_base(args), host, f"mkdir -p {shlex.quote(args.remote_dir)} && cat > "
         f"{shlex.quote(args.remote_dir)}/image_ocr_gpu_worker.py.tmp && mv "
         f"{shlex.quote(args.remote_dir)}/image_ocr_gpu_worker.py.tmp "
         f"{shlex.quote(args.remote_dir)}/image_ocr_gpu_worker.py"],
        input=Path(worker.__file__).read_bytes(), capture_output=True, timeout=120,
        env=_scrubbed(os.environ))
    if done.returncode != 0:
        raise HostFailed(f"{host}:ship_failed")


def ssh_argv(host: str, args: argparse.Namespace) -> list[str]:
    d = shlex.quote(args.remote_dir)
    # HF_HOME: the host's model cache (weights fetched once, offline from then on);
    # C_INCLUDE_PATH: Python headers for Triton's one-time kernel build on the host
    remote = (f"cd {d} && HF_HOME=$HOME/{shlex.quote(args.remote_hf)} HF_HUB_OFFLINE=1 "
              f"C_INCLUDE_PATH=$HOME/{shlex.quote(args.remote_include)} "
              "VLLM_ENABLE_V1_MULTIPROCESSING=0 "
              f"exec nice -n {int(args.nice)} {shlex.quote(args.remote_python)} image_ocr_gpu_worker.py "
              f"--gpu-memory {float(args.gpu_memory)} --max-seqs {int(args.max_seqs)}")
    return [*_ssh_base(args), host, remote]


# --- the pass -------------------------------------------------------------------------

def image_bytes(mail: Any, sha: str, fetch_dir: str | Path | None) -> bytes | None:
    """The image's bytes, re-hashed: the mail store's copy, else (an original the store
    dropped after reading it) the fast path's fetched copy; None when neither holds it."""
    try:
        return mail.blob(sha)   # re-hashed by the store
    except Exception:  # noqa: BLE001 - dropped or missing: try the fetched copy
        pass
    if fetch_dir is None:
        return None
    from .attach_fetch import blob_file
    try:
        content = blob_file(Path(fetch_dir).expanduser().resolve(), sha).read_bytes()
    except OSError:
        return None
    return content if hashlib.sha256(content).hexdigest() == sha else None


def read_pass(store: Store, mail: Any, hosts: Sequence[Host], gate: Gate, *,
              batch: int = 32, budget_seconds: float,
              fetch_dir: str | Path | None = None,
              monotonic: Callable[[], float] = time.monotonic,
              progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Read every planned image not yet read under the hosts' engine key, ``batch`` at a
    time per host, until done or ``budget_seconds``; a failing host is dropped and its
    batch goes to another (none left: the run ends, ``hosts_failed``)."""
    key = engine_key(hosts[0].hello)
    deadline = monotonic() + budget_seconds
    counts: dict[str, Any] = {"sent": 0, "stored": 0, "refused": 0, "bytes_missing": 0,
                              "hosts_failed": 0, "budget_spent": 0}
    refusals: dict[str, int] = {}
    work: "queue.Queue[list[tuple[str, bytes]]]" = queue.Queue(maxsize=2 * len(hosts))
    lock = threading.Lock()
    alive = {id(h) for h in hosts}   # a host named twice runs two workers
    redo: list[list[tuple[str, bytes]]] = []

    done = threading.Event()
    gate_lock = threading.Lock()   # the producer and a re-sent batch share the gate

    def gated(sha: str) -> bool:
        """Whether ``sha`` may leave now; a refusal is stored and counted."""
        with gate_lock:
            why = gate.why_not(mail.db, sha)   # re-reads the list as it changes
        if why is None:
            return True
        store.refuse(key, sha, why)
        with lock:
            counts["refused"] += 1
            refusals[why] = refusals.get(why, 0) + 1
        return False

    def hand(chunk: list[tuple[str, bytes]]) -> None:
        while alive:
            try:
                work.put(chunk, timeout=2)
                return
            except queue.Full:
                continue

    def producer() -> None:
        after, chunk = "", []
        try:
            while alive:
                if monotonic() >= deadline:
                    counts["budget_spent"] = 1
                    break
                shas = store.pending(key, after, 256)
                if not shas:
                    break
                after = shas[-1]
                for sha in shas:
                    # decided now, just before the bytes would leave the box
                    if not gated(sha):
                        continue
                    if monotonic() >= deadline:
                        counts["budget_spent"] = 1
                        break
                    content = image_bytes(mail, sha, fetch_dir)
                    if content is None:
                        with lock:
                            counts["bytes_missing"] += 1
                        continue
                    chunk.append((sha, content))
                    if len(chunk) >= batch:
                        hand(chunk)
                        chunk = []
            if chunk:
                hand(chunk)
        finally:
            done.set()

    def next_item() -> list[tuple[str, bytes]] | None:
        while True:
            with lock:
                again = redo.pop() if redo else None
            if again is not None:
                # a failed host's batch: decided again before it goes to another host
                again = [(sha, content) for sha, content in again if gated(sha)]
                if again:
                    return again
                continue
            try:
                return work.get(timeout=1)
            except queue.Empty:
                if done.is_set() and work.empty():
                    with lock:
                        if not redo:
                            return None

    def consumer(host: Host) -> None:
        while True:
            item = next_item()
            if item is None:
                return
            try:
                results = host.read([c for _, c in item])
            except HostFailed:
                with lock:
                    alive.discard(id(host))
                    counts["hosts_failed"] += 1
                    redo.append(item)
                return
            store.put(key, [s for s, _ in item], results)
            with lock:
                counts["sent"] += len(item)
                counts["stored"] += len(item)
            if progress:
                progress({"progress": True, **counts})

    threads = [threading.Thread(target=consumer, args=(h,), daemon=True) for h in hosts]
    for t in threads:
        t.start()
    producer()
    for t in threads:
        t.join()
    while True:   # every host gone: what is still queued is read on a later run
        try:
            item = work.get_nowait()
        except queue.Empty:
            break
        if item:
            redo.append(item)
    counts["left_unsent"] = sum(len(i) for i in redo)
    counts["refusals"] = refusals
    return counts


def _print(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m integrations.image_ocr_gpu",
                                description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("plan", "read", "counts"):
        s = sub.add_parser(name)
        s.add_argument("--state-dir", required=True, help="the workbench state (mail store)")
        s.add_argument("--out-dir", required=True, help="this pass's own store")
    r = sub.choices["read"]
    r.add_argument("--host", action="append", required=True)
    r.add_argument("--ssh-config")
    r.add_argument("--remote-dir", default="plx-ocr/gpu-worker")
    r.add_argument("--fetch-dir", help="the fast path's store (attach_fetch): bytes of "
                   "originals the mail store dropped")
    r.add_argument("--remote-python", default="../vllm/bin/python")
    r.add_argument("--remote-hf", default="plx-ocr/hf")
    r.add_argument("--remote-include", default="plx-ocr/env/include/python3.12")
    r.add_argument("--gpu-memory", type=float, default=0.25)
    r.add_argument("--max-seqs", type=int, default=128)
    r.add_argument("--nice", type=int, default=5)
    r.add_argument("--batch", type=int, default=128)
    r.add_argument("--budget-seconds", type=float, required=True)
    r.add_argument("--answer-seconds", type=float, default=1800.0,
                   help="a host silent this long on one batch is dropped (hung)")
    r.add_argument("--logs", help="directory of the hosts' stderr logs")
    r.add_argument("--dry-run", action="store_true",
                   help="check the exclusion and the hosts' hello; send no image")
    r.add_argument("--local-cmd", action="append", help=argparse.SUPPRESS)  # tests
    return p


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None
         ) -> int:
    args = parser().parse_args(argv)
    env = os.environ if environ is None else environ
    from .capture_mail import MailCaptureStore
    from .captured_text import _ocr_image
    store = Store(args.out_dir)
    mail = MailCaptureStore.open_read_only(args.state_dir, timeout=60)
    key = engine_key(expected_hello())
    if args.command == "plan":
        _print({"ok": True, **store.plan(mail.db, _ocr_image)})
        return 0
    if args.command == "counts":
        _print({"ok": True, "engine_key": key, **store.counts(key)})
        return 0
    gate = Gate.from_env(env, mail, args.state_dir)
    try:
        gate._require()
    except Refused as refused:
        _print({"ok": False, "error": "people_exclusion_unavailable", "why": refused.code})
        return 2
    hosts: list[Host] = []
    try:
        for i, name in enumerate(args.host):
            if args.local_cmd:
                argv_ = shlex.split(args.local_cmd[i % len(args.local_cmd)])
            else:
                ship_worker(name, args)
                argv_ = ssh_argv(name, args)
            log = Path(args.logs) / f"image-ocr-gpu-{name}-{i}.log" if args.logs else None
            host = Host(name, argv_, log, answer_seconds=args.answer_seconds)
            hosts.append(host)
            host.start()
        if args.dry_run:
            _print({"ok": True, "dry_run": True, "hosts": len(hosts), "engine_key": key,
                    **store.counts(key)})
            return 0
        out = read_pass(store, mail, hosts, gate, batch=args.batch,
                        budget_seconds=args.budget_seconds, fetch_dir=args.fetch_dir,
                        progress=None)
    except HostFailed as error:
        _print({"ok": False, "error": "host_refused", "why": str(error).split(":", 1)[-1]})
        return 2
    finally:
        for h in hosts:
            h.close()
    _print({"ok": out["hosts_failed"] < len(hosts), **out, **store.counts(key)})
    return 0 if out["hosts_failed"] < len(hosts) else 1


if __name__ == "__main__":
    sys.exit(main())
