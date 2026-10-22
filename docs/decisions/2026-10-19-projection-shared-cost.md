# Decision 2026-10-19: ontology.projection keeps its shared-store run with a 2,100 s checks budget

- decided: 2026-10-19 (closes open-verifier-shared-store-estimate-projection-shared-cost, open since 2026-10-18)
- status: decided; reversal condition met 2026-10-19 (below)

## Question

`ontology.projection` named no non-build budget, so the daily rotation priced its shared cost at its 5,400 s timeout, which fits no 7,200 s command after its producers, and the set settled it as `session_over_budget` without ever measuring it. How should it get a shared cost that fits?

## Options

1. Keep the typed fail: honest, but the check never measures anything.
2. Keep the projection on the shared store and bound its claims, trim and rebuild with `--checks-budget-seconds`, which the verifier reads as the non-build budget.
3. Run it once shared in a longer dedicated command to measure.

## Decision

Option 2 with `--checks-budget-seconds 2100` in `verify/live-checks/ontology.projection.json` (was 2700). The producers measure 3,741 s; 3,741 + 1.5 x 2,100 = 6,891 s stays under the 7,000 s window (at 2,700 it would be 7,791 s). No code change.

## Reversal condition

Revisit if the producers grow past 3,850 s (3,850 + 3,150 = 7,000 s) or the command window changes. The first measured shared run replaces the forecast.

## Measured 2026-10-19: the condition is met

Producer runs in `results/daily/` (duration_s; session build = its `build_seconds`, BC = `bc_capture` seconds; every session run mode cold, 1.6M objects, pass):

| session_projection checked_at | mirror.full_coverage | session_projection | build | BC capture | producers |
|---|---|---|---|---|---|
| 2026-10-18T00:46:58Z | 1,518 | 3,080 | 2,786 | 288 | 4,598 |
| 2026-10-18T04:38:14Z | 1,539 | 3,021 | 2,601 | 415 | 4,560 |
| 2026-10-18T09:00:06Z | 1,395 | 2,880 | 2,648 | 228 | 4,275 |
| 2026-10-19T02:00:52Z | 1,452 | 2,291 | 2,230 | 56 | 3,743 |
| 2026-10-19T03:42:22Z | 1,459 | 2,282 | 2,215 | 62 | 3,741 |
| 2026-10-19T07:01:47Z | 1,539 | 3,174 | 3,088 | 80 | 4,713 |
| 2026-10-19T10:45:45Z | 1,527 | 2,476 | 2,390 | 82 | 4,003 |

The 3,741 s this decision used was the fastest pair. At 4,003-4,713 s the need is 7,153-7,863 s at a 2,100 s checks budget, over the 7,000 s window, so `ontology.projection` stays `session_over_budget`. The session run is 93-97% its build; the BC capture is 56-415 s and the manifest, mail and completeness reads under 10 s together.

What fits the target (producers + 1.5 x checks budget <= 6,500 s at the slowest run), with B the checks budget, b the session build and c the non-rebuild checks (claims capped at 300 s by `json_max phase_seconds.claims`; trim and removals unmeasured live):

- Budget only (option b): B <= (6,500 - 4,713) / 1.5 = 1,191 s. Every measured build (2,215-3,088 s) exceeds it and the shared run forecasts its one cold rebuild at the manifest's `build_seconds`, so the rebuild would always be skipped (`budget_exhausted` at `rebuild_skipped`, no `rebuild_digest_equal`): a guaranteed fail, a weakened run. Not taken; the budget stays 2,100 s.
- Build cut, shared at the 1.5x rule: producers = 1,539 + 86 + b and B >= b + c, so 1,625 + b + 1.5 (b + 300) <= 6,500 gives b <= about 1,770 s, a cut of at least 43% from the slowest build (3,088 s) and 20% from the fastest (2,215 s). The build cut lowers both terms, which is why it is the only lever.
- Verifier change (projection cold in its own command after its producers, need at 1x): 4,713 + B <= 7,000 gives B <= 2,287 s, still under the slowest build plus claims (3,388 s), so it fits only with b <= about 1,987 s; without a build cut no verifier change fits either. A cold run with no session projection (mirror producer only, two full builds) is 1,539 + 2 x 3,088 + 300 = 8,015 s and fits no command.

Profile so far (keyless, synthetic): cProfile and a per-stage clock over the fixture world scaled to 25,670 and 51,270 objects (`ontology.project_incremental.build_generation`, 43 s and 86 s): fingerprint 2%, projector stages 85% (values 27-30%, links_written 18-19%, links 15-17%, load 9-10%, lineage, versions, conflicts and events 2-4% each), finish (post_project steps, content digest, commit) 13%, linear in rows. No stage is work the consumers do not read, so nothing was removed on synthetic evidence alone. Parked (needs live timings): which stage dominates the live 2,215-3,088 s build and why it varies 40% between runs over the same 1.6M objects. `scripts/ontology_projection_probe.py --produce-session` now reports `build_seconds_split` (fingerprint, each projector stage, finish) and the session spec records it and `phase_seconds`; the next daily names the stage a cut must target.

New reversal condition: the checks budget stays 2,100 s until a measured shared run exists. Take the build cut (target `build_seconds` <= 1,770 s at a 1,539 s mirror) once the first daily records `build_seconds_split`; if no cut reaches it, plan the dedicated-command verifier change, which also needs `build_seconds` <= 1,987 s. Revisit again if the mirror producer grows past 1,700 s, the command window or the 1.5x rule changes, or a measured shared run differs from 1.5 x its budget.
