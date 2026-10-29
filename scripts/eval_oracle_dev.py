"""Run the evaluation oracle's dev-set generator over raw sources (read-only).

This is the wiring layer the oracle itself is not allowed to contain: it builds the
injected ports from the reviewed GET-only clients and hands them to ``evals.oracle``
(which never imports ``ontology``, ``operating`` or ``integrations``).

* ``live`` (credentialed live verifier): Portal reporting API through
  ``integrations.workbench_read_api.WorkbenchReadApiClient``; BC Production through
  ``integrations.bc_read`` (GET only); optionally one mailbox (or one of its folders,
  ``--folder``) through ``integrations.graph_read.GraphMailReader`` and one SharePoint
  drive through ``SharePointReader``, searched (mail ``$search`` in the mailbox or the
  folder, drive search, GET only,
  every hit visibility-checked) for the numbers of the run's closed roots
  (``LiveDocuments.search``). Items are written only under ``--output-dir`` (the verifier's
  ``{tmp}``, deleted after the run). stdout carries **no values**: item ids, 16-hex item
  hashes, families, strata, answerability and pass counts only.
* ``fixture`` (keyless): the synthetic world in ``evals/oracle/fixture_world.py`` served
  through the same Portal client with an injected fetch (evidence ``injected-transport``);
  ``--write evals/dev`` freezes the items and a manifest there.
* ``heldout`` (credentialed, 10-DECISION 8): the hidden held-out set, 120 items over
  F1-F6 (F5/F6 have no plan), roots from the held-out half of the root partition, and no
  item whose answer chain touches a record of the frozen dev set (its root or any chain
  record). The first run of a run id freezes the items and their digest list in the
  host's state directory (``--state-dir``, outside the repository, never committed or
  printed); later runs re-check that frozen set and never resample. A new set needs a new
  run id (``heldout-v3``). stdout carries counts and hashes only (no item id, identifier
  or value; ``plx-eval-heldout-manifest/v1``). ``--source fixture`` runs the same path
  keyless over the fixture world; its manifest is ``evals/heldout/manifest.fixture.json``.
  **Metamorphic variants** (ENG-12, ``evals/oracle/perturb.py``): each sampled item gets
  one variant, the transforms in turn, answered by the oracle over the run's own reads
  perturbed (no source is read again); variants are frozen with their parents and checked
  like every item over the reads they were answered from (schema, determinism, label
  drift on reuse). ``counts.variants`` and ``by_variant`` count them; ``by_family`` counts
  the sampled items. A set frozen before the variants has none: draw a new run id.
  **A Portal outage blocks parts, not the run** (the landed portal-indep pattern): the
  Portal strata (or, on reuse, the frozen items whose labels need the Portal) are listed
  in ``blocked_upstream`` and counted ``counts.blocked_upstream``, never as label drift;
  BC, mail and SharePoint still measure (``counts.measured``). A generation with blocked
  parts is never frozen (``frozen.deferred``): the set would lack the Portal families.
  **Live partition** (bb664f58910f section 2, ``evals/partition.py``): the live dev runs
  (``scripts.eval_dev_baseline live``) leave a digest manifest per run in
  ``<state>/live/dev/``; a held-out generation excludes their records and every item whose
  group (shared document, root or identifier) touches their key digests
  (``partition_dropped.related_to_dev``), the frozen set keeps its own key digests for the
  dev runs to exclude, and ``counts.dev_key_overlap`` re-checks it on every run.

Pass counts: ``schema_valid`` (plx-eval-item/v1 via ``ontology.json_contract``, the C0
validator; the oracle package cannot import it), ``deterministic`` (a second oracle
run over the same cached reads gives the identical known_outcome: a determinism check, not
a fresh re-read; a failed read is kept for the run too, so the second answer meets the same
failure; ``counts.nondeterministic_by`` counts the items that differ by family and variant,
``read_failures`` names each failed read by relation or BC ``entity_set.field``, never a
value), ``root_resolves`` (the
answer has a root, i.e. is not blocked). ``blocked_upstream`` lists the strata whose source
read failed upstream (a Portal outage blocks the four Portal strata; BC and the documents
still run), so the live verifier records such a run as blocked, never as a pass. A document
stratum whose source refused every search it was asked (a tenant refusing mail ``$search``
and the listing that stands in for it: HTTP 403) is blocked upstream the same way, with the
status and error code of the refusal (``later_ask: graph-mail-directory-consent``); one
answered search keeps it measured, and no other source fills its candidates.

Usage::

    python3 -m scripts.eval_oracle_dev live --output-dir DIR [--run-id auto] [--count 30]
        [--mailbox ADDR [--folder inbox]] [--sharepoint-path /Documents ...] [--no-bc]
        [--budget-seconds 1200]
    python3 -m scripts.eval_oracle_dev fixture [--write evals/dev] [--run-id ID]
    python3 -m scripts.eval_oracle_dev heldout [--state-dir DIR] [--source live|fixture]
        [--run-id heldout-v2] [--count 120] [--budget-seconds 2400] [--manifest-out FILE]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import shutil
from pathlib import Path
import sys
from typing import Any, Callable, Collection, Mapping, Sequence

from evals.oracle import BcReads, PortalReads, Sources, run as oracle_run
from evals.oracle.generator import (BC_DOCUMENT_ROOT_SETS, HELD_OUT_COUNT, HELD_OUT_ONLY,
                                    PORTAL_STRATA, SPLIT_SALT,
                                    audit_requested, chain_digests, generate, item_hash,
                                    item_root_digest, split_of, unexplained_strata)
from evals.oracle.perturb import Perturbed, perturbation_key, variants
from evals.oracle.source import ReadFailed, digest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "plx-eval-oracle-dev-run/v1"
MANIFEST_FORMAT = "plx-eval-dev-manifest/v1"
HELDOUT_FORMAT = "plx-eval-heldout-manifest/v1"
# The run id the live spec draws (devloop/live-checks/eval.heldout.json; v1 was frozen
# before the metamorphic variants).
HELDOUT_RUN_ID = "heldout-v2"
HELDOUT_RUN_ID_SHAPE = re.compile(r"heldout-v[0-9]{1,4}")
DEV_MANIFEST = ROOT / "evals" / "dev" / "manifest.json"
FIXTURE_RUN_ID = "fixture-dev-2026-10-01"
FIXTURE_NOW = "2099-01-02T04:00:00Z"
FIXTURE_NOTE = ("evidence: injected-transport fixture (synthetic FX rows from "
                "evals/oracle/fixture_world.py), not live PLX data.")
EVAL_PRINCIPAL = "eval:oracle"
# SharePoint document sample: read units (a folder listed below each path, one GET page
# each, or a document's text read) per DRIVE_FOLDERS_PER documents asked (a further sample
# reads further), and depth.
MAX_DRIVE_FOLDERS = 40
DRIVE_FOLDERS_PER = 200
MAX_DRIVE_DEPTH = 3
# SharePoint document text: a 1/CONTENT_SHARE of the read units above go to reading files
# (every unit the listing leaves too), each at most MAX_TEXT_BYTES, read as capture reads
# them (integrations.captured_text.capture_text, local OCR included) within
# MAX_TEXT_SECONDS per sample (a reading still running then is abandoned), its text cut at
# MAX_TEXT_CHARS; the extensions read (a .csv as plain text; a saved .msg or .eml message
# as capture reads it, its headers and body). Mail: the text bodies of 1/CONTENT_SHARE of
# the messages asked (one GET each).
CONTENT_SHARE = 4
MAX_TEXT_BYTES = 4_000_000
MAX_TEXT_CHARS = 200_000
MAX_TEXT_SECONDS = 240.0
TEXT_SUFFIXES = frozenset({"pdf", "docx", "pptx", "xlsx", "txt", "csv", "msg", "eml"})
PLAIN_TEXT_SUFFIXES = frozenset({"csv"})
# Why a document's text was not (wholly) read (texts_unread_<cause>): the reader refused
# it (NotVisible) or it was gone or changed (SourceUnavailable, or a mail body of another
# changeKey than the one listed), the text-read time ran out, a PDF read only on its
# text-layer pages on a host without OCR (unread_scanned_pages: partial, its text matched,
# its pages left counted in pages_unread), a PDF read on its pages 1-MAX_PAGES only
# (skipped_by_cap: the same, its pages past the bound counted in pages_unread; a PDF whose
# page count cannot be told is never whole either), else get_content's content_status or
# capture_text's text status. Any other is failed. A search accounts for every hit (live
# c89b2bbd: SharePoint 14 documents, texts_read 0 and 1 unread, 13 never tried and never
# said): one whose text is never asked for is not_text_kind (an extension outside
# TEXT_SUFFIXES), exceeds_cap (over MAX_TEXT_BYTES) or text_budget (past the search's
# MAX_SEARCH_TEXTS).
UNREAD_CAUSES = frozenset({"refused", "unavailable", "time_budget", "exceeds_cap",
                           "exceeds_bound", "no_text", "unsupported", "failed",
                           "ocr_unavailable", "ocr_failed", "ocr_no_text",
                           "unread_scanned_pages", "skipped_by_cap", "not_text_kind",
                           "text_budget"})
# A listing the sampler's own item or page cap cut (live_source_guard.get_pages): a sample.
SAMPLE_CAPS = frozenset({"item_cap", "page_cap"})
# Root-first searches (``LiveDocuments.search``): the hits whose text is read per search
# call (mail bodies, one GET each; SharePoint files, as ``_read_texts`` reads them), and
# the HTTP statuses that mean the source refused the search itself (a tenant without
# search, a query it will not parse), typed ``refused: tenant`` with the status and error
# code, never a failed read.
MAX_SEARCH_TEXTS = 48
# Mail attachments read per message whose body was read: file attachments of a text kind
# (TEXT_SUFFIXES), not inline, each at most MAX_TEXT_BYTES, read as capture reads them.
MAX_MAIL_ATTACHMENTS = 3
# A tenant that refuses every mail ``$search`` (live daily 8ecf8680c: search_refused_tenant on
# every query) is read by listing instead: the newest ``MAIL_SCAN_PAGES`` pages of the
# mailbox, each query matched against the listed subjects and previews. Complete only if
# the listing was not cut. A failed listing is retried on a later search, at most
# ``MAIL_SCAN_TRIES`` listings per instance (a transient 503 does not refuse the whole run).
MAIL_SCAN_PAGES = 8
MAIL_SCAN_TRIES = 2
# A whole mailbox (no --folder) is read without the well-known folders of mail never sent
# or received as it stands, or discarded (directive f6427307e9b0): a message there names
# nothing (``LiveDocuments._list_mail``; counted ``excluded``, never withheld). A subfolder
# of one of them is not known as such and is read.
EXCLUDED_MAIL_FOLDERS = ("drafts", "outbox", "deleteditems", "recoverableitemsdeletions",
                         "junkemail")
SEARCH_REFUSED_STATUSES = frozenset({400, 403, 501})
# A mail capture scope's mailboxes are named by scope id (``mailbox-<n>``, the n-th mailbox
# of the scope file), never by address. A scoped mailbox the eval may not read is blocked
# by its scope with the later_ask that backs it: outside the grant (the capture scope set
# it aside, ``MAIL_SCOPE_GRANT``) or a read the people-exclusion guard cannot decide
# (``ExclusionUnavailable``, later_ask excluded-people-oids). Its searches are
# ``refused: scope``; never requested, never widened.
MAIL_SCOPE_ID = re.compile(r"mailbox-[1-9][0-9]{0,2}")
MAIL_SCOPE_GRANT = "mail-scope-grant"
MAIL_SCOPE_EXCLUSION = "excluded-people-oids"   # people_exclusion.LATER_ASK
MAIL_SCOPE_BLOCKS = frozenset({MAIL_SCOPE_GRANT, MAIL_SCOPE_EXCLUSION})
# A guard whose directory listing failed or was truncated waits on Graph, not on that
# ask: its mailbox is blocked all the same, under a code no later_ask types.
MAIL_SCOPE_UNRESOLVED = "excluded-people-unresolved"
PORTAL_PAGE_SIZE = 5000
# A live dev run's reads stop here (``Deadline``: a read still running then is abandoned,
# one after it never starts; its stratum is ``blocked: budget``): the live spec's 1500 s
# timeout keeps 300 s for the answers over the cached reads, the determinism re-run and
# the report (live 8e61f78 and 37e539e6 timed out at 1500 s with no record: the guard ran
# only before a read, and one Portal read pages up to 400 times, a BC find walks every
# company, a SharePoint sample reads document text with OCR for up to MAX_TEXT_SECONDS).
DEV_BUDGET_SECONDS = 1200.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------- live ports

Guard = Callable[[], None]


def _no_guard() -> None:
    return None


class BudgetExhausted(RuntimeError):
    """The run's time budget is spent; the oracle records the read as failed."""


