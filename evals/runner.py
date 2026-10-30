"""Run eval items through an agent on the case host, record replayable transcripts, score.

evaluation is the progress metric and Q7 (no paid model API:
batch eval runs use Claude Code sessions). Items are ``plx-eval-item/v1``
(``evals/item.schema.json``, validated with ``ontology/json_contract.py``).

**Agent.** ``ClaudeCodeAgent`` runs one headless Claude Code session per run
(``claude -p ... --output-format stream-json``) whose only tools are the Workbench case
host's, served over MCP stdio (``python3 -m operating.mcp_server`` as the item's actor;
``--tools ""`` disables Claude Code's built-in tools, ``--strict-mcp-config`` ignores
any other MCP server, ``--setting-sources ""`` ignores user/project settings, the
session runs in an empty temporary directory). Only the host's read-only tools are
allowed (``--case-tools`` adds tools that write the run's disposable case store, never
an action tool); the rest are refused twice: Claude Code's ``--disallowedTools`` and the
**tool gate** (``serve_gate``, ``python3 -m operating.agent_session gate``), a stdio proxy in front of
the host that lists only the allowed tools and answers any other ``tools/call`` (an action
or MC write such as ``plx.act``, a case-store writer, an unknown name) with a tool error
without forwarding it. Each run records ``usage.refused_tool_calls`` (calls to a tool
outside the allowed set, however refused) and ``agent.gate_refused`` (calls the gate
stopped). Each ``tool_call`` event of a session also carries the gate's wall-clock
times (``started_at``, ``ended_at``: UTC ISO-8601; ``elapsed_ms``): when the gate passed
the call to the host (or refused it) and when the answer came back. A call still waiting
when the session timed out has ``ended_at`` None. Timing only: no score reads it.
``--max-turns`` bounds a session. The environment is an allowlist
(``operating.agent_environment``): no credential, DSN, ``ANTHROPIC_API_KEY`` or
paid-provider switch (no paid API: the session uses the operator's Claude Code login).

**Isolation.** Every run gets its own ``CaseHost`` over a disposable state directory
seeded with a read-only copy of the source mirror: no run sees another run's cases,
and the employee's real case store is never written. Any other MCP client
fits by implementing ``Agent.run(prompt, host) -> AgentRun``; ``ScriptedAgent`` drives
the host in-process from a fixed script (tests, smoke runs).

**Pre-submit check.** ``presubmit_check(draft, host)`` returns the codes the scorer's own
functions raise on a draft answer (``answer_problems``, ``calculation_problem``, the claim
gate: ``citations_beside_calculation``, ``claim_uncited_number``, ...), read-only and
against no item, within a re-read budget (``PRESUBMIT_MAX_READS``). A ``ScriptedAgent``
reaches it as ``PRESUBMIT_TOOL`` (an ``answer_check`` event, never a tool call); the
Claude Code agent does not (its tools are the host's MCP catalog only; the prompt states
the rules).

**Final-answer gate.** The harness never rewrites an answer's content: the final reply
is what the Workbench's answer gate passed (``gate_final_reply``, applied by ``record``
for an agent that ``gates_final_reply``): a value the gate refused or never checked is
withheld and named, a reply stating more is replaced by the passed values and the
withheld list; the agent's own message stays in ``agent.final_reply`` (measure what
ships, the acceptance bar). The Claude Code agent reaches the
Workbench's own final-answer gate the way an employee's MCP session does
(``ANSWER_GATE_RULE``: ``plx.case_get`` answer mode, ``operating.answer_gate.gate_answer``,
the function the console's Ask runs: a refused claim is dropped, repaired once).
``agent.answer_gate`` records what that gate returned in the session
(``answer_gate_record``: submissions, outcomes, passed and refused claim counts, whether a
repair followed), from the run's own tool results.

**Transcript** (``plx-eval-transcript/v1``): the prompt (question text and answer
contract only; never the known outcome, oracle or label), the host's tool catalog with
read-only annotations, every agent tool call with its arguments and result, the final
message, the parsed answer, the harness evidence (each cited handle re-read with
``plx.source_citation`` as the item's actor after the agent finished), usage (tokens,
estimated cost, turns, tool calls) and ``timing`` (wall time from the prompt to the final
message; in-process agents stamp each tool call's ``t_ms``, a Claude Code stream carries
no per-call time; ``evals.measure``). ``transcript_sha256`` seals it; ``replay`` refuses
a transcript whose seal does not match and re-scores it with ``evals.score``, which is a
pure function of item and transcript, so the score is identical. Replay
exits non-zero unless every transcript had a stored score and all are identical.
Known limit: the seal is an unkeyed SHA-256, which detects accidental edits and
truncation, not a deliberate edit that also recomputes it (a local tampering threat
outside this model; an HMAC with a key held only in the credentialed environment
would close it).

**Splits.** Dev items live in the repo. Held-out items (``split: held_out``) load only
in the credentialed environment (``PLX_WORKBENCH_API_KEY`` set) from a directory under
``PLX_EVAL_HELD_OUT_DIR`` outside any git work tree, and their transcripts and scores
are written only there. For held-out items the runner never prints an item, a label, a
question, an answer or a per-item score: only per-family aggregates. Errors about a
held-out item name a code and a JSON path, never a value. ``prompt`` refuses held-out.

A held-out load also refuses any held-out item that shares a source document, root or
identifier with a committed dev or exposed item (``evals.partition``): held-out items are
genuinely unseen.

**Admission.** ``load_items`` returns only the items ``evals.admission.admissible`` admits;
each refused item is listed on the result (``refused``: id and reason codes) and in the
printed summary (held out: reason counts only), never silently scored or dropped.

**Eval directories** hold items and declared files only (``EVAL_FILE_FORMATS``,
``UNDECLARED_EVAL_FILES``); an unreadable or unknown JSON file is refused, never skipped.

**Partitions.** Exposed real cases (``evals/exposed``: AO, PO and F7-F9 cases a builder has
seen) are dev items labelled ``dev/exposed``: their id ends in ``-exposed``, their
directory's exposed manifest lists exactly them, a run never mixes them with synthetic dev
items and they are never held out. Results report correct / wrong / blocked (read gap
apart from the rest) / justified-abstention with coverage beside accuracy
(``evals.score.outcome_summary``). Read-gap runs above ``evals.score.DRIFT_TRIPWIRE_SHARE``
of the run fail it as they fail the dev baseline (``fail_reasons`` ``blocked_tripwire``,
``evals.score.fail_reasons``; ``run`` exits 3).
Running them through ``ClaudeCodeAgent`` spends model usage, so it is on demand only, never
in tests or the gate (tests use a stub agent). The state directory must hold the pinned
originals and mail the items cite; items that cite no mirror row need no mirror and get no
run pin::

    python3 -m evals.runner run --items evals/exposed --split dev --runs 1 \\
        --state-dir "$PLX_STATE" --source-mirror-audience "$PLX_AUDIENCE" \\
        --principal <employee principal> --out <dir outside the repo> \\
        --evidence-label live-captures

**Run pin**. Oracle items pin the
oracle's own read digests, which no tool returns. Before the agent runs, ``make_run_pin``
pins, for each relation the item's expected citations name, the first completed mirror
capture at or after ``as_known_at.time``; the prompt states the mirror generation and the
time those captures had completed; the scorer reads the pinned generation (and the
capture limit per relation) from the transcript's ``run_pin``. A drift check compares each
expected citation's field in the pinned mirror version with the oracle's read (field
digest only); a mismatch, a missing row or an unpinnable relation makes the item
``pin_drift``: excluded from pass rates, counted, never an agent failure.

**k runs and paraphrases.** ``--runs k`` repeats each phrasing; ``--paraphrases`` adds
each ``question.paraphrases`` text as its own phrasing. ``evals.score.aggregate``
reports pass^k over the standard phrasing and paraphrase pass rate and consistency
separately (09 R7 e).

**Live check.** ``eval.real_agent`` (``scripts/eval_real_agent.py``, daily, tier
``real_agent``) runs this agent at k=1 over the exposed F7-F9 items and a pinned fixture
sample. Larger runs (the whole dev set, k > 1) are on demand in the credentialed
environment::

    python3 -m evals.runner run --items evals/dev --split dev --runs 1 \\
        --state-dir "$PLX_STATE" --source-mirror-audience "$PLX_AUDIENCE" \\
        --tenant plx --principal <employee principal> --out <dir outside the repo> \\
        --evidence-label live-captures
    python3 -m evals.runner replay --items evals/dev --transcripts <that dir>

Evidence label of a run (``--evidence-label``, required, recorded in each transcript):
``live-captures`` only when the host's state holds live L1 captures and the agent is a
real Claude session; a ``ScriptedAgent`` or fixture host run is ``isolated-fixture``.
Neither is ``live-verified``, which only the live verifier grants.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from urllib.parse import unquote

from evals import measure as measuring
from evals import partition as partitions
from evals.admission import admissible
from operating.agent_environment import agent_environment
from operating.answer_gate import ANSWER_BACKING, DOCUMENT_READ_RULE
from evals import score as scoring
from operating.agent_session import (  # noqa: F401 - the shared agent session path
    MAX_RESULT_CHARS, ANSWER_GATE_RULE, ANSWER_GATE_TOOL, answer_gate_record,
    FINAL_REPLY_FORMAT, WITHHELD_REASON, CERTAINTY, ANSWER_KEYS, NOTE_REASON,
    _gate_results, _said, one_gap_per_value, gate_final_reply, AgentRun, Agent,
    _bounded, _tool_calls, _normal, parse_stream_json, mcp_tool_name, PROPOSAL_TOOL,
    CASE_WRITER_PREFIX, allowed_tools, GATE_REFUSAL, serve_gate, Spawn, MCP_STARTUP_MS,
    _KILLED_DRAIN_SECONDS, _spawn, streaming_spawn, _kill_group, _wall,
    attach_call_timing, ClaudeCodeAgent, gated_run, task_prompt, ask_prompt)

REPO_ROOT = Path(__file__).resolve().parents[1]
ITEM_SCHEMA_PATH = REPO_ROOT / "evals" / "item.schema.json"
ITEM_FORMAT = "plx-eval-item/v1"
RUNNER_VERSION = "plx-eval-runner/1"
HELD_OUT_DIR_ENV = "PLX_EVAL_HELD_OUT_DIR"
CREDENTIAL_MARKER_ENV = "PLX_WORKBENCH_API_KEY"
EVIDENCE_LABELS = ("isolated-fixture", "injected-transport", "live-captures")
MAX_EVIDENCE_READS = 200


# Every JSON file under an eval directory is an item or one of these, declared by its
# "format" (or "schema") value; any other file is refused, never skipped.
EVAL_FILE_FORMATS = {
    "plx-eval-dev-manifest/v1": "dev set manifest (scripts/eval_oracle_dev.py)",
    "plx-eval-heldout-manifest/v1": "held-out manifest (scripts/eval_oracle_dev.py)",
    "plx-eval-exposed-manifest/v1": "exposed real-case manifest: lists its items",
    "plx-eval-exposed-sources/v1": "identity of the originals exposed items cite",
    "plx-sharepoint-originals-expected/v1": "expected identity of a SharePoint folder's "
                                            "originals (ao-originals check)",
    "plx-eval-case-cards/v1": "employee-style case cards that items are converted from",
    "plx-eval-case-card-proposals/v1": "proposed case cards awaiting conversion to items "
                                       "(evals/dev/asset-tracker)",
    "plx-case-ontology-gaps/v1": "missing objects/links/readers a case recorded, each with "
                                 "its source or Dot question",
    "plx-source-extraction-eval-draft/v1": "page/span gold of converted extraction items",
    "plx-use-case-answer-review/v1": "per-card failure classes of a live use-case answers "
                                     "run (evals/dev/rro); lists no items",
    "plx-eval-objectives/v1": "free-form business objectives, each a root and a goal "
                              "(scripts/eval_objective_agent.py); lists no items",
}
# Files whose shape a strict reader owns, so they cannot carry a declaration: by name.
UNDECLARED_EVAL_FILES = {
    "packaging_stage_map_cases.json": "case sections for the packaging stage map "
                                      "(strict top-level keys)",
    "frozen.json": "a frozen held-out set's digest list (scripts/eval_oracle_dev.py)",
}
EXPOSED_MANIFEST_FORMAT = "plx-eval-exposed-manifest/v1"
VISIBLE_ITEM_DIRS = (REPO_ROOT / "evals" / "dev", REPO_ROOT / "evals" / "exposed")


class RunnerError(RuntimeError):
    """A refused run: the message is a code (and at most a JSON path), never a value."""


# --------------------------------------------------------------------------- items

def _schema() -> dict[str, Any]:
    return json.loads(ITEM_SCHEMA_PATH.read_text(encoding="utf-8"))


def _git_work_tree(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def held_out_root() -> Path:
    """The held-out directory, only in the credentialed environment (checked by name)."""
    if not os.environ.get(CREDENTIAL_MARKER_ENV):
        raise RunnerError("held_out_requires_credentialed_environment")
    root = os.environ.get(HELD_OUT_DIR_ENV)
    if not root:
        raise RunnerError(f"held_out_requires_{HELD_OUT_DIR_ENV}")
    resolved = Path(root).resolve()
    if _git_work_tree(resolved) is not None:
        raise RunnerError("held_out_directory_inside_a_git_work_tree")
    return resolved


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def validate_item(item: Any, *, schema: Mapping[str, Any] | None = None) -> list[str]:
    """Schema errors of ``item``, reduced to JSON paths when it is (or claims to be) held
    out, so no label value is ever echoed."""
    from ontology.json_contract import validate
    errors = validate(item, schema or _schema())
    if not isinstance(item, Mapping) or item.get("split") != "dev" or \
            not str(item.get("id", "")).startswith("dev-"):
        errors = sorted({error.split(":", 1)[0] for error in errors})
    return errors


def _eval_file(data: Any, name: str, where: str) -> set[str]:
    """Check one non-item file of an eval directory; return the item ids it lists as
    exposed (an exposed manifest), else nothing."""
    declared = (data.get("format") or data.get("schema")) if isinstance(data, Mapping) \
        else None
    if declared is None:
        if name in UNDECLARED_EVAL_FILES and isinstance(data, Mapping):
            return set()
        raise RunnerError(f"eval_file_undeclared:{where}")
    if declared not in EVAL_FILE_FORMATS:
        raise RunnerError(f"eval_file_format_unknown:{where}")
    if declared == EXPOSED_MANIFEST_FORMAT:
        rows = data.get("items")
        if not isinstance(rows, list) or not all(
                isinstance(row, Mapping) and isinstance(row.get("id"), str) for row in rows):
            raise RunnerError(f"eval_file_invalid:{where}")
        return {row["id"] for row in rows}
    if declared == "plx-eval-case-cards/v1":
        cards = (data.get("real_example") or {}).get("cards")
        ids = [card.get("id") for card in cards or [] if isinstance(card, Mapping)]
        if not isinstance(cards, list) or not cards or len(ids) != len(cards) or \
                not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids):
            raise RunnerError(f"eval_file_invalid:{where}")
    return set()


class LoadedItems(list):
    """``load_items``' admitted items; ``refused``: ``{"id", "reasons"}`` of every valid item
    the admission rule kept out of scoring."""

    def __init__(self, items: Iterable[dict[str, Any]] = (),
                 refused: Iterable[dict[str, Any]] = ()) -> None:
        super().__init__(items)
        self.refused = list(refused)


def load_items(directory: Path, *, split: str, check_unseen: bool = True, admit: bool = True
               ) -> LoadedItems:
    """Every admitted ``*.json`` item under ``directory`` (recursively) of ``split``.

    Admission (``evals.admission.admissible``) runs after every check below; a refused item
    is listed in ``refused`` with its reason codes, never returned for scoring.
    ``admit=False`` returns every valid item (what a builder has seen, refused or not).

    Every other JSON file must be a declared eval file (``EVAL_FILE_FORMATS``,
    ``UNDECLARED_EVAL_FILES``): an unreadable or unknown file is refused, never skipped.
    Exposed items (``score.EXPOSED_SUFFIX``) are exactly the items their directory's exposed
    manifest lists, and never held out.

    A held-out load needs ``held_out_root()`` and a directory inside it; a dev load
    refuses any held-out item it meets (a committed held-out item is a breach) and a
    held-out load refuses dev items (one split per run). A held-out item that shares a
    source document, root or identifier with a committed dev or exposed item is refused
    (``evals.partition``; ``check_unseen``)."""
    if split not in ("dev", "held_out"):
        raise RunnerError("split_must_be_dev_or_held_out")
    directory = directory.resolve()
    if split == "held_out" and not _inside(directory, held_out_root()):
        raise RunnerError(f"held_out_items_must_live_under_{HELD_OUT_DIR_ENV}")
    schema = _schema()
    items, seen, listed = [], set(), set()
    for path in sorted(directory.rglob("*.json")):
        where = path.name if split == "dev" else "<held-out file>"
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RunnerError("item_unreadable" + (f":{path.name}" if split == "dev"
                                                   else "")) from None
        if not isinstance(item, Mapping) or item.get("format") != ITEM_FORMAT:
            listed |= _eval_file(item, path.name, where)
            continue
        item_split = item.get("split")
        if item_split != split:
            raise RunnerError("held_out_item_in_dev_directory" if item_split == "held_out"
                              else "item_of_another_split")
        errors = validate_item(item, schema=schema)
        where = path.name if split == "dev" else "<held-out item>"
        if errors:
            raise RunnerError(f"item_invalid:{where}:{errors[0]}")
        if scoring.uncited_expected(item):  # a value read from a pinned original cites it
            raise RunnerError(f"item_invalid:{where}:expected_value_uncited")
        if item["id"] in seen:
            raise RunnerError("duplicate_item_id" + (f":{item['id']}" if split == "dev"
                                                     else ""))
        seen.add(item["id"])
        items.append(dict(item))
    exposed = {item["id"] for item in items if scoring.partition_of(item) == "exposed"}
    if split == "held_out" and (exposed or listed):
        raise RunnerError("exposed_item_in_held_out_directory")
    if exposed != listed:
        raise RunnerError("exposed_items_differ_from_manifest:" +
                          ",".join(sorted(exposed ^ listed)))
    if split == "held_out" and check_unseen:
        visible = [item for root in VISIBLE_ITEM_DIRS if root.is_dir()
                   for item in load_items(root, split="dev", admit=False)] + visible_cards()
        found = partitions.conflicts(items, visible)
        if found:  # kinds and counts only: never a held-out value or id
            kinds = sorted({kind for _, kind, _ in found})
            raise RunnerError(f"held_out_overlaps_visible:{','.join(kinds)}:"
                              f"{len({index for index, _, _ in found})}")
    if not admit:
        return LoadedItems(items)
    admitted, refused = [], []
    for item in items:
        reasons = admissible(item)
        if reasons:
            refused.append({"id": item["id"], "reasons": reasons})
        else:
            admitted.append(item)
    return LoadedItems(admitted, refused)


def refused_summary(refused: Sequence[Mapping[str, Any]], split: str) -> Any:
    """The refused items as printed: dev by id with reasons; held out by reason count only
    (never a held-out id)."""
    if split == "dev":
        return sorted(({"id": r["id"], "reasons": list(r["reasons"])} for r in refused),
                      key=lambda r: r["id"])
    counts: dict[str, int] = {}
    for entry in refused:
        for reason in entry["reasons"]:
            counts[reason] = counts.get(reason, 0) + 1
    return {"items": len(refused), "reasons": dict(sorted(counts.items()))}


def visible_cards() -> list[dict[str, Any]]:
    """Every case card of a committed eval directory (``plx-eval-case-cards/v1``), as an
    item-shaped question for ``evals.partition``: an exposed card is developer-visible whether
    or not it was converted to an item (DEV-005 was excluded, not unseen)."""
    cards = []
    for root in VISIBLE_ITEM_DIRS:
        for path in sorted(root.rglob("*.json")) if root.is_dir() else ():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, Mapping) or data.get("format") != "plx-eval-case-cards/v1":
                continue
            for card in (data.get("real_example") or {}).get("cards") or []:
                cards.append({"id": f"card:{card['id']}",
                              "question": {"text": card.get("question"),
                                           "paraphrases": [card.get("title")]}})
    return cards


# --------------------------------------------------------------------------- prompt

# ``as_known_at.l1_generations`` keys that pin no capture time:
# a case card set's digest
# (``case_cards``) and one original's bytes (``pinned_original``). Any other key is a
# capture generation, and the item's time is stated as its pin.
UNTIMED_GENERATIONS = frozenset({"case_cards", "pinned_original"})


def time_pinned(item: Mapping[str, Any]) -> bool:
    """Whether the item's ``as_known_at.time`` is a time the question is asked at: true
    unless every generation it pins is one of ``UNTIMED_GENERATIONS`` (an item with no
    generation, or an unreadable pin, stays time-pinned)."""
    pin = item.get("as_known_at")
    generations = pin.get("l1_generations") if isinstance(pin, Mapping) else None
    return not (isinstance(generations, Mapping) and generations
                and set(generations) <= UNTIMED_GENERATIONS)


def build_prompt(item: Mapping[str, Any], question: str,
                 run_pin: Mapping[str, Any] | None = None) -> str:
    """The agent's whole task: the question, the as-known-at pin (none for an item that
    is not time-pinned, ``time_pinned``) and the answer contract.
    It carries no known outcome, oracle, label, family or root key. With a run pin
    (``make_run_pin``) the pin is the mirror's: its generation and the time by which the
    first captures at or after the item's time had completed (never the pinned relations,
    which would hint at where the answer is)."""
    defs = _schema()["$defs"]
    roles = ", ".join(defs["quantity_role"]["enum"])
    gaps = ", ".join(defs["gap_state"]["enum"])
    if run_pin is not None:
        when = run_pin.get("known_at") or item["as_known_at"]["time"]
        pinned = (f"Answer as known at {when} (capture generations: "
                  f"portal_api={run_pin['mirror_generation']}); cite rows of those "
                  "generations.")
    elif not time_pinned(item):
        # Pinned only by a card set's digest or one original's bytes: the item's time is
        # when its card was written, not a time the question asks about, so stating it
        # would send the agent to captures older than any held.
        pinned = ("This question carries no time pin: answer from the sources as they "
                  "stand now.")
    else:
        pin = item["as_known_at"]
        generations = ", ".join(f"{source}={generation}" for source, generation in
                                sorted(pin["l1_generations"].items()))
        pinned = (f"Answer as known at {pin['time']} (capture generations: {generations}); "
                  "cite rows of those generations.")
    return task_prompt(question, pinned, roles, gaps)


# --------------------------------------------------------------------------- hosts

class Host(Protocol):
    actor: dict[str, Any]

    def tools(self) -> list[dict[str, Any]]: ...

    def call(self, name: str, arguments: dict[str, Any]) -> Any: ...

    def read(self, name: str, arguments: dict[str, Any]) -> Any: ...

    def mcp_server(self) -> dict[str, Any]: ...


class CaseHost:
    """The Workbench case host as one employee for one run: in-process for the harness
    reads and the tool catalog, and as an MCP stdio command for an external agent.

    Each host owns a **disposable state directory**: the source mirror of
    ``source_state_dir`` (``case-runtime/source-mirror.sqlite``) is copied into a fresh
    temporary directory with the SQLite backup API (the source is opened read-only),
    and the case store, originals and inbox there start empty. Runs therefore never see
    each other's cases (pass^k and paraphrase runs are independent) and never write the
    employee's real case list; ``close`` deletes the directory. No source client: nothing
    is fetched. Attached originals of the source state are not copied (known limit).

    Session identity of the stdio host (``operating.session_identity``, decision 7): the
    host has no default principal. ``fixture=True`` (fixture or injected-transport
    state) labels the disposable copy fixture and launches ``--fixture --principal ...``;
    it is refused when the source state is labelled live or was written by ingestion.
    Live captures need ``employee_args``, the host's employee identity
    (``["--employee-policy", P, "--portal-access", S, "--session-ticket-env", VAR]``, the
    ticket in the host's environment); without
    them ``mcp_server`` refuses (``live_case_host_needs_an_employee_identity``)."""

    def __init__(self, source_state_dir: Path, audience: Path, tenant: str,
                 principals: Sequence[str], *, runtime: Any = None, fixture: bool = False,
                 employee_args: Sequence[str] = (), ontology_store: Path | None = None,
                 link_mirror: bool = False, capture_audience: Path | None = None) -> None:
        """``runtime``: an already-built ``CaseRuntime`` (tests inject one whose mirror
        has a fake transport); then ``source_state_dir`` is its own state directory.
        ``ontology_store``: a projection of the source mirror the stdio host serves the
        generic tools over (``--ontology-store``; the host refuses one projected from
        another mirror). ``link_mirror``: the disposable state directory links the source
        mirror instead of copying it (``link_mirror``), for a caller whose source state is
        already its run's own copy: one copy per run, not per host.
        ``capture_audience`` (fixture only): a ``plx-live-source-audience/v1`` file gating
        the source state's own BC capture (``bc-capture/capture.sqlite``, copied into the
        disposable state): the stdio host gets ``--fixture-capture-audience`` and the
        harness's reads the same gate, with no reader built and no credential read."""
        if not principals:
            raise RunnerError("principal_required")  # no `eng` fallback (decision 7)
        if fixture and employee_args:
            raise RunnerError("fixture_host_takes_no_employee_identity")
        self.source_state_dir = Path(source_state_dir).resolve()
        self.audience = Path(audience).resolve()
        self.fixture, self.employee_args = bool(fixture), tuple(employee_args)
        self.ontology_store = None if ontology_store is None else Path(ontology_store).resolve()
        if capture_audience is not None and (not fixture or runtime is not None):
            raise RunnerError("capture_audience_is_for_a_fixture_host")
        self.capture_audience = None if capture_audience is None \
            else Path(capture_audience).resolve()
        self._work: Path | None = None
        if fixture and _definitely_live(self.source_state_dir):
            raise RunnerError("fixture_host_on_live_state")
        if runtime is None:
            self._work = Path(tempfile.mkdtemp(prefix="plx-eval-state-"))
            try:
                if fixture:
                    # Label the empty copy before the mirror lands in it, so the stdio
                    # host accepts its --fixture session there.
                    from operating.session_identity import bind_state_dir
                    bind_state_dir(self._work, fixture=True)
                (link_mirror_into if link_mirror else snapshot_mirror)(
                    self.source_state_dir, self._work)
                if self.capture_audience is not None:
                    from operating.host import BC_DIR, BC_FILE
                    captured = self.source_state_dir / BC_DIR / BC_FILE
                    if captured.is_file():
                        copy_mirror(captured, self._work / BC_DIR / BC_FILE)
            except BaseException:
                shutil.rmtree(self._work, ignore_errors=True)
                raise
        self.state_dir = self._work or self.source_state_dir
        self.tenant, self.principals = tenant, tuple(principals)
        self.actor = {"tenant": tenant, "principals": sorted(self.principals)}
        self._server: Any = None
        self._runtime: Any = runtime
        self._owns_runtime = runtime is None
        self._ids = 0

    def _session(self) -> Any:
        if self._server is None:
            from ontology.types import Actor
            from operating.case_runtime import CaseRuntime
            from operating.mcp_server import MCPServer
            if self._runtime is None:
                live = None
                if self.capture_audience is not None:
                    from operating.case_runtime import build_live_readers
                    live = build_live_readers(self.capture_audience, environ={}, sources=())
                self._runtime = CaseRuntime(self.state_dir, client=None, live=live,
                                            audience=self.audience, tenant=self.tenant)
            self._server = MCPServer(case_runtime=self._runtime, actor=Actor(
                self.tenant, frozenset(self.principals)))
            self._request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                         "clientInfo": {"name": "plx-eval-runner",
                                                        "version": "1"}})
            self._server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self._server

    def _request(self, method: str, params: dict[str, Any], *,
                 host_code: bool = False) -> dict[str, Any]:
        self._ids += 1
        return self._server.handle({"jsonrpc": "2.0", "id": self._ids, "method": method,
                                    "params": params}, host_code=host_code) or {}

    def tools(self) -> list[dict[str, Any]]:
        self._session()
        listed = (self._request("tools/list", {}).get("result") or {}).get("tools") or []
        return [{"name": tool["name"], "read_only": bool(
            (tool.get("annotations") or {}).get("readOnlyHint"))} for tool in listed]

    def call(self, name: str, arguments: dict[str, Any], *, host_code: bool = False) -> Any:
        """One tool call as the agent sees it: through the host's output boundary
        (source text as envelopes, rows as row envelopes)."""
        self._session()
        response = self._request("tools/call", {"name": name, "arguments": arguments},
                                 host_code=host_code)
        result = response.get("result") or {}
        if "error" in response or result.get("isError"):
            return {"ok": False, "error": "tool_refused"}
        return result.get("structuredContent")

    def read(self, name: str, arguments: dict[str, Any]) -> Any:
        """One tool call by the harness itself (Workbench code, never the agent): the
        result as the tool built it (``MCPServer.handle(host_code=True)``)."""
        return self.call(name, arguments, host_code=True)

    def original_refusal(self, citation: str) -> str | None:
        """Why this host's reader is not served the captured original ``citation`` now
        (``CaseRuntime.original_refusal_kind``): ``refused`` (decided), ``failed`` or
        None (served). Harness code only (``_reader_opens``); never a tool, never shown
        to the agent."""
        from ontology.types import Actor
        self._session()
        return self._runtime.original_refusal_kind(
            citation, Actor(self.tenant, frozenset(self.principals)))

    def mcp_server(self) -> dict[str, Any]:
        args = ["-m", "operating.mcp_server", "--state-dir", str(self.state_dir),
                "--source-mirror-audience", str(self.audience), "--tenant", self.tenant]
        if self.fixture:
            args.append("--fixture")
            for principal in self.principals:
                args += ["--principal", principal]
        elif self.employee_args:
            args += list(self.employee_args)
        else:
            raise RunnerError("live_case_host_needs_an_employee_identity")
        if self.ontology_store is not None:
            args += ["--ontology-store", str(self.ontology_store)]
        if self.capture_audience is not None:
            args += ["--fixture-capture-audience", str(self.capture_audience)]
        return {"command": sys.executable, "args": args, "cwd": str(REPO_ROOT),
                "env": {"PYTHONPATH": str(REPO_ROOT)}}

    def close(self) -> None:
        """Release the session, the runtime and the disposable state, each even when an
        earlier one fails to close (a later call never reuses a closed one)."""
        server, self._server = self._server, None
        try:
            if server is not None:
                server.close()  # its session's working directory (plx.find's)
        finally:
            try:
                if self._owns_runtime and self._runtime is not None:
                    runtime, self._runtime = self._runtime, None
                    runtime.close()
            finally:
                if self._work is not None:
                    shutil.rmtree(self._work, ignore_errors=True)
                    self._work = None


