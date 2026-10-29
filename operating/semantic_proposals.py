"""Proposed playbook versions: the one adoption path (RSN-08, ENG-13).

A *proposal* adds one new version file to the playbooks in force: the shipped directory
(``operating/playbooks``, code: never written here) and the host's overlay of adopted versions
(``playbooks.overlay_dir`` of its state directory), loaded together by
``PlaybookRegistry.load_layers``. A published version
file of either layer is never rewritten; the new version is later than every version of its id,
uses no retired tool, and its id has a current version to be compared with. A kind is proposable
only when the store has a runner registered for it, and ``playbook`` is the only kind with one:
generator overrides and ontology plans have no item runner, so their proposals are refused
(``no_runner_for_kind``) and never reach a comparison.

Anyone may propose, an agent included. The proposal records the proposer's principals, its
evidence (non-empty citations) and rationale, the digest of the target directory and file it
was written against (both layers), and the store's frozen case ids at that moment. The edited
layers must load with ``PlaybookRegistry.load_layers``, else nothing is recorded.

The *evaluator* (``evaluate``) is a principal disjoint from the proposer. It names a runner id
registered on the store for the proposal's kind; it cannot hand in a callable.
``playbook_eval.proposal_store`` registers the one playbook runner, ``playbook_eval.case_runner``
(``PROPOSAL_RUNNER``). The store, not the evaluator, stages two copies of the layers (as they are,
and with the edit in the overlay) and runs every frozen case on both. The store builds the runner
with a recorder that stores each report in the same ``PlaybookReportStore`` as the evaluator's.
Every case result gives, per side, an ``outcome`` (``pass``/``fail``, ``unscored`` when the
evaluator could not read the whole case: ``playbook_eval.unread_evidence``, or ``unscorable`` on
both sides with the result's ``unscorable`` reasons when NO reader of this host can: a host fact,
``UNSCORABLE_REASONS``) and an ``evidence_sha256`` that names a report recorded during this
evaluation, and the runner's ``comparison`` of the two reports (``playbook_eval.compare``, the one
regression rule: newly failing or removed passing checks, an expected gap no longer ok, more
authority violations, a new original gap, a lost ``passed_all``; a case that changed between the
runs is no comparison). A case
unscored on either side is listed in ``unscored_items``: such a comparison shows no regression it
could not see, so it is never non-regressing and ``adopt`` refuses it (``comparison_unscored``)
until a later comparison by a reader of the whole case scores every case. An unscorable case is
listed with its reasons in ``unscorable``: no reader can score it, so it is left out of the
comparison rather than blocking every adoption until the frozen set changes; ``adopt`` refuses
a comparison in which no frozen case is scored (``no_scorable_case``) or, once any case is left
out, fewer than ``MIN_SCORED_SHARE`` of the frozen cases are (``too_few_scored_cases``: a mirror
rebuild must not shrink the regression set to a sample; the host re-freezes the unscorable cases
against the current generation first). Any regression the
runner does see in an unscored or unscorable case still counts. The store keeps no
regression rule of its own: it records the runner id, a fingerprint of the runner arguments
(``_fingerprint``), outcomes, digests, report ids and each case's comparison, and
``playbooks.diff`` against the current version (widened authority or a weakened check is a
regression whatever the cases say). A malformed result, or a run that raises, is no comparison: an
``EVALUATION_FAILED`` record keeps the runner arguments, the reports already recorded and any
comparison already made, and its regression counts.

Only ``adopt`` changes a target, and only when every rule holds, re-derived from the records at
that moment: the actor is one human employee principal (``kind_confirmation.human_principal``: an
agent or service session is refused) named in the store's stewards, not the proposer and no
evaluator of the same edit; the latest comparison covers exactly the proposal's frozen case ids
before and after, which are still the store's frozen ids, with a digest and a comparison for every
case, by a runner still registered for the kind; it was made on this edit of this target state, it
was recorded under the full-case reader rule (a comparison without ``unscored_items`` predates it:
its pass/fail may rest on a partial read, so it is refused, ``comparison_predates_full_read_rule``,
and the edit is evaluated again), it scored every case that any reader can score, at least one
and, when any is unscorable, at least ``MIN_SCORED_SHARE`` of the frozen cases;
NO comparison or failed evaluation of the same edit (the same kind and either
the same file content or the same rule: playbook id and ``playbooks.behavioural_body``, so a
changed version number or wording is the same edit; whatever else the directory held, whichever
proposal and file name carried it and whatever the runner arguments) regresses, so a regression is
never re-run or re-proposed away, and the playbook diff neither widens nor weakens; the layers are
unchanged since the proposal, the store has an overlay (else ``adoption_directory_not_configured``:
a store without a state directory adopts nothing), and no other adoption of the kind is still
waiting for its move. The adoption is then applied in three steps: the decision (``ADOPTION``, with
what the overlay file held and the caller's ``confirmation``: for the MCP tool the form id and the
client's declared ``clientInfo``) commits, the file is moved atomically into the overlay, and the
move is confirmed (``APPLIED``, with the completing call's confirmation) in one write transaction
held across the move, so no other adoption interleaves with it. A crash before the confirmation
leaves the proposal ``adoption_pending``, which refuses every other adoption of the kind; a
steward's retry of ``adopt`` completes it while the layers, its file aside, are still the
proposal's and the file holds nothing or the adopted content. A confirmed adoption is never
re-applied: when its file no longer holds the adopted content ``adopt`` refuses
(``target_changed_since_adoption``); repairing the overlay is a separate, explicit act. The overlay
never holds an unrecorded version. Whatever was already loaded keeps the old content, and the
version takes effect when the layers are next loaded. Nothing adopts on anyone's behalf: there is
no default steward, and ``adopt`` acts only for the human steward actor its caller holds.

Records: the change chain of a ``playbook_reports.PlaybookReportStore`` (``changes``: one write
transaction per step). Known limits: the chain is plain sha256 (whoever can write the file can
reseal it); principals are those of the in-process ``Actor`` (the same trust as kind
confirmations); a runner argument with no JSON form (the evaluator's session ``call``) is
fingerprinted by its type only; the records have no reader API or per-record audience; the MCP
tools ``plx.playbook_propose``/``_evaluate``/``_adopt`` (``operating.playbook_tools``) are the one
product surface (no console surface). An adoption whose move cannot complete (its edit no longer
loads, or a layer changed, a deploy of the shipped directory included) stays ``adoption_pending``
and blocks further adoptions of its kind until the layers are repaired. A deploy that ships a
version an overlay file already holds with another definition fails every load
(``conflicting_definition``) until the overlay file is moved out (runbook: decision
2026-10-08-playbook-adopt-overlay). A regression recorded for an edit blocks that content and rule
for good, whatever the directory around it: a corrected version is a new proposal with another rule.
A proposal recorded before ``rule_sha256`` existed has its rule computed from its stored content
with the fields dropped when the rule was introduced, so a later change to
``playbooks.DESCRIPTIVE_FIELDS`` cannot move it; one whose content no longer loads is matched on
content only. The converse is a designed trade-off: a recorded ``rule_sha256`` keeps the
DESCRIPTIVE_FIELDS of its time, so after an edit of them a new proposal of the same behaviour that
differs from a recorded regressing one in a field the edit moved no longer matches its rule (the
same content still matches by content).
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .kind_confirmation import ConfirmationRefused, human_principal, is_agent_principal
from .playbooks import PLAYBOOK_DIR, Playbook, PlaybookRegistry, behavioural_body, diff

PROPOSAL = "plx-semantic-proposal/v1"
COMPARISON = "plx-semantic-comparison/v1"
ADOPTION = "plx-semantic-adoption/v1"
APPLIED = "plx-semantic-adoption-applied/v1"
EVALUATION_FAILED = "plx-semantic-evaluation-failed/v1"
KINDS = ("playbook",)
SCORED = ("pass", "fail")
OUTCOMES = (*SCORED, "unscored", "unscorable")
# Why no reader of the host can score a case (``CaseRuntime.hidden_from_every_reader``).
UNSCORABLE_REASONS = ("mirror_rebuilt", "source_not_configured")
# The share of frozen cases a comparison must score once any is unscorable (decision
# 2026-10-08-playbook-adopt-overlay, rule 5): most of the regression set the host froze.
MIN_SCORED_SHARE = Fraction(1, 2)
MAX_EVIDENCE = 50
# Overlay entries of a layers digest; with an empty overlay the digest is the shipped directory's
# alone, as recorded before the overlay existed.
OVERLAY_MARK = "overlay/"
_FILE = re.compile(r"[a-z0-9][a-z0-9_]{0,79}\.json")
_SHA = re.compile(r"[0-9a-f]{64}")
# The playbooks in force: (shipped directory, overlay directory or None), loaded with
# ``PlaybookRegistry.load_layers``.
Layers = tuple[Path, Path | None]
# run(before_layers, after_layers, case_id) -> {"before": {"outcome", "evidence_sha256"},
#                                               "after": {...}, "comparison": {...}}
RunItem = Callable[[Layers, Layers, str], Mapping[str, Any]]
# record(report, *, transcript_source) -> report id: stores a report as the evaluator's
Record = Callable[..., int]
# runner(proposal, record, **runner_args) -> RunItem
Runner = Callable[..., RunItem]


class ProposalRefused(PermissionError):
    """A proposal, evaluation or adoption refused; ``code`` says why."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"proposal_refused:{code}" + (f": {detail[:300]}" if detail else ""))
        self.code = code


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_bytes(content: Mapping[str, Any]) -> bytes:
    return (json.dumps(content, indent=1, ensure_ascii=False) + "\n").encode()


