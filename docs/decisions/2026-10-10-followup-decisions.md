# Decision 2026-10-10: follow-up decisions (batch of 20)

- decided: 2026-10-10
- status: decided
- evidence rule: repo paths below were checked at base 19dd655. Portal pins are given as stated (plx-customer-portal@31a2623), not re-read here.

## 1. customer-PO owner state
- Decision: the owner state mirrors Portal `canEditSalesOrderUntilShipped` exactly. A blank state is refused. A set of edit-locked states is refused. There is no FM allowed-list.
- Evidence: portal/src/lib/sales-order/status.ts@31a2623:8-33 (pin as given).
- Reverses if: Portal changes that function, or FM shows a state the function wrongly locks.

## 2. absent side in three-way match
- Decision: the process states the verdict `unknown` with an `absent_in_source` gap about `match_n`.
- Evidence: No verdict is invented for a side that was not read.
- Reverses if: a source proves absence by a complete, dated read.

## 3. vessel plan
- Decision: port `deriveVesselPlan` (project-landed-quote.ts@31a2623:1558) before per-volume hours are shown as Portal's. Until then `CHARGE_COUNT_UNDERIVED` stays unknown. Priority low.
- Evidence: `CHARGE_COUNT_UNDERIVED` at operating/landed_cost.py:2539; Portal pin as given.
- Reverses if: the port lands and matches Portal output on real quotes.

## 4. withheld is sample, for mail
- Decision: withheld-is-sample applies to mail as well (owner policy). The withheld count stays visible.
- Reverses if: the owner changes the policy.

## 5. try-it supply path
- Decision: main-ci.yml fetches the live results branch and fails closed when it is missing.

## 6. Teams chats
- Decision: Teams chats are projected. Chat membership gates who may see them.
- Evidence: Source permissions stay the rule.
- Reverses if: membership cannot be read for a chat. Then that chat is not projected.

## 7. PDF over 200 pages
- Decision: read pages 1-200. Record the rest as `skipped_by_cap` with `complete=false`.
- Reverses if: a measured cost shows a higher cap is safe.

## 8. count and sum claims, capture age
- Decision: a count claim cites one complete count entry and dedupes twins. Otherwise it says "at least N". A sum claim carries a currency. One capture-age rule: a failed or budget-stopped re-read falls back to the capture within `MAX_CAPTURE_AGE`, in both paths.
- Evidence: `MAX_CAPTURE_AGE = 48h` at operating/capture_age.py:17; used in operating/case_runtime.py:942 and operating/mcp_server.py:2229.
- Reverses if: a path needs a different age. Change the one constant, not a second rule.

## 9. BC floor and 429
- Decision: the BC floor never consumes the finds/projection reserve. `run_left` is the deadline minus the reserves. BC records a coded budget cut. A 429 on the companies listing takes the normal throttle path and records `http_status`.
- Reverses if: live runs show the reserve is never used.

## 10. BC purchase order status "In Review"
- Decision: "In Review" counts as not issued (pending approval).
- Reverses if: live data shows In Review POs with posted receipts.

## 11. ignored-directive SLA
- Decision: the SLA also covers worker-posted directives, after a longer window. No new event type.
- Reverses if: worker directives prove too noisy at the longer window.

## 12. seen refs
- Decision: record seen refs only for the state branch. Never for task branches.
- Reverses if: a task branch needs its own seen record to avoid a repeated alert.

## 13. live acceptance item
- Decision: track it on `capture.sharepoint_estate`. Never count it met at merge.
- Evidence: Live items need a live check, not a merge.
- Reverses if: a live check spec passes and is recorded.

## 14. process_pending and door operations
- Decision: `process_pending` refuses door operations. There is one write path.
- Reverses if: the door path gains the same precondition and outcome checks.