def _definitely_live(state_dir: Path) -> bool:
    """A state directory a live session labelled live or the ingestion runner wrote."""
    from operating.ingestion import STORE_DIR, STORE_FILE
    from operating.session_identity import STATE_LABEL_FILE, state_label
    if (state_dir / STORE_DIR / STORE_FILE).is_file():
        return True
    return (state_dir / STATE_LABEL_FILE).is_file() and state_label(state_dir) == "live"


def _state_mirrors(source_state_dir: Path, target_state_dir: Path) -> tuple[Path, Path]:
    from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE
    source = Path(source_state_dir) / CASE_RUNTIME_DIR / MIRROR_FILE
    if not source.is_file():
        raise RunnerError("no_source_mirror_in_state_dir")
    target = Path(target_state_dir) / CASE_RUNTIME_DIR / MIRROR_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    return source, target


def snapshot_mirror(source_state_dir: Path, target_state_dir: Path) -> Path:
    """Copy the source mirror into ``target_state_dir`` (read-only on the source)."""
    return copy_mirror(*_state_mirrors(source_state_dir, target_state_dir))


def link_mirror_into(source_state_dir: Path, target_state_dir: Path) -> Path:
    """Link the source mirror into ``target_state_dir`` (a symlink, no copy). The offline
    case runtime (no source client) fetches nothing; every host linked to one source reads
    the same file, so the source must be the caller's own copy."""
    source, target = _state_mirrors(source_state_dir, target_state_dir)
    target.symlink_to(source.resolve())
    return target


