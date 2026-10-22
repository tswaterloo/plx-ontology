# Decision 2026-10-08: Adopted playbooks go to a state-dir overlay; partial reads block adoption; elicitation stays the steward check

- decided: 2026-10-08 under owner policy
- status: decided
- serves: RSN-08 (gap-23b27b024561), G-02 (gap-a425f3305ced)

## Decision

1. **Overlay, never the shipped directory.** `plx.playbook_adopt` never writes into `operating/playbooks`. An adoption writes its version into the overlay `playbooks.overlay_dir(state_dir)` = `<PLX_WORKBENCH_STATE_DIR | --state-dir>/playbooks`. `PlaybookRegistry.load_layers(shipped, overlay)` is the one loader of the playbooks in force: it loads the shipped directory, then the overlay when it exists. An overlay file is one more version of its id, so the latest version wins by version order, whichever layer holds it. An overlay never redefines a shipped version (`conflicting_definition`). Proposals, the layers digest (`tree_sha256`), the staged copies an evaluation scores and the adoption checks all cover both layers. A deploy that changes the shipped directory therefore changes the layers, and a proposal made before it is refused (`target_changed_since_proposal`). A store without an overlay (no state directory) proposes and evaluates, but `adopt` refuses with `adoption_directory_not_configured` before any record or form. `ProposalStore` refuses an overlay inside the shipped directory, or one that contains it.
2. **A partial read is unscored, and unscored blocks adoption.** The evaluator still reads the frozen cases as itself. A case it cannot read in full is reported `unscored`. Two signals mark such a case: an item is withheld from it (the case view withholds every item that has a citation this reader cannot resolve), or it cannot re-read a cited `api://` row (`playbook_eval.unread_evidence`). The comparison is recorded with `unscored_items`, and `plx.playbook_evaluate` answers `verdict: unscored`, so the reader sees it and it never passes silently. Adoption takes the latest comparison and refuses it while any case is unscored (`comparison_unscored`). A later comparison by a reader of the whole case unblocks it. An unscored case is not a recorded regression: it does not block the edit for good, because a reader with less access must not be able to veto an edit permanently. Any regression the runner does see in it still counts.
3. **Elicitation stays the steward check. The self-consent risk is ACCEPTED.** The adoption form goes over the same MCP transport as the tool call. An agent client that runs under a steward's identity can therefore answer the form itself, and the Workbench cannot prove that a person answered. Owner policy says no feature is degraded for security caution, so this risk is accepted explicitly. The `ADOPTION` and `APPLIED` records carry the steward principal and a `confirmation`: `via: mcp_elicitation`, the form id (spent once answered, never in a tool result), and the session client's declared `clientInfo` (name and version, as declared, never verified). This makes every adoption attributable after the fact. The follow-up surface is a human-authenticated console adopt.
4. **Minors.** `plx.playbook_propose` refuses a case whose change chain is broken (`verify_chain` false: `chain_broken:<index>`). `ProposalStore.propose` turns a rule that cannot be computed into `content_invalid`, never a raw `ValueError`. A failed move during an answer is the outcome `adoption_not_completed`: the proposal stays `adoption_pending`, and the steward's next form completes it.

## Follow-ups decided (2026-10-10)

5. **A case no reader can read is unscorable, not a veto.** Rule 2 made a partly read case
   `unscored`, which blocks. A frozen case whose citations resolve for NOBODY on the host would
   then block every adoption until the frozen set changes. When the evaluator reads a case only
   in part, the runner asks the host (`CaseRuntime.hidden_from_every_reader`) whether some item's
   citation resolves for no reader, from host facts alone: `source_not_configured` (a live or
   capture scheme without a reader or store on this host) or `mirror_rebuilt` (an `api://`
   citation minted by another mirror generation). Such a case is `unscorable` on both sides,
   with its reasons in the comparison (`unscorable`) and the tool output
   (`unscorable_reasons`), and is left out of the comparison. Adoption refuses when no
   frozen case is scorable (`no_scorable_case`) and, once any frozen case is unscorable, when
   fewer than half of the frozen cases are scored (`too_few_scored_cases`, minimum
   `semantic_proposals.MIN_SCORED_SHARE` = 1/2, exact fraction); in both cases
   `plx.playbook_evaluate` answers `verdict: unscorable` (amended 2026-10-10). Why a minimum: a mirror rebuild
   makes every `api://` citation of the old generation unscorable at once, so without one the
   regression set could shrink to a single case that no one chose. Why one half: most of the
   regression set the host froze must still be compared, and a small loss (one of two, two of
   three) still adopts, so one stale case never vetoes. A fully scored set adopts at any size;
   the minimum only applies when cases dropped out. The remedy is to re-freeze: the host
   replaces the unscorable case ids in its configuration (`plx-playbook-proposals/v1`) with
   cases investigated against the current mirror generation, then the edit is evaluated
   again. The store does not re-freeze by itself: the frozen set is the host's choice.
   A case hidden from this evaluator only (permission) stays `unscored` and
   blocks, as before. Any other unresolvable reason is not a host fact and stays `unscored`
   (fails closed). Any regression the runner sees in an unscorable case still counts. The
   reasons tell a reader of the case only that some citation's source is not configured here or
   predates the mirror rebuild, never which item or row.