## 15. Retry-After wait
- Decision: one Retry-After wait may take the budget only when the time left covers the measured per-GET cost of the unread items. Otherwise the set is held as `blocked_upstream`.
- Reverses if: measured per-GET cost is unreliable.
- Amendment (gap-c28182d11b82): the reserve no longer switches to the measured cost after the first item. Per item it blends the prior `ITEM_GETS` x `GET_SECONDS` (the slowest hour, 10:33Z: 30.2 s an item), weighted as 3 items read, with the seconds of the items read (throttle waits excluded). It credits what the item being read has already spent and keeps one slowest failed attempt on top. Why: an item that reads its receipts costs about 15 times an item the oracle excludes, so the first items are no measure of the rest.
- Acceptance, restated (option (a)): (1) the reserve is this blend; (2) across 192 throttle profiles (no failures or one attempt in 6 failing after 16 s; a 429 on the invoices after 0-11 of their reads; Retry-After 10, 20, 30, 45, 60, 120, 240 or 480 s) no run ends over the 540 s budget or incomplete; (3) at one attempt in 8 failing, a 10 s Retry-After after 8 invoice reads is slept, the run completes and all 8 items score. The branch point (8b77549e) fails (1) and (3) and holds that wait (7 scored).
- Not an acceptance case: one attempt in 6 failing with a 10-45 s Retry-After after 8 invoice reads. No per-item mean can admit that wait. At the throttle (178 s) 362 s are left, 352 s after a 10 s wait. The 12 items not in flight cost 362 s at the priors alone and 355 s at the replay's own mean item (29.6 s), before the 16 s failed attempt. A forced wait still finishes at 494 s only because 7 of those 12 are excluded items.
- What holds in the injected replay at one attempt in 6 failing: waits of 10-30 s are slept and all 8 items score when the throttle comes after 0-3 invoice reads (45 s also after 0 and 2), 10-20 s after 4. From 5 reads on, the first items read (all with receipts, about 49 s each) keep the reserve above the time left. The run holds the set and blocks 1-2 items (7 scored, 8 after 9 reads). A 60 s wait is held at every position: slept, the run ends at 541-542 s. Against the branch point the blend is more cautious late in the run: after 10 reads a 10 s wait was slept with 1 item blocked and is now held with 2 blocked (7 scored in both); after 5 reads with 240 s and after 7 with 120 s, one item fewer scores. At one attempt in 8 failing it sleeps 67 of 96 profiles (branch point 50). No profile in either sweep ends incomplete.
- Priors: they stay at the slowest hour. Live (afddbcd, 2026-10-10 17:43Z, 25 failed attempts) the items cost about 20 s each (300 s for 15). The injected replay costs about 29-30 s an item (29.6 s: 444 s of item reads for 15 items), close to the priors. Priors of 20 s slept 60-150 s waits that ended the replay incomplete.
- Evidence: tests/test_eval_bc_three_way.py `test_the_reserve_weighs_the_priors_as_three_items_beside_the_items_read`, `test_no_throttle_profile_ends_the_run_over_budget_or_incomplete`, `test_a_timed_throttle_is_waited_out_only_when_the_rest_still_fits`. Injected transport only; live-unverified.
- Reverses if: live cost blocks show items slower than the priors, or waits that fit are still held live.

## 16. T2
- Decision: T2 stays as built. A new candidate is the only exit. The refusal names the train members and `failed_checks`.
- Reverses if: the owner approves a manual T2 exit.

## 17. Teams capture check
- Decision: the check carries state across runs until one full pass completes. It asserts the last run complete and unvisited falling.
- Reverses if: the capture becomes a single-run pass.

## 18. bucket split
- Decision: no further bucket split until a live bucket exceeds 4 MB.
- Reverses if: a live bucket exceeds 4 MB.

## 19. document links
- Decision: `ontology.document_links` emits `links_measured` with `json_min` 1. The Portal mirror is served when healthy and is never required.
- Evidence: The name `links_measured` was not found in any .py file at base.
- Reverses if: the Portal mirror becomes the only source.

## 20. session start
- Decision: session start is `min(stamp, btime)`. Restart risk: after a host restart, btime moves later and a session may look newer than it is. The minimum limits this but does not remove it.
- Reverses if: a stable boot-independent session id exists.