def copy_mirror(source: Path, target: Path) -> Path:
    """Copy one mirror file to ``target`` with the SQLite backup API (``source`` opened
    read-only, so a mirror another writer holds is copied as one consistent snapshot)."""
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    reader = sqlite3.connect(f"{Path(source).resolve().as_uri()}?mode=ro", uri=True)
    writer = sqlite3.connect(target)
    try:
        reader.backup(writer)
    finally:
        writer.close()
        reader.close()
    return target


# --------------------------------------------------------------------------- run pin

RUN_PIN_FORMAT = "plx-eval-run-pin/v1"
_FIELD = re.compile(r"^row\.([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?$")


def _instant(text: Any) -> Any:
    from datetime import datetime, timezone
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class MirrorPinReader:
    """Read-only view of a source mirror file for run pins: completed captures per
    relation and the row version current at a capture. Reads the tables directly
    (``mode=ro``) after a layout check, as ``ontology.project.MirrorCaptures`` does,
    because the mirror has no public version export."""

    _NEEDED = {
        "mirror_meta": {"name", "value"},
        "capture": {"capture_id", "relation", "finished_at"},
        "capture_state": {"capture_id", "state"},
        "row_current": {"relation", "key_json", "run_index", "run_length", "row_json",
                        "capture_id", "absent_since_capture"},
        "row_version": {"relation", "key_json", "capture_id", "run_index", "run_length",
                        "row_json", "superseded_by_capture"},
    }

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise RunnerError("no_source_mirror_in_state_dir")
        self._db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        self._db.row_factory = sqlite3.Row
        for table, columns in self._NEEDED.items():
            have = {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}
            if not columns <= have:
                self._db.close()
                raise RunnerError("mirror_layout_unsupported")
        row = self._db.execute(
            "SELECT value FROM mirror_meta WHERE name = 'generation'").fetchone()
        if row is None:
            self._db.close()
            raise RunnerError("mirror_generation_missing")
        self.generation: str = row["value"]

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "MirrorPinReader":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def first_captures(self, moment: Any) -> dict[str, tuple[int, str]]:
        """Per relation: the first completed capture whose completion is at or after
        ``moment`` (capture id, finished_at)."""
        out: dict[str, tuple[int, str]] = {}
        for row in self._db.execute(
                "SELECT c.capture_id, c.relation, c.finished_at FROM capture c "
                "JOIN capture_state s ON s.capture_id = c.capture_id "
                "WHERE s.state = 'complete' ORDER BY c.capture_id"):
            finished = _instant(row["finished_at"])
            if row["relation"] not in out and finished is not None and finished >= moment:
                out[row["relation"]] = (int(row["capture_id"]), row["finished_at"])
        return out

    def row_at(self, relation: str, key: Mapping[str, Any], capture_id: int
               ) -> tuple[str, Mapping[str, Any] | None]:
        """("ok", row) of the version current at ``capture_id``, or (problem, None)."""
        texts = {scoring.canonical(dict(key)),
                 scoring.canonical({k: int(v) if isinstance(v, str) and v.isdigit() else v
                                    for k, v in key.items()}),
                 scoring.canonical({k: str(v) for k, v in key.items()})}
        for key_json in sorted(texts):
            current = self._db.execute(
                "SELECT row_json, run_length, capture_id, absent_since_capture FROM "
                "row_current WHERE relation = ? AND key_json = ? AND run_index = 0",
                (relation, key_json)).fetchone()
            if current is None:
                continue
            if current["run_length"] > 1:
                return "key_not_unique", None
            if current["capture_id"] <= capture_id:
                absent = current["absent_since_capture"]
                if absent is not None and absent <= capture_id:
                    return "row_absent_at_pin", None
                return "ok", json.loads(current["row_json"])
            older = self._db.execute(
                "SELECT row_json FROM row_version WHERE relation = ? AND key_json = ? AND "
                "run_index = 0 AND capture_id <= ? AND superseded_by_capture > ? "
                "ORDER BY capture_id DESC LIMIT 1",
                (relation, key_json, capture_id, capture_id)).fetchone()
            return ("ok", json.loads(older["row_json"])) if older else \
                ("row_not_yet_captured_at_pin", None)
        return "row_missing_at_pin", None


