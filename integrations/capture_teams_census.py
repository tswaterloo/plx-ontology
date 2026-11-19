"""Teams capture: the census file, the human confirmation file and the operator
reports (reconcile / read-back / sweep counts).

Split out of ``integrations.capture_teams`` (module size cap) with no behaviour change;
``capture_teams`` re-exports every name. Module-level names of ``capture_teams`` are
read from it at call time (``ct``).
"""
from __future__ import annotations

from collections import Counter
from datetime import date
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from ontology.types import Actor

from . import capture_teams as ct
from .capture_mail_scope import assert_outside_git
from .capture_store import CaptureError, CaptureStore


def census_path(state_dir: str | Path) -> Path:
    return Path(state_dir).expanduser() / ct.STORE_DIR / ct.CENSUS_FILE


def confirmation_path(state_dir: str | Path) -> Path:
    return Path(state_dir).expanduser() / ct.STORE_DIR / ct.CONFIRMATION_FILE


def write_census(state_dir: str | Path, private: Mapping[str, Any] | None) -> bool:
    """Write the census listing to ``census_path`` (0600, atomically, outside every git
    work tree); with no listing (the census never passed the people guard), remove an
    earlier one, so a stale listing is never read as this run's. Whether one was written."""
    root = assert_outside_git(state_dir) / ct.STORE_DIR
    path = root / ct.CENSUS_FILE
    if private is None:
        path.unlink(missing_ok=True)
        return False
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = root / (ct.CENSUS_FILE + ".tmp")
    staging.unlink(missing_ok=True)
    handle = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(private, out, indent=1, sort_keys=True)
    os.replace(staging, path)
    return True


def read_census(state_dir: str | Path) -> dict[str, Any] | None:
    """The last census's private listing (``census_path``), or None when there is none
    or it is not a census of this schema (nothing is then carried)."""
    try:
        found = json.loads(census_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) and found.get("schema") == ct.CENSUS_SCHEMA else None


def confirmation_block(code: str) -> dict[str, Any]:
    """The typed block of content waiting on the human confirmation (``code``: why
    none is usable). A later ask, never cleared by code."""
    return {"part": ct.CONFIRMATION_PART, "source": "owner-confirmation", "http_status": None,
            "error_code": code, "cause": "later_ask", "later_ask": ct.CONFIRMATION_LATER_ASK}


