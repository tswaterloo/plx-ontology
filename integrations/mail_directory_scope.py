"""Directory-wide mail capture scope: every user mailbox but the excluded people's
and the HR / payroll deny list (owner directive c75dfaba0f8e).

The scope (``plx-mail-directory-scope/v1``, ``integrations/mail_all_mailboxes_scope.json``)
names the folders (inbox and sent items), the first-round window and an explicit deny
list (``deny``: address tokens and departments, with the reason). Nothing in it names
a person.

``enumerate_mailboxes`` decides the mailboxes, in this order, before any request that
names a mailbox:

1. the people-exclusion guard (``integrations.people_exclusion``) must hold its ID list
   (``PLX_EXCLUDED_PEOPLE_OIDS`` / ``PLX_EXCLUDED_PEOPLE_FILE``): unset or empty, the
   run refuses with no request at all (``excluded_people_oids_absent``);
2. the guard binds (one ``GET /users`` listing that names nobody) so it knows the
   excluded people's addresses too;
3. one paged ``GET /users`` (``id,mail,userPrincipalName,proxyAddresses,department,
   userType``; the same listing grammar, GET only) lists the tenant's member users with
   a mail address; guests are skipped;
4. every user any of whose identities (object id, UPN, mail, SMTP proxy address) is an
   excluded person's is dropped, counted and nothing else kept of it;
5. every user the deny list matches is dropped, counted: a local part (or one of its
   ``.``/``_``/``-``/``+`` separated tokens, or the local part without separators) in
   ``address_tokens``, or a department containing one of ``departments`` as whole
   words (case and ``&``/``and`` folded). A department that is not a string is denied
   (unknown shape, fail closed).

Why not ``integrations.graph_directory.GraphDirectoryReader``: it reads one named user
(plus a transitive-groups request each) and refuses disabled accounts, which is what a
shared mailbox is; there is no listing function in it. This module calls the same GET
client and the same ``graph.users`` grammar operation.

The kept object ids feed ``capture_mail.MailCapture.run`` as its ``all_mailboxes``
directory, so the existing resumable capture runs unchanged: per-folder delta paging,
bodies, sha256-pinned attachments, the store under ``PLX_WORKBENCH_STATE_DIR``, every
message version carrying the derived ``mailbox_owner`` ACL of its own mailbox
(``source_acl``), and the reader's own exclusion re-check before every mailbox lookup.
``--runs`` capture runs share ``--budget-seconds`` (each run gets an even share of what
is left), so a folder the budget cut resumes from its stored cursor in the next run or
on the next day.

CLI (credentialed live check ``graph.mail_all_mailboxes``; counts, booleans and enums
only, never names, addresses, subjects or ids)::

    python3 -m integrations.graph_read mail-all-mailboxes \\
        --live-audience examples/mail_all_mailboxes_audience.live-check.json --runs 2

A person excluded, or a mailbox denied, after it was captured: every store read hides it
at once and the next run purges it (``MailCaptureStore.purge_withheld``, decision
2026-10-21-mail-exclusion-retroactive); the run records each listed mailbox's department
first, so the deny list decides on it at read. ``exclusion_check`` counts what the store
still holds of the mailboxes this listing excluded or denied, and how much of it the
capture actor can still read (both readable counts must be 0).

``excluded_requested`` is measured on the transport's request log (``RequestLog``: a
``GuardedTransport`` observer that sees every request before it is sent, keeps only the
identity tokens each URL names, in memory, and is read once the run ends), not on the
result set. Live-unverified until ``devloop/live-checks/graph.mail_all_mailboxes.json``
passes.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote, urlsplit

from ontology.types import Actor

from . import live_source_grammar as grammar
from .people_exclusion import ExclusionUnavailable, PeopleExclusion
from .live_source_guard import LiveSourceAudience, SourceUnavailable

SCHEMA = "plx-mail-directory-scope/v1"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCOPE = REPO_ROOT / "integrations" / "mail_all_mailboxes_scope.json"
CHECK = "graph.mail_all_mailboxes"
CHECK_STATE = "mail-all-mailboxes-check"
CAPTURE_PRINCIPAL = "svc:mail-capture"
OWNER_PROBE = "staff:mailbox-owner-probe"
LISTING_SELECT = "id,mail,userPrincipalName,proxyAddresses,department,userType"
LISTING_PART = "mail.directory_listing"
LISTING_PERMISSION = {"graph.users": "User.Read.All"}
_TOKEN = re.compile(r"^[a-z0-9]{2,64}$")


class DirectoryScopeError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _words(text: str) -> str:
    """Case-folded words: ``&`` reads ``and``, anything else not alphanumeric splits."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.casefold().replace("&", " and ")).split())