def _field(row: Mapping[str, Any], field_path: str) -> tuple[bool, Any]:
    match = _FIELD.match(field_path)
    if not match or match.group(1) not in row:
        return False, None
    value = row[match.group(1)]
    if match.group(2):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return False, None
        if not isinstance(value, Mapping) or match.group(2) not in value:
            return False, None
        value = value[match.group(2)]
    return True, value


def field_digest(value: Any) -> str:
    """16-hex digest of one field value, normalized (decimals by value, text stripped)."""
    if value is None or isinstance(value, bool):
        norm: Any = value
    elif isinstance(value, (dict, list)):
        norm = scoring.canonical(value)
    else:
        number = scoring.to_decimal(value)
        norm = format(number.normalize(), "f") if number is not None else str(value).strip()
    return scoring.sha256(norm)[:16]


OracleField = Callable[[str, Mapping[str, Any], str], "tuple[bool, Any]"]


def make_run_pin(item: Mapping[str, Any], reader: MirrorPinReader, *,
                 oracle_field: OracleField | None = None) -> dict[str, Any]:
    """The run pin of one item.

    For each relation the item's expected ``api://`` citations name: the first completed
    mirror capture at or after ``as_known_at.time``. Drift check: each expected citation's
    field in that pinned row version against the oracle's read of it, by digest of the
    cited field only. ``oracle_field(relation, key, field_path) -> (found, value)`` is the
    oracle's read (the harness passes its own reads); without it the expected value is
    the oracle's read of a value that cites exactly one row, and a value citing several
    rows is not checked (``unchecked``). ``status`` is ``pinned`` or ``pin_drift`` (any
    unpinnable relation or drifted, missing or unreadable cited field)."""
    oracle = {str(g) for g in item["as_known_at"].get("l1_generations", {}).values()}
    moment = _instant(item["as_known_at"].get("time"))
    if moment is None:
        raise RunnerError("as_known_at_time_unreadable")
    first = reader.first_captures(moment)
    cites: list[tuple[str, dict[str, Any], str, Any, bool]] = []
    for value in (item.get("known_outcome") or {}).get("values") or []:
        api = [c for c in value.get("citations") or [] if scoring.row_identity(
            c.get("l1_handle"), oracle)]
        for cite in api:
            match = scoring.ORACLE_HANDLE.match(cite["l1_handle"]) or \
                scoring.API_HANDLE.match(cite["l1_handle"])
            key = json.loads(unquote(match.group(2)))
            cites.append((match.group(1), key, cite["field_path"], value.get("value"),
                          len(api) == 1))
    relations = sorted({relation for relation, *_ in cites})
    pins = {relation: {"capture_id": first[relation][0], "finished_at": first[relation][1]}
            for relation in relations if relation in first}
    problems: dict[str, int] = {}
    checks = []
    for relation in relations:
        if relation not in pins:
            problems["no_capture_at_or_after_time"] = \
                problems.get("no_capture_at_or_after_time", 0) + 1
    dev = item.get("split") == "dev"
    for relation, key, field_path, expected, single in cites:
        if relation not in pins:
            continue
        state, row = reader.row_at(relation, key, pins[relation]["capture_id"])
        status, entry = state, {"relation": relation, "field_path": field_path}
        if row is not None:
            found, got = _field(row, field_path)
            if oracle_field is not None:
                known, want = oracle_field(relation, key, field_path)
            else:
                known, want = single, expected
            if not found:
                status = "field_not_in_pinned_row"
            elif not known:
                status = "unchecked"
            else:
                status = "equal" if field_digest(want) == field_digest(got) else "field_differs"
                if dev:
                    entry.update(oracle_digest=field_digest(want), mirror_digest=field_digest(got))
        entry["status"] = status
        checks.append(entry)
        if status not in ("equal", "unchecked"):
            problems[status] = problems.get(status, 0) + 1
    known_at = max((at for _, at in first.values()), key=_instant, default=None)
    return {"format": RUN_PIN_FORMAT, "source": "portal_api",
            "mirror_generation": reader.generation,
            "item_time": item["as_known_at"].get("time"), "known_at": known_at,
            "relations": pins, "checks": checks if dev else [],
            "checked": sum(1 for c in checks if c["status"] not in ("unchecked",)),
            "unchecked": sum(1 for c in checks if c["status"] == "unchecked"),
            "problems": dict(sorted(problems.items())),
            "status": "pin_drift" if problems else "pinned"}


