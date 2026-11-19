"""Ordering the daily set: spec values, starvation, fixed-first and milestone order, degraded
relations, profiles and artifacts (``verify.live_verify``, which re-exports them)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from verify.live_verify_policy import (
    ANSWER_TIERS,
    ARTIFACTS_DIR_NAME,
    ARTIFACT_INDEX,
    ARTIFACT_INDEX_SCHEMA,
    FULL_SHA,
    HOST_STATE_CONSUMERS,
    HOST_STATE_PRODUCER,
    ID_SHAPE,
    STARVED_RUNS,
    UNDECLARED_ENV_EXIT,
    VALUE_TIERS,
)
from verify.live_verify_specs import blocking_requires, session_paths, spec_tier
from verify.live_verify_checks import DAILY_STALE_HOURS, spec_cadence
from verify.live_verify_report import _git, _stamp, measured_in_part, write_atomic


def milestone_order(entries: Sequence[Mapping[str, Any]],
                    held: Mapping[str, Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The milestone rotation: specs the set holds no result for, then the oldest held
    ``checked_at`` first, so successive runs refresh every milestone check in turn; the
    specs one requires are pulled in just before it."""
    floor = datetime.min.replace(tzinfo=timezone.utc)

    def key(entry: Mapping[str, Any]) -> tuple[bool, datetime]:
        at = _stamp((held.get(entry["id"]) or {}).get("checked_at"))
        return at is not None, at or floor
    return _after_requires(sorted(entries, key=key))


def _positive_minimum(expect: Mapping[str, Any]) -> bool:
    return any(value > 0 for value in expect.get("json_min", {}).values())


def _undeclared_read(code: int, stderr: bytes, hidden: Sequence[str]) -> str | None:
    """The hidden credential name the guard stopped the command at, if it did."""
    if code != UNDECLARED_ENV_EXIT:
        return None
    lines = stderr.decode("utf-8", "replace").strip().splitlines()
    try:
        value = json.loads(lines[-1]) if lines else None
    except ValueError:
        return None
    name = value.get("name") if isinstance(value, Mapping) and value.get(
        "error") == "UndeclaredEnv" else None
    return name if name in hidden else None


def spec_value(spec: Mapping[str, Any] | None) -> int:
    """A spec's place in the daily rotation's value order (lower runs first): the progress
    ``metric``, then answer measures (``answer_source``, ``ANSWER_TIERS``), then
    ``VALUE_TIERS`` in order, capture last."""
    spec = spec or {}
    if spec.get("metric") is True:
        return 0
    tier = spec_tier(spec)
    if spec.get("answer_source") is True or tier in ANSWER_TIERS:
        return 1
    return 2 + (VALUE_TIERS.index(tier) if tier in VALUE_TIERS else len(VALUE_TIERS))


def starved(held: Any) -> bool:
    """A carried budget deferral of ``STARVED_RUNS`` or more runs: its ``count`` grows by one
    each run of the set that defers it and the entry goes when it runs, so the count is the
    runs in a row that left it waiting."""
    count = held.get("count") if isinstance(held, Mapping) else None
    return isinstance(count, int) and count >= STARVED_RUNS


