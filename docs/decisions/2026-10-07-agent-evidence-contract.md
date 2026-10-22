# Agent loop evidence contract (2026-10-07)

Decision. What the employee agent loop may cite is defined by the tool output
contract (`operating/ontology_tool_contracts.json`), never by the shape of a string.

Context. employee-host got two `changes_required` reviews in one defect class: the
loop counted echoed or unresolved handles as evidence. First, a planner's own query
came back in `plx.find` `gaps[].searched.queries`, so employee B "answered" citing
employee A's purchase-order handle after `not_found`. Then, after 549c4eac, `plx.read`
echoed its input in `output.oneOf[0].citation` (typed `$defs/handle`) even when status
was `withheld` or `unresolvable`, so B still cited A's withheld handle and an invented one.

Rule. A handle is evidence only if all of these hold:
1. A tool returned the record RESOLVED and readable for this principal: the result's
   status is `resolved` or `changed_since` (for `plx.read`, then `current_citation`,
   `cite_as` and extractions). `withheld`, `unresolvable`, `not_found` and the like yield none.
2. It sits at a position the tool's output contract types `$defs/handle`, within the
   `oneOf`/`anyOf` branch the validated result matches.
3. It is not an argument echo: a handle equal to a handle/citation argument of the same
   call counts only when the result resolved it, so the echo field alone never counts.

Never evidence: `untrusted_text` content, `gaps`, argument echoes, any handle-shaped
string found by scanning, and every result from a tool without an output contract.

Reference. employee-host 549c4eac `evidence_handles(tool, result)` (contract-typed
positions, replaced wholesale) implemented rule 2 and the exclusions; the repair
"a handle is evidence only from a resolved, non-echo result"
implements rules 1 and 3 (`RESOLVED_STATUSES`, and an argument handle counts only
inside a resolved-status object or beside the digest of what was read), with
regressions for withheld and unresolvable `plx.read` (operating/tests/test_agent_loop.py).
The open question
(contract positions versus an explicit per-call resolved/readable signal set by each
tool) is settled here as: contract positions gated by resolved status; a tool signal
may be added later but must not widen this set.

Status. Fixture-tested only; live-unverified (no live Entra).