# --------------------------------------------------------------------------- agents


class ScriptedAgent:
    """Calls the host's tools from a fixed script, in process, then answers.

    ``script(prompt, call) -> final_text``; ``call(name, arguments)`` runs the tool and
    records it, with ``t_ms`` (milliseconds since the run started, when the result came
    back). ``call(PRESUBMIT_TOOL, {"answer": draft})`` runs ``presubmit_check`` on a
    draft instead: never a host tool call, recorded as an ``answer_check`` event (its
    ``t_ms``, ``ok`` and ``codes``; never the draft), which no tool count, budget or action
    rule reads (they read ``tool_call`` events only). The run's checks share one
    ``PresubmitReads``: each cited handle re-read once, within ``PRESUBMIT_MAX_READS``.
    Deterministic: for tests and fixture smoke runs, never a model."""

    def __init__(self, script: Callable[[str, Callable[[str, dict[str, Any]], Any]], str],
                 *, name: str = "scripted") -> None:
        self.script, self.name = script, name

    def run(self, prompt: str, host: Host) -> AgentRun:
        events: list[dict[str, Any]] = []
        reads = PresubmitReads()
        started = time.monotonic()

        def call(name: str, arguments: dict[str, Any]) -> Any:
            if name == PRESUBMIT_TOOL:
                checked = presubmit_check(arguments.get("answer"), host, reads=reads)
                events.append({"seq": len(events), "type": PRESUBMIT_EVENT,
                               "t_ms": round((time.monotonic() - started) * 1000),
                               "ok": checked["ok"], "codes": list(checked["codes"])})
                return checked
            result = host.call(name, dict(arguments))
            events.append({"seq": len(events), "type": "tool_call", "name": name,
                           "t_ms": round((time.monotonic() - started) * 1000),
                           "arguments": dict(arguments), "result": _bounded(result),
                           "is_error": isinstance(result, Mapping) and result.get("ok") is False
                           and result.get("error") == "tool_refused"})
            return result
        final = self.script(prompt, call)
        return AgentRun(events, final, {"tool_calls": _tool_calls(events)},
                        {"kind": "scripted", "name": self.name})


# --------------------------------------------------------------------------- recording

# An original's re-read also keeps what the scorer resolves a pinned original by (its
# bytes' SHA-256 and source provenance) and the page text the claim gate checks.
_EVIDENCE_KEYS = ("ok", "relation", "key", "row", "rows", "capture", "superseded",
                  "current_citation", "error", "message", "sha256", "provenance", "page",
                  "text", "text_source")


def harness_evidence(answer: Mapping[str, Any] | None, host: Host) -> dict[str, Any]:
    """Each handle the answer cites (a calculation's inputs included), re-read once as the
    item's actor (not by the agent)."""
    return {handle: _evidence_entry(host, handle)
            for handle in _cited_handles(answer)[:MAX_EVIDENCE_READS]}


def _cited_handles(answer: Mapping[str, Any] | None) -> list[str]:
    handles: list[str] = []
    for value in (answer or {}).get("values") or []:
        for cite in [*(value.get("citations") or []),
                     *((value.get("calculation") or {}).get("inputs") or [])]:
            if cite["l1_handle"] not in handles:
                handles.append(cite["l1_handle"])
    return handles


class _ReadsExhausted(Exception):
    pass


def _evidence_entry(host: Host, handle: str) -> dict[str, Any]:
    """One cited handle re-read whole, kept to the keys the scorer reads."""
    try:
        read = _whole_row(host, handle)
    except _ReadsExhausted:
        raise
    except Exception as error:  # noqa: BLE001 - recorded as an unreadable citation
        read = {"ok": False, "error": type(error).__name__}
    return ({key: read[key] for key in _EVIDENCE_KEYS if key in read}
            if isinstance(read, Mapping) else {"ok": False})


# --------------------------------------------------------------------------- pre-submit

PRESUBMIT_TOOL = "evals.answer_check"
PRESUBMIT_EVENT = "answer_check"
PRESUBMIT_FORMAT = "plx-eval-answer-check/v1"
# Host reads one run's pre-submit checks may make in all: a third of the session's
# per-minute tool limit (operating.mcp_server CALLS_PER_MINUTE, 120), which the agent's
# own calls and the harness's re-read after the answer share.
PRESUBMIT_MAX_READS = 40


class PresubmitReads:
    """The re-reads one run's pre-submit checks share: each cited handle read once (its
    ``evidence`` entry kept), at most ``budget`` host reads in all (a row paged by columns
    or a page of text read in parts costs one per part)."""

    def __init__(self, budget: int = PRESUBMIT_MAX_READS) -> None:
        self.evidence: dict[str, dict[str, Any]] = {}
        self.left = budget

    def host(self, host: Host) -> Any:
        reads = self

        class Counted:
            def read(self, name: str, arguments: dict[str, Any]) -> Any:
                if reads.left <= 0:
                    raise _ReadsExhausted()
                reads.left -= 1
                return host.read(name, arguments)
        return Counted()

    def gather(self, answer: Mapping[str, Any], host: Host) -> dict[str, Any] | None:
        """The answer's evidence, or None once the budget cannot read a handle whole."""
        counted = self.host(host)
        handles = _cited_handles(answer)[:MAX_EVIDENCE_READS]
        for handle in handles:
            if handle not in self.evidence:
                try:
                    self.evidence[handle] = _evidence_entry(counted, handle)
                except _ReadsExhausted:
                    return None
        return {handle: self.evidence[handle] for handle in handles}