def order_entries(entries: Sequence[Mapping[str, Any]],
                  history: Mapping[str, Mapping[str, Any]] | None = None,
                  carried: Mapping[str, Any] | None = None,
                  cost: Mapping[str, float] | None = None,
                  fixed: Iterable[str] = (),
                  now: datetime | None = None) -> list[Mapping[str, Any]]:
    """Specs that others ``requires`` run first, then consumers of session state (specs
    naming ``{session_state}`` that require another), then the rest (stable: declared order
    otherwise); a spec never runs before one it requires. With a ``history`` (the daily
    rotation): first the set's ``carried`` budget deferrals that waited ``STARVED_RUNS`` runs
    (``starved``), one with no stored result first (a long never-run spec, such as one whose
    timeout fits only a whole command, otherwise waits behind measured ones run after run),
    then the most deferred, then the oldest last stored result (ahead of value), then, with
    a ``now``, the daily specs whose last stored result is over ``DAILY_STALE_HOURS`` old
    (each keeps latest.json ``daily_stale``; a weekly one is due by ``WEEKLY_DAYS`` instead,
    one never run keeps its value place), the oldest first, then the rest; each by value
    (``spec_value``; a spec others require takes the best value of the specs in this run
    that require it, so a producer runs before the answers it feeds), then specs with no
    stored result, then the cheapest ``cost`` (the budget each needs to start), then the
    oldest last stored result. ``fixed`` (``fixed_since_verdict``: failing checks whose
    code changed since their last verdict, or never measured) run right after the starved
    and stale ones, before any cheaper check.
    The specs one requires are pulled in just before it."""
    needed = {up for e in entries if e["spec"] for up in e["spec"].get("requires", [])}

    def rank(entry: Mapping[str, Any]) -> int:
        spec = entry["spec"] or {}
        return 0 if entry["id"] in needed else 1 if (
            spec.get("requires") and session_paths(spec)) else 2
    if history is None:
        return _after_requires(sorted(entries, key=rank))
    value = {e["id"]: spec_value(e["spec"]) for e in entries}
    changed = True
    while changed:  # a producer takes its best consumer's value, transitively
        changed = False
        for entry in entries:
            for up in (entry["spec"] or {}).get("requires", []):
                if up in value and value[entry["id"]] < value[up]:
                    value[up], changed = value[entry["id"]], True

    def last(entry: Mapping[str, Any]) -> str:
        known = history.get(entry["id"]) or {}
        return max([known.get("last_pass") or "", *(known.get("not_passing") or [])])

    fixed_ids = set(fixed)

    def overdue(entry: Mapping[str, Any]) -> bool:
        if now is None or spec_cadence(entry["spec"]) != "daily":
            return False
        at = _stamp(last(entry))  # never run or unreadable: its value place (fail closed)
        return at is not None and now - at > timedelta(hours=DAILY_STALE_HOURS)

    def key(entry: Mapping[str, Any]) -> tuple[Any, ...]:
        held = (carried or {}).get(entry["id"])
        # A starved deferral waits on its age, not its value: never run first, then the
        # oldest last result, so a low-value check whose fix landed gets a fresh verdict. A
        # daily check over the stale age (it keeps latest.json daily_stale) waits on its
        # age too, after them: a set that took days would otherwise leave its
        # earliest checks behind every fresher one again.
        due = ((0, last(entry) != "", -held["count"], last(entry)) if starved(held)
               else (0, 0, True, last(entry)) if overdue(entry)
               else (1, 0, False, "") if entry["id"] in fixed_ids else (2, 0, False, ""))
        return (*due, value[entry["id"]], last(entry) != "",
                (cost or {}).get(entry["id"], 0), last(entry))
    return _after_requires(sorted(entries, key=key))


def command_module(spec: Mapping[str, Any]) -> str | None:
    """The first-party module a spec's command runs: the ``-m`` module or a ``.py`` path."""
    argv = [str(a) for a in spec.get("command") or []]
    if "-m" in argv[:-1]:
        return argv[argv.index("-m") + 1]
    from verify import import_graph
    path = next((a for a in argv if a.endswith(".py") and not a.startswith("/")), None)
    return import_graph.module_name(path) if path else None