6. **A comparison recorded before the full-case reader rule is never trusted.** A comparison
   without `unscored_items` was recorded by the old runner, which stored pass/fail even for a
   partial read. Adoption refuses it
   (`comparison_predates_full_read_rule`) and the edit is evaluated again; it is not
   re-scored from its stored reports.
7. **A failure before anything is recorded changes nothing.** When the overlay folder cannot be
   created, no decision is recorded, the proposal stays `evaluated` and the tool's last answer
   is `adoption_not_recorded` (a new request retries). `adoption_not_completed` keeps its
   meaning: the decision is recorded, its move is not, and the steward's next form completes it.
8. `verdict` is required in the `plx.playbook_evaluate` output contract
   (`non_regressing`, `regresses`, `unscored`, `unscorable`).

## Runbook: a deploy ships a version that clashes with an overlay file

Symptom: loading the playbooks fails with `conflicting_definition: <id> v<N>` naming the shipped
file and `<state dir>/playbooks/<file>`. The deploy shipped a version number an adoption already
used, with another definition. Nothing loads until it is cleared; it is never resolved by editing
either file.

1. Stop the host processes that serve this state directory.
2. Move the clashing overlay file out of `<state dir>/playbooks/` into a dated folder outside the
   state directory's `playbooks/` (keep it: its adoption records name its SHA-256). Never edit the
   shipped file or the change records.
3. Start the host. The playbooks load; the shipped version is in force.
4. If the adopted behaviour is still wanted, propose it again as a version later than every
   shipped one, and evaluate and adopt it. Proposals made before the move are refused
   (`target_changed_since_proposal`): the layers changed. The old adoption's records stay; its
   `adopt` answers `target_changed_since_adoption` (the overlay no longer holds it).

## Trade-off recorded

`rule_sha256` (the same-edit key that keeps a recorded regression blocking reworded re-proposals) is computed with the `playbooks.DESCRIPTIVE_FIELDS` in force when the proposal is recorded. A legacy proposal without one is pinned to the fields of its time (`_LEGACY_RULE_DROPS`). The converse is designed: after any later edit of `DESCRIPTIVE_FIELDS`, a new proposal of the same behaviour no longer matches an earlier recorded rule if it differs from that proposal only in a field the edit moved. Byte-identical content still matches by `content_sha256`. Recomputing recorded rules would rewrite append-only records, so this is accepted. Whoever edits `DESCRIPTIVE_FIELDS` should know that earlier regressions then block only their exact content.

## Evidence

- `tests/test_playbook_mcp_tools.py` covers propose, evaluate and adopt through `MCPServer` over the in-process FakeWorkbench (isolated-fixture / injected-transport, live-unverified). Adopt writes only to the temporary state directory's overlay. The shipped directory is byte-identical afterwards, and only the layered load serves the new version. A store without a state directory refuses before any form. An evaluator from whom the cited finding is withheld gets `unscored`, and adopt refuses until a reader of the whole case evaluates. A tampered chain gives `chain_broken:0`. Other tests cover: a request that changes before its answer, form expiry, an answer from another session, crash recovery through the tool, and the `--playbook-proposals` CLI wiring.
- `operating/tests/test_semantic_proposals.py`: 1 scored of 10 frozen with 9 `mirror_rebuilt` (and 4 of 10) is refused with `too_few_scored_cases`; 5 of 10 and 10 of 10 adopt. `tests/test_playbook_mcp_tools.py`: 1 scored of 3 answers `verdict: unscorable` and adopt refuses `too_few_scored_cases`; 1 of 2 adopts.
- `operating/tests/test_semantic_proposals.py`: overlay adoption, refusal of an overlay inside the shipped code, no overlay means no adoption, the typed `_rule` refusal, and the store-level crash and concurrency paths now aimed at the overlay.

## What would reverse it

- (3) A human-authenticated adopt surface (console) that lands. Elicitation would then stop being the steward check.
- (1) A requirement that adopted versions ship in Git. That would be a reviewed commit by a person, not a tool write.