def _addresses(user: Mapping[str, Any]) -> set[str]:
    found = set()
    for key in ("mail", "userPrincipalName"):
        value = user.get(key)
        if isinstance(value, str) and value:
            found.add(value.lower())
    for proxy in user.get("proxyAddresses") or ():
        if isinstance(proxy, str) and proxy.lower().startswith("smtp:"):
            found.add(proxy[5:].lower())
    return found


class DenyList:
    """The explicit HR / payroll deny list: no mailbox it matches is requested."""

    def __init__(self, address_tokens: Iterable[str], departments: Iterable[str],
                 reason: str) -> None:
        tokens, names = list(address_tokens), list(departments)
        if (not tokens or any(not isinstance(t, str) or _TOKEN.fullmatch(t) is None
                              for t in tokens)):
            raise DirectoryScopeError("deny_address_tokens_invalid")
        if not names or any(not isinstance(d, str) or not _words(d) for d in names):
            raise DirectoryScopeError("deny_departments_invalid")
        if not isinstance(reason, str) or not reason.strip():
            raise DirectoryScopeError("deny_reason_missing")
        self.address_tokens = frozenset(tokens)
        self.departments = frozenset(_words(d) for d in names)
        self.reason = reason

    def denies(self, user: Mapping[str, Any]) -> bool:
        for address in _addresses(user):
            local = address.split("@", 1)[0]
            parts = {local, re.sub(r"[._+-]+", "", local), *re.split(r"[._+-]+", local)}
            if parts & self.address_tokens:
                return True
        department = user.get("department")
        if department is None:
            return False
        if not isinstance(department, str):
            return True            # an unknown shape is denied, never guessed
        padded = f" {_words(department)} "
        return any(f" {name} " in padded for name in self.departments)


class DirectoryScope:
    """``plx-mail-directory-scope/v1``: folders, window and the deny list."""

    KEYS = {"schema", "note", "window_days", "folders", "internal_domains", "deny"}
    DENY_KEYS = {"reason", "address_tokens", "departments", "note"}

    def __init__(self, *, folders: Iterable[str], window_days: int, deny: DenyList,
                 internal_domains: Iterable[str] | None = None) -> None:
        from .capture_mail import CaptureScope

        if not isinstance(deny, DenyList):
            raise DirectoryScopeError("deny_list_required")
        self.deny = deny
        # CaptureScope validates the folders, window and domains (CaptureError).
        self._capture = CaptureScope([], window_days=window_days,
                                     internal_domains=internal_domains, all_mailboxes=True,
                                     all_mailbox_folders=list(folders))
        self.folders = self._capture.all_mailbox_folders
        self.window_days = self._capture.window_days

    def capture_scope(self) -> Any:
        """The ``capture_mail.CaptureScope`` the capture runs with (every listed id)."""
        return self._capture

    @classmethod
    def from_json(cls, document: Any) -> "DirectoryScope":
        if (not isinstance(document, dict) or document.get("schema") != SCHEMA
                or set(document) - cls.KEYS or "deny" not in document
                or "folders" not in document):
            raise DirectoryScopeError("scope_document_invalid")
        deny = document["deny"]
        if not isinstance(deny, dict) or set(deny) - cls.DENY_KEYS:
            raise DirectoryScopeError("deny_list_invalid")
        return cls(folders=document["folders"], window_days=document.get("window_days", 90),
                   internal_domains=document.get("internal_domains"),
                   deny=DenyList(deny.get("address_tokens") or [],
                                 deny.get("departments") or [], deny.get("reason")))

    @classmethod
    def from_file(cls, path: str | Path) -> "DirectoryScope":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise DirectoryScopeError("scope_unreadable") from None
        return cls.from_json(document)