def fixed_since_verdict(entries: Sequence[Mapping[str, Any]],
                        verdicts: Mapping[str, Mapping[str, Any]],
                        root: Path, head: str | None,
                        carried: Mapping[str, Any] | None = None) -> set[str]:
    """Ids of the entries whose last verdict is a failure (``fail``/``error``) measured on a
    ``main_sha`` since which a module in the command module's import closure changed on
    ``head``, or that have no verdict at all while the closure exists and the set has already
    deferred them for budget (``carried``: never measured though tried; a spec just landed
    has no deferral and waits its turn by value).
    A verdict whose sha is unknown to the repo, a closure that cannot be found and a diff
    that cannot be read leave the check out (fail closed: its old place)."""
    from verify import import_graph
    graph, deps, changed_by = None, None, {}
    out: set[str] = set()
    for entry in entries:
        spec = entry["spec"]
        module = command_module(spec) if spec else None
        if module is None:
            continue
        if graph is None:
            graph = import_graph.build(root)
            deps = import_graph.dependencies(graph)
        if module not in graph:
            continue
        closure, queue = {module}, [module]
        while queue:
            for up in deps[queue.pop()]:
                if up not in closure:
                    closure.add(up)
                    queue.append(up)
        verdict = verdicts.get(entry["id"])
        if not verdict:
            if entry["id"] in (carried or {}):
                out.add(entry["id"])
            continue
        then = verdict.get("main_sha")
        if (verdict.get("result") not in ("fail", "error") or not isinstance(then, str)
                or not head or then == head or not FULL_SHA.fullmatch(then)):
            continue
        if then not in changed_by:
            names = _git(root, "diff", "--name-only", then, head)
            changed_by[then] = None if names is None else {
                import_graph.module_name(n) for n in names.splitlines() if n.endswith(".py")}
        if changed_by[then] and closure & changed_by[then]:
            out.add(entry["id"])
    return out