def read_confirmation(state_dir: str | Path) -> tuple[dict[str, Any] | None, str]:
    """(the confirmation, ``valid``) or (None, why not): the owner's
    ``plx-teams-content-confirmation/v1`` at ``confirmation_path``, in one of two forms.

    The listed form: ``confirmed_by``, ``confirmed_at`` (a date), ``channels``
    (``{team_id, channel_id}``) and ``chats`` (chat ids), at most 200 each, naming at
    least one. No
    ``channels`` or ``chats``. It confirms exactly what the run's census clears.

    Absent, unreadable, inside a git work tree, malformed, of another ``scope`` or naming
    nothing: no content is read (fails closed). Code never writes or defaults one. The
    result holds ``form`` (``LISTED`` or ``ALL_CENSUS_CLEARED``), ``channels`` and
    ``chats`` (empty for the all form)."""
    path = confirmation_path(state_dir)
    if not path.exists() and not path.is_symlink():
        return None, "content_confirmation_absent"
    try:
        real = assert_outside_git(path)
    except ValueError:  # capture_mail's CaptureError: it resolves into a git work tree
        return None, "content_confirmation_in_git"
    except (OSError, RuntimeError):  # a symlink loop
        return None, "content_confirmation_unreadable"
    try:
        if not real.is_file() or real.stat().st_size > ct.MAX_CONFIRMATION_BYTES:
            return None, "content_confirmation_unreadable"
        document = json.loads(real.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "content_confirmation_unreadable"
    if not isinstance(document, Mapping) or document.get("schema") != ct.CONFIRMATION_SCHEMA:
        return None, "content_confirmation_invalid"
    if "scope" in document:
        return _all_form(document)
    keys = {"schema", "confirmed_by", "confirmed_at", "note", "channels", "chats"}
    if set(document) - keys:
        return None, "content_confirmation_invalid"
    by, at = document.get("confirmed_by"), document.get("confirmed_at")
    channels, chats = document.get("channels", []), document.get("chats", [])
    if not (isinstance(by, str) and 0 < len(by.strip()) <= 200 and isinstance(at, str)
            and ct._DATE.match(at) and len(at) <= 40
            and isinstance(channels, list) and len(channels) <= 200
            and isinstance(chats, list) and len(chats) <= 200
            and all(isinstance(c, Mapping) and not set(c) - {"team_id", "channel_id", "note"}
                    and ct._valid("guid", c.get("team_id"))
                    and ct._valid("teams_thread", c.get("channel_id")) for c in channels)
            and all(ct._valid("teams_thread", c) for c in chats)):
        return None, "content_confirmation_invalid"
    if not channels and not chats:
        return None, "content_confirmation_empty"
    return {"form": ct.LISTED,
            "channels": {(c["team_id"].lower(), c["channel_id"]) for c in channels},
            "chats": set(chats)}, "valid"


def _all_form(document: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The all_census_cleared form of :func:`read_confirmation` (``document`` has a
    ``scope``). A
    signer that is not a person's full name, or names code, is not the owner."""
    keys = {"schema", "scope", "confirmed_by", "confirmed_at", "directive", "note"}
    at, directive = document.get("confirmed_at"), document.get("directive")
    note = document.get("note", "")
    if document.get("scope") != ct.ALL_CENSUS_CLEARED or set(document) - keys \
            or not isinstance(note, str) or len(note) > 2000:
        return None, "content_confirmation_invalid"
    try:
        dated = isinstance(at, str) and len(at) == 10 and bool(date.fromisoformat(at))
    except ValueError:
        dated = False
    if not dated or not isinstance(directive, str) or not ct._DIRECTIVE.fullmatch(directive):
        return None, "content_confirmation_invalid"
    by = document.get("confirmed_by")
    words = set(re.split(r"[^a-z]+", by.lower())) if isinstance(by, str) else set()
    if not isinstance(by, str) or len(by) > 120 or not ct._PERSON.fullmatch(by) \
            or words & ct._CODE_SIGNERS:
        return None, "content_confirmation_not_owner"
    return {"form": ct.ALL_CENSUS_CLEARED, "channels": set(), "chats": set()}, "valid"


def confirmed_scope(scope: ct.TeamsScope, private: Mapping[str, Any] | None,
                    confirmed: Mapping[str, Any]) -> tuple[ct.TeamsScope | None, int]:
    """(the content scope, confirmed items this census did not clear): the channels and
    chats both confirmed by the owner and cleared by this run's census, with ``scope``'s
    caps and allowlist and nothing discovered from members. A confirmed item the census
    refused (an excluded member now, a members read failed) or no longer found is not
    read. The all form confirms exactly the cleared set (none left out). None when
    nothing is left."""
    cleared_channels = {(t["team_id"], c["channel_id"]) for t in (private or {}).get(
        "teams", []) for c in t["channels"]}
    cleared_chats = {c["chat_id"] for c in (private or {}).get("chats", [])}
    if confirmed.get("form") == ct.ALL_CENSUS_CLEARED:
        confirmed = {"channels": cleared_channels, "chats": cleared_chats}
    channels = sorted(confirmed["channels"] & cleared_channels)
    chats = sorted(confirmed["chats"] & cleared_chats)
    left_out = (len(confirmed["channels"]) - len(channels)
                + len(confirmed["chats"]) - len(chats))
    if not channels and not chats:
        return None, left_out
    by_team: dict[str, list[str]] = {}
    for team, channel in channels:
        by_team.setdefault(team, []).append(channel)
    return ct.TeamsScope({"schema": ct.SCOPE_SCHEMA, **scope.limits(),
                       "teams": [{"team_id": t, "channels": c} for t, c in by_team.items()],
                       "chats": chats}, allowlist=scope.allowlist), left_out


RECONCILE_STATES = ("captured", "other_tenant", "not_readable", "people_excluded",
                    "pending", "never_read")


def chats_reconcile(store: CaptureStore, chat_ids: Sequence[Any]) -> dict[str, Any]:
    """Every chat the census lists, by what the store holds of it (operator counts, no
    id): ``captured`` (current messages held), ``other_tenant`` (hosted in another
    tenant: Graph refuses its messages to this tenant's app, settled and never claimed),
    ``not_readable`` (another settled refusal), ``people_excluded``, ``pending`` (read
    begun, not settled yet: throttled, cut by the budget; ``pending_reasons`` by code)
    and ``never_read``. A census count above the captured one is explained here, never a
    silent gap."""
    out: dict[str, Any] = {state: 0 for state in RECONCILE_STATES}
    pending: Counter = Counter()
    ids = [c for c in dict.fromkeys(chat_ids) if isinstance(c, str) and c]
    holdings = store.unit_holdings(ct.CHAT_KIND.name)
    for chat in ids:
        run = holdings.get(ct.Conversation("chat", chat).unit)
        if run is not None and run["held"]:
            out["captured"] += 1
            continue
        if run is not None and run["state"] is None:
            run = None
        reason = str((run or {}).get("reason") or "")
        if run is None:
            out["never_read"] += 1
        elif reason == ct.OTHER_TENANT:
            out["other_tenant"] += 1
        elif reason.startswith(ct.NOT_READABLE):
            out["not_readable"] += 1
        elif reason == ct.PEOPLE_EXCLUDED:
            out["people_excluded"] += 1
        else:
            out["pending"] += 1
            pending[reason.split(":", 1)[0] or str((run or {}).get("state") or "open")] += 1
    out["listed"] = len(ids)
    out["pending_reasons"] = dict(sorted(pending.items()))
    return out


def _read_back(store: CaptureStore, units: Sequence[tuple[str, str]], actor: Actor
               ) -> tuple[list[str], int]:
    """(citations ``actor`` may see, items withheld from it) over ``units``."""
    seen, withheld = [], 0
    for kind_name, unit in units:
        after = None
        while True:
            try:
                page = store.records(actor, kind_name, unit, after_path=after, limit=1000)
            except CaptureError:
                break  # the unit is not visible to this actor at all
            seen += [item["citation"] for item in page["items"]]
            withheld += page["withheld_count"]
            after = page["next_after_path"]
            if after is None:
                break
    return seen, withheld


def _report_run(number: int, run: Mapping[str, Any]) -> dict[str, Any]:
    discovery = run["discovery"]
    return {"run": number, "complete": run["complete"],
            "discovery": {**{k: discovery[k] for k in (
                "status", "reasons", "conversations", "discovered_total", "unvisited",
                "members_outside_allowlist")},
                **{k: discovery.get(k, 0) for k in ("due", "deferred", "chat_previews",
                                                    "chat_preview_fallbacks",
                                                    "chat_listings_capped")},
                # Due now and not read this run (none: the rest is fresh or waiting).
                "due_left": max(0, discovery.get("due", 0) - len(run["conversations"]))},
            "next_due_seconds": run.get("next_due_seconds"),
            "complete_bound": run["complete_bound"], "bound_total": run["bound_total"],
            "throttled": bool(run.get("throttled")),
            "tenant_holds": run.get("tenant_holds", 0),
            "retry_after_seconds": round(float(run.get("retry_after_seconds") or 0.0), 1),
            # Counts only: who was excluded is never reported.
            "people_excluded": run["people_excluded"], "people_dropped": run["people_dropped"],
            # Conversations saved as read because the steadiness state failed (logged).
            "state_fallbacks": run.get("state_fallbacks", 0),
            "reply_sweep": run.get("reply_sweep"),
            "conversations": [{"index": i, "kind": c["kind"], "membership": c["membership"],
                               "status": c["status"], "reasons": c["reasons"],
                               "round": c["round"], "resumed_from": c["resumed_from"],
                               "acl_source": c["acl_source"],
                               "counts": {k: c["counts"][k] for k in ct._COUNT_KEYS}}
                              for i, c in enumerate(run["conversations"])]}


def _member_groups(api: Any, identities: Iterable[str], allowlist: ct.MailScope) -> set[str]:
    """``group:<id>`` of the groups of a reader's allowlisted addresses, read the way the
    employee permissions read them (``integrations.graph_directory``: the transitive
    membership, or derived from the joined Teams when that read is refused): what a
    real member of a ``group_fallback`` channel holds. An unread user adds none."""
    from .graph_directory import GraphDirectoryReader

    reader, groups = GraphDirectoryReader(api), set()
    for identity in sorted(identities):
        if "@" in identity and allowlist.allows([identity]):
            read = reader.read(identity)
            if read.status == "read":
                groups |= {f"group:{g}" for g in read.group_ids}
    return groups


def _sweep_report(sweep: Mapping[str, Any] | None, runs: Sequence[Mapping[str, Any]],
                  seconds: float) -> dict[str, Any] | None:
    if sweep is None:
        return None
    settled = sum(r["complete_bound"] for r in runs)
    rate = settled / (seconds / 3600) if seconds > 0 and settled else None
    left = runs[-1]["discovery"]["unvisited"] if runs else 0
    return {**sweep, "unvisited": left, "settled_reads": settled,
            "settled_per_hour": round(rate, 1) if rate else None,
            "eta_hours": 0.0 if not left else round(left / rate, 2) if rate else None}