@dataclass(frozen=True)
class Enumeration:
    """The mailboxes a directory-wide run captures (object ids) and counts only."""

    mailboxes: tuple[str, ...]
    status: str            # complete | truncated:<reason>
    listed: int            # member users with a mail address
    excluded: int          # dropped for the people exclusion
    denied: int            # dropped by the deny list
    # In memory only, never reported: the excluded and denied object ids (what the store
    # must no longer serve) and each listed, not excluded user's department.
    withheld: frozenset[str] = field(default=frozenset(), repr=False, compare=False)
    departments: Mapping[str, Any] = field(default_factory=dict, repr=False,
                                           compare=False, hash=False)

    def directory(self) -> tuple[list[str], str, int]:
        """``MailCapture.run``'s ``directory`` callable: the same answer every run."""
        return list(self.mailboxes), self.status, self.excluded + self.denied


def enumerate_mailboxes(api: Any, scope: DirectoryScope,
                        people: PeopleExclusion) -> Enumeration:
    """The tenant's user mailboxes less the excluded people's and the deny list's.

    ``ExclusionUnavailable`` before any request when the ID list is unset, empty or
    invalid; after the guard's own listing when that listing failed (its cause on the
    chain). ``SourceUnavailable`` when this listing failed. No excluded identity is
    returned, kept or counted beyond the total."""
    people.require()                      # no I/O: refuse before anything is requested
    people.bind(api)                      # the listing that names nobody
    url = grammar.url("graph.users", {"$select": LISTING_SELECT, "$top": "999"})
    users, completeness = api.get_pages(url, max_pages=50, max_items=5000)
    kept: set[str] = set()
    withheld: set[str] = set()
    departments: dict[str, Any] = {}
    listed = excluded = denied = 0
    for user in users:
        oid = str(user.get("id") or "").lower()
        if (not grammar.TYPES["user_id"].valid(oid) or not isinstance(user.get("mail"), str)
                or not user["mail"] or user.get("userType") == "Guest"):
            continue
        listed += 1
        if people.excluded([oid, *_addresses(user)]):
            excluded += 1
            withheld.add(oid)
            continue
        departments[oid] = user.get("department")
        if scope.deny.denies(user):
            denied += 1
            withheld.add(oid)
            continue
        kept.add(oid)
    status = ("complete" if completeness.get("complete")
              else f"truncated:{completeness.get('truncation_reason')}")
    return Enumeration(tuple(sorted(kept)), status, listed, excluded, denied,
                       frozenset(withheld), departments)


class RequestLog:
    """``GuardedTransport`` observer: the identity tokens (object ids, addresses, the
    ``/users/<x>`` segment) every request URL names, counted per token set. In memory
    only; ``excluded_requests`` is its one output, a count."""

    _GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    _ADDRESS = re.compile(r"[a-z0-9._%+-][a-z0-9._%+'-]*@[a-z0-9-]+(?:\.[a-z0-9-]+)+")
    _USER = re.compile(r"/users(?:/|\(')([^/?'()]+)")

    def __init__(self) -> None:
        self.requests = 0
        self._named: Counter[frozenset[str]] = Counter()

    def __call__(self, method: str, url: str) -> None:
        self.requests += 1
        text = unquote(str(url)).lower()
        tokens = set(self._GUID.findall(text)) | set(self._ADDRESS.findall(text))
        tokens |= set(self._USER.findall(urlsplit(text).path))
        self._named[frozenset(tokens)] += 1

    def excluded_requests(self, people: PeopleExclusion) -> int | None:
        """Requests whose URL named an excluded person (object id, or an address once
        the guard is bound); None when the guard cannot decide (unusable)."""
        if people.unavailable or not people.bound:
            return None
        return sum(n for tokens, n in self._named.items() if people.excluded(tokens))


