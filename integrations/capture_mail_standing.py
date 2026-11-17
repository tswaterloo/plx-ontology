"""Mail capture unit standing: the scheduling bands of a folder's listing, the reasons a
unit is cut, blocked, refused or hidden, and how a run reports them (split from
``integrations.capture_mail``, which re-exports every name)."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Sequence

from .graph_read import MailboxAbsent
from .live_source_guard import SourceUnavailable, unavailable_detail
from .plx_hub_guard import GRAPH_BLOCKS, upstream_blocks
from .capture_mail_scope import CaptureError


# The capture scheduler's terminal reasons: a unit refused by the source (HTTP 403, e.g.
# an Exchange application access policy) or not visible to the capture actor stays
# incomplete with that reason until a later run reads it; it is re-probed after every
# other unit, never treated as work the budget left.
# ``no_mailbox``: Graph reports no Exchange Online mailbox for the directory user (404
# ``MailboxNotEnabledForRESTAPI``, ``graph_read.MailboxAbsent``: unlicensed, inactive,
# deleted or on-premises): nothing to read through Graph, re-checked every run; never
# reported as mail the capture failed to read.
NO_MAILBOX = "no_mailbox"
TERMINAL_REASONS = frozenset({"forbidden", "not_visible", NO_MAILBOX, "source_error:http_403"})


def hidden_reason(exc: BaseException) -> str:
    """The recorded reason of a mailbox or folder a read answered ``NotVisible`` for:
    ``no_mailbox`` when the user has no mailbox at all, else ``not_visible``."""
    return NO_MAILBOX if isinstance(exc, MailboxAbsent) else "not_visible"


def terminal_reason(reason: str | None) -> bool:
    """Whether a unit's (comma-joined) reason is terminal: every part of it is."""
    parts = set(filter(None, str(reason or "").split(",")))
    return bool(parts) and parts <= TERMINAL_REASONS


# A unit's standing for the scheduler (schedule_units), in the order a run takes them.
FRONTIER, PENDING, COMPLETE, TERMINAL = range(4)


def schedule_units(units: Sequence[Any], standing: Callable[[Any], tuple[int, str]]
                   ) -> list[Any]:
    """The order one budget-bound run captures its units in (mail folders, SharePoint
    roots): one rule for both.
    ``standing(unit)`` is ``(kind, since)``:

    * ``FRONTIER``: a unit with a stored frontier (a cut round or walk), the one started
      earliest first (``since``: when its round or walk started), so a unit larger than
      one budget resumes every run and completes;
    * ``PENDING``: other incomplete units, never started ones among them, in the declared
      order;
    * ``COMPLETE``: complete units, each reading only its delta, the oldest position first
      (``since``: when it was last brought current; empty: unknown, the oldest);
    * ``TERMINAL``: units with a terminal reason (:func:`terminal_reason`).

    Ties keep the declared order."""
    def key(numbered: tuple[int, Any]) -> tuple[int, str, int]:
        kind, since = standing(numbered[1])
        return kind, since if kind in (FRONTIER, COMPLETE) else "", numbered[0]
    return [unit for _n, unit in sorted(enumerate(units), key=key)]


def folder_standing(state: Mapping[str, Any] | None) -> tuple[int, str]:
    """A mail folder's :func:`schedule_units` standing from its stored row."""
    if state is None:
        return PENDING, ""
    if state["status"] == "complete":
        return COMPLETE, state["last_run_at"] or ""
    if terminal_reason(state["reason"]):
        return TERMINAL, ""
    if state["next_url"]:
        return FRONTIER, state["round_started_at"] or ""
    return PENDING, ""


# The cut a kept store resumes is a later_ask while the store is not kept between runs.
PERSISTENT_STATE_ASK = "live-host-persistent-state-dir"


def folder_error_reason(exc: BaseException) -> str:
    """The typed reason of a folder that raised: ``folder_error:<cause>``, the cause a
    ``CaptureError``'s fixed code or the exception class name (a short identifier),
    never the message."""
    cause = exc.code if isinstance(exc, CaptureError) else type(exc).__name__
    cause = re.sub(r"[^A-Za-z0-9_]", "_", str(cause))[:40] or "unclassified"
    return f"folder_error:{cause}"


