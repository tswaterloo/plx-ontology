"""Directory-wide mail capture scope: every user mailbox but the excluded people's
and the HR / payroll deny list.

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

``excluded_requested`` is measured on the transport's request log (``RequestLog``: a
``GuardedTransport`` observer that sees every request before it is sent, keeps only the
identity tokens each URL names, in memory, and is read once the run ends), not on the
result set. Live-unverified until ``verify/live-checks/graph.mail_all_mailboxes.json``
passes.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
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
    listed = excluded = denied = 0
    for user in users:
        oid = str(user.get("id") or "").lower()
        if (not grammar.TYPES["user_id"].valid(oid) or not isinstance(user.get("mail"), str)
                or not user["mail"] or user.get("userType") == "Guest"):
            continue
        listed += 1
        if people.excluded([oid, *_addresses(user)]):
            excluded += 1
            continue
        if scope.deny.denies(user):
            denied += 1
            continue
        kept.add(oid)
    status = ("complete" if completeness.get("complete")
              else f"truncated:{completeness.get('truncation_reason')}")
    return Enumeration(tuple(sorted(kept)), status, listed, excluded, denied)


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
    carried = cm.store_exists(state_dir)
    store = cm.MailCaptureStore(state_dir, max_store_bytes=args.max_store_bytes)
    try:
        api, transport = build_graph(environ, sender=sender,
                                     retry=BoundedBackoff(sleep=sleep, budget_seconds=60))
        log = RequestLog()
        transport.observers.append(log)
        people = from_api(api)
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
        if enumeration is not None:
            deadline = None if budget is None else monotonic() + budget
            refused = []
            for number in range(1, args.runs + 1):
                share = (None if deadline is None else
                         max(0.0, (deadline - monotonic()) / (args.runs - number + 1)))
                capture = cm.MailCapture(store, reader, actor, page_size=args.page_size,
                                         sleep=sleep, budget_seconds=share,
                                         monotonic=monotonic)
                run = capture.run(scope.capture_scope(), directory=enumeration.directory)
                runs.append(cm._report_run(number, run))
                refused.append(cm.refused_parts(run))
            if all(refused):
                blocked = refused[-1]
        mine = _visible_rows(store, actor, audience)
        resolved = sum(1 for h in mine["handles"]
                       if store.resolve(h, actor, audience) is not None)
        hidden = _visible_rows(store, withheld, audience)
        hidden_resolved = sum(1 for h in mine["handles"]
                              if store.resolve(h, withheld, audience) is not None)
        completeness = store.completeness(actor, audience)
        marking = store.owner_marking(actor, audience)
        # One mailbox's owner (its object id as their identity, source ACL required):
        # they see that mailbox and nothing of any other.
        boxes = completeness["mailboxes"]
        probe_box = next((m["mailbox"] for m in boxes if m["current_messages"]),
                         boxes[0]["mailbox"] if boxes else None)
        probe = {"mailboxes_visible": 0, "rows_visible": 0, "only_own_mailbox": None}
        if probe_box is not None:
            owner_audience = LiveSourceAudience(
                {"mail:*": [OWNER_PROBE]}, tenant=audience.tenant,
                classifications={"mail:*": "restricted"},
                identities={OWNER_PROBE: [probe_box]}, source_acl_required=["mail:*"])
            owner = Actor(audience.tenant, frozenset({OWNER_PROBE}), "restricted")
            seen = store.completeness(owner, owner_audience)["mailboxes"]
            own_rows = next((m["current_messages"] for m in boxes
                             if m["mailbox"] == probe_box), 0)
            probe = {"mailboxes_visible": len(seen),
                     "rows_visible": store.counts(owner, owner_audience)["messages_current"],
                     "only_own_mailbox": [m["mailbox"] for m in seen] == [probe_box]}
            probe["rows_match_own_mailbox"] = probe["rows_visible"] == own_rows
        excluded_requested = log.excluded_requests(people)
        folders = [f for m in boxes for f in m["folders"]]
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
            "blocked_upstream": blocked,
            "summary": {"mailboxes": len(boxes),
                        "mailboxes_complete": sum(1 for m in boxes if m["complete"]),
                        "folders": len(folders),
                        "folders_complete": sum(1 for f in folders if f["complete"]),
                        "messages_current": mine["counts"]["messages_current"],
                        "versions": mine["counts"]["versions"],
                        "attachments_captured": mine["counts"]["attachments_captured"],
                        "resumed_from_stored_cursor": resumes,
                        "complete_bound": runs[-1]["complete_bound"] if runs else 0,
                        "bound_total": runs[-1]["bound_total"] if runs else 0,
                        "store_bytes": store.store_bytes(),
                        "store_full": store.store_full()},
            "owner_marking": marking,
            "owner_probe": probe,
            "resolve_check": {"handles_checked": len(mine["handles"]),
                              "resolved_offline": resolved},
            "leak_check": {"withheld_actor_mailboxes": hidden["mailboxes"],
                           "withheld_actor_rows": hidden["rows"],
                           "withheld_actor_counts": hidden["total"]
                           + sum(hidden["counts"].values()),
                           "withheld_actor_resolved": hidden_resolved},
            "relations": {name: n for name, n in (
                ("messages", mine["counts"]["messages_current"]),
                ("attachments", mine["counts"]["attachments_captured"])) if n},
            "requests_sent": transport.requests_sent,
            "operations_read": sorted(transport.operations_read),
        }
        leak = report["leak_check"]
        report["ok"] = bool(
            not blocked and enumeration is not None and enumeration.status == "complete"
            and enumeration.mailboxes and excluded_requested == 0
            and report["request_log"]["complete"] and marking["without"] == 0
            and resolved == len(mine["handles"])
            and not any(leak.values())
            and (probe_box is None or (probe["only_own_mailbox"]
                                       and probe["rows_match_own_mailbox"])))
        return report
    finally:
        store.close()


__all__ = ["CHECK", "DEFAULT_SCOPE", "DenyList", "DirectoryScope", "DirectoryScopeError",
           "Enumeration", "RequestLog", "SCHEMA", "add_cli_arguments", "enumerate_mailboxes",
           "run_cli"]