def _people_part(people: PeopleExclusion, exc: ExclusionUnavailable) -> dict[str, Any]:
    """The guard's refusal: a refused listing (401/403) is the source's, else the
    guard's own code (``excluded_people_oids_absent`` ...)."""
    from .capture_mail import graph_block

    cause = exc.__cause__
    if isinstance(cause, SourceUnavailable):
        part = graph_block(LISTING_PART, "graph.users", LISTING_PERMISSION,
                           getattr(cause, "http_status", None))
        if part is not None:
            return part
    return people.block_part("mail.people_exclusion")


def add_cli_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scope", default=str(DEFAULT_SCOPE),
                        help="plx-mail-directory-scope/v1 file")
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting the capture "
                             "principal every mailbox (mail:*)")
    parser.add_argument("--state-dir", default=None,
                        help="Workbench state directory (outside every git work tree); "
                             "default $PLX_WORKBENCH_STATE_DIR/" + CHECK_STATE)
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="wall time of all runs together; each run gets an even share "
                             "of what is left, a cut folder resumes from its stored cursor")
    parser.add_argument("--principal", default=CAPTURE_PRINCIPAL)
    parser.add_argument("--withheld-principal", default="staff:mail-capture-withheld")
    parser.add_argument("--runs", type=int, default=2,
                        help="capture runs; run 2+ resumes from the stored cursors")
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--max-store-bytes", type=int, default=None)


def _visible_rows(store: Any, actor: Actor, audience: LiveSourceAudience) -> dict[str, Any]:
    """Rows, counts, mailboxes and offline resolutions one reader gets (counts only)."""
    view = store.messages(actor, audience, limit=1000)
    counts = store.counts(actor, audience)
    return {"rows": len(view["items"]), "total": view["total"], "counts": counts,
            "mailboxes": len(store.completeness(actor, audience)["mailboxes"]),
            "handles": [item["l1_handle"] for item in view["items"]]}


# The share of ``--budget-seconds`` kept back from capture for the read-back probes
# (resolves, the withheld, owner and exclusion reads): the whole command ends within
# the budget, so the verifier's timeout never kills it (gap-86a8a6debd2e).
PROBE_RESERVE_SHARE = 0.15


class _ProbesCut(Exception):
    """The budget ran out during the read-back probes."""