# The paths ``behavioural_body`` dropped when ``rule_sha256`` was first recorded. A proposal
# recorded before then has its rule computed with them, never the current DESCRIPTIVE_FIELDS:
# its record cannot be rewritten, so its rule must not move when those fields change.
_LEGACY_RULE_DROPS = ("schema", "version", "question_pattern", "required_evidence[].key_hint",
                      "expected_gaps[].about")


def _rule(content: Mapping[str, Any], dropped: Sequence[str] | None = None) -> str:
    return _sha(_canonical(behavioural_body(Playbook(content).definition, dropped)))


def _rule_sha256(proposal: Mapping[str, Any]) -> str | None:
    """The proposal's rule (module doc): recorded, or computed with ``_LEGACY_RULE_DROPS`` from
    the stored content of a proposal recorded before ``rule_sha256`` existed (None when that
    content no longer loads)."""
    if "rule_sha256" in proposal:
        return proposal["rule_sha256"]
    try:
        return _rule(proposal["content"], _LEGACY_RULE_DROPS)
    except ValueError:
        return None


def _tree_sha256(layers: Layers, without: str | None = None) -> str:
    """Digest of both layers (``without``: that overlay file left out)."""
    shipped, overlay = layers
    entries = [(p.name, _sha(p.read_bytes())) for p in shipped.glob("*.json")]
    if overlay is not None and overlay.exists():
        entries += [(OVERLAY_MARK + p.name, _sha(p.read_bytes()))
                    for p in overlay.glob("*.json") if p.name != without]
    return _sha(_canonical(sorted(entries)))


