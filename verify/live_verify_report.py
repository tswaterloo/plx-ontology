"""The live-verify report: detail caps, git refs, history, tier and answer summaries, merging
and the known-red rules (``verify.live_verify``, which re-exports them)."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

from verify.live_verify_policy import (
    ANSWER_COUNTS,
    BLOCKED_RED_HOURS,
    DAILY_LOCK_PREFIX,
    DEFAULT_CADENCE,
    EARLIER_KEYS,
    EARLIER_RUNS,
    ENVELOPE_KEYS,
    FULL_SHA,
    HISTORY_SCHEMA,
    HISTORY_STAMPS,
    KNOWN_RED_RUNS,
    LIVE_REF,
    LOCKED_EXIT,
    LOCK_LIVE_HOURS,
    LOCK_NAME,
    MAX_DETAIL_CHARS,
    MAX_DETAIL_CHARS_CEILING,
    MAX_STRING_CHARS,
    RESULTS,
    SCHEMA,
    TIERS,
    UNCLAIMED_EXIT,
)
from verify.live_verify_specs import spec_tier
from verify.live_verify_checks import under, upstream_cause


def detail_cap(spec: Mapping[str, Any] | None = None) -> int:
    """The detail cap of ``spec``: its validated ``max_detail_chars``, else
    ``MAX_DETAIL_CHARS``. A value outside (MAX_DETAIL_CHARS, MAX_DETAIL_CHARS_CEILING] (a spec
    that never went through validate_spec) reads as the default, never a larger cap."""
    cap = (spec or {}).get("max_detail_chars")
    if isinstance(cap, bool) or not isinstance(cap, int) or \
            not MAX_DETAIL_CHARS < cap <= MAX_DETAIL_CHARS_CEILING:
        return MAX_DETAIL_CHARS
    return cap


def _cap(detail: dict[str, Any], spec: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``detail`` within ``detail_cap(spec)`` (``MAX_DETAIL_CHARS`` unless the spec declares
    its own). The recorded fields that hold a path
    ``spec``'s expectations assert (the path itself, above or below it) are kept first:
    the asserted part of each (all of it when a value at it or above it is asserted, else
    the asserted paths below it projected out of the recorded value, else all of it),
    smallest first, then each of those whole where it still fits, then the rest in order,
    each one that still fits. ``fields_truncated`` counts the paths dropped (``projected`` the fields kept
    only in their asserted parts). Verdicts are already computed and ``detail`` already
    scrubbed: this only decides which parts of the record stay, never adds a value."""
    limit = detail_cap(spec)
    if len(json.dumps(detail, sort_keys=True)) <= limit:
        return detail

    def fits(trial: Mapping[str, Any]) -> bool:
        return len(json.dumps(trial, sort_keys=True)) <= limit

    fields = detail.get("fields") or {}
    expects = [(spec or {}).get(k) or {} for k in ("expect", "expect_blocked")]
    present = [path for expect in expects for path in expect.get("json_fields", ())]
    valued = [*(path for expect in expects for path in expect.get("json_empty", ())),
              *(path for expect in expects for key in ("json_equals", "json_min", "json_max")
                for path in expect.get(key, {})),
              *(path for expect in expects for pair in expect.get("json_same", {}).items()
                for path in pair)]
    asserted = [*present, *valued]
    first = [key for key in fields
             if under(key, asserted) or any(under(path, (key,)) for path in asserted)]
    absent = object()

    def project(value: Any, segments: list[str]) -> Any:
        # the part of ``value`` at ``segments``, nested as it is: below a fan-out or a value
        # that is no mapping all of it stays, below a missing key nothing
        if not segments or not isinstance(value, Mapping):
            return value
        name = segments[0].partition("[")[0]
        if name not in value:
            return absent
        part = value[name] if name != segments[0] else project(value[name], segments[1:])
        return absent if part is absent else {name: part}

    def merge(left: Any, right: Any) -> Any:
        if not (isinstance(left, Mapping) and isinstance(right, Mapping)):
            return left  # both are the same recorded value or one holds the other
        return {**left, **{k: merge(left[k], v) if k in left else v for k, v in right.items()}}

    needed: dict[str, Any] = {}
    for key in first:
        if under(key, valued):  # its value (or one above it) is asserted: all of it
            needed[key] = fields[key]
            continue
        for path in (path for path in asserted if path != key and under(path, (key,))):
            part = (project(fields[key], path[len(key) + 1:].split("."))
                    if path[len(key)] == "." else fields[key])  # key[*]...: all of it
            if part is not absent:
                needed[key] = part if key not in needed else merge(needed[key], part)
        if key not in needed:  # nothing asserted below reads in it: as recorded
            needed[key] = fields[key]

    def keep(base: Mapping[str, Any], kept: dict[str, Any], keys: Sequence[str],
             value_of: Mapping[str, Any]) -> None:
        for key in keys:
            if fits({**base, "fields": {**kept, key: value_of[key]}}):
                kept[key] = value_of[key]

    def by_size(keys: Sequence[str]) -> list[str]:
        return sorted(keys, key=lambda k: len(json.dumps(needed[k], sort_keys=True)))

    capped = {k: v for k, v in detail.items() if k != "fields"}
    capped["fields_truncated"] = {"paths": len(fields), "projected": len(needed)}  # bounds
    kept: dict[str, Any] = {}
    keep(capped, kept, by_size(list(needed)), needed)
    for key in first:  # an asserted field whole where it fits, before any other field
        if key in kept and kept[key] is not fields[key] \
                and fits({**capped, "fields": {**kept, key: fields[key]}}):
            kept[key] = fields[key]
    keep(capped, kept, [key for key in fields if key not in first], fields)
    capped["fields"] = {k: kept[k] for k in fields if k in kept}
    if not fits(capped):
        capped = {"failures": (detail.get("failures") or [])[:10],
                  "fields_truncated": {"paths": len(fields), "projected": len(needed)},
                  # the envelope (why the command stopped) is short and always stays
                  "fields": {**{k: fields[k] for k in ENVELOPE_KEYS
                                if k in fields and not isinstance(fields[k], (dict, list))},
                             **{k: fields[k] for k in ("store", "disk") if k in fields}},
                  # the blocked parts and the completeness verdict always stay
                  **{k: detail[k] for k in ("blocked", "complete", "incomplete") if k in detail}}
        # then each asserted part that still fits, scalars and small mappings first
        keep(capped, capped["fields"], by_size([k for k in needed
                                                 if k not in capped["fields"]]), needed)
    projected = sum(capped["fields"][k] is not fields[k] for k in capped["fields"])
    capped["fields_truncated"] = {"paths": len(fields) - len(capped["fields"]),
                                  **({"projected": projected} if projected else {})}
    return capped


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def unreviewed_head(root: Path) -> str | None:
    """Why `root` is not reviewed main (HEAD differs from `git ls-remote origin main`,
    either is unreadable, or a tracked file is changed), or None when it is."""
    if _git(root, "status", "--porcelain", "--untracked-files=no") != "":
        return "tracked files changed or status unreadable"
    head = _git(root, "rev-parse", "HEAD")
    listed = _git(root, "ls-remote", "origin", "refs/heads/main")
    remote = listed.split()[0] if listed else None
    if head is None or remote is None:
        return "cannot read HEAD or origin main"
    return None if head == remote else f"HEAD {head[:12]} is not origin main {remote[:12]}"