class Deadline:
    """One total read budget for a bounded run. As a port's guard (it is callable) it runs
    before every read and stops each read at the deadline (``call``)."""

    def __init__(self, seconds: float, monotonic: Callable[[], float] | None = None) -> None:
        import time
        self._monotonic = monotonic or time.monotonic
        self._end = self._monotonic() + max(0.0, float(seconds))
        self.hit = False

    def guard(self) -> None:
        if self._monotonic() >= self._end:
            self.hit = True
            raise BudgetExhausted("run_budget_exhausted")

    __call__ = guard

    def remaining(self) -> float:
        return max(0.0, self._end - self._monotonic())

    def call(self, read: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """``read(*args, **kwargs)`` on a daemon thread until the deadline. A read still
        running then is abandoned (``child_process.abandon``: its OCR children stopped) and
        its result unused; one that ended, failed or not, past the deadline is not used
        either: both raise ``BudgetExhausted``."""
        self.guard()
        got = _on_thread(self.remaining(), read, *args, **kwargs)
        if got is None or self._monotonic() >= self._end:
            self.hit = True
            raise BudgetExhausted("run_budget_exhausted")
        result, error = got
        if error is not None:
            raise error
        return result


def _read(guard: Guard, read: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """``read(*args, **kwargs)`` after ``guard()``; a ``Deadline`` also stops it at the
    deadline (``Deadline.call``)."""
    if isinstance(guard, Deadline):
        return guard.call(read, *args, **kwargs)
    guard()
    return read(*args, **kwargs)


def portal_port(client: Any, guard: Guard = _no_guard
                ) -> Callable[[str, Sequence[str]], Mapping[str, Any]]:
    """``read_relation(name, columns)`` over the GET-only reporting API client.
    ``guard()`` runs before each read and may raise to stop a run (a time budget); the
    oracle then records the read as failed, never as absence."""
    def read(name: str, columns: Sequence[str]) -> Mapping[str, Any]:
        return _read(guard, client.read_relation, name, columns=list(columns),
                     page_size=PORTAL_PAGE_SIZE, max_rows=1_000_000, max_pages=400)
    return read


# The dated fields of a BC document, newest first (``LiveBcPort._sample``), and how many
# records one read of a set BC sorts by none of them takes to keep its newest
# (grammar.BC_MAX_TOP: one GET per company).
BC_DATED_FIELDS = ("postingDate", "orderDate")
BC_DATED_READ = 1000


def _newest(record: Mapping[str, Any]) -> str:
    """A BC record's first dated field (ISO text sorts as its date); "" when undated."""
    fields = record.get("fields") or {}
    return next((str(fields[f]) for f in BC_DATED_FIELDS if fields.get(f)), "")


class LiveBcPort:
    """The oracle's BC port over ``integrations.bc_read.BusinessCentralReader``: every
    company the eval principal may list, GET only, completeness carried through."""

    def __init__(self, reader: Any, actor: Any, *, max_matches: int = 50,
                 guard: Guard = _no_guard) -> None:
        self._guard = guard
        self._reader = reader
        self._actor = actor
        self._max = max_matches
        self._companies: list[dict[str, Any]] | None = None
        self._companies_complete = True

    def companies(self) -> list[dict[str, Any]]:
        if self._companies is None:
            listing = self._reader.companies(self._actor)
            self._companies_complete = not listing["completeness"].get("truncated")
            self._companies = [c for c in listing["items"] if isinstance(c.get("id"), str)]
        return self._companies

    @staticmethod
    def _record(record: Mapping[str, Any], company: Mapping[str, Any]) -> dict[str, Any]:
        return {"company_id": record["company_id"],
                "company_name": company.get("name") or company.get("display_name"),
                "entity_set": record["entity_set"], "id": record["id"],
                "fields": record["fields"], "citation": record["citation"],
                "last_modified": record.get("last_modified")}

    def find(self, entity_set: str, field: str, value: str) -> dict[str, Any]:
        return _read(self._guard, self._find, entity_set, field, value)

    def _find(self, entity_set: str, field: str, value: str) -> dict[str, Any]:
        records, complete = [], self._companies_complete
        for company in self.companies():
            found = self._reader.find_by_number(self._actor, company["id"], entity_set, value,
                                                field=field, max_matches=self._max)
            complete = complete and found["status"] not in ("truncated", "partially_withheld")
            records += [self._record(m["record"], company) for m in found["matches"]]
        return {"records": records, "complete": complete}

    def lines(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return _read(self._guard, self._lines, record)

    def _lines(self, record: Mapping[str, Any]) -> dict[str, Any]:
        company = next((c for c in self.companies()
                        if str(c["id"]).lower() == str(record["company_id"]).lower()), {})
        listing = self._reader.list_lines(self._actor, record["company_id"],
                                          record["entity_set"], record["id"], page_size=100,
                                          max_pages=5, max_items=500)
        done = listing["completeness"]
        complete = not (done.get("truncated") or done.get("withheld_count") or
                        done.get("provenance_dropped"))
        return {"records": [self._record(r, company) for r in listing["items"]],
                "complete": complete}

    def sample(self, entity_set: str, limit: int) -> dict[str, Any]:
        return _read(self._guard, self._sample, entity_set, limit)

    def _sample(self, entity_set: str, limit: int) -> dict[str, Any]:
        """``limit`` records per company; a set read only for the roots documents may name
        (``generator.BC_DOCUMENT_ROOT_SETS``) newest first, as documents name recent
        transactions: sorted by BC on the first of ``BC_DATED_FIELDS`` its grammar sorts by
        (posted receipts by posting date, sales orders by order date), else (purchase
        orders: no sort allowed) the newest ``limit`` by those fields of one read of
        ``BC_DATED_READ`` records."""
        from integrations.live_source_grammar import BC_QUERY_SPECS

        records, complete, withheld = [], True, 0
        companies = self.companies()
        spec = BC_QUERY_SPECS.get(entity_set)
        dated = entity_set in BC_DOCUMENT_ROOT_SETS
        sort = next((f for f in BC_DATED_FIELDS if spec is not None and f in spec.orderby),
                    None) if dated else None
        width = max(limit, BC_DATED_READ) if dated and sort is None else limit
        order = {"orderby": f"{sort} desc"} if sort else {}
        for company in companies:
            listing = self._reader.list_entities(self._actor, company["id"], entity_set,
                                                 top=min(width, 1000),
                                                 page_size=min(width, 1000), max_pages=1,
                                                 max_items=width, **order)
            done = listing["completeness"]
            items = listing["items"]
            if width > limit:
                items = sorted(items, key=_newest, reverse=True)[:limit]
            complete = complete and not done.get("truncated") and \
                len(items) == len(listing["items"])
            withheld += int(done.get("withheld_count") or 0)
            records += [self._record(r, company) for r in items]
        return {"records": records, "source": [f"bc:{entity_set}"],
                "complete": complete and self._companies_complete and not withheld,
                "withheld": withheld, "companies": len(companies)}


class _MailUnit:
    """One mailbox folder read (``folder`` None: the whole mailbox), its scope id (None for
    a bare ``--mailbox``), its tenant-without-search listing (fetched at most
    ``MAIL_SCAN_TRIES`` times), its ``EXCLUDED_MAIL_FOLDERS`` ids and, once blocked, the
    later_ask code that blocks it."""

    def __init__(self, scope_id: str | None, mailbox: str | None, folder: str | None,
                 blocked: str | None = None) -> None:
        self.scope_id, self.mailbox, self.folder, self.blocked = (scope_id, mailbox, folder,
                                                                   blocked)
        self.scan: dict[str, Any] | None = None
        self.scan_failures = 0
        self.excluded: frozenset[str] | None = None

    @property
    def source(self) -> str:
        """The name the read is reported by: the scope id, never an address in scope mode."""
        return f"mail:{self.scope_id or self.mailbox or ''}/{self.folder or ''}"

    @property
    def label(self) -> str:
        return self.scope_id or "mailbox"


def _exclusion_block(exc: BaseException) -> str:
    """The block code of a mailbox the people guard refused: its later_ask when the guard
    wants its ID list (``graph_read.exclusion_later_ask``), else ``MAIL_SCOPE_UNRESOLVED``."""
    from integrations.graph_read import exclusion_later_ask

    return MAIL_SCOPE_EXCLUSION if exclusion_later_ask(exc) else MAIL_SCOPE_UNRESOLVED


class LiveDocuments:
    """Mail subjects/previews/bodies of one mailbox folder (or of the mailbox folders of a
    mail capture scope, ``mail_units``) and SharePoint file names and
    document text (breadth first below each drive path, ``MAX_DRIVE_FOLDERS`` read units per
    ``DRIVE_FOLDERS_PER`` documents asked and ``MAX_DRIVE_DEPTH`` levels; a folder left
    unread makes the read incomplete); the texts only feed exact identifier matching and
    are never written to stdout. Each sample names what it read (``source``: mailbox/folder,
    drive paths), what the audience or ACL withheld and whether a cap cut it: ``capped``
    when only this sampler's own limits (items, pages, folders, depth) left it
    incomplete, a sample of a readable source; any other gap (a folder without a path, a
    cut of unknown cause) leaves it incomplete and not capped. A kind without a reader says
    ``not_configured`` and why, so an unread source is never reported empty. The dev set's
    document strata and the cross-system sample search root first (``search``); ``sample``
    is the port's listing read (``DocumentPort.sample``; ``windows`` ignored). A capture
    scope's walk bounds (``max_drive_depth``, ``max_drive_folders``: the folders listed in
    all, ``max_text_bytes``: its ``max_content_bytes``) tighten these, never widen them.

    Mail is read newest first. ``1/CONTENT_SHARE`` of the messages asked, ranked
    by the seed, get their text body read (``get_message``, the listed folder expected, the
    mailbox's directory lookup shared with the listing); a body of another changeKey than
    the listed message's is not attached (``unavailable``). SharePoint spends
    ``1/CONTENT_SHARE`` of its read units (and every unit the listing left) on document
    text: readable files (``TEXT_SUFFIXES``, at most ``MAX_TEXT_BYTES``) ranked by the seed,
    each read through the reader's visibility-checked ``get_content`` and
    ``captured_text.capture_text``, the one reading capture stores (a scanned page
    OCR-read when this host can). A body or text not read, or read only in part, is counted
    (``texts_unread``, by cause in ``texts_unread_by``, ``UNREAD_CAUSES``), never a
    listing gap and never ``texts_read`` (the scanned pages a PDF read in part left:
    ``pages_unread``).

    Scope mode (``mail_units``: (scope id, address, folder) per mailbox folder of a capture
    scope; ``mail_blocked``: (scope id, later_ask code) per scoped mailbox the eval may not
    read): each unit is searched as one mailbox is, the results merged per query (an answer
    from any unit answers it; one more result says why another unit did not), and the
    reads are named by scope id. A unit whose read the people-exclusion guard cannot decide
    (``ExclusionUnavailable``) is blocked from then on (``MAIL_SCOPE_EXCLUSION``, or
    ``MAIL_SCOPE_UNRESOLVED`` when its directory listing failed: never typed); a blocked
    unit is never requested, its searches ``refused: scope``, and every answer names the
    blocked units (``scope_blocked``: scope id -> later_ask code)."""

    def __init__(self, actor: Any, *, mail_reader: Any = None, mailbox: str | None = None,
                 folder: str | None = None,
                 mail_units: Sequence[tuple[str, str, str | None]] = (),
                 mail_blocked: Sequence[tuple[str, str]] = (), drive_reader: Any = None,
                 drive_paths: Sequence[str] = (), guard: Guard = _no_guard,
                 max_drive_depth: int = MAX_DRIVE_DEPTH, max_drive_folders: int | None = None,
                 max_text_bytes: int = MAX_TEXT_BYTES) -> None:
        self._guard = self._root_guard = guard
        self._shares: dict[str, Deadline] = {}
        self._max_depth = min(max_drive_depth, MAX_DRIVE_DEPTH)
        self._max_folders = max_drive_folders
        self._max_bytes = min(max_text_bytes, MAX_TEXT_BYTES)
        self._actor = actor
        self._mail, self._mailbox, self._folder = mail_reader, mailbox, folder
        if (mail_units or mail_blocked) and (mailbox or folder):
            raise ValueError("mail_scope_replaces_mailbox")
        units = [_MailUnit(None, mailbox, folder)] if mailbox else []
        for scope_id, address, unit_folder in mail_units:
            if not isinstance(scope_id, str) or not MAIL_SCOPE_ID.fullmatch(scope_id) \
                    or not isinstance(address, str) or not address:
                raise ValueError("mail_scope_unit_invalid")
            units.append(_MailUnit(scope_id, address, unit_folder))
        for scope_id, code in mail_blocked:
            if not isinstance(scope_id, str) or not MAIL_SCOPE_ID.fullmatch(scope_id) \
                    or code not in MAIL_SCOPE_BLOCKS:
                raise ValueError("mail_scope_block_invalid")
            units.append(_MailUnit(scope_id, None, None, blocked=code))
        self._units = units
        self._drive, self._paths = drive_reader, list(drive_paths)
        # Search hits by versioned handle, their text read (or tried) once per run: a
        # later search finding one again reuses it.
        self._hits: dict[str, dict[str, Any]] = {}

    def sample(self, kind: str, limit: int, *, windows: Sequence[str] = (),
               seed: str = "") -> dict[str, Any]:
        return self._shared(kind, self._sample, kind, limit, seed)

    def _shared(self, kind: str, read: Callable[..., Any], *args: Any) -> Any:
        """``read`` under the run's ``Deadline`` as this kind's share of it: the first time
        a kind is read it gets an equal part of the time left among the configured kinds not
        yet started (mail, SharePoint), so a stratum that spends its part is blocked by the
        budget alone and cannot starve the next (live b823b276b1: mail spent the run's
        1200 s, SharePoint searched 0 of 24 roots). What a kind leaves unspent goes to the
        kinds after it. Any other guard is not divided."""
        whole = self._root_guard
        if not isinstance(whole, Deadline):
            return _read(whole, read, *args)
        if kind not in self._shares:
            kinds = {"mail": self._mail is not None and bool(self._units),
                     "sharepoint": self._drive is not None and bool(self._paths)}
            left = sum(1 for k, on in kinds.items() if on and k not in self._shares)
            self._shares[kind] = Deadline(whole.remaining() / max(1, left), whole._monotonic)
        self._guard = self._shares[kind]
        try:
            return _read(self._guard, read, *args)
        except BudgetExhausted:
            if whole.remaining() <= 0:
                whole.hit = True   # the run's own budget is spent, not only this share
            raise
        finally:
            self._guard = whole

    def list_documents(self, kind: str, limit: int, *, seed: str = "") -> dict[str, Any] | None:
        """``DocumentPort.list_documents``, read when the root searches named no root:
        SharePoint's listing read (``sample``; the drive search covers only what Graph
        indexed; live 2026-10-18: 47 of 51 root searches found nothing), each file's text
        read and counted once a run with the searches' (a file a search found keeps its
        read); mail's is the newest-first listing of each mailbox unit not blocked
        (``_list_units``, the one ``_scan_mail`` reads: gap-f713876847b6, a mailbox's own
        search finds what Graph indexed). None when no mailbox can be listed."""
        if kind == "mail":
            if self._mail is None or not any(u.blocked is None for u in self._units):
                return None
            return self._shared(kind, self._list_units, limit, seed)
        if kind != "sharepoint":
            return None
        return self._shared(kind, self._sharepoint, limit, seed, True)

    def search(self, kind: str, queries: Sequence[str], *, hits: int,
               seed: str = "") -> dict[str, Any]:
        """``DocumentPort.search``: each query through the reader's own search (mail:
        ``list_messages(search=...)`` in the folder, or the whole mailbox without one;
        SharePoint: ``SharePointReader.search`` over the drive), GET only, at most ``hits`` documents each, every hit through the
        reader's audience and ACL check (a refused one is ``withheld``). The text of
        ``MAX_SEARCH_TEXTS`` distinct hits, ranked by the seed, is read as ``sample`` reads
        it, once per run however many searches find it: each hit new to the run is counted
        once, read or unread by cause (``UNREAD_CAUSES``), one never tried included. A
        query the guard, the audience or the grammar refuses is ``refused: guard``, one the source answers 400/403/501
        ``refused: tenant``, any other failure (a reader's ValueError included) ``failed``."""
        return self._shared(kind, self._search, kind, list(queries), hits, seed)

    def _mail_unconfigured(self) -> dict[str, Any] | None:
        """The ``not_configured`` answer of mail without a reader or mailbox, else None."""
        if self._mail is not None and self._units:
            return None
        return {"records": [], "source": [u.source for u in self._units] or [
            f"mail:{self._mailbox or ''}/{self._folder or ''}"],
            "not_configured": "mail_reader_not_configured" if self._units
            else "mailbox_not_given"}

    def budget_state(self) -> dict[str, dict[str, bool]]:
        """By configured document kind: whether its share of the run's read time ran out
        (``exhausted``), a kind not yet read having spent none. Mail's ``--mailbox`` or
        scope units and SharePoint's paths decide which kinds are configured."""
        kinds = {"mail": self._mail is not None and bool(self._units),
                 "sharepoint": self._drive is not None and bool(self._paths)}
        return {k: {"exhausted": k in self._shares and self._shares[k].hit}
                for k, on in kinds.items() if on}

    def blocked_mail(self) -> dict[str, str]:
        """Each blocked mail unit's later_ask code by label (scope id, or ``mailbox`` for
        the one ``--mailbox``, which no scope id names): ``blocked_upstream`` types the mail
        stratum by it."""
        return dict(sorted((u.label, u.blocked) for u in self._units if u.blocked))

    def _scope_blocked(self) -> dict[str, Any]:
        blocked = self.blocked_mail()
        return {"scope_blocked": blocked} if blocked else {}

    def _search(self, kind: str, queries: list[str], hits: int, seed: str) -> dict[str, Any]:
        if kind == "mail":
            unconfigured = self._mail_unconfigured()
            if unconfigured is not None:
                return {**unconfigured, "results": []}
            source = [u.source for u in self._units]
            per_unit: list[list[dict[str, Any]]] = []
            messages: dict[str, tuple[_MailUnit, Mapping[str, Any]]] = {}
            with self._mail.directory_cache():
                for unit in self._units:
                    results, found = self._each_query(queries, lambda q, u=unit: self._list_mail(
                        u, search=q, page_size=min(hits, 50), max_pages=1, max_items=hits),
                        unit=unit)
                    if self._tenant_refused_all(results):
                        results, found = self._scan_mail(unit, results, hits)
                    per_unit.append(results)
                    for message in found:
                        messages.setdefault(message["citation"], (unit, message))
                seen = [self._hits[c] for c in messages if c in self._hits]
                read = self._read_bodies([pair for c, pair in messages.items()
                                          if c not in self._hits],
                                         MAX_SEARCH_TEXTS, seed, every=True,
                                         first=_first(queries, [r for rs in per_unit
                                                                for r in rs]))
            self._hits.update((r["handle"], r) for r in read["records"])
            return {"source": source, "results": _merged(per_unit),
                    **read, "records": seen + read["records"], **self._scope_blocked()}
        if kind == "sharepoint":
            if self._drive is None or not self._paths:
                return {"records": [], "results": [],
                        "source": [f"sharepoint:{path}" for path in self._paths],
                        "not_configured": "sharepoint_paths_not_given" if not self._paths
                        else "sharepoint_drive_not_configured"}
            results, entries = self._each_query(
                queries, lambda q: self._drive.search(self._actor, q, max_items=hits))
            seen = [self._hits[e["citation"]] for e in entries if e["citation"] in self._hits]
            files = [({"handle": entry["citation"], "texts": [entry.get("name") or ""]}, entry)
                     for entry in entries if entry["citation"] not in self._hits]
            read, unread, pages = self._read_texts(files, MAX_SEARCH_TEXTS, seed, every=True,
                                                   first=_first(queries, results))
            self._hits.update((record["handle"], record) for record, _ in files)
            return {"records": seen + [record for record, _ in files],
                    "source": ["sharepoint:/"], "results": results,
                    **_text_counts(read, unread), "pages_unread": pages}
        raise ValueError(f"unknown document kind {kind!r}")

    @staticmethod
    def _tenant_refused_all(results: Sequence[Mapping[str, Any]]) -> bool:
        """True when the source refused every query it was asked (``refused: tenant``; a
        query the guard or grammar refused was never asked): the tenant has no search."""
        asked = [r for r in results if r.get("refused") != "guard"]
        return bool(asked) and all(r.get("refused") == "tenant" for r in asked)

    def _scan_mail(self, unit: _MailUnit, results: Sequence[Mapping[str, Any]], hits: int
                   ) -> tuple[list[dict[str, Any]], list[Mapping[str, Any]]]:
        """The search of a tenant without mail ``$search``: one newest-first listing of the
        mailbox (GET, fetched once per instance; a failed listing is tried again on a later
        search, at most ``MAIL_SCAN_TRIES`` listings in all), each tenant-refused query
        answered by the listed messages whose subject or preview holds it (at most
        ``hits``; ``complete`` only when neither the listing nor the match list was cut),
        marked ``scan``. The first answer carries the listing's ``withheld`` and
        ``excluded`` counts. If the listing itself fails the results stay
        ``refused: tenant``, marked ``scan_failed``; if the exclusion guard cannot decide
        it, the unit is blocked and they are ``refused: scope``. Other results (a guard
        refusal) stay."""
        if not self._scanned(unit):
            if unit.blocked:
                return [{"query": r["query"], "refused": "scope"}
                        if r.get("refused") == "tenant" else dict(r) for r in results], []
        listing = unit.scan or {}
        if not listing:
            return [{**r, "scan_failed": True} if r.get("refused") == "tenant" else dict(r)
                    for r in results], []
        done = listing["completeness"]
        items = [i for i in listing["items"] if isinstance(i.get("citation"), str)]
        first = True
        out: list[dict[str, Any]] = []
        found: dict[str, Mapping[str, Any]] = {}
        for result in results:
            if result.get("refused") != "tenant":
                out.append(dict(result))
                continue
            needle = str(result["query"]).lower()
            every = [i for i in items if needle in " ".join(
                str(i.get(k) or "") for k in ("subject", "body_preview")).lower()]
            matched = every[:hits]
            answer = {"query": result["query"], "handles": [i["citation"] for i in matched],
                      "complete": not done.get("truncated") and len(every) <= hits,
                      "withheld": int(done.get("withheld_count") or 0) if first else 0,
                      "scan": True}
            if first and listing.get("excluded"):
                answer["excluded"] = listing["excluded"]
            first = False
            out.append(answer)
            for item in matched:
                found.setdefault(item["citation"], item)
        return out, list(found.values())

    def _scanned(self, unit: _MailUnit) -> bool:
        """Whether ``unit`` holds its newest-first listing (``_scan_mail``), fetched here at
        most ``MAIL_SCAN_TRIES`` times; one the exclusion guard cannot decide blocks it."""
        from integrations.live_source_guard import (NotVisible, ReadOnlyGuardError,
                                                    SourceUnavailable)
        from integrations.people_exclusion import ExclusionUnavailable
        if unit.scan is None and unit.blocked is None and unit.scan_failures < MAIL_SCAN_TRIES:
            self._guard()
            try:
                unit.scan = self._list_mail(unit, page_size=50, max_pages=MAIL_SCAN_PAGES,
                                            max_items=50 * MAIL_SCAN_PAGES)
            except ExclusionUnavailable as exc:
                unit.blocked = _exclusion_block(exc)
            except (SourceUnavailable, NotVisible, ReadOnlyGuardError, ValueError):
                unit.scan_failures += 1
        return unit.scan is not None

    def _list_units(self, limit: int, seed: str) -> dict[str, Any]:
        """Mail's ``list_documents``: the listing of every unit not blocked (``_scanned``,
        shared with ``_scan_mail``), round robin over the units newest first, at most
        ``limit`` messages; the text bodies of ``MAX_SEARCH_TEXTS`` new ones, ranked by the
        seed, read as a search's are (a message a search read keeps its read). Complete
        only when every unit was listed to its end; ``capped`` when only this sampler's
        own caps cut it; a unit blocked or not listed is a gap, never a sample."""
        capped = gap = False
        withheld = 0
        queues: list[list[tuple[_MailUnit, Mapping[str, Any]]]] = []
        with self._mail.directory_cache():
            for unit in self._units:
                if not self._scanned(unit):
                    gap = True
                    continue
                done = unit.scan["completeness"]
                if done.get("truncated"):
                    if done.get("truncation_reason") in SAMPLE_CAPS:
                        capped = True
                    else:
                        gap = True
                withheld += int(done.get("withheld_count") or 0)
                queues.append([(unit, m) for m in unit.scan["items"]
                               if isinstance(m.get("citation"), str)])
            listed: dict[str, tuple[_MailUnit, Mapping[str, Any]]] = {}
            for rank in range(max((len(q) for q in queues), default=0)):
                for queue in queues:
                    if rank < len(queue):
                        listed.setdefault(queue[rank][1]["citation"], queue[rank])
            if len(listed) > limit:
                capped = True
                listed = dict(list(listed.items())[:limit])
            seen = {c: self._hits[c] for c in listed if c in self._hits}
            read = self._read_bodies([pair for c, pair in listed.items() if c not in seen],
                                     MAX_SEARCH_TEXTS, seed, every=True)
        self._hits.update((r["handle"], r) for r in read["records"])
        fresh = {r["handle"]: r for r in read["records"]}
        return {**read, "records": [seen.get(c) or fresh[c] for c in listed],
                "source": [u.source for u in self._units],
                "complete": not (capped or gap), "capped": capped and not gap,
                "withheld": withheld, **self._scope_blocked()}

    def _each_query(self, queries: Sequence[str],
                    search: Callable[[str], Mapping[str, Any]], unit: _MailUnit | None = None
                    ) -> tuple[list[dict[str, Any]], list[Mapping[str, Any]]]:
        """(one result per query, every item found once) of ``search`` over ``queries``. A
        query outside the search grammar is refused here, before the reader; a reader's
        ValueError on a query the grammar admits is a failed search, never a refusal. A
        mail ``unit`` blocked by its scope is never searched (``refused: scope``); one
        whose read the exclusion guard cannot decide is blocked from then on."""
        from integrations import live_source_grammar as grammar
        from integrations.live_source_guard import (NotVisible, ReadOnlyGuardError,
                                                    SourceUnavailable)
        from integrations.people_exclusion import ExclusionUnavailable
        results: list[dict[str, Any]] = []
        found: dict[str, Mapping[str, Any]] = {}
        for query in queries:
            self._guard()
            if unit is not None and unit.blocked:
                results.append({"query": query, "refused": "scope"})
                continue
            if not grammar.search_text_ok(query):
                results.append({"query": query, "refused": "guard"})
                continue
            try:
                listing = search(query)
            except ExclusionUnavailable as exc:
                if unit is None:   # no mail unit: the guard's refusal, as any NotVisible
                    results.append({"query": query, "refused": "guard"})
                    continue
                unit.blocked = _exclusion_block(exc)
                results.append({"query": query, "refused": "scope"})
                continue
            except (ReadOnlyGuardError, NotVisible):
                results.append({"query": query, "refused": "guard"})
                continue
            except ValueError:
                results.append({"query": query, "failed": "reader_error"})
                continue
            except SourceUnavailable as exc:
                failure = ReadFailed.of("search", exc)
                if failure.http_status in SEARCH_REFUSED_STATUSES:
                    results.append({"query": query, "refused": "tenant",
                                    **{k: v for k, v in failure.fields().items()
                                       if k != "blocked"}})
                else:
                    results.append({"query": query, "failed": "unavailable"})
                continue
            done = listing["completeness"]
            items = [i for i in listing["items"] if isinstance(i.get("citation"), str)]
            results.append({"query": query, "handles": [i["citation"] for i in items],
                            "complete": not done.get("truncated"),
                            "withheld": int(done.get("withheld_count") or 0)})
            if listing.get("excluded"):
                results[-1]["excluded"] = listing["excluded"]
            for item in items:
                found.setdefault(item["citation"], item)
        return results, list(found.values())

    def _sample(self, kind: str, limit: int, seed: str) -> dict[str, Any]:
        if kind == "mail":
            unconfigured = self._mail_unconfigured()
            if unconfigured is not None:
                return {**unconfigured, "complete": False}
            from integrations.people_exclusion import ExclusionUnavailable
            readable = [u for u in self._units if u.blocked is None]
            share = max(1, limit // max(1, len(readable)))
            complete, capped, withheld = True, True, 0
            pairs: list[tuple[_MailUnit, Mapping[str, Any]]] = []
            with self._mail.directory_cache():   # one mailbox lookup for list and bodies
                for unit in readable:
                    try:
                        listing = self._list_mail(unit, page_size=50,
                                                  max_pages=max(1, share // 50),
                                                  max_items=share)
                    except ExclusionUnavailable as exc:
                        if unit.scope_id is None:   # one mailbox: the guard's refusal
                            raise
                        unit.blocked = _exclusion_block(exc)
                        continue
                    done = listing["completeness"]
                    if done.get("truncated"):
                        complete = False
                        capped = capped and done.get("truncation_reason") in SAMPLE_CAPS
                    withheld += int(done.get("withheld_count") or 0)
                    pairs += [(unit, m) for m in listing["items"]]
                whole = all(u.blocked is None for u in self._units)
                complete = complete and whole
                return {"source": [u.source for u in self._units], "complete": complete,
                        "capped": not complete and capped and whole,
                        "withheld": withheld, **self._scope_blocked(),
                        **self._read_bodies(pairs, max(1, limit // CONTENT_SHARE), seed)}
        if kind == "sharepoint":
            return self._sharepoint(limit, seed)
        raise ValueError(f"unknown document kind {kind!r}")

    def _list_mail(self, unit: _MailUnit, **kwargs: Any) -> dict[str, Any]:
        """``list_messages`` of the unit's folder, or of its whole mailbox without the
        messages of ``EXCLUDED_MAIL_FOLDERS`` (their ids resolved once per run; a folder the
        mailbox does not have excludes nothing, any other failure to resolve one fails the
        read), counted ``excluded``."""
        if unit.blocked is not None:   # a blocked unit is never requested
            raise ValueError("mail_unit_blocked")
        listing = self._mail.list_messages(self._actor, unit.mailbox, folder=unit.folder,
                                           **kwargs)
        if unit.folder is not None:
            return listing
        if unit.excluded is None:
            from integrations.live_source_guard import NotVisible
            from integrations.people_exclusion import ExclusionUnavailable
            ids = set()
            for name in EXCLUDED_MAIL_FOLDERS:
                self._guard()
                try:
                    ids.add(self._mail.resolve_folder(self._actor, unit.mailbox, name))
                except ExclusionUnavailable:
                    raise
                except NotVisible:   # the mailbox has no such folder
                    continue
            unit.excluded = frozenset(ids)
        kept = [m for m in listing["items"] if m.get("parent_folder_id") not in unit.excluded]
        return {**listing, "items": kept, "excluded": len(listing["items"]) - len(kept)}

    def _read_bodies(self, messages: Sequence[tuple[_MailUnit, Mapping[str, Any]]],
                     count: int, seed: str, every: bool = False,
                     first: Callable[..., tuple] | None = None) -> dict[str, Any]:
        """The records of ``messages`` (each with the unit it was listed in), the text
        bodies of ``count`` of them in their ``contents`` (ranked by ``first``, then by a
        digest of the seed and the handle), the names and text of a read message's
        attachments (``_attachments``), and the read counts; ``every``: the messages not
        tried are counted too (``text_budget``, one without an id ``failed``)."""
        from integrations.live_source_guard import NotVisible, SourceUnavailable
        pairs = [(_message(message), message, unit) for unit, message in messages]
        ranked = sorted(((digest([seed, record["handle"]]), record, message, unit)
                         for record, message, unit in pairs
                         if isinstance(message.get("id"), str)),
                        key=lambda found: ((first(found[1], found[2]) if first else ()),
                                           found[0]))
        unread: dict[str, int] = {}
        read = 0
        if every:
            for _ in range(len(pairs) - len(ranked)):
                _count(unread, "failed")
            for _ in ranked[count:]:
                _count(unread, "text_budget")
        for _, record, message, unit in ranked[:count]:
            self._guard()
            try:
                got = self._mail.get_message(
                    self._actor, unit.mailbox, message["id"], include_body=True,
                    expected_folder_id=message.get("parent_folder_id"))
            except NotVisible:
                _count(unread, "refused")
                continue
            except SourceUnavailable:
                _count(unread, "unavailable")
                continue
            listed = message.get("change_key")
            if not isinstance(listed, str) or got.get("change_key") != listed:
                _count(unread, "unavailable")   # edited since listed: not this record's body
                continue
            body = got.get("body_text")
            if not isinstance(body, str) or not body.strip():
                _count(unread, "no_text")
                continue
            record["contents"] = [body]
            read += 1
            if message.get("has_attachments") is True:
                self._attachments(unit, got, record, unread)
        return {"records": [record for record, _, _ in pairs], **_text_counts(read, unread)}

    def _attachments(self, unit: _MailUnit, message: Mapping[str, Any],
                     record: dict[str, Any], unread: dict[str, int]) -> None:
        """The names of a read message's attachments are added to its ``texts`` and the text
        of up to ``MAX_MAIL_ATTACHMENTS`` of its non-inline file attachments of a text kind
        (not over ``MAX_TEXT_BYTES``) to its ``contents``, as capture reads them; one not
        read is counted in ``unread`` by cause as a text (``_unread_cause``), within
        ``MAX_TEXT_SECONDS`` as ``_read_texts`` reads files. A listing the reader refuses or
        cannot read adds nothing."""
        import time
        from integrations.captured_text import capture_text
        from integrations.live_source_guard import NotVisible, SourceUnavailable
        self._guard()
        try:
            listed = self._mail.list_attachments(self._actor, unit.mailbox, message["id"])
        except (NotVisible, SourceUnavailable, ValueError):
            return
        items = [a for a in listed["items"] if isinstance(a, Mapping)]
        record["texts"] = [*record["texts"], *(a["name"] for a in items if a.get("name"))]
        end = time.monotonic() + MAX_TEXT_SECONDS
        tried = 0
        for item in items:
            if tried >= MAX_MAIL_ATTACHMENTS:
                break
            if item.get("is_inline") or not text_kind(item.get("name")) \
                    or not isinstance(item.get("id"), str) or (
                        isinstance(item.get("size"), int) and item["size"] > self._max_bytes):
                continue
            tried += 1
            left = end - time.monotonic()
            if isinstance(self._guard, Deadline):
                left = min(left, self._guard.remaining())
            if left <= 0:
                _count(unread, "time_budget")
                continue
            self._guard()
            try:
                got = self._mail.get_attachment(self._actor, unit.mailbox, message["id"],
                                                item["id"], max_bytes=self._max_bytes,
                                                message=message)
            except NotVisible:
                _count(unread, "refused")
                continue
            except (SourceUnavailable, ValueError):
                _count(unread, "unavailable")
                continue
            meta = {"name": item["name"], "content_type": item.get("content_type")}
            if text_kind(item["name"]) in PLAIN_TEXT_SUFFIXES:
                meta = {**meta, "content_type": None, "mime_type": "text/plain"}
            found = _within(left, capture_text, got["content"], meta, max_chars=MAX_TEXT_CHARS)
            if found is None:
                self._guard()
                _count(unread, "time_budget")
                end = time.monotonic()
                continue
            status, text = found
            if text is None or not text.strip():
                _count(unread, "no_text" if text is not None else _unread_cause(status))
                continue
            record["contents"] = [*record["contents"], text[:MAX_TEXT_CHARS]]

    def _sharepoint(self, limit: int, seed: str, once: bool = False) -> dict[str, Any]:
        """``once``: a file the run's searches read is reused, one read here is kept for
        them (``list_documents``); else an independent read (``sample``)."""
        source = [f"sharepoint:{path}" for path in self._paths]
        if self._drive is None or not self._paths:
            return {"records": [], "complete": False, "source": source,
                    "not_configured": "sharepoint_paths_not_given" if not self._paths
                    else "sharepoint_drive_not_configured"}
        # Breadth-first below each path: library folders hold folders (one per customer),
        # not files, so a top-level listing alone reads no document.
        # capped: a gap this sampler's own limits made; gap: any other (never a sample)
        files, capped, gap, withheld, listed = [], False, False, 0, 0
        units = MAX_DRIVE_FOLDERS * max(1, -(-limit // DRIVE_FOLDERS_PER))
        folders = units - units // CONTENT_SHARE
        if self._max_folders is not None:
            folders = min(folders, self._max_folders)
        queue = [(path, 0) for path in self._paths]
        while queue and len(files) < limit:
            if listed >= folders:
                break
            path, depth = queue.pop(0)
            self._guard()
            listing = self._drive.list_folder(self._actor, path, max_items=limit)
            listed += 1
            done = listing["completeness"]
            if done.get("truncated"):
                if done.get("truncation_reason") in SAMPLE_CAPS:
                    capped = True
                else:
                    gap = True
            withheld += int(done.get("withheld_count") or 0)
            for entry in listing["items"]:
                if entry.get("kind") == "file":
                    files.append(({"handle": entry["citation"],
                                   "texts": [entry.get("name") or ""]}, entry))
                elif entry.get("kind") == "folder":
                    if not isinstance(entry.get("path"), str):
                        gap = True  # a folder left unread: never a full read
                    elif depth + 1 > self._max_depth:
                        capped = True
                    else:
                        queue.append((entry["path"], depth + 1))
        capped = capped or bool(queue) or len(files) > limit
        files = files[:limit]
        # once: a file a search of this run already found keeps the text it read (or
        # tried), read and counted once a run, as a later search reuses a listed one
        seen = {record["handle"]: self._hits[record["handle"]] for record, _ in files
                if once and record["handle"] in self._hits}
        new = [(record, entry) for record, entry in files if record["handle"] not in seen]
        tried: list[dict[str, Any]] = []
        read, unread, pages = self._read_texts(new, units - listed, seed, tried=tried)
        if once:
            self._hits.update((record["handle"], record) for record in tried)
        return {"records": [seen.get(record["handle"], record) for record, _ in files],
                "source": source,
                "complete": not (capped or gap), "capped": capped and not gap,
                "withheld": withheld, **_text_counts(read, unread), "pages_unread": pages}

    def _read_texts(self, files: Sequence[tuple[dict[str, Any], Mapping[str, Any]]],
                    budget: int, seed: str, every: bool = False,
                    tried: list[dict[str, Any]] | None = None,
                    first: Callable[..., tuple] | None = None
                    ) -> tuple[int, dict[str, int], int]:
        """Reads the text of up to ``budget`` readable files into their records'
        ``contents``, ranked by ``first`` (``_first``), then by a digest of the seed and
        the handle, as capture reads them; (read, unread by cause, scanned pages left unread). ``every``: the files
        not tried are counted too: ``not_text_kind``, ``exceeds_cap`` (over
        ``MAX_TEXT_BYTES``) or ``text_budget`` (past ``budget``). A reading the
        ``MAX_TEXT_SECONDS`` end (a long OCR) and every file after it are
        ``time_budget``; a reading the run's deadline ends is abandoned at it. A PDF
        read on its text-layer pages only (no OCR on this host) keeps that text, is
        ``unread_scanned_pages`` and never read; one read on its first pages only (the
        page bound) keeps that text, is ``skipped_by_cap`` and never read, so no match
        in it is ever taken for absence. ``tried`` receives each record read or counted."""
        import time
        from integrations.captured_text import OCR_UNAVAILABLE, capture_text
        from integrations.live_source_guard import NotVisible, SourceUnavailable
        from ontology.mentions import text_pages
        unread: dict[str, int] = {}
        readable = []
        for record, entry in files:
            if not (text_kind(entry.get("name")) and isinstance(entry.get("id"), str)):
                cause = "not_text_kind"
            elif isinstance(entry.get("size"), int) and entry["size"] > self._max_bytes:
                cause = "exceeds_cap"
            else:
                readable.append((digest([seed, record["handle"]]), record, entry))
                continue
            if every:
                _count(unread, cause)
                if tried is not None:
                    tried.append(record)
        readable.sort(key=lambda ranked: ((first(ranked[1], ranked[2]) if first else ()),
                                          ranked[0]))
        if every:
            for _, record, _ in readable[max(0, budget):]:
                _count(unread, "text_budget")
                if tried is not None:
                    tried.append(record)
        read = pages = 0
        end = time.monotonic() + MAX_TEXT_SECONDS
        for _, record, entry in readable[:max(0, budget)]:
            if tried is not None:
                tried.append(record)
            left = end - time.monotonic()
            if isinstance(self._guard, Deadline):
                left = min(left, self._guard.remaining())
            if left <= 0:
                _count(unread, "time_budget")
                continue
            self._guard()
            try:
                got = self._drive.get_content(self._actor, entry["id"],
                                              max_bytes=self._max_bytes)
            except NotVisible:
                _count(unread, "refused")
                continue
            except SourceUnavailable:
                _count(unread, "unavailable")   # gone or changed while read
                continue
            content = got.get("content")
            if not isinstance(content, bytes):
                _count(unread, str(got.get("content_status")))
                continue
            listed = got.get("record")
            meta = listed if isinstance(listed, Mapping) else entry
            if text_kind(meta.get("name")) in PLAIN_TEXT_SUFFIXES:
                meta = {**meta, "content_type": None, "mime_type": "text/plain"}
            found = _within(left, capture_text, content, meta, max_chars=MAX_TEXT_CHARS)
            if found is None:   # still reading (a long OCR) when the text time ran out
                self._guard()   # or the run's: the sample is cut, blocked by the budget
                _count(unread, "time_budget")
                end = time.monotonic()
                continue
            status, text = found
            if text is None or not text.strip():
                _count(unread, "no_text" if text is not None else _unread_cause(status))
                continue
            record["contents"] = [text[:MAX_TEXT_CHARS]]
            skipped = _pages_skipped(content, meta, text)
            pages += skipped or 0
            if status == OCR_UNAVAILABLE:   # its scanned pages were not read
                _count(unread, "unread_scanned_pages")
                pages += sum(not page.strip() for page in text_pages(text))
                continue
            if skipped is not None:   # its pages past the bound were not read
                _count(unread, "skipped_by_cap")
                continue
            read += 1
        return read, unread, pages


def _pages_skipped(content: bytes, record: Mapping[str, Any], text: str) -> int | None:
    """The pages past the page bound a PDF's text (pages 1-``MAX_PAGES``, as capture reads
    it) left unread: the ``skipped_by_cap`` of the reading capture makes of these bytes
    and that text (``captured_text.read_text``), 0 when its page count cannot be told
    (never a whole read either, as that reading rules), None when the text is the whole
    file's."""
    from integrations.captured_text import read_text
    from ontology.core import ValidationError
    try:
        skipped = read_text(content, record.get("content_type") or record.get("mime_type")
                            or "", filename=str(record.get("name") or ""),
                            text=text).skipped_by_cap
    except ValidationError:   # the PDF's page count cannot be told
        return 0
    return None if skipped is None else skipped["last_page"] - skipped["first_page"] + 1


def _within(seconds: float, read: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """``read(*args, **kwargs)``, or None when it still runs after ``seconds``: it is
    abandoned (``child_process.abandon``: its OCR children stopped, their temporary folders
    removed) and left to end on its daemon thread, its result unused."""
    got = _on_thread(seconds, read, *args, **kwargs)
    if got is None:
        return None
    return got[0] if got[1] is None else ("failed", None)


def _on_thread(seconds: float, read: Callable[..., Any], *args: Any, **kwargs: Any
               ) -> tuple[Any, BaseException | None] | None:
    """(result, None) or (None, its exception) of ``read(*args, **kwargs)`` run on a
    daemon thread, or None when it still runs after ``seconds``: abandoned
    (``child_process.abandon``) and left to end on its thread, its result unused."""
    import threading
    from integrations import child_process
    out: list[tuple[Any, BaseException | None]] = []

    def work() -> None:
        try:
            out.append((read(*args, **kwargs), None))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            out.append((None, exc))
    worker = threading.Thread(target=work, name="eval-oracle-read", daemon=True)
    worker.start()
    worker.join(max(0.0, seconds))
    if worker.is_alive():
        child_process.abandon(worker)
        return None
    return out[0]


_OCR_TYPED = re.compile(r"ocr_failed_[a-z_]{1,24}")


def _count(unread: dict[str, int], cause: str) -> None:
    """One more text unread for ``cause`` (``UNREAD_CAUSES``, or ``ocr_failed_<cause>``
    from ``_unread_cause``; any other is ``failed``)."""
    cause = cause if cause in UNREAD_CAUSES or _OCR_TYPED.fullmatch(cause) else "failed"
    unread[cause] = unread.get(cause, 0) + 1


def _unread_cause(status: str) -> str:
    """A text status as an unread cause: an OCR run that failed keeps its typed cause
    (``ocr_failed_deadline``), any other status loses a suffix it has not."""
    from integrations.captured_text import ocr_failure_cause, text_status_kind
    cause = ocr_failure_cause(status)
    return text_status_kind(status) if cause is None else f"ocr_failed_{cause}"


def _alnum(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _first(queries: Sequence[str], results: Sequence[Mapping[str, Any]]
           ) -> Callable[[Mapping[str, Any], Mapping[str, Any]], tuple[int, int, int]]:
    """The order a search reads its hits in, spending the text budget where a root can be
    named first (live 2026-10-20: 53 of 405 mail documents read by a digest of the seed,
    352 left past the budget): a hit whose subject, preview or file name holds a number
    searched for (letters and digits alone), then a message with attachments (their names
    and text may), then the hit the fewest documents answered a query with (the most
    specific search), the seed's digest after (``_read_bodies``, ``_read_texts``)."""
    wanted = [n for n in map(_alnum, queries) if n]
    hits: dict[str, int] = {}
    for result in results:
        handles = result.get("handles") or ()
        for handle in handles:
            hits[handle] = min(hits.get(handle, len(handles)), len(handles))

    def key(record: Mapping[str, Any], entry: Mapping[str, Any]) -> tuple[int, int, int]:
        head = _alnum(" ".join(map(str, record.get("texts") or ())))
        return (not any(n in head for n in wanted), entry.get("has_attachments") is not True,
                hits.get(record.get("handle"), 10 ** 9))
    return key


def _text_counts(read: int, unread: Mapping[str, int]) -> dict[str, Any]:
    return {"texts_read": read, "texts_unread": sum(unread.values()),
            "texts_unread_by": dict(sorted(unread.items()))}


# Which result of the units not answering a merged mail query stands for them, first
# first: a failure, then the guard's refusal, the tenant's, any other, the scope's.
_UNANSWERED_RANK = ("failed", "guard", "tenant", "other", "scope")


def _unanswered_rank(result: Mapping[str, Any]) -> int:
    if result.get("failed"):
        return 0
    cause = result.get("refused")
    return _UNANSWERED_RANK.index(cause if cause in _UNANSWERED_RANK else "other")


def _merged(per_unit: Sequence[Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """The results of each query over the mail units, merged: one unit's stand as they
    are; else, per query, one answer of every unit that answered (its handles once each,
    ``complete`` only when each of them was, ``withheld``/``excluded`` summed, ``scan``
    when any was a scan) and, when any unit did not answer, one more result saying why
    (``_UNANSWERED_RANK``: a failure before a refusal, the scope's last), so a query some
    unit could not be asked is never reported as fully answered."""
    if len(per_unit) == 1:
        return [dict(r) for r in per_unit[0]]
    out: list[dict[str, Any]] = []
    for index in range(max((len(r) for r in per_unit), default=0)):
        row = [results[index] for results in per_unit if index < len(results)]
        answered = [r for r in row if "handles" in r]
        if answered:
            handles = list(dict.fromkeys(h for r in answered for h in r["handles"]))
            answer: dict[str, Any] = {
                "query": answered[0]["query"], "handles": handles,
                "complete": all(r.get("complete") for r in answered),
                "withheld": sum(int(r.get("withheld") or 0) for r in answered)}
            excluded = sum(int(r.get("excluded") or 0) for r in answered)
            if excluded:
                answer["excluded"] = excluded
            if any(r.get("scan") for r in answered):
                answer["scan"] = True
            out.append(answer)
        rest = [r for r in row if "handles" not in r]
        if rest:
            # beside an answer the row speaks for a unit, not for the query: marked so a
            # consumer counts it apart from the searches (one per query, not per unit)
            out.append({**min(rest, key=_unanswered_rank), **({"unit": True} if answered else {})})
    return out


def _message(message: Mapping[str, Any]) -> dict[str, Any]:
    return {"handle": message["citation"],
            "texts": [message.get("subject") or "", message.get("body_preview") or ""]}


def text_kind(name: Any) -> str | None:
    """The extension of a file name the documents' text is read for (``TEXT_SUFFIXES``),
    or None."""
    if not isinstance(name, str) or "." not in name:
        return None
    suffix = name.rsplit(".", 1)[1].lower()
    return suffix if suffix in TEXT_SUFFIXES else None


def build_live(environ: Mapping[str, str], args: argparse.Namespace, *,
               guard: Guard = _no_guard) -> tuple[Sources, Any, dict[str, str]]:
    """The live ports, each read run under ``guard`` (a ``Deadline`` stops it there)."""
    from integrations.workbench_read_api import WorkbenchReadApiClient
    from ontology.types import Actor

    if not source_configured(environ, args):
        raise ValueError("live_requires_a_configured_source")   # nothing to read at all
    actor = Actor("eval", frozenset({EVAL_PRINCIPAL}), "restricted")
    if portal_configured(environ):
        # A relation read the deadline abandoned stops at its next page. Only a
        # Deadline's: it types a read it cut as budget (``Deadline.call``); a plain guard
        # firing mid-read would cut a read it never owned.
        client = WorkbenchReadApiClient(
            environ["PLX_WORKBENCH_API_URL"], environ["PLX_WORKBENCH_API_KEY"],
            before_page=guard if isinstance(guard, Deadline) else None)
        read_portal = portal_port(client, guard)
        configured = {"portal": "live-read"}
    else:  # the Portal strata are then blocked (not_configured), BC and documents still run
        read_portal = _portal_not_configured
        configured = {"portal": "not_configured"}
    bc = None
    if not args.no_bc and all(environ.get(n) for n in ("BC_TENANT_ID", "BC_CLIENT_ID",
                                                        "BC_CLIENT_SECRET", "BC_ENVIRONMENT")):
        from integrations.bc_read import BusinessCentralReader, build_bc
        from integrations.live_source_guard import LiveSourceAudience
        api, _, tenant_id, environment = build_bc(environ)
        audience = LiveSourceAudience({f"bc:{environment.lower()}:*": [EVAL_PRINCIPAL]},
                                      tenant="eval")
        reader = BusinessCentralReader(api, audience, tenant_id=tenant_id,
                                       environment=environment)
        bc = BcReads(LiveBcPort(reader, actor, guard=guard))
        configured["bc"] = "live-read"
    documents = None
    graph_env = ("MICROSOFT_GRAPH_TENANT_ID", "MICROSOFT_GRAPH_CLIENT_ID",
                 "MICROSOFT_GRAPH_CLIENT_SECRET")
    units, blocked = getattr(args, "mail_units", ()) or (), getattr(args, "mail_blocked", ()) or ()
    mail = bool(args.mailbox or units or blocked)
    if (mail or args.sharepoint_path) and not all(environ.get(n) for n in graph_env):
        configured["documents"] = "not_configured"
    elif mail or args.sharepoint_path:
        from integrations.graph_read import GraphMailReader, SharePointReader, build_graph
        from integrations.live_source_guard import LiveSourceAudience
        api, transport = build_graph(environ)
        drive = environ.get("SHAREPOINT_DRIVE_ID")
        patterns = {"mail:*": [EVAL_PRINCIPAL]}
        if drive:
            patterns[f"sharepoint:{drive}:/*"] = [EVAL_PRINCIPAL]
        audience = LiveSourceAudience(patterns, tenant="eval")
        documents = LiveDocuments(
            actor, mail_reader=GraphMailReader(api, audience) if mail else None,
            mailbox=args.mailbox, folder=args.folder, mail_units=units, mail_blocked=blocked,
            drive_reader=SharePointReader(api, audience, drive, transport=transport)
            if drive and args.sharepoint_path else None,
            drive_paths=args.sharepoint_path or (), guard=guard,
            **getattr(args, "sharepoint_bounds", None) or {})
        configured["documents"] = "live-read"
    return Sources(PortalReads(read_portal), bc), documents, configured


class PortalNotConfigured(RuntimeError):
    """No Portal credentials on this host: every Portal read is blocked, never empty."""
    error_code = "not_configured"


def _portal_not_configured(name: str, columns: Sequence[str]) -> Mapping[str, Any]:
    raise PortalNotConfigured(name)


def portal_configured(environ: Mapping[str, str]) -> bool:
    return bool(environ.get("PLX_WORKBENCH_API_URL") and environ.get("PLX_WORKBENCH_API_KEY"))


def source_configured(environ: Mapping[str, str], args: argparse.Namespace) -> bool:
    """Whether a live run has any source to read: the Portal, BC or the documents."""
    bc = not args.no_bc and all(environ.get(n) for n in (
        "BC_TENANT_ID", "BC_CLIENT_ID", "BC_CLIENT_SECRET", "BC_ENVIRONMENT"))
    mail = bool(args.mailbox or getattr(args, "mail_units", ()) or getattr(
        args, "mail_blocked", ()))
    documents = bool(mail or args.sharepoint_path) and all(environ.get(n) for n in (
        "MICROSOFT_GRAPH_TENANT_ID", "MICROSOFT_GRAPH_CLIENT_ID",
        "MICROSOFT_GRAPH_CLIENT_SECRET"))
    return portal_configured(environ) or bc or documents


def build_fixture(world: Any = None) -> tuple[Sources, Any, dict[str, str], Any]:
    """Ports over a fixture world: by default the original small world (other fixture
    evals project it too); the dev and held-out sets pass ``build_eval_world()``."""
    from integrations.workbench_read_api import WorkbenchReadApiClient
    from evals.oracle.fixture_world import build_world

    world = build_world() if world is None else world
    client = WorkbenchReadApiClient("https://workbench-fixture.invalid", "plxwb_" + "f" * 40,
                                    fetch=world.fetch)
    sources = Sources(PortalReads(portal_port(client)), BcReads(world.bc_port()))
    return sources, world.documents(), {"portal": "injected-transport",
                                        "bc": "injected-transport",
                                        "documents": "injected-transport"}, world


def eval_world() -> Any:
    """The fixture world the frozen dev items and the held-out fixture manifest use."""
    from evals.oracle.fixture_world import build_eval_world
    return build_eval_world()


# ----------------------------------------------------------------- run + report

def _validator() -> Callable[[Mapping[str, Any]], list[str]]:
    from ontology.json_contract import validate
    schema = json.loads((ROOT / "evals" / "item.schema.json").read_text(encoding="utf-8"))
    return lambda item: validate(item, schema, root=schema)


def execute(sources: Sources, documents: Any, *, run_id: str, now: str, count: int,
            audit: int, id_suffix: str | None, evidence_note: str | None,
            evidence_label: str, configured: Mapping[str, str], split: str = "dev",
            exclude_roots: Sequence[str] = (), exclude_keys: Collection[str] = ()
            ) -> tuple[dict, list[dict]]:
    """Generate and check one run's items. ``exclude_keys`` (partition key digests of the
    other partition, ``evals/partition.py``) drops every item whose group touches one
    (``partition_dropped.related_to_other``). A held-out run adds one metamorphic variant
    per item, the transforms in turn (``evals.oracle.perturb``: the run's own reads
    perturbed), checked like every item over the reads it was answered from;
    ``by_family`` counts the standard items, ``by_variant`` every item by its label."""
    from evals.partition import related
    generated = generate(sources, run_id=run_id, now=now, count=count, documents=documents,
                         id_suffix=id_suffix, audit=audit, evidence_note=evidence_note,
                         split=split, exclude_roots=exclude_roots,
                         perturb=(lambda items: variants(sources, items, now=now,
                                                         rotate=True))
                         if split == "held_out" else None)
    dropped = related(generated.items, exclude_keys) if exclude_keys else set()
    generated.items[:] = [i for n, i in enumerate(generated.items) if n not in dropped]
    validate = _validator()
    rows, counts = [], {"items": 0, "variants": 0, "schema_valid": 0, "deterministic": 0,
                        "root_resolves": 0, "audit_requested": 0, "answerable": 0,
                        "partial": 0, "blocked": 0}
    # The items whose second answer differed, by family and by variant label (no item id).
    unstable: dict[str, dict[str, int]] = {"family": {}, "variant": {}}
    by_family: dict[str, int] = {}
    by_variant: dict[str, int] = {}
    again = rerun(generated.items, sources)
    for item, repeated in zip(generated.items, again):
        result = item["known_outcome"]
        valid = not validate(item)
        counts["items"] += 1
        counts["variants"] += item["variant"] != "standard"
        counts["schema_valid"] += valid
        counts["deterministic"] += repeated == result
        if repeated != result:
            for axis in unstable:
                unstable[axis][item[axis]] = unstable[axis].get(item[axis], 0) + 1
        counts["root_resolves"] += result["answerability"] != "blocked"
        counts["audit_requested"] += audit_requested(item)
        counts[result["answerability"]] += 1
        if item["variant"] == "standard":
            by_family[item["family"]] = by_family.get(item["family"], 0) + 1
        by_variant[item["variant"]] = by_variant.get(item["variant"], 0) + 1
        rows.append({"id": item["id"], "family": item["family"], "variant": item["variant"],
                     "stratum": generated.item_strata.get(item["id"]),
                     # company-name slugs (bc_<name>) never reach stdout
                     "lineage": "bc" if item["lineage_stratum"].startswith("bc_")
                     else item["lineage_stratum"],
                     "hash16": item_hash(item)[:16],
                     "answerability": result["answerability"],
                     "gap_reasons": sorted({g.get("reason", "none") for g in result["gaps"]}),
                     "schema_valid": valid, "audit_requested": audit_requested(item)})
    report = {"schema": SCHEMA, "evidence_label": evidence_label,
              "adapter_status": "live-unverified", "values_published": False,
              "run_id": run_id, "split": split, "sources": dict(configured),
              "partition_dropped": {**generated.dropped,
                                    **({"related_to_other": len(dropped)} if exclude_keys
                                       else {})},
              "counts": {**counts, "nondeterministic_by": {
                  axis: dict(sorted(n.items())) for axis, n in unstable.items()}},
              "by_family": by_family,
              "by_variant": dict(sorted(by_variant.items())),
              "strata": strata_report(generated.strata),
              "candidates_by_family": candidates_by_family(generated.strata),
              # Strata with no candidate and no reason (status "read" with 0 candidates, or
              # another status without a reason). The live check requires it empty.
              "unexplained_strata": unexplained_strata(generated.strata),
              "read_failures": sorted(generated.failures),
              "items": rows}
    # looked up on the class: a pure state read, never a read a proxy's guard would spend
    blocked_mail = getattr(type(documents), "blocked_mail", None)
    report["blocked_upstream"] = blocked_upstream(
        generated.strata, mail_blocked=blocked_mail(documents) if blocked_mail else None)
    report["counts"]["blocked_upstream"] = len(report["blocked_upstream"])
    return report, generated.items


def rerun(items: Sequence[Mapping[str, Any]], sources: Sources) -> list[dict[str, Any]]:
    """Each item's oracle answer again, over the reads it was answered from (a variant:
    ``sources`` perturbed as it records), grouped by perturbation so each perturbed read
    is built once; in ``items`` order."""
    perturbed = Perturbed(sources)
    out: dict[int, dict[str, Any]] = {}
    for n in sorted(range(len(items)), key=lambda n: perturbation_key(items[n])):
        item = items[n]
        out[n] = oracle_run(item["family"], item["root"], item["as_known_at"],
                            perturbed.of(item)).result
    return [out[n] for n in range(len(items))]


def blocked_upstream(strata: Mapping[str, Mapping[str, Any]], *,
                     mail_blocked: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """The strata whose source read failed upstream (``blocked: upstream``): the parts of
    the run that did not happen, with the source, HTTP status and error code (the live
    verifier's ``blocked_upstream``). A stratum its capture scope blocked (``blocked:
    scope``) is a part with ``cause: later_ask`` and the later_ask it waits on
    (``eval_unfamiliar_sample.scope_block`` over the stratum's ``scope_blocked``, else the
    documents' ``mail_blocked``: the one ``--mailbox`` has no scope id): the people
    exclusion (source ``people-exclusion``) or the mail grant; an untyped one is not
    listed. The other strata still ran."""
    from scripts.eval_unfamiliar_sample import scope_block

    def source(name: str) -> str:
        return ("portal-workbench-api" if name in PORTAL_STRATA else
                "business-central" if name.startswith("bc_") else "microsoft-graph")

    out = []
    for name, entry in sorted(strata.items()):
        if entry.get("blocked") == "upstream":
            out.append({"part": name, "source": source(name),
                        **{key: entry[key] for key in ("http_status", "error_code")
                           if entry.get(key)}})
        elif entry.get("blocked") == "scope":
            typed = scope_block(entry.get("scope_blocked")
                                or (mail_blocked if name == "mail" else None))
            if typed is None:
                continue
            later = typed.split(":", 1)[1]
            out.append({"part": name, "source": ("people-exclusion"
                                                 if later == MAIL_SCOPE_EXCLUSION
                                                 else "mail-scope"),
                        "cause": "later_ask", "later_ask": later})
    return out


def strata_report(strata: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per stratum: status, candidate and item counts (also per family), the funnel of
    where rows dropped out (row counts) and, apart from it, the candidates the split
    dropped (BC: the closed ``document_roots`` read for the document search). A
    stratum not ``read`` gives its reason and the source it tried (relation
    columns, BC entity sets, mailbox/folder, drive paths); a BC or document read says when
    it was cut off or withheld items; an unavailable one says ``blocked`` (``upstream``
    or ``budget``) with the HTTP status and error code. Counts, codes and source names only (the verifier's
    record budget keeps a ``read`` stratum's sources out)."""
    out: dict[str, dict[str, Any]] = {}
    for name, entry in strata.items():
        row: dict[str, Any] = {"status": entry.get("status"),
                               "candidates": entry.get("candidates", 0),
                               "items": entry.get("items", 0),
                               "funnel": dict(entry.get("funnel", {}))}
        for key in ("candidates_by_family", "items_by_family"):
            if entry.get(key):
                row[key] = dict(entry[key])
        for key in ("candidates_dropped_by_split", "status_out_of_vocabulary", "companies",
                    "document_roots"):
            if key in entry:
                row[key] = entry[key]
        if entry.get("status") != "read":
            row["tried"] = list(entry.get("tried", []))
        if entry.get("complete") is False:
            row["complete"] = False
        # an unavailable stratum: blocked upstream (or budget), with HTTP status and code
        for key in ("reason", "within_read", "withheld", "blocked", "http_status",
                    "error_code"):
            if entry.get(key):
                row[key] = entry[key]
        out[name] = row
    return out


def candidates_by_family(strata: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for entry in strata.values():
        for family, count in (entry.get("candidates_by_family") or {}).items():
            totals[family] = totals.get(family, 0) + count
    return dict(sorted(totals.items()))


def write_dev(items: Sequence[Mapping[str, Any]], directory: Path, *, run_id: str,
              report: Mapping[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("dev-F*.json"):
        old.unlink()
    for item in items:
        (directory / f"{item['id']}.json").write_text(
            json.dumps(item, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest = {"format": MANIFEST_FORMAT, "evidence_label": report["evidence_label"],
                "live": False, "source": "evals/oracle/fixture_world.py (synthetic, not PLX data)",
                "generated_by": f"python3 -m scripts.eval_oracle_dev fixture --run-id {run_id} "
                                f"--write {directory.as_posix()}",
                "run_id": run_id, "counts": report["counts"], "by_family": report["by_family"],
                "audit_requested": [i["id"] for i in items if audit_requested(i)],
                "split_salt": SPLIT_SALT,
                # Every record the dev answers touch: a held-out item may touch none of them.
                "chain_records_sha256": sorted(set().union(*(
                    chain_digests(i["root"], i["known_outcome"]) for i in items))),
                "items": [{"id": i["id"], "sha256": item_hash(i),
                           "root_sha256": item_root_digest(i)} for i in items]}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n",
                                             encoding="utf-8")


# ----------------------------------------------------------------- held-out set

def content_hash(item: Mapping[str, Any]) -> str:
    """Item digest without the oracle code version (named once in the manifest), so a
    manifest moves only when an item's question, root or ground truth moves."""
    oracle = {k: v for k, v in (item.get("oracle") or {}).items() if k != "id"}
    return digest({**item, "oracle": oracle})


def dev_manifest_label(path: Path) -> str | None:
    """Evidence label of the dev manifest a held-out run is kept apart from."""
    label = json.loads(path.read_text(encoding="utf-8")).get("evidence_label")
    return label if isinstance(label, str) else None


def dev_records(path: Path) -> tuple[set[str], set[str]]:
    """(root digests, root + chain record digests) of the frozen dev set, from its
    manifest; a missing manifest or one without them is an error, never an empty
    exclusion."""
    manifest = json.loads(path.read_text(encoding="utf-8"))
    roots = [e.get("root_sha256") for e in manifest.get("items", [])]
    records = manifest.get("chain_records_sha256")
    if not roots or not all(isinstance(r, str) for r in roots) or \
            not isinstance(records, list) or not all(isinstance(r, str) for r in records):
        raise ValueError("dev_manifest_without_record_digests")
    return set(roots), set(roots) | set(records)


def _outside_repository(path: Path) -> bool:
    resolved = path.resolve()
    return resolved != ROOT and ROOT not in resolved.parents


def default_state_dir(env: Mapping[str, str]) -> Path:
    """The credentialed host's runtime state directory for held-out sets (never in Git).
    Each source gets its own subdirectory below it (``<state>/<source>/<run id>``)."""
    for name in ("PLX_EVAL_HELD_OUT_DIR", "PLX_EVAL_HELDOUT_DIR"):
        if env.get(name):
            return Path(env[name])
    return _old_default_state_dir(env)


def _old_default_state_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_STATE_HOME") or str(Path(env.get("HOME") or Path.home()) /
                                             ".local" / "state")
    return Path(base) / "plx-workbench" / "eval-heldout"


def _adopt_legacy_frozen(state: Path, env: Mapping[str, str], source: str,
                         run_id: str) -> None:
    """Copy a set frozen at the legacy location (``PLX_EVAL_HELDOUT_DIR``, else the old
    default) into ``state`` when ``state`` holds none, so the set keeps its set_sha256
    and is never re-sampled. The copy is digest-verified; labels stay on the host."""
    target = state / source / run_id
    if (target / FROZEN).exists():
        return
    legacy = Path(env.get("PLX_EVAL_HELDOUT_DIR") or _old_default_state_dir(env))
    old = legacy / source / run_id
    if legacy.resolve() == state.resolve() or _load_frozen(old) is None:
        return
    shutil.copytree(old, target, dirs_exist_ok=True)


LIVE_DEV_FORMAT = "plx-eval-live-dev-manifest/v1"


def live_dev_dir(state: Path) -> Path:
    """Where the live dev runs leave their digest manifests (host state, never Git)."""
    return state / "live" / "dev"


def write_live_dev_manifest(state: Path, run_id: str, items: Sequence[Mapping[str, Any]]
                            ) -> Path:
    """One live dev run's records and partition keys, as digests only."""
    from evals.partition import key_digests
    path = live_dev_dir(state) / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "format": LIVE_DEV_FORMAT, "run_id": run_id, "evidence_label": "live-read",
        "root_sha256": sorted({r for r in (item_root_digest(i) for i in items) if r}),
        "chain_records_sha256": sorted(set().union(set(), *(
            chain_digests(i["root"], i["known_outcome"]) for i in items))),
        "key_sha256": sorted(set().union(set(), *(key_digests(i) for i in items)))},
        indent=1) + "\n", encoding="utf-8")
    return path


def live_dev_records(state: Path) -> tuple[set[str], set[str], set[str], int]:
    """(roots, roots + chain records, key digests, manifest count) of every live dev run
    recorded in the host state directory."""
    roots: set[str] = set()
    records: set[str] = set()
    keys: set[str] = set()
    paths = sorted(live_dev_dir(state).glob("*.json"))
    for path in paths:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("format") != LIVE_DEV_FORMAT:
            raise ValueError("live_dev_manifest_format")
        roots |= set(manifest["root_sha256"])
        records |= set(manifest["root_sha256"]) | set(manifest["chain_records_sha256"])
        keys |= set(manifest["key_sha256"])
    return roots, records, keys, len(paths)


def frozen_heldout_keys(state: Path) -> tuple[set[str], int]:
    """Key digests of every frozen live held-out set (what a dev run must not hold)."""
    keys: set[str] = set()
    paths = sorted((state / "live").glob("heldout-v*/" + FROZEN))
    for path in paths:
        keys |= set(json.loads(path.read_text(encoding="utf-8")).get("key_sha256") or [])
    return keys, len(paths)


def dev_item_keys(manifest: Path) -> set[str]:
    """Key digests of the committed (fixture) dev items beside their manifest."""
    from evals.partition import key_digests
    return set().union(set(), *(key_digests(json.loads(p.read_text(encoding="utf-8")))
                                for p in sorted(manifest.parent.glob("dev-F*.json"))))


def heldout_manifest(report: Mapping[str, Any], items: Sequence[Mapping[str, Any]],
                     dev: tuple[set[str], set[str]], *, budget: Mapping[str, Any],
                     dev_keys: set[str] = frozenset()) -> dict[str, Any]:
    """Counts and hashes of a held-out run: no question text, identifier, root or value."""
    from evals.partition import key_digests
    dev_roots_set, dev_records_set = dev
    counts = dict(report["counts"])
    counts["dev_key_overlap"] = sum(1 for i in items if key_digests(i) & dev_keys)
    counts["dev_root_overlap"] = sum(1 for i in items if item_root_digest(i) in dev_records_set)
    counts["dev_chain_overlap"] = sum(
        1 for i in items if chain_digests(i["root"], i["known_outcome"]) & dev_records_set)
    counts["partition_violations"] = sum(
        1 for i in items if split_of(i["root"]["relation"], i["root"]["key"]) != "held_out")
    counts["families_without_plan"] = len({i["family"] for i in items} & HELD_OUT_ONLY)
    hashes = sorted(content_hash(i) for i in items)
    oracles = sorted({i["oracle"]["id"] for i in items})
    return {"schema": HELDOUT_FORMAT, "evidence_label": report["evidence_label"],
            "adapter_status": "live-unverified", "values_published": False,
            "set": "heldout", "run_id": report["run_id"], "split_salt": SPLIT_SALT,
            "sources": report["sources"], "oracle": oracles, "counts": counts,
            "by_family": report["by_family"],
            "by_variant": dict(report.get("by_variant") or {}),
            "strata": report["strata"],
            "unexplained_strata": unexplained_strata(report["strata"]),
            "partition_dropped": report["partition_dropped"],
            "blocked_upstream": list(report.get("blocked_upstream") or []),
            "read_failures": report["read_failures"], "budget": dict(budget),
            "dev_roots_excluded": len(dev_roots_set),
            "dev_records_excluded": len(dev_records_set),
            # No item ids: ho-* ids never enter Git (ontology/tests/test_contracts.py).
            "items": [{"family": i["family"], "variant": i["variant"],
                       "hash16": content_hash(i)[:16],
                       "answerability": i["known_outcome"]["answerability"]} for i in items],
            "set_sha256": digest(hashes)}


_PORTAL_GENERATION = re.compile(r'(api://[^"@]+)@[0-9A-Za-z_-]+')


def label_view(result: Mapping[str, Any], frozen: Mapping[str, Any] | None = None) -> str:
    """An oracle result without its Portal read generation (``api://...@<digest of the
    whole read>``): that digest moves whenever any served row changes, so it is not a
    change of this label. BC handles keep their record etag. Against a ``frozen`` label,
    the matching annotations a later oracle adds where the frozen label has none are left
    out: a gap's ``concerns`` and the citations of a value the frozen label cites nothing
    for (an F4 verdict's cells). They change no value, state or answerability, so a set
    frozen before them is not drifted by them."""
    if frozen is not None and isinstance(result.get("values"), list):
        result = dict(result)
        if isinstance(result.get("gaps"), list) and not any(
                "concerns" in gap for gap in frozen.get("gaps") or []):
            result["gaps"] = [{k: v for k, v in gap.items() if k != "concerns"}
                              for gap in result["gaps"]]
        uncited = {value.get("name") for value in frozen.get("values") or []
                   if not value.get("citations")}
        result["values"] = [{k: v for k, v in value.items() if k != "citations"}
                            if value.get("name") in uncited else value
                            for value in result["values"]]
    return _PORTAL_GENERATION.sub(r"\1", json.dumps(result, sort_keys=True))


MAX_LABEL_DRIFT = 0.10  # controller decision: more than 10% drifted labels fails the run


def heldout_ok(manifest: Mapping[str, Any]) -> bool:
    if manifest.get("refused"):
        return False
    counts = manifest["counts"]
    fresh = not (manifest.get("frozen") or {}).get("reused")
    return (counts["items"] > 0 and counts["schema_valid"] == counts["items"]
            and counts.get("label_drift", 0) <= MAX_LABEL_DRIFT * counts.get(
                "measured", counts["items"])
            and (counts["deterministic"] == counts["items"] or not fresh)
            and counts["dev_root_overlap"] == 0 and counts["dev_chain_overlap"] == 0
            and counts.get("dev_key_overlap", 0) == 0
            and counts["partition_violations"] == 0
            and not manifest["budget"]["exhausted"])


FROZEN = "frozen.json"


def _load_frozen(set_dir: Path) -> tuple[dict, list[dict]] | None:
    """The frozen set of one run id, verified against its stored digest list."""
    path = set_dir / FROZEN
    if not path.exists():
        return None
    frozen = json.loads(path.read_text(encoding="utf-8"))
    items = [json.loads(p.read_text(encoding="utf-8"))
             for p in sorted((set_dir / "items").glob("ho-*.json"))]
    hashes = sorted(content_hash(i) for i in items)
    if hashes != frozen.get("item_sha256") or digest(hashes) != frozen.get("set_sha256"):
        raise RuntimeError("heldout_frozen_set_does_not_match_its_digests")
    return frozen, items


def needs_portal(item: Mapping[str, Any]) -> bool:
    """Whether an item's label reads the Portal: its root or a chain record is a Portal
    relation, or it cites a Portal row."""
    relations = [item["root"]["relation"], *(e.get("relation") for e in
                                             item["known_outcome"].get("chain") or [])]
    return any(not str(r).startswith("bc.") for r in relations) or \
        "api://" in json.dumps(item["known_outcome"])


def portal_blocked(sources: Sources) -> dict[str, Any] | None:
    """The Portal outage of today's reads: every relation the oracle reads failed upstream
    (as the unfamiliar eval's ``portal_outage``: the whole source, not one relation). The
    blocked part with the first failure's HTTP status and error code, or None."""
    from evals.oracle.keys import RELATIONS
    failed = [sources.portal.failure(relation) for relation in RELATIONS]
    if not failed or not all(f and f.get("blocked") == "upstream" for f in failed):
        return None
    return {"part": "portal_relations", "source": "portal-workbench-api",
            **{k: failed[0][k] for k in ("http_status", "error_code") if k in failed[0]}}


def run_heldout(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[dict, int]:
    """Generate the held-out set once per run id and freeze it in the host's state
    directory (items + the digest list); later runs with that run id re-check the frozen
    items (integrity, schema, labels against the current source, dev disjointness) and
    never resample. A new set needs a new run id (heldout-v3). A Portal outage blocks the
    parts that read the Portal, never the run; a generation with blocked parts is not
    frozen."""
    from evals.partition import key_digests
    if not HELDOUT_RUN_ID_SHAPE.fullmatch(args.run_id):
        raise ValueError("heldout_run_id_shape")
    state = args.state_dir if args.state_dir is not None else default_state_dir(env)
    if not _outside_repository(state):
        raise ValueError("heldout_state_inside_repository")
    dev = dev_records(args.dev_manifest)
    dev_label = dev_manifest_label(args.dev_manifest)
    deadline = Deadline(args.budget_seconds)
    if args.source == "fixture":
        sources, documents, configured, _ = build_fixture(eval_world())
        now, evidence, note = FIXTURE_NOW, "injected-transport", FIXTURE_NOTE
        dev_keys = dev_item_keys(args.dev_manifest)
        live_dev = 0
    else:
        if not source_configured(env, args):
            raise ValueError("heldout_requires_credentialed_environment")
        sources, documents, configured = build_live(env, args, guard=deadline)
        now, evidence, note = _utc_now(), "live-read", None
        # The live dev runs' records and partition keys (digests in the host state dir).
        roots, records, dev_keys, live_dev = live_dev_records(state)
        dev = (dev[0] | roots, dev[1] | records)
        if live_dev:
            dev_label = "live-read"
    # A fixture run and a live run never share a frozen set.
    set_dir = state / args.source / args.run_id
    if args.state_dir is None:
        _adopt_legacy_frozen(state, env, args.source, args.run_id)
    frozen = _load_frozen(set_dir)
    budget: dict[str, Any] = {"seconds": args.budget_seconds}
    origin = {"source": args.source, "evidence_label": evidence, "id_suffix": args.source}
    if frozen is not None and any(frozen[0].get(k) != v for k, v in origin.items()):
        refused = {"schema": HELDOUT_FORMAT, "set": "heldout", "run_id": args.run_id,
                   "evidence_label": evidence, "adapter_status": "live-unverified",
                   "values_published": False, "refused": "heldout_frozen_source_mismatch",
                   "frozen": {"reused": False,
                              "frozen_source": str(frozen[0].get("source")),
                              "frozen_evidence_label": str(frozen[0].get("evidence_label"))}}
        return refused, 3
    if frozen is not None:
        record, items = frozen
        validate = _validator()
        drift = resolved = blocked = 0
        outage = None
        try:
            sources.portal.prefetch()
            outage = portal_blocked(sources)
            # its label reads the Portal: blocked today, not drifted
            measured = [i for i in items if outage is None or not needs_portal(i)]
            blocked = len(items) - len(measured)
            for item, again in zip(measured, rerun(measured, sources)):
                drift += label_view(again, item["known_outcome"]) != label_view(
                    item["known_outcome"])
                resolved += again["answerability"] != "blocked"
        except BudgetExhausted:
            pass
        budget_by = budget_by_stratum(documents)
        budget["exhausted"] = deadline.hit or any(s["exhausted"] for s in budget_by.values())
        failures = sources.portal.failures()
        if sources.bc is not None:
            failures.update(sources.bc.failures())
        report = {**record["report"], "evidence_label": evidence, "sources": dict(configured),
                  "read_failures": sorted(failures),
                  "blocked_upstream": [outage] if outage is not None else []}
        report["counts"] = {**record["report"]["counts"],
                            "schema_valid": sum(1 for i in items if not validate(i)),
                            "root_resolves": resolved}
        manifest = heldout_manifest(report, items, dev, budget=budget, dev_keys=dev_keys)
        manifest["budget_by_stratum"] = budget_by
        manifest["counts"].update(label_drift=drift, blocked_upstream=blocked,
                                  measured=len(items) - blocked)
        manifest["frozen"] = {"reused": True, "frozen_at": record["frozen_at"],
                              "source": record["source"]}
    else:
        try:
            report, items = execute(sources, documents, run_id=args.run_id, now=now,
                                    count=args.count, audit=0, id_suffix=args.source,
                                    evidence_note=note, evidence_label=evidence,
                                    configured=configured, split="held_out",
                                    exclude_roots=sorted(dev[1]), exclude_keys=dev_keys)
        except BudgetExhausted:
            raise RuntimeError("heldout_budget_exhausted_before_items") from None
        budget_by = budget_by_stratum(documents)
        budget["exhausted"] = deadline.hit or any(s["exhausted"] for s in budget_by.values())
        manifest = heldout_manifest(report, items, dev, budget=budget, dev_keys=dev_keys)
        manifest["budget_by_stratum"] = budget_by
        manifest["counts"].update(label_drift=0, blocked_upstream=0, measured=len(items))
        manifest["frozen"] = {"reused": False, "frozen_at": now, "source": args.source}
        if report["blocked_upstream"]:
            # A set drawn while parts were blocked would lack their families: not frozen.
            manifest["frozen"]["deferred"] = "blocked_upstream"
        elif any(s["exhausted"] for s in budget_by.values()):
            # A document kind whose share of the read time ran out is exhausted for this
            # gate: its stratum is cut by the budget, however it is typed (decision
            # 2026-10-21-oracle-freeze-budget). Named by stratum, never frozen.
            manifest["frozen"]["deferred"] = "budget_exhausted:" + ",".join(
                k for k, s in budget_by.items() if s["exhausted"])
        elif heldout_ok(manifest):  # only a clean first generation is frozen
            item_dir = set_dir / "items"
            item_dir.mkdir(parents=True, exist_ok=True)
            for item in items:  # held-out items and labels stay in the host state dir only
                (item_dir / f"{item['id']}.json").write_text(json.dumps(item, indent=1) + "\n",
                                                            encoding="utf-8")
            hashes = sorted(content_hash(i) for i in items)
            kept = {k: v for k, v in report.items() if k != "items"}
            (set_dir / FROZEN).write_text(json.dumps(
                {"run_id": args.run_id, "frozen_at": now, **origin, "item_sha256": hashes,
                 "set_sha256": digest(hashes), "report": kept,
                 # what a live dev run must not hold (evals/partition.py key digests)
                 "key_sha256": sorted(set().union(set(), *(key_digests(i) for i in items)))},
                indent=1) + "\n", encoding="utf-8")
    # The committed dev manifest is fixture-only: against a live set the record check is
    # vacuous until a live dev run left its digest manifest in the host state directory.
    manifest["dev_manifest_evidence_label"] = dev_label
    manifest["disjointness_vacuous"] = dev_label != evidence
    manifest["live_dev_manifests"] = live_dev
    # Blocked parts and nothing left to measure: the whole check is blocked upstream (the
    # spec's blocked_when), never a failure of the held-out set.
    manifest["outage_unmeasured"] = ("portal_outage" if manifest["blocked_upstream"]
                                     and manifest["counts"]["measured"] == 0 else None)
    text = json.dumps(manifest, indent=1, sort_keys=True) + "\n"
    if args.manifest_out is not None:
        args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_out.write_text(text, encoding="utf-8")
    return manifest, 0 if heldout_ok(manifest) else 3


MAX_SCOPE_MAILBOXES = 999   # scope ids mailbox-1 .. mailbox-999


def mail_scope_target(path: Path) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
    """(units, blocked) of a mail capture scope file (``plx-mail-capture-scope/v1``, as
    ``examples/mail_capture_scope.*.json``), parsed by the capture's own
    ``CaptureScope.from_file``: each named mailbox gets a scope id by its place in the file
    (``mailbox-<n>``, the only name a report gives it); a mailbox inside the allowlist
    (``operating/case_mail_scope.json``) is one unit (scope id, address, folder) per
    folder, one outside it is blocked (``mail-scope-grant``) and never read. The scope
    names mailboxes only: ``allowlisted_mailboxes``, ``all_mailboxes`` enabled or an
    unrestricted allowlist would widen it past the file and are refused, as is anything the
    capture refuses (a ValueError either way, so a bad scope fails closed)."""
    from integrations.capture_mail import CaptureError, CaptureScope

    scope = CaptureScope.from_file(path)   # the capture's own validation first
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if "allowlisted_mailboxes" in document or scope.all_mailboxes \
            or scope.allowlist.is_unrestricted:
        raise CaptureError("mail_scope_widens")
    named = [entry["address"] for entry in document.get("mailboxes", [])]
    if not named or len(named) > MAX_SCOPE_MAILBOXES:
        raise CaptureError("mail_scope_mailboxes_invalid")
    # ``all_mailboxes.exclude`` names mailboxes never read: dropped from the target (their
    # scope ids stay those of their place in the file). An exclusion matching no named
    # mailbox (an object id, a typo) is not understood: refused, never ignored.
    # ``internal_domains`` only classes senders; it selects no mailbox.
    lowered = {address.lower() for address in named}
    if scope.excluded - lowered:
        raise CaptureError("mail_scope_exclude_unmatched")
    folders = dict(scope.mailboxes)
    outside = set(scope.outside_allowlist)
    units: list[tuple[str, str, str]] = []
    blocked: list[tuple[str, str]] = []
    for index, address in enumerate(named, 1):
        scope_id = f"mailbox-{index}"
        if address.lower() in scope.excluded:
            continue
        if address in folders:
            units += [(scope_id, address, folder) for folder in folders[address]]
        elif address in outside:
            blocked.append((scope_id, "mail-scope-grant"))
        else:   # the capture kept a mailbox neither read nor set aside: never guess
            raise CaptureError("mail_scope_mailboxes_invalid")
    if not units and not blocked:   # everything excluded: nothing left to read
        raise CaptureError("mail_scope_mailboxes_invalid")
    return units, blocked


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         live_build: Callable[..., tuple[Sources, Any, dict[str, str]]] | None = None,
         monotonic: Callable[[], float] | None = None) -> int:
    """``live_build`` and ``monotonic`` (tests): the live run's source builder, with
    ``build_live``'s signature, and the clock of its read deadline."""
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_oracle_dev",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("live", help="credentialed, read-only; counts only")
    live.add_argument("--output-dir", required=True, type=Path)
    live.add_argument("--run-id", default="auto",
                      help="sampling seed; 'auto' = live-<UTC hour> (a new sample each hour)")
    live.add_argument("--count", type=int, default=30)
    live.add_argument("--audit", type=int, default=10)
    live.add_argument("--no-bc", action="store_true")
    live.add_argument("--mailbox")
    live.add_argument("--folder")
    live.add_argument("--mail-scope", type=Path, default=None,
                      help="a mail capture scope (plx-mail-capture-scope/v1, named mailboxes "
                           "only): its allowlisted mailboxes and folders are the mail read, "
                           "instead of --mailbox/--folder, each reported by scope id; one "
                           "outside the allowlist is blocked (later_ask mail-scope-grant)")
    live.add_argument("--sharepoint-path", action="append", default=[])
    live.add_argument("--budget-seconds", type=float, default=DEV_BUDGET_SECONDS,
                      help="reads stop after this; the strata they cut say blocked: budget")
    fixture = commands.add_parser("fixture", help="keyless, injected transport")
    fixture.add_argument("--run-id", default=FIXTURE_RUN_ID)
    fixture.add_argument("--write", type=Path, default=None,
                         help="freeze the items and a manifest in this directory")
    heldout = commands.add_parser("heldout", help="hidden held-out set; counts and hashes only")
    heldout.add_argument("--state-dir", type=Path, default=None,
                         help="host state directory outside the repository where the frozen "
                              "set persists (default $PLX_EVAL_HELD_OUT_DIR, "
                              "else $PLX_EVAL_HELDOUT_DIR, else "
                              "$XDG_STATE_HOME/plx-workbench/eval-heldout; a set frozen "
                              "at the legacy location is adopted, not re-sampled)")
    heldout.add_argument("--source", choices=("live", "fixture"), default="live")
    heldout.add_argument("--run-id", default=HELDOUT_RUN_ID)
    heldout.add_argument("--count", type=int, default=HELD_OUT_COUNT)
    heldout.add_argument("--budget-seconds", type=float, default=2400.0)
    heldout.add_argument("--dev-manifest", type=Path, default=DEV_MANIFEST)
    heldout.add_argument("--manifest-out", type=Path, default=None,
                         help="also write the manifest (counts and hashes only) here")
    heldout.add_argument("--no-bc", action="store_true")
    heldout.add_argument("--mailbox")
    heldout.add_argument("--folder")
    heldout.add_argument("--sharepoint-path", action="append", default=[])
    args = parser.parse_args(argv)
    if getattr(args, "mail_scope", None) is not None:
        if args.mailbox or args.folder:
            parser.error("--mail-scope replaces --mailbox and --folder")
        try:
            args.mail_units, args.mail_blocked = mail_scope_target(args.mail_scope)
        except (OSError, ValueError) as exc:
            parser.error(f"--mail-scope: {type(exc).__name__}")
    env = os.environ if environ is None else environ
    try:
        if args.command == "heldout":
            manifest, code = run_heldout(args, env)
            print(json.dumps(manifest, indent=1, sort_keys=True))
            return code
        if args.command == "fixture":
            sources, documents, configured, _ = build_fixture(eval_world())
            report, items = execute(sources, documents, run_id=args.run_id, now=FIXTURE_NOW,
                                    count=30, audit=10, id_suffix="fixture",
                                    evidence_note=FIXTURE_NOTE,
                                    evidence_label="injected-transport", configured=configured)
            if args.write is not None:
                write_dev(items, args.write, run_id=args.run_id, report=report)
        else:
            now = _utc_now()
            run_id = args.run_id if args.run_id != "auto" else "live-" + now[:13]
            if not portal_configured(env):
                raise ValueError("live_requires_the_portal_credentials")
            deadline = Deadline(args.budget_seconds, monotonic)
            sources, documents, configured = (live_build or build_live)(
                env, args, guard=deadline)
            report, items = execute(sources, documents, run_id=run_id, now=now,
                                    count=args.count, audit=args.audit, id_suffix="live",
                                    evidence_note=None, evidence_label="live-read",
                                    configured=configured)
            stamp_budget(report, documents, seconds=args.budget_seconds, hit=deadline.hit)
            args.output_dir.mkdir(parents=True, exist_ok=True)
            for item in items:  # values stay in the verifier's temporary directory
                (args.output_dir / f"{item['id']}.json").write_text(
                    json.dumps(item, indent=1) + "\n", encoding="utf-8")
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1, sort_keys=True))
    return exit_code(report)


def budget_by_stratum(documents: Any) -> dict[str, dict[str, bool]]:
    """Each configured document kind's share of the read time: ``exhausted`` when it ran
    out (``LiveDocuments.budget_state``; a fixture port has no shares, so none)."""
    state = getattr(type(documents), "budget_state", None)   # a pure state read
    return state(documents) if state else {}


def stamp_budget(report: dict[str, Any], documents: Any, *, seconds: float,
                 hit: bool) -> None:
    """A live report's budget: exhausted when the run's deadline hit or any document
    kind's share ran out (decision 2026-10-21-oracle-freeze-budget)."""
    report["budget_by_stratum"] = budget_by_stratum(documents)
    report["budget"] = {"seconds": seconds, "exhausted": hit or any(
        s["exhausted"] for s in report["budget_by_stratum"].values())}


def exit_code(report: Mapping[str, Any]) -> int:
    """0 when every item is valid and deterministic and there is one (or a blocked part:
    a run whose blocked strata left no item is partial, not empty, and the verifier
    records it blocked); 3 otherwise."""
    counts = report["counts"]
    ok = (counts["items"] > 0 or bool(report.get("blocked_upstream"))) \
        and counts["schema_valid"] == counts["items"] \
        and counts["deterministic"] == counts["items"]
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
