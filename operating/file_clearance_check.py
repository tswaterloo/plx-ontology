"""Live check: the security class of every captured SharePoint file, and its clearance.

live check ``sharepoint.restricted_class``. One GET-only capture run of
the configured scope (``integrations.capture_sharepoint``), metadata-only: metadata,
item permissions and eTags as the content capture reads them, no content GET (each file's
content recorded ``skipped_metadata_only``; a full content capture of a ~1,300-file
scope ran past the capture budget on its downloads), then each captured file's
DocumentCategory column read GET-only (below) and its security class resolved as the employee gate resolves it
(``doc_metadata_taxonomy.file_security_class``). Two synthetic employees are resolved
by the real resolver (``operating.employee_permissions``) from synthetic Portal and
Entra reads: an ADMIN of Operations (Portal membership and Entra department; no
clearance, the Portal role never widens it), a STAFF member of Finance and a STAFF
member whose Entra department says Finance but whose Portal membership is not
confirmed (the Entra department only narrows: no clearance). Every file the uncleared reader
may not read (Finance, Business Central, someone's Staff Scratch, unresolved) must be
refused to the uncleared and the unconfirmed reader by
``EmployeeEntitlement.file_class_allows``: ``restricted_visible_to_uncleared`` and
``restricted_visible_to_unconfirmed`` are 0.

DocumentCategory is read per library, not per file
(``SharePointReader.library_item_fields``, behind the reader's drive gate): one GET of
the drive's list, then its list items in pages of 200 with each row's drive item id and
eTag, until every captured file of that library is bound. A row binds only to a captured
file of the same id and the same eTag; values of every other row are dropped unread. The
library-wide read is made only when the grant covers the whole library
(``LiveSourceAudience.covers_whole``); any file it does not bind (the grant is narrower,
the file changed since the capture, the page read was refused, failed or cut) is read on
its own, through its audience and ACL check (``SharePointReader.list_item_fields``: four
GETs a file, the read path that ran out of ``--budget-seconds`` on a ~1,200-file scope).
``listitem_fallbacks`` counts those files by why the library read left them
(``grant_narrower``, ``unbound``, ``page_cap``: the library has more than ``_MAX_PAGES``
pages, ``budget_cut``, or the read's refusal or failure as below), whether or not their
own read then succeeds.

Bounded so it ends with a verdict before a caller's timeout: the capture by
``--capture-budget-seconds`` (a root it cuts is incomplete: ``capture_complete`` false
and the check not ok), the whole check by ``--budget-seconds``. A file whose
DocumentCategory was not read is never classified and the check is not ok:
``listitem_failures`` counts it by cause (``budget_cut``: not read in time;
``NotVisible``; ``SourceUnavailable.<detail>``, the guard's fixed-vocabulary
diagnosis, e.g. ``SourceUnavailable.retried.http_429``) and, per library whose page read
left a file unread, why (``pages.<cause>``). ``listitem_pages_read`` counts the pages.

Prints counts and class names only: never a name, path, id or column value.
Live-unverified until the verifier records a result; the tests run it over an
injected transport (``operating/tests/test_employee_permissions.py``,
``operating/tests/test_file_clearance_library.py``).
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any, Callable, Mapping

from integrations import capture_sharepoint as cs
from integrations.live_source_guard import (LiveSourceAudience, NotVisible, SourceUnavailable,
                                            unavailable_detail)
from ontology.types import Actor

from . import doc_metadata_taxonomy as taxonomy
from . import employee_permissions as ep
from . import employee_permissions_portal as portal

CHECK = "sharepoint.restricted_class"
# Synthetic identities: never a real tenant, user or department membership.
_TENANT_ID = "00000000-0000-4000-8000-00000000c1ea"
_UNCLEARED_OID = "00000000-0000-4000-8000-0000000000a1"
_CLEARED_OID = "00000000-0000-4000-8000-0000000000f1"
_UNCONFIRMED_OID = "00000000-0000-4000-8000-0000000000e1"
_MAX_LISTITEMS = 2000
_COLUMN = "DocumentCategory"
# A library's list items are read in pages of 200, at most this many pages per library.
_MAX_PAGES = 250
# ``SharePointReader.list_item_fields``' own refusals, which the guard's diagnosis
# (``unavailable_detail``) reads as ``unclassified``: a fixed token each.
_LISTITEM_REFUSALS = (
    ("lacks SharePoint list identity", "list_identity_missing"),
    ("list identity invalid", "list_identity_missing"),
    ("listItem identity mismatch", "listitem_identity_mismatch"),
    ("listItem scope mismatch", "listitem_identity_mismatch"),
    ("listItem fields unavailable", "listitem_fields_missing"),
    ("changed during listItem read", "item_changed"),
    ("is not a file", "not_a_file"),
    ("lacks eTag", "record_invalid"),
    ("not finite JSON", "listitem_fields_invalid"),
    ("fields exceed bound", "listitem_fields_invalid"),
)
# The CLI's bounds (seconds): the capture's, and the whole check's from its start.
CAPTURE_BUDGET_SECONDS = 1200
BUDGET_SECONDS = 2100


def synthetic_entitlement(tenant: str, object_id: str, role: str, department: str, *,
                          portal_confirms: bool = True) -> ep.EmployeeEntitlement:
    """An employee resolved by the real resolver from synthetic Portal and Entra reads;
    the Portal confirms the Entra department's membership unless ``portal_confirms``
    is false (then no membership was read)."""
    policy = ep.EmployeePermissionPolicy.from_json({
        "schema": ep.POLICY_SCHEMA, "tenant": tenant, "entra_tenant_id": _TENANT_ID,
        "clearance_by_access_level": {level: "restricted" for level in portal.ACCESS_LEVELS},
        "principals": {}})
    name = f"synthetic-{object_id[-4:]}"
    user = ep.PortalAccessRead(
        user_id=name, email=f"{name}@example.invalid",
        role=role, access_level=portal.LEGACY_ROLE_BOOTSTRAP[role], is_active=True,
        verified_identity=True, entra_tenant_id=_TENANT_ID, entra_object_id=object_id,
        source="synthetic:portal", read_at="synthetic",
        clearance_departments=frozenset(filter(None, [ep.PORTAL_DEPARTMENT_CODES.get(
            department.casefold())])) if portal_confirms else None)
    directory = ep.EntraDirectoryRead("read", object_id, department=department,
                                      source="synthetic:directory", read_at="synthetic")
    resolver = ep.EmployeePermissionResolver(policy, lambda principal: user,
                                             directory=lambda principal: directory)
    return resolver.resolve(ep.EmployeePrincipal(_TENANT_ID, object_id))


def run(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None,
        sender: Any = None, sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """The check. Its metadata-only capture is written to a fresh private directory under
    ``--state-dir`` (``restricted-class-*``, 0700) and removed afterwards: never into a
    store a host or another capture opens (``<state-dir>/sharepoint-capture``), whose
    files it would leave without content."""
    budget = getattr(args, "budget_seconds", None)
    deadline = None if budget is None else monotonic() + budget
    base = Path(args.state_dir).expanduser()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = Path(tempfile.mkdtemp(prefix="restricted-class-", dir=base))
    try:
        return _check(args, state, deadline, environ=environ, sender=sender, sleep=sleep,
                      monotonic=monotonic)
    finally:
        shutil.rmtree(state, ignore_errors=True)


def _check(args: argparse.Namespace, state: Path, deadline: float | None, *,
           environ: Mapping[str, str] | None, sender: Any,
           sleep: Callable[[float], None] | None,
           monotonic: Callable[[], float]) -> dict[str, Any]:
    from integrations.graph_read import SharePointReader, build_graph
    from integrations.live_source_guard import BoundedBackoff

    capture = cs.run_cli(argparse.Namespace(
        state_dir=str(state), scope=args.scope, live_audience=args.live_audience,
        principal=args.principal, withheld_principal="staff:restricted-class-withheld",
        folder_only_principal="staff:restricted-class-folder-only", runs=1,
        max_store_bytes=None, budget_seconds=getattr(args, "capture_budget_seconds", None),
        read_content=False),
        environ=environ, sender=sender, sleep=sleep, monotonic=monotonic)
    audience = LiveSourceAudience.from_file(args.live_audience)
    actor = Actor(audience.tenant, frozenset({args.principal}), "restricted")
    api, transport = build_graph(environ, sender=sender, retry=BoundedBackoff(
        sleep=sleep or time.sleep, budget_seconds=60))
    records, failures, page_failures, pages_read = [], Counter(), Counter(), 0
    fallbacks: Counter = Counter()
    store = cs.SharePointCaptureStore.open_read_only(state)
    try:
        items = store.index(actor, audience, limit=100_000)["items"]
        by_drive: dict[str, list[dict[str, Any]]] = {}
        for item in items[:_MAX_LISTITEMS]:
            view = store.resolve(item["l1_handle"], actor, audience, text=False)
            if view is None:
                failures["capture_unresolved"] += 1
            else:
                by_drive.setdefault(view["row"]["drive_id"], []).append(dict(view["row"]))
    finally:
        store.close()

    def late() -> bool:
        return deadline is not None and monotonic() >= deadline

    for drive, files in by_drive.items():
        reader = SharePointReader(api, audience, drive, transport=transport,
                                  read_permissions=True)
        bound, cause, unread = {}, "grant_narrower", 0
        if audience.covers_whole(actor, reader.resource("/")):
            try:
                read = reader.library_item_fields(
                    actor, {record["id"]: record["etag"] for record in files},
                    field_names=[_COLUMN], max_pages=_MAX_PAGES, late=late)
                bound, cause = read["snapshots"], read["stopped"] or "unbound"
                pages_read += read["pages_read"]
            except (NotVisible, SourceUnavailable, ValueError) as exc:
                bound, cause = getattr(exc, "snapshots", {}), _cause(exc)
                pages_read += getattr(exc, "pages_read", 0)
        for record in files:
            snapshot = bound.get(record["id"])
            if snapshot is None:
                fallbacks[cause] += 1   # why the library read left it to its own read
                if late():
                    failures["budget_cut"] += 1
                    unread += 1
                    continue
                try:
                    snapshot = reader.list_item_fields(actor, record["id"],
                                                       field_names=[_COLUMN])["snapshot"]
                except (NotVisible, SourceUnavailable, ValueError) as exc:
                    failures[_cause(exc)] += 1
                    unread += 1
                    continue
            records.append({**record, "list_item_fields": snapshot})
        if unread and cause != "grant_narrower":
            page_failures[f"pages.{cause}"] += 1   # libraries whose page read left it unread
    failures.update(page_failures)
    classes = Counter(taxonomy.file_security_class(r)[0] for r in records)
    uncleared = synthetic_entitlement(audience.tenant, _UNCLEARED_OID, "ADMIN", "Operations")
    cleared = synthetic_entitlement(audience.tenant, _CLEARED_OID, "STAFF", "Finance")
    unconfirmed = synthetic_entitlement(audience.tenant, _UNCONFIRMED_OID, "STAFF", "Finance",
                                        portal_confirms=False)
    not_open = [r for r in records if taxonomy.file_security_class(r)[0]
                not in ("STANDARD", "CUSTOMER_ORIGINAL")]
    finance = [r for r in records if taxonomy.file_security_class(r)[0] == "FINANCE"]
    report = {
        "check": CHECK,
        "evidence_label": capture["evidence_label"],
        "read_only_guard": capture["read_only_guard"],
        "files_current": capture["summary"]["files_current"],
        # Every root listed to the end (metadata-complete; the content is skipped).
        "capture_complete": bool(capture["summary"]["roots"]) and
        capture["summary"]["roots_metadata_complete"] == capture["summary"]["roots"],
        "files_classified": len(records),
        "files_over_bound": max(0, len(items) - _MAX_LISTITEMS),
        "listitems_read": len(records),
        "listitem_pages_read": pages_read,
        "listitem_failures": dict(sorted(failures.items())),
        "listitem_fallbacks": dict(sorted(fallbacks.items())),
        "class_counts": {cls: classes.get(cls, 0)
                         for cls in (*taxonomy.SECURITY_CLASSES, taxonomy.UNRESOLVED_CLASS)},
        "restricted_total": len(not_open),
        "restricted_visible_to_uncleared": sum(
            1 for r in not_open if uncleared.file_class_allows(r)),
        "restricted_visible_to_unconfirmed": sum(
            1 for r in not_open if unconfirmed.file_class_allows(r)),
        "finance_visible_to_cleared": sum(1 for r in finance if cleared.file_class_allows(r)),
        "requests_sent": capture["requests_sent"] + transport.requests_sent,
    }
    report["ok"] = bool(
        records and not failures and not report["files_over_bound"]
        and report["capture_complete"]
        and None not in (uncleared.actor, cleared.actor, unconfirmed.actor)
        and report["restricted_visible_to_uncleared"] == 0
        and report["restricted_visible_to_unconfirmed"] == 0
        and report["finance_visible_to_cleared"] == len(finance))
    return report


def _cause(exc: BaseException) -> str:
    """A fixed token for why one file's DocumentCategory was not read; never source text."""
    if not isinstance(exc, SourceUnavailable):
        return type(exc).__name__
    detail = unavailable_detail(exc)
    if detail == "unclassified":
        detail = next((token for text, token in _LISTITEM_REFUSALS if text in str(exc)), detail)
    return f"SourceUnavailable.{detail}"


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.file_clearance_check",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True,
                        help="a directory outside every git work tree; the check captures "
                        "into a fresh private subdirectory of it and removes it")
    parser.add_argument("--scope", default=str(cs.DEFAULT_SCOPE),
                        help="plx-sharepoint-capture-scope/v1 file")
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting the capture principal")
    parser.add_argument("--principal", default=cs.CAPTURE_PRINCIPAL)
    parser.add_argument("--capture-budget-seconds", type=float,
                        default=CAPTURE_BUDGET_SECONDS,
                        help="stop the capture walk after this long (roots left incomplete)")
    parser.add_argument("--budget-seconds", type=float, default=BUDGET_SECONDS,
                        help="stop reading columns this long after the start (counted)")
    args = parser.parse_args(argv)
    report = run(args, environ=environ)
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