def presubmit_check(draft: Any, host: Host | None = None, *,
                    evidence: Mapping[str, Any] | None = None,
                    contracts: Any = None, require_citations: bool = True,
                    reads: PresubmitReads | None = None) -> dict[str, Any]:
    """The agent's pre-submit check of a draft answer (the answer object, or a final
    message holding one): the codes the scorer would raise on its form and claims, from
    the scorer's own functions, never a copy of their rules. Read-only: it submits
    nothing, scores nothing against an item and writes no state; the only reads are the
    harness's re-reads of the cited handles (``host.read``, as ``harness_evidence``), or
    the ``evidence`` given. Re-reads go through ``reads`` (one ``PresubmitReads`` per
    check unless the caller shares one over a run): a draft whose handles the budget
    cannot read is not gated, ``presubmit_reads_exhausted``.

    * form: ``scoring.answer_problems`` (any problem: ``answer_malformed``, which the
      scorer scores ``no_answer``) and, per calculated value, ``scoring.calculation_problem``
      (``citations_beside_calculation``, ``inputs``, ...); a malformed draft is not gated,
      as the scorer gates none;
    * claims: ``scoring.gate`` with ``require_citations`` (the item's
      ``scoring.citations_required`` when the caller knows the item; strict when not, so an
      item that waives citations may see ``claim_uncited`` the scorer would not raise): every
      gate failure code, ``claim_malformed`` for a value the gate cannot parse, each
      calculation's recompute code, and ``claim_uncited_number`` when a code of
      ``scoring.UNCITED_NUMBER_GATE_CODES`` is raised (a prose number no value claim
      states, or a value with no citation).

    No answer object at all is ``answer_missing``. ``ok`` is true only with no code but
    the gate's unverified ones (Workbench coverage, never failing)."""
    if evidence is None and host is None:
        raise RunnerError("presubmit_needs_a_host_or_evidence")
    if isinstance(draft, str):
        answer = scoring.answer_candidate(draft)
    elif isinstance(draft, Mapping) and draft.get("format") == scoring.ANSWER_FORMAT:
        answer = json.loads(json.dumps(draft))   # the caller's draft is never touched
    else:
        answer = None
    failures: list[dict[str, Any]] = []

    def fail(where: str, code: str, value: Any = None) -> None:
        failures.append({"where": where, "code": code,
                         **({"value": value} if value is not None else {})})

    gated = False
    if answer is None:
        fail("answer", "answer_missing")
    else:
        values = answer.get("values")
        for index, value in enumerate(values if isinstance(values, list) else []):
            if isinstance(value, Mapping) and value.get("calculation") is not None:
                code = scoring.calculation_problem(value["calculation"], value.get("citations"))
                if code:
                    fail(f"values.{index}.calculation", code)
        problems = scoring.answer_problems(answer)
        for path in problems:
            fail(path, "answer_malformed")
        if not problems and evidence is None:
            evidence = (reads or PresubmitReads()).gather(answer, host)
            if evidence is None:
                fail("answer", "presubmit_reads_exhausted")
        if not problems and evidence is not None:
            gated = True
            checked = scoring.gate(answer, evidence, contracts,
                                   require_citations=require_citations)
            gate_failures = list(checked["report"]["failures"])
            for failure in gate_failures:
                fail(str(failure.get("item")), str(failure["code"]), failure.get("value"))
            for index, entry in sorted(checked["values"].items()):
                if entry.get("malformed"):
                    fail(f"values.{index}", "claim_malformed")
                code = (entry.get("calculation") or {}).get("code")
                if code:
                    fail(f"values.{index}.calculation", code)
            if any(f["code"] in scoring.UNCITED_NUMBER_GATE_CODES for f in gate_failures):
                fail("answer", "claim_uncited_number")
    codes = sorted({failure["code"] for failure in failures})
    return {"format": PRESUBMIT_FORMAT, "ok": not set(codes) - scoring.UNVERIFIED_GATE_CODES,
            "codes": codes, "gated": gated, "failures": failures}


def _whole_row(host: Host, handle: str) -> Any:
    """One citation re-read whole: a row paged by columns is followed to its last page; a
    row with a cut cell (``truncated_cells``) is an unreadable citation, never a partial
    row the gate could score. An original page's text is followed to its end and checked
    against ``page_text_sha256``; anything short of the whole page is unreadable."""
    read = host.read("plx.source_citation", {"citation": handle})
    if handle.startswith("original://") and isinstance(read, Mapping) and "page" in read:
        return _whole_page(host, handle, read)
    pages = 0
    while (isinstance(read, Mapping) and read.get("next_column_offset") is not None
           and pages < MAX_EVIDENCE_READS):
        more = host.read("plx.source_citation", {"citation": handle,
                                                 "column_offset": read["next_column_offset"]})
        if not isinstance(more, Mapping) or not more.get("ok"):
            return {"ok": False, "error": "row_page_unreadable"}
        merged = dict(more)
        if isinstance(read.get("row"), Mapping):
            merged["row"] = {**read["row"], **more.get("row", {})}
        else:
            merged["rows"] = [{**a, **b} for a, b in zip(read.get("rows") or [],
                                                          more.get("rows") or [])]
        merged["truncated_cells"] = sorted(set(read.get("truncated_cells") or ())
                                           | set(more.get("truncated_cells") or ()))
        read, pages = merged, pages + 1
    if isinstance(read, Mapping) and (read.get("truncated_cells")
                                      or read.get("next_column_offset") is not None):
        return {"ok": False, "error": "row_not_readable_whole"}
    return read


def _whole_page(host: Host, handle: str, read: Mapping[str, Any]) -> Any:
    text, reads = str(read.get("text") or ""), 0
    while (read.get("ok") and read.get("next_text_offset") is not None
           and reads < MAX_EVIDENCE_READS):
        read = host.read("plx.source_citation", {"citation": handle,
                                                 "text_offset": read["next_text_offset"]})
        if not isinstance(read, Mapping):
            return {"ok": False, "error": "page_text_unreadable"}
        text, reads = text + str(read.get("text") or ""), reads + 1
    if not read.get("ok"):
        return read
    if read.get("next_text_offset") is not None or read.get("page_text_sha256") != \
            hashlib.sha256(text.encode("utf-8")).hexdigest():
        return {"ok": False, "error": "page_not_readable_whole"}
    return {**read, "text": text}