def _fingerprint(runner_args: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """What an evaluation ran with, per runner argument: the sha256 of its canonical JSON or,
    for a value without one (a session callable, a generator), its type, marked opaque."""
    out = {}
    for name in sorted(runner_args):
        value = runner_args[name]
        try:
            out[name] = {"sha256": _sha(_canonical(value))}
        except (TypeError, ValueError):
            out[name] = {"opaque": f"{type(value).__module__}.{type(value).__qualname__}"}
    return out


def _report_sha(side: Any) -> Any:
    return side.get("report_sha256") if isinstance(side, Mapping) else None


def _principals(actor: Any) -> list[str]:
    return sorted({str(p) for p in (getattr(actor, "principals", None) or ())})


def _unscorable(run: Mapping[str, Any], item_id: str) -> bool:
    """True when a comparison or run record holds ``item_id`` unscorable on both sides, with its
    reasons."""
    return (bool((run.get("unscorable") or {}).get(item_id))
            and (run.get("before") or {}).get(item_id) == "unscorable"
            == (run.get("after") or {}).get(item_id))


def scored_share_refusal(frozen: int, unscorable: int) -> str | None:
    """The refusal code when ``frozen - unscorable`` scored cases are too few to adopt on (none,
    or below ``MIN_SCORED_SHARE`` of ``frozen`` once any is unscorable), else None."""
    scored = frozen - unscorable
    if scored <= 0:
        return "no_scorable_case"
    if unscorable and Fraction(scored, frozen) < MIN_SCORED_SHARE:
        return "too_few_scored_cases"
    return None


def _unscored(run: Mapping[str, Any]) -> list[str]:
    """The cases a comparison or run record did not score on both sides and that are not
    unscorable (any other outcome but pass or fail: fails closed on an outcome this module does
    not know)."""
    before, after = run.get("before") or {}, run.get("after") or {}
    return [i for i in run["item_ids"]
            if i in before and not _unscorable(run, i)
            and (before[i] not in SCORED or after.get(i) not in SCORED)]


@contextmanager
def _staged(layers: Layers, file: str, content: Mapping[str, Any] | None) -> Iterator[Layers]:
    """Private copies of the layers, with ``content`` written as ``file`` into the overlay copy
    when given."""
    shipped, overlay = layers
    with tempfile.TemporaryDirectory(prefix="plx-proposal-") as tmp:
        staged = (Path(tmp) / "shipped", Path(tmp) / "overlay")
        shutil.copytree(shipped, staged[0])
        if overlay is not None and overlay.exists():
            shutil.copytree(overlay, staged[1])
        else:
            staged[1].mkdir()
        if content is not None:
            (staged[1] / file).write_bytes(_file_bytes(content))
        yield staged


def _replace(root: Path, file: str, data: bytes) -> None:
    """Write ``data`` as ``root/file`` atomically and durably (temp file, fsync, rename)."""
    fd, tmp = tempfile.mkstemp(dir=root, prefix=f".{file}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, root / file)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    directory = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _playbook_reasons(layers: Layers, content: Mapping[str, Any]) -> list[str]:
    """Why the proposed playbook version asks less or may do more than the current one."""
    new = Playbook(content)
    change = diff(PlaybookRegistry().load_layers(*layers).get(new.id), new)
    return ([f"widens_authority:{name}" for name in
             change["tools"]["added"] + change["writes_case_state"]["added"]]
            + change["weakening_reasons"])


class ProposalStore:
    """Proposals, comparisons and adoptions of playbook versions.

    ``reports`` is the ``PlaybookReportStore`` whose change chain keeps the records.
    ``runners`` maps a kind to its registered runners by id (``Runner``); a kind without one is
    neither proposable nor evaluable. ``frozen_items`` names, per kind, the case ids every
    comparison runs. ``stewards`` are the human employee principals who may adopt; none by
    default. ``shipped`` overrides a kind's read-only directory (default ``PLAYBOOK_DIR``);
    ``overlays`` names the directory a kind's adopted versions are written to
    (``playbooks.overlay_dir`` of the host's state directory), never the shipped one; without
    one the store proposes and evaluates but adopts nothing.
    """

    def __init__(self, reports: Any, *, runners: Mapping[str, Mapping[str, Runner]],
                 frozen_items: Mapping[str, Sequence[str]], stewards: Iterable[str] = (),
                 shipped: Mapping[str, str | Path] | None = None,
                 overlays: Mapping[str, str | Path | None] | None = None) -> None:
        unknown = sorted({*runners, *frozen_items, *(shipped or {}), *(overlays or {})}
                         - set(KINDS))
        if unknown:
            raise ValueError(f"unknown target kinds: {unknown}")
        self._runners: dict[str, dict[str, Runner]] = {}
        for kind, registered in runners.items():
            if not registered or not all(isinstance(i, str) and i and callable(r)
                                         for i, r in registered.items()):
                raise ValueError(f"runners[{kind}] must map runner ids to runners")
            self._runners[kind] = dict(registered)
        self._frozen: dict[str, tuple[str, ...]] = {}
        for kind, ids in frozen_items.items():
            if isinstance(ids, str) or not ids or not all(isinstance(i, str) and i for i in ids):
                raise ValueError(f"frozen_items[{kind}] must be a non-empty list of item ids")
            self._frozen[kind] = tuple(sorted(set(ids)))
        self._stewards = frozenset(stewards)
        if any(not s.startswith("employee:") or is_agent_principal(s) for s in self._stewards):
            raise ValueError("stewards must be employee principals")
        self._layers: dict[str, Layers] = {}
        for kind in KINDS:
            base = Path((shipped or {}).get(kind) or PLAYBOOK_DIR)
            overlay = (overlays or {}).get(kind)
            overlay = None if overlay is None else Path(overlay)
            if overlay is not None and any(
                    a.is_relative_to(b) or b.is_relative_to(a) for a, b in (
                        (overlay.resolve(), code.resolve()) for code in (base, PLAYBOOK_DIR))):
                raise ValueError("an adoption overlay never overlaps the shipped playbooks")
            self._layers[kind] = (base, overlay)
        self._reports = reports

    # ------------------------------------------------------------------ propose

    def propose(self, actor: Any, *, kind: str, file: str, content: Mapping[str, Any],
                rationale: str, evidence: Sequence[str]) -> str:
        """Record a proposal (idempotent: the same proposal returns its id) and return its id."""
        if not isinstance(kind, str) or kind not in self._runners:
            raise ProposalRefused("no_runner_for_kind", str(kind)[:40])
        if not isinstance(file, str) or not _FILE.fullmatch(file):
            raise ProposalRefused("bad_target_file")
        if kind not in self._frozen:
            raise ProposalRefused("no_frozen_items_for_kind", kind)
        proposer = _principals(actor)
        if not proposer:
            raise ProposalRefused("no_principal")
        if not isinstance(content, Mapping):
            raise ProposalRefused("content_not_an_object")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ProposalRefused("rationale_required")
        if (isinstance(evidence, str) or not evidence or len(evidence) > MAX_EVIDENCE
                or not all(isinstance(e, str) and e.strip() for e in evidence)):
            raise ProposalRefused("evidence_required", "a non-empty list of citations")
        content = json.loads(_canonical(content))
        layers = self._layers[kind]
        with self._reports.changes() as (events, append):
            if any(layer is not None and (layer / file).exists() for layer in layers):
                raise ProposalRefused("published_playbook_version_is_immutable")
            self._check_playbook(layers, content)
            self._load_edited(layers, file, content)
            try:
                rule = _rule(content)
            except ValueError as error:
                raise ProposalRefused("content_invalid", str(error)) from None
            event = {"schema": PROPOSAL, "kind": kind, "file": file, "content": content,
                     "content_sha256": _sha(_file_bytes(content)), "base_sha256": None,
                     "tree_sha256": _tree_sha256(layers), "rationale": rationale.strip(),
                     "evidence": [e.strip() for e in evidence], "proposer": proposer,
                     "item_ids": list(self._frozen[kind])}
            event["id"] = "sp-" + _sha(_canonical(event))[:24]
            # a function of the content, so not part of the id (ids recorded before it hold)
            event["rule_sha256"] = rule
            if not any(e["schema"] == PROPOSAL and e["id"] == event["id"] for e in events):
                append(event)
        return event["id"]

    @staticmethod
    def _check_playbook(layers: Layers, content: Mapping[str, Any]) -> None:
        try:
            new = Playbook(content)
            versions = [row["version"] for row in PlaybookRegistry().load_layers(*layers).listing()
                        if row["id"] == new.id]
        except (ValueError, OSError) as error:
            raise ProposalRefused("content_invalid", str(error)) from None
        if new.retired:
            raise ProposalRefused("playbook_uses_retired_tools")
        if not versions:
            raise ProposalRefused("no_current_playbook_to_compare")
        if new.version <= max(versions):
            raise ProposalRefused("playbook_version_not_later")

    @staticmethod
    def _load_edited(layers: Layers, file: str, content: Mapping[str, Any]) -> None:
        with _staged(layers, file, content) as staged:
            try:
                PlaybookRegistry().load_layers(*staged)
            except (ValueError, OSError) as error:
                raise ProposalRefused("content_invalid", str(error)) from None

    # ------------------------------------------------------------------ evaluate

    def evaluate(self, proposal_id: str, actor: Any, runner_id: str,
                 **runner_args: Any) -> dict[str, Any]:
        """Run every frozen case of the proposal before and after the edit with the registered
        runner ``runner_id`` (built with ``runner_args``); store the comparison. A run that
        raises or gives a malformed result stores an ``EVALUATION_FAILED`` record and re-raises."""
        evaluator = _principals(actor)
        with self._reports.changes() as (events, _append):
            proposal = self._proposal(events, proposal_id)
        kind = proposal["kind"]
        if kind not in self._runners:
            raise ProposalRefused("no_runner_for_kind", kind)
        runner = self._runners[kind].get(runner_id) if isinstance(runner_id, str) else None
        if runner is None:
            raise ProposalRefused("unregistered_runner", str(runner_id)[:80])
        if not evaluator:
            raise ProposalRefused("no_principal")
        if set(evaluator) & set(proposal["proposer"]):
            raise ProposalRefused("evaluator_is_proposer")
        layers = self._layers[kind]
        if _tree_sha256(layers) != proposal["tree_sha256"]:
            raise ProposalRefused("target_changed_since_proposal")
        stored: dict[str, int] = {}

        def record(report: Mapping[str, Any], *, transcript_source: str) -> int:
            row = self._reports.record(report, evaluated_by=",".join(evaluator),
                                       transcript_source=transcript_source)
            stored[row["report_sha256"]] = row["id"]
            return row["id"]
        run_record = {"proposal_id": proposal_id, "evaluator": evaluator, "runner": runner_id,
                      "runner_args": _fingerprint(runner_args),
                      "item_ids": list(proposal["item_ids"]),
                      "item_set_sha256": _sha(_canonical(proposal["item_ids"])),
                      "tree_sha256": proposal["tree_sha256"],
                      "content_sha256": proposal["content_sha256"],
                      "before": {}, "after": {}, "unscorable": {}, "evidence": {},
                      "item_comparisons": {}, "reports": stored}
        try:
            run = runner(json.loads(_canonical(proposal)), record, **runner_args)
            with ExitStack() as stack:
                before = stack.enter_context(_staged(layers, proposal["file"], None))
                after = stack.enter_context(_staged(layers, proposal["file"], proposal["content"]))
                reasons = _playbook_reasons(before, proposal["content"])
                for item_id in proposal["item_ids"]:
                    self._take(run_record, item_id, run(before, after, item_id), stored)
            regressed = self._regressed(run_record)
            event = {"schema": COMPARISON, **json.loads(_canonical(run_record)),
                     "regressed_items": regressed, "unscored_items": _unscored(run_record),
                     "playbook_reasons": reasons, "regression": bool(regressed or reasons)}
            with self._reports.changes() as (events, append):
                self._proposal(events, proposal_id)
                if _tree_sha256(layers) != proposal["tree_sha256"]:
                    raise ProposalRefused("target_changed_since_proposal")
                append(event)
        except Exception as error:
            regressed = self._regressed(run_record)
            with self._reports.changes() as (_events, append):
                append({"schema": EVALUATION_FAILED, **run_record, "regressed_items": regressed,
                        "unscored_items": _unscored(run_record),
                        "regression": bool(regressed), "error": type(error).__name__,
                        "code": getattr(error, "code", None)})
            raise
        return event

    @staticmethod
    def _take(run_record: dict[str, Any], item_id: str, result: Any,
              stored: Mapping[str, int]) -> None:
        """Add one case's result to ``run_record`` once it is well formed: an outcome and a
        digest of a report recorded in this evaluation per side (``unscorable`` on both sides or
        neither, and then with known reasons), and a comparison of exactly those two reports."""
        sides = {}
        for side in ("before", "after"):
            got = result.get(side) if isinstance(result, Mapping) else None
            outcome = got.get("outcome") if isinstance(got, Mapping) else None
            digest = got.get("evidence_sha256") if isinstance(got, Mapping) else None
            if outcome not in OUTCOMES or not isinstance(digest, str) or not _SHA.fullmatch(digest):
                raise ProposalRefused("bad_item_result", f"{item_id} ({side})")
            sides[side] = (outcome, digest)
        if any(digest not in stored for _outcome, digest in sides.values()):
            raise ProposalRefused("evidence_not_recorded", item_id)
        reasons = result.get("unscorable")
        if ((sides["before"][0] == "unscorable" or sides["after"][0] == "unscorable"
             or reasons is not None)
                and (sides["before"][0] != "unscorable" or sides["after"][0] != "unscorable"
                     or not isinstance(reasons, list) or not reasons
                     or not set(reasons) <= set(UNSCORABLE_REASONS))):
            raise ProposalRefused("bad_item_result", f"{item_id} (unscorable)")
        comparison = result.get("comparison")
        if (not isinstance(comparison, Mapping)
                or not isinstance(comparison.get("regression"), bool)
                or (_report_sha(comparison.get("from")), _report_sha(comparison.get("to")))
                != (sides["before"][1], sides["after"][1])):
            raise ProposalRefused("bad_item_result", f"{item_id} (comparison)")
        comparison = json.loads(_canonical(comparison))
        for side, (outcome, digest) in sides.items():
            run_record[side][item_id] = outcome
            run_record["evidence"].setdefault(item_id, {})[side] = digest
        run_record["item_comparisons"][item_id] = comparison
        if reasons is not None:
            run_record["unscorable"][item_id] = sorted(set(reasons))

    @staticmethod
    def _regressed(run_record: Mapping[str, Any]) -> list[str]:
        """The cases whose recorded comparison regresses (the runner's verdict, never re-made)."""
        return [i for i in run_record["item_ids"]
                if i in run_record["item_comparisons"]
                and run_record["item_comparisons"][i]["regression"] is not False]

    # ------------------------------------------------------------------ adopt

    def adopt(self, proposal_id: str, actor: Any, *,
              confirmation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """A human steward adopts a proposal whose latest comparison holds and no comparison
        of whose edit regresses, or completes an adoption recorded but not yet confirmed.
        ``confirmation``: how the caller confirmed the steward (recorded as given)."""
        steward = self.steward_of(actor)
        confirmation = None if confirmation is None else json.loads(_canonical(confirmation))
        with self._reports.changes() as (events, append):
            if self._adoption(events, proposal_id) is None:
                decision = self._decide(events, proposal_id, steward)
                # the overlay (``_decide`` refuses a store without one) exists before anything
                # is recorded: a missing state directory leaves no record
                self._layers[decision["kind"]][1].mkdir(exist_ok=True)
                append({**decision, "confirmation": confirmation})
        adoption, applied = self._apply(proposal_id, steward, confirmation)
        if applied is None:
            raise ProposalRefused("already_adopted")
        return adoption

    def steward_of(self, actor: Any) -> str:
        """The one human employee principal of ``actor`` when it is a steward, else
        ``ProposalRefused`` (an agent or service session is never a steward)."""
        try:
            steward = human_principal(actor)
        except ConfirmationRefused:
            raise ProposalRefused("steward_must_be_one_human_employee") from None
        if steward not in self._stewards:
            raise ProposalRefused("not_a_steward")
        return steward

    def check_adoption(self, proposal_id: str, actor: Any) -> dict[str, Any]:
        """The adoption record ``adopt`` would commit now for ``actor``, with the proposed
        playbook's id and version and the scope of the comparison it rests on (``frozen``: the
        frozen case count; ``unscorable``: the cases left out, with their reasons), committing
        nothing: every rule of ``adopt`` is re-derived from the records (``ProposalRefused``
        when one fails)."""
        steward = self.steward_of(actor)
        with self._reports.changes() as (events, _append):
            decision = self._decide(events, proposal_id, steward)
            content = self._proposal(events, proposal_id)["content"]
            comparison = [e for e in events if e["schema"] == COMPARISON
                          and e["proposal_id"] == proposal_id][-1]
        return {**decision, "playbook": {"id": content.get("id"),
                                         "version": content.get("version")},
                "frozen": len(comparison["item_ids"]),
                "unscorable": {i: comparison["unscorable"][i] for i in comparison["item_ids"]
                               if _unscorable(comparison, i)}}

    def _decide(self, events: list[dict[str, Any]], proposal_id: str,
                steward: str) -> dict[str, Any]:
        """The adoption record, once every rule holds on ``events`` (module doc)."""
        proposal = self._proposal(events, proposal_id)
        layers = self._layers[proposal["kind"]]
        if layers[1] is None:
            raise ProposalRefused("adoption_directory_not_configured")
        if steward in proposal["proposer"]:
            raise ProposalRefused("steward_is_proposer")
        edit = (proposal["tree_sha256"], proposal["content_sha256"])
        # the same edit is the same content or rule of the same kind, keyed by the proposals'
        # own records: not the directory state, the file name or the runner arguments
        rule = _rule_sha256(proposal)
        same_edit = {e["id"] for e in events if e["schema"] == PROPOSAL
                     and e["kind"] == proposal["kind"]
                     and (e["content_sha256"] == proposal["content_sha256"]
                          or (rule is not None and _rule_sha256(e) == rule))}
        runs = [e for e in events if e["schema"] in (COMPARISON, EVALUATION_FAILED)
                and e.get("proposal_id") in same_edit]
        comparisons = [e for e in events
                       if e["schema"] == COMPARISON and e["proposal_id"] == proposal_id]
        if not comparisons:
            raise ProposalRefused("no_comparison")
        comparison = comparisons[-1]
        if any(steward in e["evaluator"] for e in runs):
            raise ProposalRefused("steward_is_evaluator")
        kind, ids = proposal["kind"], proposal["item_ids"]
        before, after = comparison.get("before") or {}, comparison.get("after") or {}
        if not (list(self._frozen.get(kind, ())) == ids == comparison["item_ids"]
                and sorted(before) == sorted(after) == sorted(ids)):
            raise ProposalRefused("item_set_differs")
        runner_id = comparison.get("runner")
        if not isinstance(runner_id, str) or runner_id not in self._runners.get(kind, {}):
            raise ProposalRefused("unregistered_runner", str(runner_id)[:80])
        evidence = comparison.get("evidence") or {}
        if (sorted(evidence) != sorted(ids)
                or sorted(comparison.get("item_comparisons") or {}) != sorted(ids)
                or any(sorted(evidence[i]) != ["after", "before"] for i in ids)):
            raise ProposalRefused("evidence_missing")
        if (comparison["tree_sha256"], comparison["content_sha256"]) != edit:
            raise ProposalRefused("comparison_not_of_this_edit")
        confirmed = {e["proposal_id"] for e in events if e["schema"] == APPLIED}
        pending = [e["proposal_id"] for e in events if e["schema"] == ADOPTION
                   and e["kind"] == kind and e["proposal_id"] not in confirmed]
        if pending:
            raise ProposalRefused("adoption_pending", pending[0])
        if _tree_sha256(layers) != proposal["tree_sha256"]:
            raise ProposalRefused("target_changed_since_proposal")
        regressing = [e for e in runs if e.get("regression") is not False]
        reasons = _playbook_reasons(layers, proposal["content"])
        if regressing or reasons:
            items = sorted({i for e in regressing for i in e.get("regressed_items") or ()})
            raise ProposalRefused("comparison_regresses",
                                  ", ".join(items + reasons) or "recorded as regressing")
        if "unscored_items" not in comparison:
            # recorded before the full-case reader rule: its pass/fail may be a partial read
            raise ProposalRefused("comparison_predates_full_read_rule", "evaluate it again")
        unscored = _unscored(comparison)
        if unscored:
            raise ProposalRefused("comparison_unscored", ", ".join(unscored))
        left_out = sum(_unscorable(comparison, i) for i in ids)
        refusal = scored_share_refusal(len(ids), left_out)
        if refusal:
            raise ProposalRefused(refusal, f"{len(ids) - left_out} of {len(ids)} frozen cases "
                                           f"scored, {left_out} unscorable")
        self._load_edited(layers, proposal["file"], proposal["content"])
        target = layers[1] / proposal["file"]
        return {"schema": ADOPTION, "proposal_id": proposal_id, "steward": steward,
                "comparison_sha256": _sha(_canonical(comparison)), "kind": kind,
                "file": proposal["file"],
                "replaced_sha256": _sha(target.read_bytes()) if target.exists() else None,
                "adopted_sha256": _sha(_file_bytes(proposal["content"]))}

    def _apply(self, proposal_id: str, steward: str, confirmation: Any
               ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Move the recorded adoption's content into its overlay file and confirm it, in one
        write transaction (no other adoption interleaves). Returns the adoption and the
        confirmation, or None when it was already confirmed and the file holds the content.
        A confirmed adoption is never re-applied, and an unconfirmed one only completes while
        the layers, its file aside, are the proposal's and the file holds nothing or the
        adopted content (a crash after the move)."""
        with self._reports.changes() as (events, append):
            adoption = self._adoption(events, proposal_id)
            proposal = self._proposal(events, proposal_id, adopted_ok=True)
            data = _file_bytes(proposal["content"])
            if (adoption is None or adoption["kind"] not in self._layers
                    or (adoption["kind"], adoption["file"], adoption["adopted_sha256"])
                    != (proposal["kind"], proposal["file"], _sha(data))):
                raise ProposalRefused("adoption_record_mismatch")
            layers = self._layers[adoption["kind"]]
            overlay = layers[1]
            if overlay is None:
                raise ProposalRefused("adoption_directory_not_configured")
            target = overlay / adoption["file"]
            held = _sha(target.read_bytes()) if target.exists() else None
            if any(e["schema"] == APPLIED and e["proposal_id"] == proposal_id for e in events):
                if held == adoption["adopted_sha256"]:
                    return adoption, None
                raise ProposalRefused("target_changed_since_adoption")
            if (held not in (None, adoption["adopted_sha256"])
                    or _tree_sha256(layers, without=adoption["file"]) != proposal["tree_sha256"]):
                raise ProposalRefused("target_changed_since_proposal")
            if held is None:
                self._load_edited(layers, adoption["file"], proposal["content"])
                overlay.mkdir(exist_ok=True)
                _replace(overlay, adoption["file"], data)
            applied = {"schema": APPLIED, "proposal_id": proposal_id, "by": steward,
                       "kind": adoption["kind"], "file": adoption["file"],
                       "applied_sha256": adoption["adopted_sha256"],
                       "replaced_sha256": adoption.get("replaced_sha256"),
                       "confirmation": confirmation}
            append(applied)
        return adoption, applied

    def status(self, proposal_id: str) -> str:
        """``proposed``, ``evaluated``, ``adoption_pending`` (recorded, move not confirmed) or
        ``adopted``."""
        with self._reports.changes() as (events, _append):
            self._proposal(events, proposal_id, adopted_ok=True)
            mine = {e["schema"] for e in events
                    if e["schema"] != PROPOSAL and e.get("proposal_id") == proposal_id}
        return ("adopted" if APPLIED in mine else "adoption_pending" if ADOPTION in mine
                else "evaluated" if COMPARISON in mine else "proposed")

    @staticmethod
    def _adoption(events: list[dict[str, Any]], proposal_id: str) -> dict[str, Any] | None:
        return next((e for e in events
                     if e["schema"] == ADOPTION and e["proposal_id"] == proposal_id), None)

    @staticmethod
    def _proposal(events: list[dict[str, Any]], proposal_id: str, *,
                  adopted_ok: bool = False) -> dict[str, Any]:
        found = next((e for e in events if e["schema"] == PROPOSAL and e["id"] == proposal_id), None)
        if found is None:
            raise ProposalRefused("unknown_proposal")
        if not adopted_ok and any(e["schema"] == ADOPTION and e["proposal_id"] == proposal_id
                                  for e in events):
            raise ProposalRefused("already_adopted")
        return found


__all__ = ["ADOPTION", "APPLIED", "COMPARISON", "EVALUATION_FAILED", "KINDS", "Layers",
           "OUTCOMES", "PROPOSAL", "ProposalRefused", "ProposalStore", "Record", "RunItem",
           "Runner", "SCORED", "UNSCORABLE_REASONS"]