def _probes(store: Any, actor: Actor, withheld: Actor, audience: LiveSourceAudience,
            enumeration: Any, spent: Callable[[], bool]) -> dict[str, Any]:
    """The store read back as the capture actor, a withheld actor, one mailbox's owner
    and over the excluded mailboxes; ``spent()`` is checked before every store read.
    Cut by it: ``cut`` true and every probe not finished null (never zero: a null
    leak count is not a clean one)."""
    def check() -> None:
        if spent():
            raise _ProbesCut

    out: dict[str, Any] = {
        "cut": False, "boxes": None, "probe_box": None,
        "mine_counts": {"messages_current": None, "versions": None,
                        "attachments_captured": None},
        "resolve_check": {"handles_checked": None, "resolved_offline": None},
        "leak_check": {"withheld_actor_mailboxes": None, "withheld_actor_rows": None,
                       "withheld_actor_counts": None, "withheld_actor_resolved": None},
        "marking": {"with": None, "without": None},
        "probe": {"mailboxes_visible": None, "rows_visible": None,
                  "only_own_mailbox": None, "rows_match_own_mailbox": None},
        "exclusion": {"mailboxes_stored": None, "items_stored": None,
                      "items_readable": None, "mailboxes_readable": None}}
    try:
        check()
        mine = _visible_rows(store, actor, audience)
        out["mine_counts"] = {k: mine["counts"][k] for k in out["mine_counts"]}
        resolved = 0
        for handle in mine["handles"]:
            check()
            resolved += store.resolve(handle, actor, audience) is not None
        out["resolve_check"] = {"handles_checked": len(mine["handles"]),
                                "resolved_offline": resolved}
        check()
        hidden = _visible_rows(store, withheld, audience)
        hidden_resolved = 0
        for handle in mine["handles"]:
            check()
            hidden_resolved += store.resolve(handle, withheld, audience) is not None
        out["leak_check"] = {"withheld_actor_mailboxes": hidden["mailboxes"],
                             "withheld_actor_rows": hidden["rows"],
                             "withheld_actor_counts": hidden["total"]
                             + sum(hidden["counts"].values()),
                             "withheld_actor_resolved": hidden_resolved}
        check()
        boxes = store.completeness(actor, audience)["mailboxes"]
        out["boxes"] = boxes
        check()
        out["marking"] = store.owner_marking(actor, audience)
        # One mailbox's owner (its object id as their identity, source ACL required):
        # they see that mailbox and nothing of any other.
        probe_box = next((m["mailbox"] for m in boxes if m["current_messages"]),
                         boxes[0]["mailbox"] if boxes else None)
        out["probe_box"] = probe_box
        probe: dict[str, Any] = {"mailboxes_visible": 0, "rows_visible": 0,
                                 "only_own_mailbox": None}
        if probe_box is not None:
            owner_audience = LiveSourceAudience(
                {"mail:*": [OWNER_PROBE]}, tenant=audience.tenant,
                classifications={"mail:*": "restricted"},
                identities={OWNER_PROBE: [probe_box]}, source_acl_required=["mail:*"])
            owner = Actor(audience.tenant, frozenset({OWNER_PROBE}), "restricted")
            check()
            seen = store.completeness(owner, owner_audience)["mailboxes"]
            own_rows = next((m["current_messages"] for m in boxes
                             if m["mailbox"] == probe_box), 0)
            check()
            probe = {"mailboxes_visible": len(seen),
                     "rows_visible": store.counts(owner, owner_audience)["messages_current"],
                     "only_own_mailbox": [m["mailbox"] for m in seen] == [probe_box]}
            probe["rows_match_own_mailbox"] = probe["rows_visible"] == own_rows
        out["probe"] = probe
        # What the store still holds of the mailboxes this listing excluded or denied,
        # and what of it the capture actor can read (decision
        # 2026-10-21-mail-exclusion-retroactive).
        check()
        held = store.held(sorted(enumeration.withheld) if enumeration else [])
        withheld_boxes = enumeration.withheld if enumeration else frozenset()
        readable = 0
        for handle in held["handles"]:
            check()
            readable += store.resolve(handle, actor, audience) is not None
        out["exclusion"] = {
            "mailboxes_stored": held["mailboxes"], "items_stored": held["items"],
            "items_readable": readable,
            "mailboxes_readable": sum(1 for m in boxes if m["mailbox"] in withheld_boxes)}
    except _ProbesCut:
        out["cut"] = True
    return out


