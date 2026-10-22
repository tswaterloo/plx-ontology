# Decision 2026-10-02: capture-persistence (bound-first completeness; durable L1 only on a persistent credentialed host, never in Git)

- decided: 2026-10-02T00:40Z
- status: decided

## Context

All 93 relations are to be captured and kept, and L2 is disposable
because L1 is durable. Today L1 is not durable: every credentialed cloud run starts with
an empty mirror in an ephemeral container.

Evidence (live `mirror.full_coverage`, live-2026-10-01T23:32:38Z, main c067a11):

- 53/93 relations complete in 1,502 s, 329,850 current rows; lineage totals fm_bronze
  197,956, gold_native_staging 99,940, sage_bronze 16,251, gold_etl_from_sage 8,084,
  gold_etl_from_fm 7,619. No throttling (0 waits). Batch resolve: 5,000 handles in 0.56 s.
- Five gold relations (app_assembly_order_damage/_status, app_batch_schedule,
  app_manufacturing_order_line, app_production_task) each staged 1,000,000 distinct
  keys and were discarded at `max_rows_per_relation`.
- Code reading of `etl-silver-to-gold.ts`: child ETLs insert a
  fresh cuid each run. Five child relations at 1M+ rows, for a business with ~933 sales
  orders and ~6k PO rows, fits re-insertion far better than real volume. That
  is an upstream data-truth defect, not a paging problem. `scripts/mirror_measure.py`
  (ca037a1) now classifies exactly this
  (`ids_reinserted` / `ids_replaced` across two reads).

Binding owner rules:

- 2026-10-01: keep everything captured "in the Workbench's own
  capture store in the credentialed environment, never in Git, logs or advisory records".
  This rules out option (a) as written (a git data branch), encrypted or
  not.
- 2026-10-02: nothing is recorded as a blocker for a human; decide
  or build on a default.
- No Azure Storage or AWS. Paid storage is excluded anyway.

So durable L1 can only live on a credentialed host whose disk survives between runs. The
cloud routine containers do not; the operator's local host (where the API key
lives) does.

## Options

(a) persistent snapshot on a git data branch; (b) completeness scoped to relations bound
in ontology/schema/domains first; (c) both.

## Decision

(c), with the storage half placed where the owner rule allows.

1. **Now: ordered, bound-first completeness.** The daily full coverage syncs bound
   relations first (built on ca037a1). The completeness metric that the
   eval and the projection report is `complete_bound_relation_count / bound_total`; the
   93-relation figure stays as a secondary number. A relation that
   `mirror.relation_counts` classifies as `data_truth_defect` on two daily runs leaves the
   denominator, is reported upstream (Portal ETL), and its bindings carry
   a `capture_unstable_ids` gap. It is never staged to 1M rows again: the probe reads it
   key-only.
2. **Durable L1 = the capture stores' own state dir on a persistent credentialed host.**
   No new store: `source_mirror` and `capture_store` already persist to `--state-dir`. A
   run on a host with a persistent disk keeps its state dir between runs and refreshes
   incrementally (`since`; BC watermark per bc-capture-incremental) with the nightly
   key-only absence scan. Cloud routine runs rebuild the bound set per run; at
   the measured rate (330k rows in 1,502 s, no throttling) that fits the daily budget
   once the unstable relations leave it. Never in Git, logs or advisory records.
   Recorded in `later_asks`, not as a blocker: "a persistent credentialed host for the
   daily capture", for when per-run rebuild time becomes the limit.
3. **Restore safety.** A state dir whose integrity check fails starts empty and says so; a
   partial capture is never served as complete (the mirror's completed-capture
   invariant already holds this).
4. **oracle-live-dev-items** is settled by the same owner rule: option (b). Live-derived
   items stay in the credentialed environment like the held-out set; `evals/dev` holds
   fixture items only.

## Consequences

- The eval denominator becomes the bound set now, so capture failures in unbound
  relations stop masking reasoning results.
- Each cloud credentialed run still pays the bound-relation sync (about 25 min for 53
  relations at the measured rate); a persistent host removes that cost.
- An upstream ETL report is likely; it is a finding for Petra, not code here.

## How we will know this was wrong

- Two daily `mirror.relation_counts` runs show stable ids and real 1M+ volumes in those
  relations: then they are real data, the denominator rule does not apply, and they need
  key-range sharding inside the state dir.
- The bound-relation sync exceeds the daily budget (3,600 s) twice: a persistent
  credentialed host becomes the next step (move the `later_asks` item up), still never
  Git.