def _failure(exc: SourceUnavailable) -> str:
    """The recorded reason of a mailbox or folder the source refused or failed:
    ``forbidden`` for HTTP 403 (e.g. outside the Exchange application access
    policy), else ``source_error:<detail>``."""
    detail = unavailable_detail(exc) or "unclassified"
    return "forbidden" if detail.removeprefix("retried.") == "http_403" \
        else f"source_error:{detail}"


def graph_block(part: str, op: str, permissions: Mapping[str, str],
                status: Any) -> dict[str, Any] | None:
    """The ``blocked_upstream`` part of a Graph read ``op`` that answered ``status``,
    or None when the one upstream rule (``plx_hub_guard.upstream_blocks``, source
    ``graph``) does not block it. The one shape of a Graph refusal in the mail, Teams
    and SharePoint list captures: ``error_code`` from ``GRAPH_BLOCKS``
    (``missing_permission`` on 403, ``unauthorized`` on 401), on 403 the application
    permission ``op`` needs (``permissions[op]``), cause ``host_misconfiguration``."""
    if not upstream_blocks("http", None, status, None, "graph"):
        return None
    return {"part": part, "source": "microsoft-graph", "http_status": status,
            "error_code": GRAPH_BLOCKS[status], "cause": "host_misconfiguration",
            **({"permission": permissions[op]} if status == 403 else {})}


def blocked_reason(part: Mapping[str, Any]) -> str:
    """A blocked read's reason: ``blocked:<error_code>[:<permission>]``."""
    return ":".join(["blocked", part["error_code"],
                     *([part["permission"]] if "permission" in part else [])])


# The Graph application permission each mail read needs, named when Graph refuses it
# (403). A mailbox is opened by its directory lookup (``graph.user``); a folder is read
# through its mail endpoints (``graph.mail_folder``, then the message delta under the
# same Mail.Read grant). A folder 403 with Mail.Read granted is an Exchange application
# access policy that leaves the mailbox out ("Graph refusals").
_PERMISSION = {"graph.user": "User.Read.All", "graph.mail_folder": "Mail.Read"}
# The part and the Graph call each refused unit stands for.
REFUSED_PARTS = {"mailbox": ("mail.mailbox_lookup", "graph.user"),
                 "folder": ("mail.folder_read", "graph.mail_folder")}


def _blocked(kind: str, exc: BaseException) -> dict[str, Any] | None:
    """The ``graph_block`` part of a mailbox or folder read that failed with ``exc``
    (its HTTP status: a mail read's or the token request's), None when not blocked."""
    part, op = REFUSED_PARTS[kind]
    return graph_block(part, op, _PERMISSION, getattr(exc, "http_status", None))


def refused_parts(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The live verifier's ``blocked_upstream`` parts of a capture run the source
    refused outright: when every mailbox the run bound (the allowlist's refusals aside)
    is refused under the one upstream rule (``graph_block``: HTTP 403 or 401), on its
    directory lookup or on every one of its folders, the run read nothing because the
    host's grant or credential is missing, not because the product failed. One part
    per refused call and status (``mail.mailbox_lookup`` needs User.Read.All,
    ``mail.folder_read`` Mail.Read): the unit's ``blocked`` part, or for a folder the
    budget deferred with its stored ``forbidden`` reason, that 403. A run where any
    bound mailbox or folder was not refused (read, throttled, not visible, budget-cut)
    has no parts: a partial refusal stays a capture gap in ``reasons`` ("Graph refusals")."""
    if run.get("people_blocked"):
        return [run["people_blocked"]]
    bound = [m for m in run.get("mailboxes") or [] if m.get("status") != "outside_allowlist"]
    units: list[tuple[str, Mapping[str, Any]]] = []
    for entry in bound:
        if entry.get("folders"):
            units += [("folder", f) for f in entry["folders"]]
        else:
            units.append(("mailbox", entry))
    parts = []
    for kind, unit in units:
        part, op = REFUSED_PARTS[kind]
        found = unit.get("blocked") or (
            graph_block(part, op, _PERMISSION, 403)
            if kind == "folder" and unit.get("reason") == "forbidden" else None)
        if found is None:
            return []
        parts.append(found)
    unique = {json.dumps(p, sort_keys=True): p for p in parts}.values()
    return sorted(unique, key=lambda p: (p["part"], p["http_status"]))