def seal(transcript: Mapping[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in transcript.items() if key != "transcript_sha256"}
    return {**body, "transcript_sha256": scoring.sha256(body)}


# A line anchor on a pinned original in ``oracle.derived_from`` (a proposed card's cited
# lines, ``scripts/eval_use_case_items.py``: ``original://sha256/<hex>#L<a>-L<b>``): the
# original is the part before it.
_LINE_ANCHOR = re.compile(r"#L[0-9]+-L[0-9]+$")


def item_pins(item: Mapping[str, Any]) -> list[str]:
    """The pinned originals (``scoring.PINNED_ORIGINAL``) an item rests on: those its
    expected values cite and those its oracle names in ``derived_from`` (a proposal or
    comprehension card has no expected value but names the originals it is about), a line
    anchor dropped; sorted, each once."""
    cited = [str(cite.get("l1_handle")) for value in (item.get("known_outcome") or {})
             .get("values") or [] for cite in value.get("citations") or []
             if isinstance(cite, Mapping)]
    derived = [_LINE_ANCHOR.sub("", str(ref))
               for ref in (item.get("oracle") or {}).get("derived_from") or []]
    return sorted({pin for pin in cited + derived if scoring.PINNED_ORIGINAL.match(pin)})


def _held_reads(state_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Every original the host's state holds, as ``scoring.resolves_to_pin`` reads them
    (``{ok, sha256, provenance}``), and the stores that exist but could not be read.

    Held means bytes the host can serve: the case runtime's originals; captured case
    originals (``operating.captured_originals``) read in full with a known source unit
    (one with none is served to nobody); SharePoint drive item versions and mail
    attachments whose bytes the captures hold. A captured original's read names its
    ``entry_id``: whether the scoring reader may open it is decided by ``_reader_opens``."""
    from operating.case_originals import DB_FILE
    from operating.case_runtime import CASE_RUNTIME_DIR
    reads: list[dict[str, Any]] = []
    unreadable: list[str] = []

    def rows(path: Path, sql: str, name: str) -> list[tuple]:
        if not path.is_file():
            return []
        try:
            db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                return list(db.execute(sql))
            finally:
                db.close()
        except sqlite3.Error:
            unreadable.append(name)
            return []

    for sha, source in rows(state_dir / CASE_RUNTIME_DIR / DB_FILE,
                            "SELECT o.sha256, s.provenance_json FROM original o "
                            "LEFT JOIN original_source s USING (original_id)",
                            "case_runtime_originals"):
        try:
            provenance = json.loads(source) if source else None
        except ValueError:
            unreadable.append("case_runtime_originals")
            provenance = None
        reads.append({"ok": True, "sha256": sha, "provenance": provenance})
    from operating import captured_originals as captured
    receipts = state_dir / captured.STORE_DIR / "receipts"
    if not (state_dir / CASE_RUNTIME_DIR / DB_FILE).is_file() and not receipts.is_dir():
        # No originals store at all: what the host holds is unknown, never "not held".
        unreadable.append("originals_store_missing")
    if receipts.is_dir():
        # ``entries()`` skips a receipt it cannot read; that receipt may be the one held,
        # so any unreadable receipt makes the store unknown.
        try:
            for path in sorted(receipts.glob("*/*.json")):
                if not isinstance(json.loads(path.read_text(encoding="utf-8")), Mapping):
                    unreadable.append("captured_originals")
        except (OSError, ValueError):
            unreadable.append("captured_originals")
        try:
            entries = captured.CapturedOriginals(
                state_dir, gate=lambda *_: False).entries().values()
            reads += [{"ok": True, "sha256": entry.receipt["sha256"], "provenance": None,
                       "entry_id": entry.id}
                      for entry in entries if entry.reason is None and entry.unit
                      and (state_dir / entry.receipt["stored"]).is_file()]
        except (OSError, ValueError, KeyError, TypeError):
            unreadable.append("captured_originals")
    from integrations import capture_mail, capture_sharepoint
    for sha, drive, item, etag in rows(
            state_dir / capture_sharepoint.STORE_DIR / capture_sharepoint.DB_FILE,
            "SELECT content_sha256, drive_id, item_id, etag FROM items WHERE "
            f"content_sha256 IS NOT NULL AND content_status = '{capture_sharepoint.CAPTURED}'",
            "sharepoint_capture"):
        reads.append({"ok": True, "sha256": sha,
                      "provenance": {"source": "sharepoint-drive", "drive_id": drive,
                                     "item_id": item, "etag": etag}})
    for (sha,) in rows(state_dir / capture_mail.STORE_DIR / capture_mail.DB_FILE,
                       "SELECT content_sha256 FROM attachments WHERE content_sha256 IS NOT "
                       "NULL AND content_status = 'captured'",
                       "mail_capture"):
        reads.append({"ok": True, "sha256": sha, "provenance": None})
    return reads, sorted(set(unreadable))


# The answer a reader gets for a captured original the host does not serve it
# (``operating.captured_originals``). It reads alike for a decided refusal and for a
# failure inside the gate, so it alone never says "not openable".
ORIGINAL_REFUSED = "original_not_visible"
# The refusal kind (``operating.captured_originals.REFUSED``) that alone counts: the
# source ACL denies this reader, the original has no known source ACL (served to nobody
# by policy), or the people exclusion decided it is excluded.
REFUSAL_DECIDED = "refused"


def _reader_opens(reader: Any, entry_id: str) -> bool | None:
    """May the scoring reader open the captured original ``entry_id`` now? Its harness
    read (``plx.source_citation`` as host code, the item's actor, through the host's own
    gate): True when it reads. False only when that read answers ``ORIGINAL_REFUSED``
    and the host's gate says the refusal was decided (``original_refusal``:
    ``REFUSAL_DECIDED``). None (unknown) for anything else: a failing read, a timeout,
    a refused tool, another error, a gate failure (``failed``: an exception, a people
    exclusion that cannot decide, a source not configured, an unreadable store), or a
    host that cannot say why."""
    citation = f"original://{entry_id}"
    try:
        read = reader.read("plx.source_citation", {"citation": citation})
    except Exception:  # noqa: BLE001 - a failed check is unknown, never "not held"
        return None
    if not isinstance(read, Mapping):
        return None
    if read.get("ok") is True:
        return True
    if read.get("ok") is not False or read.get("error") != ORIGINAL_REFUSED:
        return None
    why = getattr(reader, "original_refusal", None)
    if not callable(why):
        return None
    try:
        kind = why(citation)
    except Exception:  # noqa: BLE001 - a failed check is unknown, never "not held"
        return None
    return False if kind == REFUSAL_DECIDED else None


def originals_held(item: Mapping[str, Any], state_dir: Any, reader: Any = None
                   ) -> dict[str, Any] | None:
    """How many pinned originals the item rests on (``pinned``: ``item_pins``) and how
    many of them the host does not hold for the scoring reader (``missing``). Held means
    bytes the host serves to this reader:
    bytes with that SHA-256 or that SharePoint drive item version (``_held_reads`` and
    ``scoring.resolves_to_pin``) and, when they are held only as captured case originals,
    one of them the reader opens (``_reader_opens`` on ``reader``, the item's host). A pin
    whose every captured original the reader's source gate refuses, each refusal decided
    (not a failure inside the gate), is missing and counted in ``not_openable`` too.
    Without a ``reader`` captured originals are not checked (held, as before).

    None when the item names no pinned original or the host has no state directory.
    Unknown leaves nothing missing (``unreadable`` names why): a state directory that is
    missing or unreadable, a state with no originals store at all (neither the case
    runtime's originals nor captured case originals), a store that exists but cannot be
    read, a receipt that cannot be read, or a reader check that failed
    (``reader_check_failed``). An unknown never excuses an abstention. The scorer
    blocks an abstention when one or more pins are missing."""
    pins = item_pins(item)
    if not pins or state_dir is None:
        return None
    if not Path(state_dir).is_dir():
        return {"pinned": len(pins), "missing": 0, "unreadable": ["state_dir"]}
    try:
        reads, unreadable = _held_reads(Path(state_dir))
    except OSError:
        return {"pinned": len(pins), "missing": 0, "unreadable": ["state_dir"]}
    if unreadable:
        return {"pinned": len(pins), "missing": 0, "unreadable": unreadable}
    missing = not_openable = 0
    for pin in pins:
        found = [read for read in reads if scoring.resolves_to_pin(read, pin)]
        if not found:
            missing += 1
            continue
        if reader is None or any("entry_id" not in read for read in found):
            continue   # held where no captured original's gate decides
        opens = [_reader_opens(reader, read["entry_id"])
                 for read in sorted(found, key=lambda r: r["entry_id"])]
        if True in opens:
            continue
        if None in opens:
            return {"pinned": len(pins), "missing": 0, "unreadable": ["reader_check_failed"]}
        missing += 1
        not_openable += 1
    return {"pinned": len(pins), "missing": missing,
            **({"not_openable": not_openable} if not_openable else {})}


# An item no capture generation pins (``time_pinned`` false): the prompt of a question
# asked about the sources as they stand now, as the app's Ask asks it (``ask_prompt``).
UNPINNED_ITEM = {"as_known_at": {"l1_generations": {"case_cards": "unpinned"}}}


def record(item: Mapping[str, Any], agent: Agent, host: Host, *, phrasing_index: int = 0,
           run_index: int = 0, evidence_label: str = "isolated-fixture",
           run_pin: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run ``agent`` once on ``item`` and return the sealed transcript.

    ``run_pin`` (``make_run_pin``): written before the agent runs, given to it in the
    prompt and recorded in the transcript; ``evals.score`` reads the pinned generation
    from it and scores a drifted pin ``pin_drift``."""
    if evidence_label not in EVIDENCE_LABELS:
        raise RunnerError("unknown_evidence_label")
    texts = [item["question"]["text"]] + list(item["question"].get("paraphrases") or [])
    if not 0 <= phrasing_index < len(texts):
        raise RunnerError("no_such_phrasing")
    prompt = build_prompt(item, texts[phrasing_index], run_pin)
    tools = host.tools()
    started = time.monotonic()
    run = gated_run(agent, prompt, host, texts[phrasing_index])
    # Wall time from the prompt to the final message (evals.measure time_to_answer_ms).
    timing = {"clock": "monotonic", "answer_ms": round((time.monotonic() - started) * 1000)}
    answer, error = scoring.parse_answer(run.final_text)
    extra = {"run_pin": json.loads(json.dumps(run_pin))} if run_pin else {}
    # What the host held for this reader when the agent answered: an abstention over
    # originals the state lacks, or that the reader's source gate refuses, says nothing
    # about the agent (evals.score: blocked, originals_not_held).
    held = originals_held(item, getattr(host, "state_dir", None), reader=host)
    if held is not None:
        extra["originals"] = held
    return seal({**extra,
        "format": scoring.TRANSCRIPT_FORMAT, "runner": RUNNER_VERSION,
        "item_id": item["id"], "family": item["family"], "split": item["split"],
        "partition": scoring.partition_of(item),
        "variant": item["variant"], "phrasing_index": phrasing_index, "run_index": run_index,
        "evidence_label": evidence_label, "prompt": prompt,
        "prompt_sha256": scoring.sha256(prompt),
        "agent": dict(run.agent), "host": {"actor": dict(host.actor), "tools": tools},
        "events": run.events, "final_text": run.final_text,
        "answer": answer, "answer_error": error,
        "evidence": harness_evidence(answer, host),
        "usage": {**run.usage, "tool_calls": _tool_calls(run.events)}, "timing": timing,
    })


def verify_seal(transcript: Mapping[str, Any]) -> bool:
    return seal(transcript)["transcript_sha256"] == transcript.get("transcript_sha256")


def _parent(item: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]]
            ) -> Mapping[str, Any] | None:
    return by_id.get(item.get("parent_id")) if item.get("parent_id") else None


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(path)


def _out_dir(out: Path, split: str) -> Path:
    out = out.resolve()
    if split == "held_out" and not _inside(out, held_out_root()):
        raise RunnerError(f"held_out_output_must_live_under_{HELD_OUT_DIR_ENV}")
    if _inside(out, REPO_ROOT):
        raise RunnerError("transcripts_never_go_into_the_repository")
    return out


def run_items(items: Sequence[Mapping[str, Any]], agent: Agent,
              host_for: Callable[[Mapping[str, Any]], Host], *, out: Path,
              runs: int = 1, paraphrases: bool = False,
              evidence_label: str = "isolated-fixture",
              pin_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
              only_phrasing: int | None = None) -> list[dict[str, Any]]:
    """Record and score every item × phrasing × run; write each transcript and score
    under ``out`` (held-out scores redacted). Returns the full scores (in memory).
    ``pin_for(item)``: the item's run pin, written once before its runs.
    ``only_phrasing``: run that phrasing of each item only (``scripts.eval_messy``: a
    messy variant is ``question.paraphrases[0]``, phrasing 1); an item without it is
    ``no_such_phrasing``."""
    if runs < 1:
        raise RunnerError("runs_must_be_positive")
    splits = {item["split"] for item in items}
    if len(splits) > 1:
        raise RunnerError("one_split_per_run")
    if len({scoring.partition_of(item) for item in items}) > 1:
        raise RunnerError("one_partition_per_run")  # exposed results never mix with dev
    out = _out_dir(out, next(iter(splits), "dev"))
    by_id = {item["id"]: item for item in items}
    results = []
    for item in items:
        pin = pin_for(item) if pin_for is not None else None
        phrasings = 1 + (len(item["question"].get("paraphrases") or []) if paraphrases else 0)
        for phrasing in (range(phrasings) if only_phrasing is None else [only_phrasing]):
            for run in range(runs):
                host = host_for(item)  # a fresh host (and case store) for every run
                try:
                    transcript = record(item, agent, host, phrasing_index=phrasing,
                                        run_index=run, evidence_label=evidence_label,
                                        run_pin=pin)
                finally:
                    close = getattr(host, "close", None)
                    if close:
                        close()
                result = scoring.score(item, transcript, parent=_parent(item, by_id))
                base = out / item["id"] / f"p{phrasing}-r{run}"
                _write(base.with_suffix(".transcript.json"), transcript)
                _write(base.with_suffix(".score.json"),
                       scoring.redact(result) if item["split"] == "held_out" else result)
                results.append(result)
    return results


def replay(items: Sequence[Mapping[str, Any]], transcripts: Path) -> dict[str, Any]:
    """Re-score every recorded transcript under ``transcripts``; compare with the stored
    score digest. A transcript whose seal is broken is refused, not scored."""
    by_id = {item["id"]: item for item in items}
    dev = all(item["split"] == "dev" for item in items)
    scores, identical, compared = [], 0, 0
    for path in sorted(transcripts.resolve().rglob("*.transcript.json")):
        transcript = json.loads(path.read_text(encoding="utf-8"))
        if not verify_seal(transcript):
            raise RunnerError("transcript_seal_broken" + (f":{path.name}" if dev else ""))
        item = by_id.get(transcript.get("item_id"))
        if item is None:
            continue  # another split's transcript
        result = scoring.score(item, transcript, parent=_parent(item, by_id))
        stored = path.with_name(path.name.replace(".transcript.json", ".score.json"))
        if stored.exists():
            compared += 1
            identical += json.loads(stored.read_text(encoding="utf-8")).get(
                "score_sha256") == result["score_sha256"]
        scores.append(result)
    return {"scores": scores, "compared": compared, "identical": identical,
            "ok": bool(scores) and compared == len(scores) and identical == compared}


def public_summary(scores: Sequence[Mapping[str, Any]], split: str,
                   refused: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """What the CLI prints: the partition label, outcomes with coverage beside accuracy,
    aggregates and the run's ``blocked_share`` and ``fail_reasons`` (``evals.score``: the
    dev baseline's rule) always; the items admission refused (``refused_summary``) when
    any; per-item results only for dev."""
    labels = sorted({scoring.partition_label(s) for s in scores})
    outcomes = scoring.outcome_summary(scores)
    summary: dict[str, Any] = {"split": split, "partition": labels[0] if len(labels) == 1
                               else labels, "runs": len(scores), "outcomes": outcomes,
                               "measure": measuring.summarize(s.get("measure") for s in scores),
                               "aggregate": scoring.aggregate(scores),
                               "blocked_share": scoring.blocked_share(
                                   outcomes["read_gap"], len(scores))["blocked_share"],
                               "fail_reasons": scoring.fail_reasons(outcomes["read_gap"],
                                                                    len(scores))}
    if refused:
        summary["refused"] = refused_summary(refused, split)
    unheld = sum("originals_not_held" in s["codes"] for s in scores)
    if unheld:
        # Runs blocked because the host's state lacked pinned originals: a capture gap the
        # live verifier records as blocked (cause capture_gap, capture tier, red after 24 h).
        summary["originals_not_held"] = unheld
        summary["blocked_upstream"] = [{"part": "pinned_originals", "source": "case-originals",
                                        "cause": "capture_gap"}]
    if split == "dev":
        summary["items"] = [{"item_id": s["item_id"], "phrasing_index": s["phrasing_index"],
                             "run_index": s["run_index"], "passed": s["passed"],
                             "outcome": scoring.outcome_of(s),
                             "codes": s["codes"], "violations": s["violations"],
                             "value_codes": sorted({c for v in s["values"]
                                                    for c in v["codes"]})}
                            for s in scores]
    return summary


# --------------------------------------------------------------------------- CLI

def _vs_manual(scores: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The run against the committed manual baselines (``evals.measure.compare``):
    ``unmeasured`` unless a live run and a human-timed record share an item."""
    return measuring.compare([(s["item_id"], s["measure"]) for s in scores
                              if isinstance(s.get("measure"), Mapping)],
                             measuring.load_manual())


def _cites_rows(item: Mapping[str, Any]) -> bool:
    oracle = {str(g) for g in item["as_known_at"].get("l1_generations", {}).values()}
    return any(scoring.row_identity(cite.get("l1_handle"), oracle)
               for value in (item.get("known_outcome") or {}).get("values") or []
               for cite in value.get("citations") or [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m evals.runner",
                                     description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run items through Claude Code on the case host")
    replay_cmd = sub.add_parser("replay", help="re-score recorded transcripts")
    prompt = sub.add_parser("prompt", help="print one dev item's prompt")
    gate = sub.add_parser("gate", help="serve the tool gate in front of an MCP stdio host")
    gate.add_argument("--allow", action="append", default=[])
    gate.add_argument("--log", type=Path, required=True)
    gate.add_argument("--timing", type=Path, help="append each tool call's wall-clock "
                      "start and end here (serve_gate)")
    gate.add_argument("server", nargs=argparse.REMAINDER)
    for command in (run, replay_cmd, prompt):
        command.add_argument("--items", type=Path, required=True)
        command.add_argument("--split", choices=("dev", "held_out"), default="dev")
    prompt.add_argument("--item", required=True)
    prompt.add_argument("--phrasing", type=int, default=0)
    replay_cmd.add_argument("--transcripts", type=Path, required=True)
    run.add_argument("--state-dir", type=Path, required=True)
    run.add_argument("--source-mirror-audience", type=Path, required=True)
    run.add_argument("--tenant", default="plx")
    run.add_argument("--principal", action="append", default=[],
                     help="the employee the host serves (an item's actor overrides it)")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--runs", type=int, default=1)
    run.add_argument("--paraphrases", action="store_true")
    run.add_argument("--only", action="append", default=[], help="item id (dev only)")
    run.add_argument("--claude-bin", default="claude")
    run.add_argument("--model")
    run.add_argument("--timeout", type=int, default=900)
    run.add_argument("--max-turns", type=int)
    run.add_argument("--case-tools", action="store_true",
                     help="also allow tools that write the run's disposable case store "
                          "(never action tools); default: read-only tools only")
    run.add_argument("--evidence-label", choices=EVIDENCE_LABELS, required=True,
                     help="live-captures only when --state-dir holds live L1 captures")
    args = parser.parse_args(argv)
    if args.command == "gate":
        server = args.server[1:] if args.server[:1] == ["--"] else args.server
        if not server:
            parser.error("gate needs the host command after --")
        return serve_gate(args.allow, server, args.log, timing=args.timing)
    try:
        items = load_items(args.items, split=args.split)
        if args.command == "prompt":
            if args.split != "dev":
                raise RunnerError("held_out_prompts_are_never_printed")
            item = next((item for item in items if item["id"] == args.item), None)
            refused = next((r for r in items.refused if r["id"] == args.item), None)
            if refused is not None:
                raise RunnerError("item_refused:" + ",".join(refused["reasons"]))
            if item is None:
                raise RunnerError("no_such_item")
            texts = [item["question"]["text"]] + list(item["question"].get("paraphrases")
                                                      or [])
            if not 0 <= args.phrasing < len(texts):
                raise RunnerError("no_such_phrasing")
            print(build_prompt(item, texts[args.phrasing]))
            return 0
        if args.command == "replay":
            replayed = replay(items, args.transcripts)
            summary = public_summary(replayed["scores"], args.split, items.refused)
            summary["vs_manual"] = _vs_manual(replayed["scores"])
            summary.update(compared=replayed["compared"], identical=replayed["identical"])
            print(json.dumps(summary, indent=1, sort_keys=True))
            # Nothing compared (no stored scores) is not an identical replay.
            return 0 if replayed["ok"] else 1
        if args.only:
            if args.split != "dev":
                raise RunnerError("only_selects_dev_items")
            items = LoadedItems((item for item in items if item["id"] in set(args.only)),
                                (r for r in items.refused if r["id"] in set(args.only)))

        def host_for(item: Mapping[str, Any]) -> Host:
            principals = (item.get("actor") or {}).get("principals") or args.principal
            # Fixture or injected-transport state launches a --fixture host; live
            # captures need an employee identity (CaseHost employee_args).
            return CaseHost(args.state_dir, args.source_mirror_audience, args.tenant,
                            principals, fixture=args.evidence_label != "live-captures")
        agent = ClaudeCodeAgent(binary=args.claude_bin, model=args.model,
                                timeout=args.timeout, case_tools=args.case_tools,
                                max_turns=args.max_turns)
        if not any(_cites_rows(item) for item in items):
            # Items read only from pinned originals and mail (the exposed cases): no mirror
            # row to pin, so no run pin and no mirror needed.
            scores = run_items(items, agent, host_for, out=args.out, runs=args.runs,
                               paraphrases=args.paraphrases,
                               evidence_label=args.evidence_label)
        else:
            from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE
            with MirrorPinReader(args.state_dir / CASE_RUNTIME_DIR / MIRROR_FILE) as reader:
                scores = run_items(items, agent, host_for, out=args.out, runs=args.runs,
                                   paraphrases=args.paraphrases,
                                   evidence_label=args.evidence_label,
                                   pin_for=lambda item: make_run_pin(item, reader))
        summary = public_summary(scores, args.split, items.refused)
        summary["vs_manual"] = _vs_manual(scores)
        print(json.dumps(summary, indent=1, sort_keys=True))
    except (RunnerError, scoring.ScoreError, measuring.MeasureError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    return 3 if summary["fail_reasons"] else 0


if __name__ == "__main__":
    sys.exit(main())
