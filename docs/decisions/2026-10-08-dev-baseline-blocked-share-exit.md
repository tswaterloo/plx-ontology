# dev baseline: blocked_share stays red on every path

- decided: 2026-10-08
- status: adopted; no code change (the build already exits red).
- question: should a `blocked_share` failure on the daily live check exit red (as built) or be reported as blocked?

## Decision

It exits red, on every path: the dev baseline, the real-agent path and the ask path. A blocked answer is a failure of the product to answer, not an environmental skip.

## Evidence

`blocked_tripwire` was enforced only by the dev baseline, and the build's behaviour was an earlier choice. Reporting blocked as non-red would let a run that cannot read its sources pass.

## What would reverse it

Blocked answers that stem from causes outside the product (for example a live outage) dominating the daily check; then a separate `outage` verdict, not a green blocked, is the change.