def run_cli(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None,
            sender: Any = None, sleep: Callable[[float], None] = time.sleep,
            monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Enumerate once, capture ``--runs`` times, read the store back as the capture
    actor, a withheld actor and one mailbox's owner. Counts, booleans and enums only."""
    from . import capture_mail as cm
    from .graph_read import GraphMailReader, build_graph
    from .live_source_guard import BoundedBackoff
    from .people_exclusion import from_api

    if isinstance(args.runs, bool) or not isinstance(args.runs, int) or not 1 <= args.runs <= 5:
        raise ValueError("runs must be 1..5")
    budget = args.budget_seconds
    if budget is not None and not budget >= 0:
        raise ValueError("budget_seconds must be >= 0")
    scope = DirectoryScope.from_file(args.scope)
    audience = LiveSourceAudience.from_file(args.live_audience)
    env = os.environ if environ is None else environ
    state_base = None
    if args.state_dir is None:
        state_dir, base, state_base = cm.check_state_dir(env, CHECK_STATE,
                                                         injected=sender is not None)
        cm.assert_outside_git(state_dir)
        for n in range(1, len(state_dir.relative_to(base).parts) + 1):
            base.joinpath(*state_dir.relative_to(base).parts[:n]).mkdir(
                mode=0o700, parents=True, exist_ok=True)
    else:
        state_dir = cm.assert_outside_git(args.state_dir)
    # The whole command within the budget (the verifier kills it at its timeout): the
    # enumeration and every run inside the capture share, the read-back probes inside
    # the reserve the capture leaves them.
    deadline = None if budget is None else monotonic() + budget
    capture_deadline = None if deadline is None else deadline - budget * PROBE_RESERVE_SHARE
    carried = cm.store_exists(state_dir)
    store = cm.MailCaptureStore(state_dir, max_store_bytes=args.max_store_bytes,
                                deny=scope.deny)
    try:
        api, transport = build_graph(environ, sender=sender,
                                     retry=BoundedBackoff(sleep=sleep, budget_seconds=60))
        log = RequestLog()
        transport.observers.append(log)
        people = from_api(api)
        store.people = people         # every read below applies this run's exclusion
        reader = GraphMailReader(api, audience, people=people)
        actor = Actor(audience.tenant, frozenset({args.principal}), "restricted")
        withheld = Actor(audience.tenant, frozenset({args.withheld_principal}), "restricted")
        blocked: list[dict[str, Any]] = []
        enumeration: Enumeration | None = None
        try:
            enumeration = enumerate_mailboxes(api, scope, people)
        except ExclusionUnavailable as exc:
            blocked = [_people_part(people, exc)]
        except SourceUnavailable as exc:
            part = cm.graph_block(LISTING_PART, "graph.users", LISTING_PERMISSION,
                                  getattr(exc, "http_status", None))
            if part is None:
                raise
            blocked = [part]
        runs: list[dict[str, Any]] = []
        last_run: Mapping[str, Any] | None = None
        if enumeration is not None:
            store.record_directory(enumeration.departments)
            refused = []
            for number in range(1, args.runs + 1):
                share = (None if capture_deadline is None else
                         (capture_deadline - monotonic()) / (args.runs - number + 1))
                if share is not None and share <= 0:
                    break    # nothing left of the capture share: no run opens anything
                capture = cm.MailCapture(store, reader, actor, page_size=args.page_size,
                                         sleep=sleep, budget_seconds=share,
                                         monotonic=monotonic)
                run = capture.run(scope.capture_scope(), directory=enumeration.directory)
                runs.append(cm._report_run(number, run))
                refused.append(cm.refused_parts(run))
                last_run = run
            if refused and all(refused):
                blocked = refused[-1]
        probes = _probes(store, actor, withheld, audience, enumeration,
                         lambda: deadline is not None and monotonic() >= deadline)
        excluded_requested = log.excluded_requests(people)
        boxes = probes["boxes"]
        folders = [f for m in boxes or [] for f in m["folders"]]
        mine = probes["mine_counts"]
        resumes = sum(1 for r in runs[1:] for m in r["mailboxes"] for f in m["folders"]
                      if f["resumed_from"] in ("next_url", "delta_url"))
        report: dict[str, Any] = {
            "check": CHECK,
            "evidence_label": ("injected-transport" if transport.sender_injected
                               else "live-read"),
            "read_only_guard": "GET-only; token POST only",
            "storage": {"outside_git_tree": True,
                        "under_state_dir": Path(store.root).parent == state_dir.resolve(),
                        "host_state": args.state_dir is None, "carried": carried,
                        "state_base": state_base},
            "directory": {
                "status": enumeration.status if enumeration else None,
                "mailboxes_listed": enumeration.listed if enumeration else 0,
                "mailboxes_excluded": enumeration.excluded if enumeration else 0,
                "mailboxes_denied": enumeration.denied if enumeration else 0,
                "mailboxes_enumerated": len(enumeration.mailboxes) if enumeration else 0,
                "folders_per_mailbox": len(scope.folders)},
            "request_log": {"requests_logged": log.requests,
                            "complete": log.requests == transport.requests_sent,
                            "excluded_requested": excluded_requested},
            "runs": runs,
            # Runs the budget left no capture share for (nothing opened or read).
            "runs_skipped": args.runs - len(runs) if enumeration is not None else 0,
            "blocked_upstream": blocked,
            "summary": {"mailboxes": None if boxes is None else len(boxes),
                        "mailboxes_complete": None if boxes is None
                        else sum(1 for m in boxes if m["complete"]),
                        "folders": None if boxes is None else len(folders),
                        "folders_complete": None if boxes is None
                        else sum(1 for f in folders if f["complete"]),
                        "messages_current": mine["messages_current"],
                        "versions": mine["versions"],
                        "attachments_captured": mine["attachments_captured"],
                        "resumed_from_stored_cursor": resumes,
                        "complete_bound": runs[-1]["complete_bound"] if runs else 0,
                        "bound_total": runs[-1]["bound_total"] if runs else 0,
                        "store_bytes": store.store_bytes(),
                        "store_full": store.store_full(),
                        "purged": {key: sum(r["purged"][key] for r in runs)
                                   for key in cm.PURGE_COUNTS}},
            "exclusion_check": probes["exclusion"],
            "owner_marking": probes["marking"],
            "owner_probe": probes["probe"],
            "resolve_check": probes["resolve_check"],
            "leak_check": probes["leak_check"],
            # The read-back the budget cut (probes_cut): every probe it did not finish
            # is null, so the check fails closed, never passes or blocks on it.
            "probes_cut": probes["cut"],
            "relations": {name: n for name, n in (
                ("messages", mine["messages_current"]),
                ("attachments", mine["attachments_captured"])) if n},
            "requests_sent": transport.requests_sent,
            "operations_read": sorted(transport.operations_read),
        }
        leak, exclusion = report["leak_check"], probes["exclusion"]
        probe, resolve = probes["probe"], probes["resolve_check"]
        rest_ok = bool(
            not probes["cut"]
            and enumeration is not None and enumeration.status == "complete"
            and enumeration.mailboxes and excluded_requested == 0
            and report["request_log"]["complete"] and probes["marking"]["without"] == 0
            and resolve["resolved_offline"] == resolve["handles_checked"]
            and not any(v != 0 for v in leak.values())
            and exclusion["items_readable"] == 0 and exclusion["mailboxes_readable"] == 0
            and (probes["probe_box"] is None or (probe["only_own_mailbox"]
                                                 and probe["rows_match_own_mailbox"])))
        if not blocked and rest_ok:
            # A last run the budget alone cut over a store that was not carried (or no
            # run left a share): later_ask live-host-persistent-state-dir, the one rule
            # of capture_mail.budget_cut_block; never an untyped fail.
            units = (cm.mail_cut_units(last_run) if last_run is not None
                     else [("mail.run", [cm.TIME_BUDGET])])
            blocked = cm.budget_cut_block(units, carried=carried)
            report["blocked_upstream"] = blocked
        report["ok"] = bool(not blocked and rest_ok)
        return report
    finally:
        store.close()


__all__ = ["CHECK", "DEFAULT_SCOPE", "DenyList", "DirectoryScope", "DirectoryScopeError",
           "Enumeration", "RequestLog", "SCHEMA", "add_cli_arguments", "enumerate_mailboxes",
           "run_cli"]