def _fetched(root: Path, ref: str) -> str | None:
    """``ref`` (``<remote>/<branch>``) after fetching that branch into it, else None (the
    fetch failed or the ref names no remote branch). A remote ref is read only right after
    its own fetch, never from a tracking ref some earlier command left behind."""
    remote, _, branch = ref.partition("/")
    if not remote or not branch:
        return None
    done = _git(root, "fetch", "--quiet", "--no-tags", remote,
                f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
    return ref if done is not None else None


def unclaimed_daily(root: Path, session_id: str
                    ) -> tuple[dict[str, Any] | None, tuple[int, str, str] | None]:
    """``LIVE_REF``'s ``results/running.json`` (fetched first) when it names this session,
    else the refusal (exit code, error, reason): ``UNCLAIMED_EXIT`` when it names no session
    (absent, unfetched or unreadable), ``LOCKED_EXIT`` when another one. The routine pushes
    that marker before it launches a run, so two machines never run at once."""
    ref = _fetched(root, LIVE_REF)
    text = _git(root, "show", f"{ref}:results/{LOCK_NAME}") if ref else None
    try:
        held = json.loads(text) if text is not None else None
    except ValueError:
        held = None
    owner = held.get("session") if isinstance(held, Mapping) else None
    if owner == session_id:
        return dict(held), None
    if isinstance(owner, str) and owner:
        return None, (LOCKED_EXIT, "run_locked", f"results/{LOCK_NAME} on {LIVE_REF} names "
                      f"session {owner!r}, not {session_id!r}")
    return None, (UNCLAIMED_EXIT, "marker_unpushed", f"no results/{LOCK_NAME} naming a session "
                  f"on {LIVE_REF} (unfetched, absent or unreadable): push it before the run")


def _on_ref(root: Path, ref: str, path: str) -> bool:
    return _git(root, "cat-file", "-e", f"{ref}:{path}") is not None


def _stamp(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_red(check: Mapping[str, Any]) -> bool:
    return check.get("result") == "blocked" and (check.get("detail") or {}).get("red") is True


def failing_ids(checks: Sequence[Mapping[str, Any]]) -> list[str]:
    """Ids of the ``fail`` and ``error`` checks and of the checks blocked for over 24 h, so
    a comparison names them, not counts."""
    return sorted(c["id"] for c in checks if c["result"] in ("fail", "error") or _is_red(c))


def _not_passing(check: Mapping[str, Any]) -> bool:
    """A result that ran and did not pass (an upstream skip counts; a plain skip did not
    run and leaves the history alone)."""
    detail = check.get("detail") if isinstance(check.get("detail"), Mapping) else {}
    return check.get("result") != "skipped" or bool(detail.get("upstream"))


def folded(entry: Mapping[str, Any] | None, check: Mapping[str, Any],
           at: str | None) -> dict[str, Any]:
    """A check's history entry with one stored result added: ``last_pass`` (newest pass)
    and ``not_passing`` (sorted stamps of non-passing results after it, the earliest and
    the newest ``HISTORY_STAMPS - 1`` kept). Idempotent and, but for the trimmed stamps,
    order-independent, so stored reports fold in any order and more than once; a stored
    ``blocked_since`` counts as a non-passing result at that stamp."""
    entry = entry or {}
    last = entry.get("last_pass") or ""
    stamps = list(entry.get("not_passing") or [])
    at = at if _stamp(at) else None
    if check.get("result") == "pass":
        if at and at > last:
            last, stamps = at, [s for s in stamps if s > at]
    elif check.get("result") in RESULTS and _not_passing(check):
        detail = check.get("detail") if isinstance(check.get("detail"), Mapping) else {}
        since = detail.get("blocked_since") if check.get("result") == "blocked" else None
        stamps = sorted({*stamps, *(s for s in (at, since) if _stamp(s) and s > last)})
        stamps = stamps[:1] + stamps[1:][-(HISTORY_STAMPS - 1):]
    return {"last_pass": last or None, "not_passing": stamps}


def fold_report(history: dict[str, dict[str, Any]], report: Mapping[str, Any] | None) -> None:
    """Fold every check of a stored report into ``history``, at its own ``checked_at`` or
    else the report's stamp; a daily set spans runs, so its check without ``checked_at``
    (origin unknown) folds with no stamp of its own."""
    report = report or {}
    fallback = None if report.get("cadence") == "daily" else report.get("generated_at")
    for check in report.get("checks") or []:
        if isinstance(check, Mapping) and isinstance(check.get("id"), str):
            history[check["id"]] = folded(history.get(check["id"]), check,
                                          check.get("checked_at") or fallback)


def load_history(store: Path | None, results_dir: Path | None) -> dict[str, dict[str, Any]]:
    """The blocked-age history: the persisted store, then every stored report in
    ``results_dir`` and its ``daily/`` dir (the the live-results branch results), so a lost
    store or an overwritten report never shortens an outage."""
    history: dict[str, dict[str, Any]] = {}
    try:
        value = json.loads(store.read_text(encoding="utf-8")) if store else None
    except (OSError, ValueError):
        value = None
    if isinstance(value, dict) and value.get("schema") == HISTORY_SCHEMA:
        for check_id, entry in (value.get("checks") or {}).items():
            if isinstance(entry, Mapping):
                history[check_id] = folded(None, {"result": "pass"}, entry.get("last_pass"))
                for stamp in entry.get("not_passing") or []:
                    history[check_id] = folded(history[check_id], {"result": "fail"}, stamp)
    if results_dir is not None:
        for path in sorted([*results_dir.glob("*.json"), *results_dir.glob("daily/*.json")]):
            fold_report(history, load_report(path))
    return history


def stamp_blocked(check: dict[str, Any], previous: Mapping[str, Any] | None,
                  now: datetime, history: Mapping[str, Mapping[str, Any]] | None = None
                  ) -> dict[str, Any]:
    """Give a blocked check ``blocked_since``: its first non-passing stored result after
    its last pass (``history`` and the previous report's same check; a run that did not
    run it changes nothing), else now; ``blocked_hours`` and ``red`` (more than 24 h)."""
    if check.get("result") != "blocked":
        return check
    entry = (history or {}).get(check.get("id"))
    for before in (previous or {}).get("checks") or []:
        if isinstance(before, Mapping) and before.get("id") == check.get("id"):
            entry = folded(entry, before, (previous or {}).get("generated_at"))
    stamps = [_stamp(s) for s in (entry or {}).get("not_passing") or []]
    since = min([s for s in stamps if s is not None] + [now])
    hours = round((now - since).total_seconds() / 3600, 2)
    check["detail"] = {**(check.get("detail") or {}),
                       "blocked_since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "blocked_hours": hours, "red": hours > BLOCKED_RED_HOURS}
    return check


def _check_tier(check: Mapping[str, Any]) -> str:
    """The check's tier; a check blocked by a capture gap counts in the capture tier (the
    lead's decision on originals_not_held: a persistent capture gap turns capture red)."""
    if check.get("result") == "blocked" and upstream_cause(check) == "capture_gap":
        return "capture"
    return check["tier"] if check.get("tier") in TIERS else spec_tier(None, str(check.get("id")))


def _check_cause(check: Mapping[str, Any]) -> str:
    """Why a check produced no measurement: its blocked cause, ``host_misconfiguration``
    for missing environment, else its result (``disabled``, ``error``, ``fail``)."""
    detail = check.get("detail") or {}
    if check.get("result") == "blocked":
        return upstream_cause(check)
    if check.get("result") == "skipped":
        return "disabled" if detail.get("disabled") else "host_misconfiguration"
    return str(check.get("result") or "unknown")


def measured_in_part(check: Mapping[str, Any]) -> bool:
    """A blocked check that ran and measured something: only its own ``blocked_upstream``
    parts are blocked and the rest met an ``expect_blocked`` that requires a positive
    minimum (``run_spec`` records ``blocked.measured``; scored, never a pass). A check
    whose parts all failed, whose ``expect_blocked`` asks for no positive quantity or whose
    ``blocked_when`` fired stays blocked."""
    detail = check.get("detail") or {}
    blocked = detail.get("blocked") if isinstance(detail.get("blocked"), Mapping) else {}
    return (check.get("result") == "blocked" and bool(blocked.get("parts"))
            and blocked.get("measured") is True and not detail.get("failures")
            and not detail.get("reason"))


def tier_summary(checks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per tier: result counts, the failing and blocked ids (with causes) and one
    ``status``: ``unmeasured`` (no check ran), ``red``, ``blocked`` (nothing measured: every
    check that ran is blocked whole), ``green`` (all passed), ``partial`` (some passed or
    were ``measured_in_part``); the capture tier says ``complete`` only when every passing
    check met its ``complete_when`` and ``incomplete`` otherwise (``completeness``).
    A Portal outage blocks parts, not tiers: ``measured_in_part`` names the blocked checks
    whose other parts were scored, ``blocked_parts`` counts their blocked parts by cause,
    and ``answers`` sums the tier's eval answer blocks with blocked, wrong, abstained
    and correct kept apart."""
    out: dict[str, Any] = {}
    for tier in TIERS:
        group = [c for c in checks if _check_tier(c) == tier]
        entry: dict[str, Any] = {"checks": len(group),
                                 **{r: sum(c["result"] == r for c in group) for r in RESULTS}}
        red = failing_ids(group)
        blocked = {c["id"]: _check_cause(c) for c in group if c["result"] == "blocked"}
        if red:
            entry["failing"] = red
        if blocked:
            entry["blocked_ids"] = dict(sorted(blocked.items()))
        in_part = sorted(c["id"] for c in group if measured_in_part(c))
        if in_part:
            entry["measured_in_part"] = in_part
            parts: dict[str, int] = {}
            for check in group:
                if check["id"] in in_part:
                    for part in check["detail"]["blocked"]["parts"]:
                        cause = str(part.get("cause"))
                        parts[cause] = parts.get(cause, 0) + 1
            entry["blocked_parts"] = dict(sorted(parts.items()))
        answers = {key: 0 for key in ANSWER_COUNTS}
        blocks = [b for c in group if c.get("result") in ("pass", "fail", "blocked")
                  and (b := _answer_block(c, "answer")) is not None]
        for block in blocks:
            _add_counts(answers, block)
        if blocks:
            entry["answers"] = {**answers, **_rates(answers)}
        passed = [c for c in group if c["result"] == "pass"]
        if tier == "capture":
            flags = {c["id"]: (c.get("detail") or {}).get("complete") for c in passed}
            entry["completeness"] = {
                "complete": sum(v is True for v in flags.values()),
                "incomplete": sorted(k for k, v in flags.items() if v is False),
                "unknown": sorted(k for k, v in flags.items() if v is None)}
        if red:
            entry["status"] = "red"
        elif blocked and not passed and not in_part:
            entry["status"] = "blocked"
        elif group and len(passed) == len(group):
            entry["status"] = ("green" if tier != "capture" else "complete"
                               if entry["completeness"]["complete"] == len(passed)
                               else "incomplete")
        elif passed or in_part:
            entry["status"] = "partial"
        else:
            entry["status"] = "unmeasured"
        out[tier] = entry
    return out


def _answer_block(check: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    fields = (check.get("detail") or {}).get("fields") or {}
    block = fields.get(f"eval.{name}")
    if not isinstance(block, Mapping):
        nested = fields.get("eval") if isinstance(fields.get("eval"), Mapping) else {}
        block = nested.get(name)
    return block if isinstance(block, Mapping) else None


def _add_counts(total: dict[str, int], block: Mapping[str, Any]) -> None:
    for key in ANSWER_COUNTS:
        value = block.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            total[key] += value


def _rates(total: Mapping[str, int]) -> dict[str, str]:
    """B1's one answer rate as counts (``evals.score.answer_rates``): accuracy correct/answered, coverage answered over the
    items that ran (sampled less blocked upstream); an abstention is not answered."""
    answered = total["correct"] + total["wrong"]
    return {"accuracy": f"{total['correct']}/{answered}",
            "coverage": f"{answered}/{total['sampled'] - total['blocked_upstream']}"}


def answer_summary(checks: Sequence[Mapping[str, Any]], now: datetime,
                   previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """Answer correctness over the answer-source evals (an ``eval.answer`` block, or
    ``answer_source`` in the spec): counts summed over the evals that ran, root lookup
    (``find_root``) apart, item causes of what was not scored, and each eval's result or
    the cause it did not measure. ``last_scored_at`` and ``unmeasured_since`` carry over from
    the previous report, so ``status`` names how long nothing has been scored."""
    answer = {key: 0 for key in ANSWER_COUNTS}
    roots = {key: 0 for key in ANSWER_COUNTS}
    sources: dict[str, Any] = {}
    item_causes: dict[str, int] = {}
    for check in sorted(checks, key=lambda c: str(c.get("id"))):
        block = _answer_block(check, "answer")
        if not str(check.get("id")).startswith("eval.") or (
                block is None and not check.get("answer_source")):
            continue
        # a check blocked in part still scored its other items
        if block is not None and check.get("result") in ("pass", "fail", "blocked"):
            _add_counts(answer, block)
            _add_counts(roots, _answer_block(check, "find_root") or {})
            for cause, count in (block.get("causes") or {}).items():
                if isinstance(count, int) and count > 0:
                    item_causes[str(cause)] = item_causes.get(str(cause), 0) + count
            sources[check["id"]] = {"result": check["result"], "scored": block.get("scored")}
        else:
            sources[check["id"]] = {"result": check.get("result"), "cause": _check_cause(check)}
    before = (previous or {}).get("answer_correctness")
    before = before if isinstance(before, Mapping) else {}
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    out: dict[str, Any] = {**answer, **_rates(answer), "find_root": {**roots, **_rates(roots)},
                           "causes": dict(sorted(item_causes.items())), "sources": sources}
    if answer["scored"]:
        out.update(status="measured", last_scored_at=stamp)
        return out
    since = before.get("last_scored_at") or before.get("unmeasured_since") or stamp
    causes = sorted({v["cause"] for v in sources.values() if "cause" in v} | set(item_causes))
    out.update(status=f"unmeasured since {since} (blocked: "
                      f"{', '.join(causes) or 'no_answer_eval'})", unmeasured_since=since,
               **({"last_scored_at": before["last_scored_at"]}
                  if before.get("last_scored_at") else {}))
    return out


def unmeasured_budget(check: Mapping[str, Any]) -> bool:
    """A check settled without running because it fits no command's budget."""
    detail = check.get("detail") if isinstance(check.get("detail"), Mapping) else {}
    return check.get("result") == "skipped" and detail.get("unmeasured") == "budget"


def summarize_checks(checks: Sequence[Mapping[str, Any]], now: datetime,
                     previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """The report keys derived from the checks: disjoint result buckets, failing and
    red-blocked ids, the lifted eval blocks, the tiers and answer correctness."""
    summary = {result: 0 for result in RESULTS}
    for check in checks:
        summary[check["result"]] += 1
    budget = sum(1 for c in checks if unmeasured_budget(c))
    if budget:  # its own bucket, out of skipped: refused for budget, never measured
        summary["skipped"] -= budget
        summary["unmeasured_budget"] = budget
    out: dict[str, Any] = {"summary": summary}
    if failing_ids(checks):
        out["failing"] = failing_ids(checks)
    red = sorted(c["id"] for c in checks if _is_red(c))
    if red:
        out["blocked_red"] = red
    evals = eval_blocks(checks)
    if evals:
        out["eval"] = evals
    out["tiers"] = tier_summary(checks)
    out["answer_correctness"] = answer_summary(checks, now, previous)
    return out


DERIVED_KEYS = ("summary", "failing", "blocked_red", "eval", "tiers", "answer_correctness")


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` in one step so a killed run never leaves a torn report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        temp.unlink(missing_ok=True)
        raise


def load_report(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("schema") == SCHEMA else None


def _publishable(report: Mapping[str, Any] | None) -> bool:
    """A complete, finished hourly report with a full main SHA and a readable stamp."""
    return (isinstance(report, Mapping) and report.get("complete") is True
            and report.get("stop_reason") == "finished"
            and report.get("cadence") in (DEFAULT_CADENCE, "all")
            and isinstance(report.get("main_sha"), str)
            and FULL_SHA.fullmatch(report["main_sha"]) is not None
            and _stamp(report.get("generated_at")) is not None)


def with_earlier(check: Mapping[str, Any], before: Mapping[str, Any] | None
                 ) -> Mapping[str, Any]:
    """``check`` replacing ``before``, the set's check of the same id from before this run:
    a re-run (a session producer run again for its consumer) keeps the run it replaces in
    ``earlier_runs`` (its origin and result, newest first, at most ``EARLIER_RUNS``), so
    the run a consumer's ``producer_run`` cites stays in the set."""
    if not isinstance(before, Mapping):
        return check
    cause = red_cause(before)
    earlier = [{**{key: before[key] for key in EARLIER_KEYS if key in before},
                **({"cause": cause} if cause is not None else {})},
               *(run for run in before.get("earlier_runs") or [] if isinstance(run, Mapping))]
    return {**check, "earlier_runs": earlier[:EARLIER_RUNS]}


def red_cause(check: Any) -> str | None:
    """Why a ``fail`` failed, comparably across runs: ``timed_out``, else its detail's
    ``cause``, else its expectation failures (sorted, joined); an ``earlier_runs`` entry keeps
    it as ``cause``. None for any other result, or a fail that names none."""
    if not isinstance(check, Mapping) or check.get("result") != "fail":
        return None
    detail = check.get("detail")
    if not isinstance(detail, Mapping):  # an earlier_runs entry: the cause it kept
        kept = check.get("cause")
        return kept if isinstance(kept, str) and kept else None
    if detail.get("timed_out") is True:
        return "timed_out"
    if isinstance(detail.get("cause"), str) and detail["cause"]:
        return detail["cause"]
    failures = detail.get("failures")
    if isinstance(failures, list) and failures and all(isinstance(f, str) for f in failures):
        return "; ".join(sorted(failures))[:MAX_STRING_CHARS]
    return None


def known_red(check: Any, digest: str) -> dict[str, Any] | None:
    """A capture check's known-red record: the check and its newest earlier runs, the last
    ``KNOWN_RED_RUNS`` records, are each a ``fail`` for one cause (``red_cause``) under the
    spec digest ``digest`` (the spec as loaded now). Fewer records, a record without a digest
    or cause, or any other digest or cause: None (it runs)."""
    if not isinstance(check, Mapping):
        return None
    runs = [check, *(r for r in check.get("earlier_runs") or [] if isinstance(r, Mapping))]
    runs = runs[:KNOWN_RED_RUNS]
    causes = {red_cause(r) for r in runs}
    if (len(runs) < KNOWN_RED_RUNS or len(causes) != 1 or None in causes
            or any(r.get("spec_digest") != digest for r in runs)):
        return None
    return {"skipped": "known_red", "cause": causes.pop(), "spec_digest": digest,
            "since": min(str(r.get("checked_at") or "") for r in runs) or None}


def resume_target(path: Path, sha: str | None) -> tuple[Path, dict[str, Any] | None, bool]:
    """``--resume-daily PATH``: the file the run writes (``<sha>.json`` beside PATH, PATH
    itself when ``sha`` is not a full SHA), the newest daily report beside PATH (latest.json
    aside; by ``started_at``, else ``generated_at``, then its last write) and whether the run
    starts a new set: that report is complete, or there is none."""
    newest: tuple[tuple[str, str, str], dict[str, Any]] | None = None
    for candidate in path.parent.glob("*.json"):
        report = load_report(candidate) if candidate.name != "latest.json" else None
        if report is None or report.get("cadence") != "daily":
            continue
        key = (str(report.get("started_at") or report.get("generated_at") or ""),
               str(report.get("generated_at") or ""), candidate.name)
        if newest is None or key > newest[0]:
            newest = (key, report)
    held = newest[1] if newest is not None else None
    target = path.with_name(f"{sha}.json") if sha and FULL_SHA.fullmatch(sha) else path
    return target, held, held is None or held.get("complete") is not False


def spec_digest(spec: Mapping[str, Any]) -> str:
    """A spec's content digest (canonical JSON, so a formatting-only edit is no change)."""
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


def _instant(value: Any) -> datetime | None:
    """An ISO-8601 UTC stamp (``Z`` or an offset; naive reads as UTC), else None."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
                ).astimezone(timezone.utc)
    except (ValueError, OverflowError):  # 0001-01-01T00:00:00+01:00 has no UTC instant
        return None


def marker_live(marker: Any, mtime: datetime | None, now: datetime) -> bool:
    """A run marker is live while ``updated_at`` is under 2 h old; a legacy marker without
    it, while ``started_at`` is; an unreadable one, while the file is."""
    value = marker if isinstance(marker, Mapping) else {}
    stamp = _instant(value.get("updated_at")) or _instant(value.get("started_at")) or mtime
    return stamp is None or now - stamp < timedelta(hours=LOCK_LIVE_HOURS)


def daily_store(directory: Path) -> str:
    """The store a daily set in ``directory`` belongs to: the git remote of its repository
    and the directory's path in it (two checkouts of the results branch share it), else the
    directory's own resolved path."""
    near = next((p for p in (directory, *directory.parents) if p.is_dir()), directory)
    top = _git(near, "rev-parse", "--show-toplevel")
    remote = _git(near, "config", "--get", "remote.origin.url") if top else None
    with contextlib.suppress(ValueError, OSError):
        if top and remote:
            return f"{remote}:{directory.resolve().relative_to(Path(top).resolve())}"
    return str(directory.resolve())


def hold_daily(directory: Path, marker: Mapping[str, Any]) -> tuple[int | None, str | None]:
    """Take the host-wide flock of the daily set in ``directory`` (module docstring, "One
    daily attempt at a time") and write ``marker`` into it: the descriptor to close when the
    run ends, else the refusal reason naming the attempt that holds it."""
    name = hashlib.sha256(daily_store(directory).encode()).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / f"{DAILY_LOCK_PREFIX}{name}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            held = json.loads(os.read(fd, 4096) or b"null")
        except (OSError, ValueError):
            held = None
        os.close(fd)
        held = held if isinstance(held, Mapping) else {}
        return None, (f"daily attempt of session {held.get('session', 'unreadable')} (pid "
                      f"{held.get('pid')}, {held.get('file')}) since {held.get('started_at')}")
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(dict(marker)).encode())
    return fd, None


# Keys the verifier writes into a lifted eval block; a check's own block cannot set them.
EVAL_VERIFIER_KEYS = ("result", "spec", "exit_code", "failures")


def eval_blocks(checks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Top-level ``eval``: each ``eval.*`` check's recorded ``eval`` block (already reduced
    to counts and enums by ``_recorded`` and scrubbed), keyed by check id. The verifier's
    own keys (``EVAL_VERIFIER_KEYS``: the check's result, spec, exit code and failures)
    come from the check entry and always override what the block says."""
    out: dict[str, Any] = {}
    for check in checks:
        check_id = str(check.get("id") or "")
        detail = check.get("detail") or {}
        fields = detail.get("fields") or {}
        block = fields.get("eval") if isinstance(fields, Mapping) else None
        if check_id.startswith("eval.") and isinstance(block, Mapping):
            own = {"result": check.get("result"), "spec": check.get("spec"),
                   "exit_code": check.get("exit_code"),
                   "failures": list(detail.get("failures") or [])}
            out[check_id] = {**{k: v for k, v in block.items()
                                if k not in EVAL_VERIFIER_KEYS}, **own}
    return out
