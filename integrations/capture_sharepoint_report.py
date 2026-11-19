"""SharePoint capture run reports: the state directories, incomplete and blocked scope
marks, budget cut causes and the site registry read-back a run reports (split from
``integrations.capture_sharepoint``, which re-exports every name)."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from . import live_source_grammar as grammar
from .capture_mail import budget_cut_block
from .capture_mail_scope import CaptureError, check_state_dir
from .live_source_guard import LiveSourceAudience


CAPTURE_PRINCIPAL = "svc:sharepoint-capture"
# The host state directory of the capture check (``default_state_dir``), below the host's
# state base: the pattern of ``capture_teams`` and ``eval.heldout``.
CHECK_STATE = ("plx-workbench", "sharepoint-capture-check")


def default_state_dir(env: Mapping[str, str], *, injected: bool) -> Path:
    """The host's state directory of the capture check, kept across its runs
    (:func:`capture_mail.check_state_dir`): ``$PLX_WORKBENCH_STATE_DIR/sharepoint-capture-check``,
    else ``$XDG_STATE_HOME/plx-workbench/sharepoint-capture-check``, else under
    ``$HOME/.local/state``, never in Git. An injected transport gets its own
    subdirectory, so a fixture run never shares the live store. None set to an absolute
    path: refused, never a store in a place nobody chose."""
    return check_state_dir(env, CHECK_STATE[-1], injected=injected)[0]


STATE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")


def state_name_dir(env: Mapping[str, str], name: Any, *, injected: bool
                   ) -> tuple[Path, Path, str]:
    """:func:`capture_mail.check_state_dir` for the named store ``sharepoint-capture-
    check-<name>`` (``--state-name``); a name that is not 1..48 lowercase letters, digits
    and hyphens is refused (``ValueError``) before any directory is made."""
    if not isinstance(name, str) or STATE_NAME.fullmatch(name) is None:
        raise ValueError("state_name_invalid")
    return check_state_dir(env, f"{CHECK_STATE[-1]}-{name}", injected=injected)


def blocked_scope(entries: Sequence[Mapping[str, Any]], gaps: Sequence[str] = (), *,
                  carried: bool | None = None) -> list[dict[str, Any]]:
    """The verifier's ``blocked_upstream`` parts (cause ``later_ask``) of an
    :func:`incomplete_scope` that only a human step settles, by the one rule of
    :func:`capture_mail.budget_cut_block`: every incomplete root is cut by the time
    budget alone and its resume has no kept store. One root incomplete any other way,
    a discovery gap (``gaps``), or a cut that resumes over a kept store (it finishes by
    itself, the check fails until it has), yields no part: the check fails, never
    blocked. ``carried``: the store was carried into this run (default: any entry's
    ``persisted``; pass it when only discovery gaps may be named)."""
    units = [(f"{e['target']}.root{e['root']}", e["reasons"]) for e in entries]
    units += [("estate.discovery", [gap]) for gap in gaps]
    if carried is None:
        carried = any(e["persisted"] for e in entries)
    return budget_cut_block(units, carried=carried)


def _spread(items: Sequence[Any], sample: int | None) -> list[Any]:
    """``items``, or at most ``sample`` of them evenly spread (first and last kept)."""
    if sample is None or len(items) <= sample:
        return list(items)
    if sample < 1:
        raise ValueError("verify_sample must be at least 1")
    step = (len(items) - 1) / max(1, sample - 1)
    return [items[round(n * step)] for n in range(sample)]


def _root_reasons(root: Mapping[str, Any]) -> set[str]:
    return set(filter(None, (root["reason"] or "").split(",")))


BUDGET_CUT_CAUSES = ("unreached", "start_cut", "walk_cut", "finish_cut")


def registry_site_names(path: str | Path) -> list[str]:
    """The site URL names of a source registry's ``sharepoint`` ``site`` entries (names
    already committed there, so a report naming them discloses nothing new). Anything
    malformed is refused."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or not all(isinstance(e, Mapping) for e in entries):
        raise CaptureError("registry_invalid")
    names = [e.get("name") for e in entries
             if e.get("system") == "sharepoint" and e.get("kind") == "site"]
    if not all(isinstance(n, str) and grammar.TYPES["site_name"].valid(n) for n in names):
        raise CaptureError("registry_invalid")
    return names


NO_ACCESS_GAPS = ("not_visible", "source_error:http_401", "source_error:http_403")


def credit_readback(outcomes: dict[str, Any], readback: Mapping[str, Any]) -> None:
    """A site whose libraries were all walked but which holds no file read nothing: it
    moves from ``captured`` to ``unexercised`` with its typed reason, so the registry
    never credits an empty site as read."""
    for name in list(outcomes["captured"]):
        if not readback[name]["items_read"]:
            outcomes["captured"].remove(name)
            outcomes["unexercised"].append(name)
            outcomes["reasons"][name] = readback[name]["reason"]
    outcomes["unexercised"].sort()


def _report_run(number: int, run: Mapping[str, Any]) -> dict[str, Any]:
    targets = []
    for entry in run["targets"]:
        roots = [{"status": r["status"], "complete": r["complete"], "reason": r["reason"],
                  "round": r["round"], "full_walk_reason": r["full_walk_reason"],
                  "walk_position": r["walk_position"], "counts": dict(r["counts"])}
                 for r in entry["roots"]]
        targets.append({"index": entry["index"], "status": entry["status"],
                        "complete": entry["complete"], "roots": roots,
                        "discovered": bool(entry.get("discovered")),
                        # planned from the store while a listing failed (``_carry``)
                        "carried": bool(entry.get("carried"))})
    report = {"run": number, "complete": run["complete"], "targets": targets,
              "complete_bound": run["complete_bound"], "bound_total": run["bound_total"],
              "incomplete_reasons": run["incomplete_reasons"]}
    if "discovery" in run:
        report["discovery"] = {"status": run["discovery"]["status"],
                               "complete": run["discovery"]["complete"],
                               "gaps": sorted(set(run["discovery"]["gaps"])),
                               "counts": dict(run["discovery"]["counts"])}
    if "named_items" in run:   # counts and typed outcomes only, never an item
        named = run["named_items"]
        report["named_items"] = {"named": named["named"],
                                 "outcomes": {k: v for k, v in named["outcomes"].items() if v},
                                 "counts": {k: v for k, v in named["counts"].items() if v}}
    return report


def folder_only_audience(audience: LiveSourceAudience, principal: str,
                         roots: Sequence[tuple[str, str]]) -> LiveSourceAudience:
    """``principal`` granted the captured root folders (``(drive id, root)``) and
    everything under them, with the item permissions required on those drives and no
    source identity: the live shape of an employee whose folder grant is wider than
    the files shared with them."""
    grants: dict[str, list[str]] = {}
    for drive_id, root in roots:
        grants[f"sharepoint:{drive_id}:{root}"] = [principal]
        grants[f"sharepoint:{drive_id}:{root.rstrip('/')}/*"] = [principal]
    return LiveSourceAudience(grants, tenant=audience.tenant,
                              source_acl_required=sorted({f"sharepoint:{d}:*"
                                                          for d, _ in roots}))