def _after_requires(ordered: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """``ordered`` with the specs each one requires (those in it) pulled in just before it,
    deepest first, in their own order (stable otherwise; a cycle, already a spec error, is
    cut where it closes)."""
    position = {e["id"]: i for i, e in enumerate(ordered)}
    placed: set[str] = set()
    out: list[Mapping[str, Any]] = []

    def place(entry: Mapping[str, Any], path: frozenset[str]) -> None:
        if entry["id"] in placed or entry["id"] in path:
            return
        ups = {up for up in (entry["spec"] or {}).get("requires", []) if up in position}
        for up in sorted(ups, key=position.__getitem__):
            place(ordered[position[up]], path | {entry["id"]})
        placed.add(entry["id"])
        out.append(entry)
    for entry in ordered:
        place(entry, frozenset())
    # A host-scoped check follows the host producer whose state it reads (it is cheap; the
    # producer keeps its own place in the rotation).
    ids = [e["id"] for e in out]
    if HOST_STATE_PRODUCER in ids:
        late = [e for e in out if e["id"] in HOST_STATE_CONSUMERS
                and ids.index(e["id"]) < ids.index(HOST_STATE_PRODUCER)]
        out = [e for e in out if e not in late]
        at = [e["id"] for e in out].index(HOST_STATE_PRODUCER) + 1
        out[at:at] = late
    return out


def degraded_relations(check: Mapping[str, Any]) -> list[str] | None:
    """The relations a degraded check names unavailable: it is blocked only by
    ``relation_unavailable`` parts and its served rest was measured
    (``measured_in_part``); None for any other check."""
    if not measured_in_part(check):
        return None
    parts = check["detail"]["blocked"]["parts"]
    if any(not isinstance(p, Mapping) or p.get("cause") != "relation_unavailable"
           or not isinstance(p.get("part"), str) for p in parts):
        return None
    return sorted({p["part"] for p in parts})


def valid_specs(entries: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Every loaded spec without an error, by id."""
    return {e["id"]: e["spec"] for e in entries if e["spec"] is not None and e["error"] is None}


def failed_upstream(spec: Mapping[str, Any], known: Mapping[str, Mapping[str, Any]],
                    specs: Mapping[str, Mapping[str, Any]] | None = None
                    ) -> Mapping[str, Any] | None:
    """The first required spec whose known result is a fail, error or blocked, unless it
    is only degraded (``degraded_relations``): its dependents still run. With ``specs``
    (every valid spec by id), a session producer never blocks (``blocking_requires``)."""
    for upstream in (spec.get("requires", []) if specs is None
                     else blocking_requires(spec, specs)):
        check = known.get(upstream)
        if check is not None and check.get("result") in ("fail", "error", "blocked") \
                and degraded_relations(check) is None:
            return check
    return None


def unavailable_relations_needed(spec: Mapping[str, Any],
                                 known: Mapping[str, Mapping[str, Any]]
                                 ) -> tuple[Mapping[str, Any], list[str]] | None:
    """The first degraded required spec naming unavailable a relation the spec's
    ``requires_relations`` lists, with those relations; None when the spec may run."""
    needed = set(spec.get("requires_relations", []))
    for upstream in spec.get("requires", []) if needed else []:
        check = known.get(upstream)
        names = degraded_relations(check) if check is not None else None
        hit = sorted(needed.intersection(names or ()))
        if hit:
            return check, hit
    return None


def externalize_profile(check: Mapping[str, Any], profiles_dir: Path,
                        report_dir: Path | None = None) -> dict[str, Any]:
    """Move a check's profile to ``<profiles_dir>/<id>.json`` (one file per spec, rewritten
    only when the content changed) and leave the file name, digest and counts inline."""
    profile = check.get("profile")
    if not isinstance(profile, Mapping) or not ID_SHAPE.fullmatch(str(check.get("id"))):
        return dict(check)
    text = json.dumps(profile, indent=1, sort_keys=True) + "\n"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = profiles_dir / f"{check['id']}.json"
    try:
        unchanged = path.read_text(encoding="utf-8") == text
    except OSError:
        unchanged = False
    if not unchanged:
        path.write_text(text, encoding="utf-8")
    out = {k: v for k, v in check.items() if k != "profile"}
    # Relative to the report's directory, so the reference resolves wherever the profiles
    # dir sits (results/profiles/x.json -> "profiles/x.json"; elsewhere -> "../p/x.json").
    out["profile_file"] = Path(os.path.relpath(path.resolve(), (report_dir or Path.cwd())
                                               .resolve())).as_posix()
    out["profile_summary"] = {
        "sha256": hashlib.sha256(text.encode()).hexdigest(), "changed": not unchanged,
        **{f: profile.get(f) for f in ("valid", "target_count", "targets_truncated",
                                        "sanitizer_dropped") if f in profile}}
    return out


def publish_artifacts(check_id: str, captured: Sequence[tuple[dict[str, Any], bytes | None]],
                      artifacts_dir: Path | None, main_sha: str | None) -> list[dict[str, Any]]:
    """Write each passed artifact to ``<artifacts_dir>/<id>/<sha256>.<ext>`` and append it to
    that dir's index only when its digest differs from the newest one of its name."""
    rows = []
    for row, raw in captured:
        if raw is None or artifacts_dir is None:
            rows.append(row if raw is None else {**row, "status": "unpublished"})
            continue
        folder = artifacts_dir / check_id
        extension = row["name"].rpartition(".")[2]
        relative = f"{ARTIFACTS_DIR_NAME}/{check_id}/{row['sha256']}.{extension}"
        target = folder / Path(relative).name
        try:
            held = target.read_bytes() if target.is_file() else None
        except OSError:
            held = None
        try:
            if held is None or hashlib.sha256(held).hexdigest() != row["sha256"]:
                folder.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(target.name + ".tmp")
                temp.write_bytes(raw)
                os.replace(temp, target)
            try:
                index = json.loads((folder / ARTIFACT_INDEX).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                index = None
            captures = [c for c in (index.get("captures") if isinstance(index, dict)
                                    and isinstance(index.get("captures"), list) else [])
                        if isinstance(c, Mapping)]
            newest = next((c for c in reversed(captures) if c.get("name") == row["name"]), None)
            changed = newest is None or newest.get("sha256") != row["sha256"]
            if changed:
                captures.append({"name": row["name"], "kind": row["kind"],
                                 "sha256": row["sha256"], "bytes": row["bytes"],
                                 "captured_at": row["captured_at"], "main_sha": main_sha,
                                 "file": relative})
                write_atomic(folder / ARTIFACT_INDEX, json.dumps(
                    {"schema": ARTIFACT_INDEX_SCHEMA, "check": check_id,
                     "captures": captures}, indent=1) + "\n")
        except OSError:
            rows.append({**row, "status": "unpublished", "reason": "write_failed"})
            continue
        rows.append({**row, "status": "published" if changed else "unchanged",
                     "file": relative})
    return rows


def _sanitize_profile(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        from verify.data_profile import sanitize_profile
    except ImportError:  # run by path: verify/ itself is sys.path[0]
        from data_profile import sanitize_profile  # type: ignore[no-redef]
    return sanitize_profile(document)
