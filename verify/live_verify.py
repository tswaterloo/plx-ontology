#!/usr/bin/env python3
"""Run the declarative live-check specs in ``verify/live-checks/`` (stdlib only).

The live-check runner: it runs the specs in ``verify/live-checks/`` against the configured
sources and writes a report (schema ``plx-live-verify/v1``). Run as a script, it refuses to
run a spec unless the checkout's HEAD is ``git ls-remote origin main``. Keyless, it runs only
with ``--dry-run`` (validate specs, run nothing)::

    python3 verify/live_verify.py --dry-run
    python3 verify/live_verify.py [--only <id> ...] [--output results/<sha>.json]

A spec is ``verify/live-checks/<id>.json``::

    {"id": "bc.adapter_probe", "description": "...",
     "command": ["python3", "-m", "integrations.bc_read", "probe"],
     "expect": {"exit_code": 0, "json_fields": ["token", "companies_count"]}}

A ``json_fields`` path must be present and not null (a null is a value the run did not
measure, never a pass).

Optional spec fields: ``source``, ``timeout_seconds`` (default 300, max 1800),
``requires_env`` (variable names; a missing one makes the check ``skipped`` and
names it, never a value), ``requires_env_any`` (at least one must be set),
``optional_env`` (used when set, never required), ``blocked_without_env`` (names, each also in
``optional_env``: while one is unset the check is ``blocked`` with ``reason`` ``upstream
<source> credentials_absent``, cause ``later_ask`` and ``missing_settings`` naming the unset
ones, without starting the command: a supplied credential the check waits on, asked
for rather than skipped silently). A spec's command sees only the
credential-shaped variables (``CREDENTIAL_ENV_NAME``) these three fields name; one that
reads a set but undeclared credential stops at that read and the check is ``error`` with
``undeclared_env`` naming it, so a missing declaration never runs silently without it,
``record`` (stdout JSON paths copied into the detail; default: every expected
path; the top-level envelope ``ENVELOPE_KEYS`` (status, phase, stage, error, code,
elapsed_seconds, a disk refusal's ``need_bytes`` and ``free_bytes``; a SQLite stop line's
``store`` and ``disk`` byte counts and a build's measured ``disk.peak``, ``ENVELOPE_DISK``)
is always recorded, identifier-shaped strings and scalars only), ``record_keys`` (paths whose mappings may show their short, plain keys;
any other mapping is reduced to its key count), ``record_enums`` (paths whose
short, plain strings and keys may be copied; every other string is reduced to
its length), ``disabled`` (a reason; the check is ``skipped`` and its module
need not be allowlisted), ``legacy_ids`` (ids earlier ad-hoc verifier runs
used), ``notes``, ``registry_names`` ({pass|fail|unexercised: [recorded paths]}: how the
source registry reads the names under those paths, one outcome per name; a spec without
it credits every name it records with the check's result), ``profile`` (true: a data-shape profile spec, below), ``cadence``
(``hourly``, the default, or ``daily``: a check too long for the hourly run; a daily
spec may set ``timeout_seconds`` up to 7200), ``requires`` (ids of specs that must have
passed first, e.g. ``["portal.health_catalog"]``: a required spec runs before the specs that
need it, and when its result is not a pass the dependent is not run but recorded as
``blocked`` with detail ``reason`` ``upstream <source> <http_status or error_code>``; the
upstream result comes from this run or from the ``--merge-into`` report; a *degraded* upstream,
blocked only by ``relation_unavailable`` parts whose served rest was measured, does not block:
the dependent runs unless its ``requires_relations`` (``schema.relation`` names its data needs)
names one of those relations, then it is ``blocked`` with cause ``relation_unavailable`` and
``relations``), ``expect_blocked``
(an ``expect`` used instead of ``expect`` when the command reports Portal or other upstream
parts it could not run: a non-empty top-level ``blocked_upstream`` list of ``{part, source,
http_status, error_code, cause}``; the check is then ``blocked``, never ``pass``, and the
full ``expect`` failures are recorded as ``unverified``), ``complete_when`` ({path: value},
``json_equals`` semantics, recorded as ``complete`` and ``incomplete`` paths; a check whose
every expectation held is ``fail`` with ``incomplete <path>`` failures while one is not met,
since green claims the whole capture), ``tier`` (one of ``TIERS``; default
from the id prefix, ``TIER_BY_PREFIX``) and ``answer_source`` (true: an eval whose
``eval.answer`` block feeds the answer-correctness summary, counted as blocked when it does
not run), ``metric`` (true: the check measures the progress metric, so it runs first
in each new daily set, below), ``blocked_when``
(a stdout JSON path: when the command meets ``exit_code`` and that path holds a short
plain string, the check is ``blocked`` with ``reason`` ``upstream <source> <string>``, the
probe's own finding that a precondition outside the product is missing; never a pass; its
cause is the spec's ``blocked_cause``, one of ``CAUSES``, default ``upstream_outage``, e.g.
``source_gap`` when the source lacks the data rather than failing to answer, or
``later_ask`` when it waits on a value only a human can supply, such as an app
registration; ``blocked_causes`` ({string: cause}) overrides it for the blocked strings it
names). Optional
``expect`` fields:
``json_equals`` {path: value}, ``json_min`` {path: number}, ``json_max`` {path: number},
``json_same`` {path: other path} (both present, not null and equal, no ``[*]``), ``json_empty``
[paths: null/[]/{} everywhere they occur], ``json_absent`` [paths that must not
occur]. A path is dotted keys with ``[*]`` over lists, e.g.
``case_entities[*].entities[*].select_allowlist_absent``.

Data profiles (``verify/live-checks/profile.*.json``, ``"profile": true``): the
command must be ``python3 -m verify.data_profile ...`` and the id must start with
``profile.``. Its stdout is rebuilt by ``verify.data_profile.sanitize_profile`` (names,
fixed enums, counts, rates and ``AAA-9999`` shape masks shared by >= 5 rows; never a
value). With ``--profiles-dir DIR``, or ``--output`` inside a directory named
``results`` (then ``results/profiles/``), each profile is written to one file per spec,
``<dir>/<id>.json``, overwritten each run and rewritten only when its content changed,
and the check carries ``profile_file`` (relative to the report directory) plus counts; the per-run report stays small and
the caller must commit those files with the report. Otherwise (including ``--output``
anywhere else) the profile is inline as the check's ``profile``.

Schema artifacts (``"artifacts": [{"path": "{tmp}/<name>.<ext>", "kind": <kind>}]``): a file
the command writes (the path must be one of its arguments, whole or after ``--flag=``) whose
kind is one of ``ARTIFACT_KINDS``, schema-only documents with no record in them
(``bc_metadata_csdl``: an OData CSDL ``$metadata``, entity sets, types, properties, keys and
annotations; ``.xml``). After the command exits (pass or fail; never after a timeout) the
verifier reads only the declared paths (a regular file, never a symlink), validates each
against its kind, which fails closed (a refusal code: an element or namespace outside
CSDL, such as an entity instance's ``m:properties``, text outside an annotation constant,
a comment, a DOCTYPE, a credential's value, ...), and, with a results/ dir, publishes it
byte for byte to ``results/artifacts/<id>/<sha256>.<ext>`` and appends ``{sha256,
captured_at, main_sha, bytes, file}`` to ``results/artifacts/<id>/index.json`` only when the
digest differs from the newest one there. The check carries ``artifacts`` (per path: name,
kind, ``status`` ``published``, ``unchanged``, ``unpublished`` (no results/ dir),
``refused`` with its ``reason``, or ``absent``; digest, size, capture UTC and file). An
undeclared file in ``{tmp}`` is never read.

Safety: a command is an argv list (no shell) of the form
``python3 -m <module> ...`` where ``<module>`` is one of ``ALLOWED_MODULES``
(``integrations.*``, ``operating.*``, ``scripts.*`` read-only probes and the
``verify.data_profile`` profiler, which reads through the same GET-only clients); any
other spec is refused and never run. The placeholders are ``{tmp}``, a fresh
per-check temporary directory, ``{session_state}`` (below) and ``{daily_report}``, allowed
only as the whole argument ``--previous-report={daily_report}``: it becomes the
``--daily-report`` path or, when that file is absent, the newest complete daily report beside
the ``--merge-into`` file (so a set's specs keep their previous run) unless that set is older
than ``PREVIOUS_DAILY_MAX_HOURS`` (its oldest check, as ``daily_status`` ages it; the run
records ``previous_daily_gap``), and the argument is dropped when there is none. For a spec
with ``carry_previous_measured`` (true; its probe keeps its own memory in its previous
fields, as ``mirror.relation_counts`` its rotation), when that report's check of the spec
recorded no fields (blocked, timed out or refused), the newest
daily report beside it whose check did, up to ``CARRIED_PREVIOUS_MAX_HOURS`` old, is passed
instead and the check records its ``checked_at`` as ``previous_report_carried``
(``measured_previous``); every other spec gets that report as is, so a consumer of another
spec's fields never reads an older run of them. The
detail recorded per check is counts, booleans, short enums and field names only; every
string is scrubbed of key/secret/token-shaped text and of the values of credential-named
environment variables, and the detail is capped in length. Raw stdout and stderr are never
stored: a failure records only the error code, or for a traceback its exception class and
the innermost repository frame (``path:line``), never the message; and for a Portal
readiness or relation failure (``REASONED_ERRORS``) its plain-text ``reason``.

Session state (one capture per verifier session and capture audience): ``{session_state}``
is one directory per run of this script, made on first use in the system temp directory
(never inside the repository; mode 0700, named for the verifier's pid) and removed when the
run ends; a run first removes the session directories and per-check ``{tmp}``
directories (``live-verify-<pid>-*``) of verifier processes no longer alive (a SIGKILLed run
leaves its own behind; ``session_dirs_swept``), and records in ``host_disk`` (byte counts
only, ``sweep_leftovers``) the free bytes of the temp file system at its start, the
``live-verify-*`` directories it found there (count and bytes) and those it removed, and the
bytes its own session directory holds after each check (``session_dir_bytes``, the most in
``session_dir_peak_bytes``). The verifier writes nothing into it. With
``PLX_WORKBENCH_STATE_DIR`` set, the directory is made in the warm store
``<STATE>/live-verify-warm`` instead (``operating.warm_store``: one verifier at a time, a
second one runs a temporary session), and the stores of the paths ``/mirror.sqlite`` and
``/projection`` outlive the run: each is restored when this session first uses its key,
only under the same capture audience (an HMAC of its sources, credential values and
reader files), and kept at the end only when its last writer filled it, so the session
producers top up the last run's mirror and build the projection incrementally. Each
kept store carries its writer (check, verdict, run, session start, and whether every store
it read was left as it read it: ``coherent``). Warm adoption, the one rule for every session
consumer: a path a spec only reads (``session_reads``) and a session producer of it names,
with no writer yet in this session, is ``warm`` (adopted) when every such path of the spec
can be taken from one earlier session's writers: a store a consumer already adopted in this
session (no spec of it has rewritten what that store was made from), or the one the warm
store would restore (same audience, under 26 h old) whose writer filled it coherently, all
of one verifier session; else nothing new is adopted and the producers run as below. An
adopted path is restored as kept and never written (its writer's verdict and run are its
``producer_verdict`` and ``producer_run``); the producers of such a consumer are not run
for it, nor are those of a path it writes itself once all it reads is adopted; and it gets
the adopting session's start as ``CAPTURE_FRESH_SINCE_ENV`` (so it opens that session's
generation) unless it shares a read path or produces one itself. An adopted store no spec
wrote is kept again at the end with its writer and its first keep's age (``carried``),
coherent no longer once a store it was made from is rewritten. A run killed before its end
still carries them: after each spec is recorded (and before one that writes a store runs,
less what it writes) the session journals what the end would keep, and the next session's
warm store keeps those stores of the killed run's directory before removing it
(``salvaged``). The report's ``warm_store`` says what happened (``status``, ``runs_seen``,
``persists_between_runs``, ``restored``, ``kept`` and ``salvaged`` per key, ``not_refreshed`` (per kept
mirror, its relations this session did not refresh; a mirror ages from its oldest
refresh, never from the keep), ``kept_bytes``,
``free_bytes``, ``reset``; else the ``status`` and ``setting`` of why not, such as
``state_dir_unset`` naming ``PLX_WORKBENCH_STATE_DIR``). It appears only as
``{session_state}/<relative path>`` ending an argument (optionally after ``--flag=``), and each path names one capture and what its writer keeps
beside it. Every such path declares its capture audience in ``session_audience``
(``{path: {"sources": [...], "env": [...], "reader": "service" | [files]}}``): the sources it
reads (from the spec's ``source``), the credentials it reads them with (names the spec
declares) and the reader its capture is bound to, ``service`` or the audience and scope
files its command passes; an undeclared or inconsistent one refuses the spec. The session
keeps one store per path and audience (``session_key``), so two specs share a capture only
when both name the same path under the same audience, never by check name, and a spec under
another audience captures cold in its own store. A spec tops up the store of its key when an
earlier spec of this session filled it (it ran to its own exit, not timed out, with a JSON
report naming no ``error``; an expectation it missed does not void what it captured) and no
later writer of it failed (``shared``); else it captures cold: into the session store of its
key when none was written (``produced``; ``cold_not_run`` when a spec it ``requires`` names
the path under the same audience, its session producer, and did not fill it this session;
``cold_audience`` when one it requires names the path under another audience), or in its
own ``{tmp}`` when the store's writer failed (``cold_failed``). Only a writer's outcome
decides that: a spec that lists a path in ``session_reads`` only reads that store when it
shares it (its commands open it read-only, or top it up only through the store's own
writer, whose commits are atomic), so its failure or timeout never spoils it for the next
consumer; one that shares a path it does not list there tops the store up, and a failure of
its run spoils it as a producer's does. A check with session paths
gets the session's start (``CAPTURE_FRESH_SINCE_ENV``): a consumer reuses only what this
session completed and reads again a store an earlier invocation left. A session producer's
``requires`` never blocks its consumer. The check records ``session_state``: ``paths`` (each
path's outcome) and, for a consumer (a path a session producer names, or one it shared),
``shared_store`` (true only when every such path was shared or adopted), ``producer_verdict`` (the
store writer's own result) and ``producer_run`` (``{id, checked_at, main_sha}``, the run it
used: a producer re-run in a daily set keeps the run it replaces in ``earlier_runs``). Specs
others require run first (a spec after those it requires), then consumers. In the daily
rotation a consumer whose producer did not run in this session gets it run first, after the
producer's own session producers that did not run either (a chain, deepest first:
``mirror.full_coverage`` before ``ontology.session_projection``), when their last durations
(else their timeouts) plus the consumer's need (below) fit the budget left. A consumer that
will share reads its need from its last shared run, never a cold one (``shared_store``
false: that duration counts the sync and build its producers do instead): its last shared
run in the set or the previous report (also when its last result was cold, settled or
carries no session state), else its non-build budget (the sum of its command's
``--<phase>-budget-seconds``, a bare ``--budget-seconds`` being its build's; a
``--total-budget-seconds`` alone; with no shared run its need is that budget itself, not
1.5 x it: the command ends inside it), else its timeout. When they do not fit the budget left
the consumer waits (``deferred_budget``), never runs cold for it; when they fit no
command's ``--budget-seconds`` (no producer pending in this set without a result), it is
settled, not run,
as a ``skipped`` check ``unmeasured`` ``budget`` (``cause`` ``session_over_budget``; never a
pass, a fail or ``cold_not_run``; counted in the summary's ``unmeasured_budget``, not in
``skipped``, and never a fresh result in the history) so the set completes. One deferred after its producers ran for
it (``after_producers``) captures cold next time, so a producer never re-runs for a
consumer that then does not run. A consumer that cannot run cold (``session_required``:
true beside ``session_reads``, which names every path its producers name; eval.unfamiliar_sample
pins eval.product.source session_projection, ontology.session_projection errors without a
session mirror; ``reads_producers``) never runs cold for budget: its producers run within
the budget left, and when one is killed at it or the consumer's need no longer fits after
them, it is settled ``session_over_budget``; a deferral it carries ``after_producers``
re-runs its producers.

Cadence: a run without ``--only`` runs the specs of one cadence, ``--cadence hourly``
(default) or ``daily`` (``all`` runs both); the report names the cadence and lists the
ids it left for the other one (``not_run``). ``--only`` runs the named ids whatever
their cadence. ``--dry-run`` validates every spec and shows each one's cadence.
``--daily-report FILE`` (the last daily report, results/daily/latest.json) adds its
freshness (``daily_status``): ``daily_oldest_check_at`` (the oldest ``checked_at`` of its
checks: a set that took days to complete is as old as its oldest check; ``daily_last_run_at``
is its alias), ``daily_completed_at`` (the file's ``generated_at``), ``daily_age_hours`` and
``daily_stale`` (absent, unreadable, no checks, older than 26 h, or a check of unknown origin
that another spec requires; each unknown one is listed in ``daily_origin_unknown``, aged
from the set's ``started_at``, and marks only itself) to the report, so a missed daily run is visible in every hourly one.

Daily rotation: ``--resume-daily results/daily/<sha>.json`` writes ``<the run's main_sha>.json``
in that directory (the named file when the SHA is not a full one), so a daily file always
carries its own run's ``main_sha``, and copies every write of it to ``latest.json`` beside
it, a partial set included (a failed copy is a ``latest_write_failed`` warning on stderr,
never an error stop). It continues the newest daily set there (by ``started_at``, then its
last write) while that set is incomplete, whatever main SHA it started on (a set spans main
moves and runs instead of restarting per SHA): it runs only the set's ``pending`` ids, never
one the set already holds a result for (a failed upstream another pending spec requires
excepted, and a session producer re-run for its consumer, below). A continued set's pending
ids only shrink (a weekly spec that comes due excepted, once per ``WEEKLY_DAYS``, and a
daily spec the set holds no result for, a landed one included, which is pending): a spec
changed since its result and a skip whose environment is now present wait for the next
set, so a set completes
however often main moves while it runs (each check still records the ``spec_digest`` it ran
under). After a complete set (or none) it starts a new set that carries the last
set's checks, each with its own origin, until each runs again: every daily spec is pending
again, and ``latest.json`` always holds the newest result of each, and, first,
``never_passed``: the set's ids with no pass in the check history (never run included). A
resumed run starts with
the set's starved budget deferrals (deferred in ``STARVED_RUNS`` runs in a row), the most
deferred first, then the never run, then the oldest last result: the first of them runs
whatever budget is left after the specs it requires (unless session producers re-run for it
ran slower than before), so a spec whose timeout
exceeds what a run has left after others is never deferred forever, but only while its
timeout and ``KILL_GRACE_SECONDS`` fit what the runtime's 7200 s limit per command
(``COMMAND_LIMIT_SECONDS``, counted from the process start, its startup included) leaves
after the specs run before it, an upstream it requires included; else it waits for the next
command, which it starts. Then the daily specs whose last result is over
``DAILY_STALE_HOURS`` old (each keeps the set ``daily_stale``), the oldest first, with no
budget exception (one never run keeps its value place). Then the rest by product value (``spec_value``): the ``metric``
specs (the progress metric first in each new set), the other answer measures
(``answer_source``, ``ANSWER_TIERS``), then ``VALUE_TIERS`` in order, capture last; a spec
others require takes the best value of the specs in the run that require it and runs before
them. Within one value: specs with no stored result, then the cheapest (the budget each
needs, below), then the oldest last result first (results at earlier SHAs count); the specs
one requires are pulled in just before it. The set lists its newest ``RUN_LOG`` runs in
``runs``: each run's ``id``, ``started_at``, ``main_sha``, ``stop_reason``, the ids it
``ran`` and the ids it ``deferred``, each with its reason.
A spec fits the ``--budget-seconds`` left on its measured duration: 1.5 x its last
``duration_s`` (rounded up), never more than its timeout (its newest run with a positive
``duration_s``, earlier sets' and ``earlier_runs`` included, so a settled or other unrun
result never puts it back on its timeout; a spec never measured, or whose newest measured
run timed out: its timeout); one that fits on that but not on its timeout runs killed
at the budget left. One killed there, short of its own timeout, has no verdict: it stays
pending, deferred with ``full_timeout``, and fits on its timeout from then on. One that
does not fit stays pending and its ``deferred_budget`` entry (``count`` of runs that
deferred it, ``reason``) grows; the first spec of a command always runs. Every check records the ``main_sha``
of its run and the ``checked_at`` it finished at (skipped, blocked and refused ones too),
and keeps both across every later merge; a carried check without them (a set from before
checks carried them) is marked ``origin`` ``unknown``, never stamped from the report, and
read as ``daily_status`` says. The set is ``complete`` once no daily spec is pending (the
write right after its last check says so; a spec of another cadence the run left waiting is
not the set's). Every
run writes its report before the first spec and after each one; each write carries
``stop_reason``
(``running`` until the run ends, then ``finished``, ``budget``, ``interrupted``,
``terminated`` or ``error <class>``) and the run's ``exit_code`` (null while running).

Agent-eval host: for ``AGENT_HOST_CHECKS`` (eval.real_agent, eval.ask_agent,
eval.case_cards_answers, eval.reader_coverage) each of ``PLX_WORKBENCH_SESSION_SECRET``,
``PLX_WORKBENCH_STATE_DIR`` and ``PLX_WORKBENCH_HOST_CONFIG`` the spec declares in
``optional_env`` and the run leaves unset is provisioned for that check's run: a fresh
random secret made once per run, held only in memory and scrubbed like every credential
value (``generated``), a state directory in the run's session directory (``session_dir``),
and the checkout's ``AGENT_HOST_CONFIG`` (its path; ``not_provisioned:... missing`` when the
file is absent, never invented). ``host_provisioned`` names which were made, never a value.
The check's command is told the same in ``AGENT_PROVISIONED_ENV`` (the names it was given,
comma-separated; never a value), so its own result can say which settings it ran on; the
variable never reaches a check from the run's environment. The eval's own employee-identity
and ticket checks are unchanged.

Publishing: a finished run whose ``--output`` is in a results/ dir advances
``results/latest.json`` (``advance_latest``) to the newest complete hourly report there
(``complete``, ``stop_reason`` ``finished``, cadence ``hourly`` or ``all``, by
``generated_at``) whose ``main_sha`` is ``origin/main`` (fetched first) or an ancestor of it
in the running checkout: never a partial, never a SHA off main, never an older report
than the one it holds. It is copied byte for byte, so it carries the report's ``main_sha`` and
``generated_at`` (its age for readers). It runs after the run's report is final: a failed
copy is a ``latest_write_failed`` warning, never an error stop.

One run at a time: a run with a results/ dir holds ``results/running.json`` (``{session,
token, sha, phase, started_at, updated_at}``, ``token`` unique to the process). It refuses
to start (exit ``LOCKED_EXIT``) beside another session's live marker: ``updated_at`` under
2 h old, or, for a legacy marker without it, ``started_at`` (an unreadable marker: the
file's age). Every read-then-write of the marker holds an flock on the results/ dir, so
two runs never both find it free. A heartbeat thread refreshes ``updated_at`` every
``HEARTBEAT_SECONDS`` while the marker carries its token; on exit (normal, exception or
SIGTERM) a run touches the marker only while it carries its token: it removes it, or puts
back, refreshed, the marker of its own session (``--session``, default env
``SESSION_ENV``) it found, so runs of one session never remove each other's marker. One
daily attempt at a time: a run that writes a daily set (``--resume-daily``, ``--merge-into``)
first takes a host-wide flock named for that set's store (the git remote of its directory
and the directory's path in the repository, so two checkouts of the results branch share
it; else the directory's own path), holding its marker ``{session, pid, sha, file,
started_at}``; while another attempt holds it the run refuses (exit ``DAILY_RUNNING_EXIT``)
before it reads the set or touches ``results/running.json``. The kernel releases the flock
when its process ends, so a killed attempt never leaves it held. The flock guards one host
(and one ``TMPDIR``) only: across hosts the lock is the pushed ``results/running.json`` on
``LIVE_REF``; there is no other lock. A reviewed-main run that holds ``results/running.json``
(or resumes a daily set) runs only while that pushed marker (fetched first) names its
``--session``: with none naming a session (absent, unfetched or unreadable) it exits
``UNCLAIMED_EXIT`` (``marker_unpushed``), naming another session ``LOCKED_EXIT``
(``run_locked``), and runs nothing either way. Under its own pushed marker the run's exit
puts back, refreshed, the marker of its session (the local one it found, else the pushed
one), so a daily path that follows keeps the lock.

Results are ``pass``, ``fail`` (ran; an expectation failed or it timed out),
``error`` (refused spec or the runner could not start it), ``blocked`` (not run, or run
only in part, because an upstream failed: a ``requires`` spec failed or was blocked, or the
command's own ``blocked_upstream``; with a ``cause``: ``upstream_outage``,
``projection_timeout``, ``missing_bindings``, ``host_misconfiguration``, ``relation_unavailable``
(the source serves the rest but not a relation the check needs, or the schema binds one its
catalog marks unavailable; a part may carry its plain-text upstream ``reason``), ``source_gap`` (the source answered but does not hold
what the check needs, e.g. no served row carries the requested number: a spec's
``blocked_cause``), ``later_ask`` (a value only a human can supply is missing, e.g. an
app registration) or ``capture_gap``:
a source the product should have captured is not held, e.g. an eval's pinned originals; such
a check counts in the capture tier whatever its own) and ``skipped``
(missing environment or disabled); the summary buckets are disjoint and sum to the number of
checks. A blocked check carries ``blocked_since``: its first non-passing result after its
last pass, from every stored report (the ``results/`` dir of ``--output``, ``--merge-into``
or ``--daily-report`` and its ``daily/``), the persisted ``--blocked-history`` store
(default ``results/blocked-history.json``, rewritten after every spec) and the previous
report (``--previous``, default ``results/latest.json`` beside an ``--output`` in
``results/``; for a daily merge the ``--daily-report`` file, else ``latest.json`` beside the
merge file); a run that did not run the check never resets it. It
turns red after 24 h: it is then listed in ``blocked_red`` and ``failing`` and the run exits
1. An ``eval.*`` check whose recorded detail holds an ``eval`` block is also lifted into the
report's top-level ``eval`` map (keyed by check id, with its result), for
``results/latest.json``.

Tiers are kept apart in the
report's ``tiers``: access, capture (its ``completeness`` beside the results: integrity PASS is
not complete ingestion), projected, deterministic_tools, eval, real_agent and
employee_usable; a tier with no checks is ``unmeasured``. An upstream outage blocks parts,
not tiers: a check blocked only in its own parts whose rest met an ``expect_blocked`` with a
positive ``json_min`` (``blocked.measured``) is listed in
the tier's ``measured_in_part`` (its parts counted by cause in ``blocked_parts``, the tier then
``partial``, not ``blocked``), and a tier's eval answer blocks are summed in its ``answers``
(blocked, wrong, abstained and correct apart). ``answer_correctness`` sums the
answer-source evals' ``eval.answer`` blocks (a check blocked in part included) (``sampled``, ``blocked_upstream``, ``not_held``,
``unscored_other``, ``product_error``, ``scored``, ``correct``, ``wrong``, ``abstain_ok``, ``missed``, with
``accuracy`` = correct/answered beside ``coverage`` = answered/(sampled less blocked
upstream), answered being correct plus wrong: decision 2026-10-19-b1-one-answer-rate) and their ``find_root``
blocks apart; when nothing is scored its ``status`` is ``unmeasured since <ts> (blocked:
<causes>)``.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
from stat import S_ISDIR as stat_is_dir, S_ISREG as stat_is_reg
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterable, Mapping, Sequence
import uuid
import xml.etree.ElementTree as ET

SCHEMA = "plx-live-verify/v1"
# results/latest.json advances only to a report whose main_sha is this ref or its ancestor.
MAIN_REF = "origin/main"
FULL_SHA = re.compile(r"[0-9a-f]{40}")
ROOT = Path(__file__).resolve().parent.parent
if __name__ == "__main__" and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # run as a script: sys.path[0] is verify/, not the repo root
SPECS_DIR_NAME = "verify/live-checks"
MODE = ("read-only (specs from verify/live-checks: GET-only adapters, OAuth token POSTs, "
        "MC MCP initialize+tools/list and read-tool tools/call only)")

# Read-only probes and case scripts reviewed for live use. Adding a module here is a
# reviewed change: it must only read (GET-only client / guarded MC and brain reads).
ALLOWED_MODULES = frozenset({
    "integrations.graph_read",
    "integrations.graph_directory",  # Entra user + transitive groups, GET only
    "integrations.shared_link",  # /shares/{u!..}/driveItem + item /content, GET only
    "integrations.bc_read",
    "integrations.bc_purchase",
    # FBA external sources (decision 2026-10-19-fba-external-sources): Freightcom test host
    # GET only; SP-API GETs plus the one LWA token POST; counts and enums only.
    "integrations.freightcom_read",
    "integrations.amazon_sp_read",
    "integrations.mc_brain_probe",  # MC read tools only via tools/call
    # MC operator-scope capture into {tmp}: read tools only (CAPTURE_TOOLS), counts only.
    "integrations.mc_sources",
    "integrations.knowledge_source",  # brain agent/items GET only, namespace-trimmed (GOV-13)
    "operating.sharepoint_originals",
    # Ingestion (GET only) and one purchase-order case opened offline through the generic
    # tools, then the oracle's own GET-only read of the same purchase order (evals/oracle via
    # the Portal client); counts, booleans and reason codes only.
    "scripts.eval_packaging_oracle_check",
    # The same landed-cost case after copying the console's review store into {tmp} (a
    # read-only SQLite backup): the confirm step a missing link needs; counts and enums only.
    "scripts.eval_human_link_check",
    "operating.customer_po_coverage",
    "operating.reconciliation_rules",
    # Issued PO PDF -> sent message -> served status: one GET-only mail capture of the
    # purchasing Sent Items and one GET-only SharePoint capture into {tmp}, read back as the
    # reader with the Portal mirror (GET only); stdout is counts and enums only.
    "operating.issued_sends",
    "operating.case_mail",
    "operating.ingestion",
    # Capture cycles into {tmp} through the host's GET-only capture modules, the projection
    # there, then counts, leak and per-reader trim checks over it (host.cycles).
    "operating.host",
    # One link, one tier (R1): plx.around, plx.aggregate and plx.process asked about the same
    # SalesOrderLine.line_of links over a throwaway projection; counts and enums only.
    "scripts.link_tier_agree_probe",
    # The scheduled proactive engine: set-based rules over the projection store of one
    # capture cycle into {tmp}, the gap inbox beside it, per-reader reads; counts and enums.
    "operating.proactive_engine",
    # The connector entry point: one capture cycle, the engine's run into {tmp}, then per
    # reader an MCP stdio session asking plx.find's home question; counts and enums only.
    "operating.connector_home_check",
    # Status cards: deterministic rules over the purchase orders of one capture cycle,
    # offline; counts and enums only.
    "operating.status_card",
    # The shared PO status definition over one capture cycle's PO headers and lines,
    # offline; status words and counts only.
    "operating.po_status",
    # Ingests (operating.ingestion, GET only), then opens and renders cases offline over the
    # mirror; its only other read is the opt-in GET-only Graph reader for AO originals.
    "operating.case_workspace_live",
    # Ingestion of one SharePoint unit (GET only) + local intake (operating.use_case_intake),
    # then dev eval items from the intake state in {tmp}; counts only.
    "scripts.eval_use_case_items",
    # One GET-only SharePoint capture run into {tmp}, its projection there, then
    # plx.documents through the case host (live re-reads GET only); counts only.
    "operating.sharepoint_host_check",
    # The same GET-only capture run into {tmp}, then each file's listItem columns (GET
    # only) and its security class for synthetic employees; counts and class names only.
    "operating.file_clearance_check",
    # The same GET-only capture of one SharePoint folder into {tmp}, its projection there,
    # then container-member counts and gate checks offline (ING-17); counts and enums only.
    "integrations.sharepoint_container_check",
    "scripts.live_read_probes",
    # Reads the console journal and ticket audit in the state directory (offline, no
    # client, writes nothing); counts and booleans only (session.employee_recorded).
    "scripts.session_employee_recorded",
    "scripts.access_control_proof",
    # Concurrent sessions on one case, offline on a throwaway journal (session.shared_case).
    "scripts.session_shared_case",
    # Reads the build report of this session's committed projection generation (offline,
    # no client, writes nothing); money and unit gap counts, rule ids and shapes only.
    "scripts.money_units_probe",
    # The employee walk-through of the fixture console in headless Chromium (offline, 127.0.0.1,
    # synthetic rows, no client, no credential); step names and statuses only.
    "scripts.eval_console_walkthrough",
    # Data-shape profiler (profile.* specs): reads through WorkbenchReadApiClient,
    # bc_read and graph_read only; prints a sanitized shape profile, never values.
    "verify.data_profile",
    # Evaluation oracle dev-set run (evals/oracle via the GET-only Portal API client, BC
    # reader and Graph mail/SharePoint readers); stdout is ids, hashes and counts only.
    "scripts.eval_oracle_dev",
    # Unfamiliar-transaction find eval: the oracle's cross-system sample (same GET-only
    # readers as eval_oracle_dev), a GET-only Portal mirror sync and a projection into
    # {tmp}, then plx.find/plx.object over it; stdout is kinds, outcomes, counts and hashes.
    "scripts.eval_unfamiliar_sample",
    "scripts.eval_bc_three_way",  # BC-only three-way eval: BC GETs, projection in {tmp}
    "scripts.eval_order_margin",  # BC-only order margin eval (P9/F10): BC GETs, projection in {tmp}, no Portal
    # Dev-set baseline: the oracle's dev items (same GET-only readers as eval_oracle_dev), a
    # GET-only Portal mirror sync and a projection into {tmp}, then the generic tools on a
    # throwaway case host; no model; stdout is rates, codes, counts and hashes.
    "scripts.eval_dev_baseline",
    # Document-class measure (operating.sharepoint_host_check: one GET-only SharePoint
    # capture into {tmp}, its projection) plus the exposed items scored over that capture;
    # counts only.
    "scripts.eval_document_fields_live",
    # No-store keyset counter over the GET-only Portal API client: counts, booleans,
    # 16-hex key-list hashes and relation names; never a row value or a key.
    "scripts.mirror_measure",
    # Ontology projection: a GET-only Portal mirror sync into {tmp}, two streamed builds of
    # the store there and its claim/permission checks; stdout is counts, booleans, timings.
    "scripts.ontology_projection_probe",
    # plx.around / plx.timeline over that probe's throwaway store (same GET-only build);
    # the tools only read the store; stdout is counts, booleans, timings and error codes.
    "scripts.ontology_traverse_probe",
    # BC-only ontology projection: a GET-only BC capture (integrations.bc_read) into {tmp}, its
    # projection there and the tools over it; no Portal; stdout is counts, booleans and states.
    "scripts.ontology_projection_bc_probe",
    # Pinned originals the exposed eval items cite (evals/exposed/sources.json): SharePoint
    # drive items and mail attachments through the GET-only Graph readers only; stdout is
    # counts, booleans, row indexes and error classes.
    "scripts.eval_exposed_sources",
    # Every registered stakeholder original (examples/case_originals.json) read through the
    # GET-only Graph readers (drive item content, /shares/ resolution, folder listing) into
    # {tmp}; stdout is per-entry statuses, codes, counts and the reader identity.
    "scripts.case_originals",
    # Employee object workspace views over the ontology projection probe's throwaway
    # store (same GET-only build), offline through the console's workspace; counts only.
    "scripts.object_workspace_probe",
    # plx.process prerequisites over that probe's throwaway store (same GET-only build),
    # offline through the served host's tools; counts, stage names and gap reasons only.
    "scripts.process_dependencies_probe",
    # Identity scoring over that probe's throwaway store (same GET-only build), offline on a
    # read-only connection under the served tools' scope; counts, shares and names only.
    "scripts.identity_scoring_probe",
    "scripts.po_lateness_probe",  # PO lateness over the served host (process.po_lateness)
    # Customer orders at risk from late POs over the served host (process.impact_chain);
    # counts, shares, link types and currency codes only.
    "scripts.impact_chain_probe",
    # Real-agent eval (tier real_agent): headless Claude Code sessions on the fixture case
    # host behind the runner's read-only tool gate; reads no business system, no credential
    # in the session; stdout is counts, rates and enums.
    "scripts.eval_real_agent",
    # The flagship path (demo.flagship_path): one host capture cycle into the throwaway
    # state, then per order the lateness oracle picks (raw captures) the home recall, the
    # console Ask scored against the oracle and timed, landed cost, a follow-up recorded on
    # a throwaway case (never approved, executed or sent) and the fed margin; stdout is
    # counts and enums.
    "scripts.eval_flagship_path",
    # Every committed use-case card set answered by headless Claude Code sessions on the
    # persistent host as one named employee (eval_real_agent.LiveHost, read-only tool
    # gate); half the cards per run by rotation; stdout is counts, rates and enums.
    "scripts.eval_case_cards",
    # Objective-investigation eval (eval.objective_agent): headless Claude Code
    # sessions as plx.agent.investigate on the fixture case host behind the objective gate
    # (read-only tools, the case-store writers and plx.case_propose recorded only; plx.act
    # and every MC tool refused, nothing sent); stdout is counts, rates and enums.
    "scripts.eval_objective_agent",
    # Home's objective box (workspace.objective_box): three free-form objectives through
    # operator_ui.objective_box.answer on the reader's console workspace of the persistent
    # host, a headless Claude Code planner, read-only; stdout is counts and enums.
    "scripts.eval_objective_box",
    # The console question box's one path (workspace.console_question_one_path): one
    # two-part question about an order of the reader's home gaps through
    # operator_ui.objective_box.answer on the reader's console workspace of the persistent
    # host, a headless Claude Code planner, read-only; stdout is counts and enums.
    "scripts.eval_console_question",
    # Employee tool checks (tools.*): operating.host cycles of a GET-only BC capture into
    # {tmp}, then plx.schema / plx.find / plx.investigate on the served host; counts only.
    "scripts.live_employee_tools",
    # Goal-2 MC journal read (mc.action_journal): its CLI offers only ``mc-journal``, one
    # mc_get_task through a MissionControlMcpClient holding no write tool; shape only.
    "operating.action_tools",
    # The static HTTP-egress gate over the verifier's checkout (gate.http_egress): no network,
    # no credential; prints finding counts and the allowlisted senders' path:function names.
    "scripts.check_http_egress",
})
PROFILE_MODULE = "verify.data_profile"
PROFILE_ID_PREFIX = "profile."
MODULE_SHAPE = re.compile(r"(integrations|operating|scripts|verify)(\.[a-z_][a-z0-9_]*)+")
ID_SHAPE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
RELATION_NAME = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
# A failure reason the probes print as plain text (integrations/workbench_read_api.py):
# words, numbers and punctuation without URL, query, quote or address characters.
REASON_TEXT = re.compile(r"[A-Za-z0-9 _.,:;=<>()\[\]+\-]{1,160}")
REASONED_ERRORS = ("WorkbenchNotReady", "RelationUnavailable")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,80}")
PATH_SEGMENT = re.compile(r"([A-Za-z0-9_\-]+)((?:\[\*\])*)")
ENUM_STRING = re.compile(r"[A-Za-z0-9_.:/\-]{0,64}")
ERROR_CODE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:\-]{0,79}")
# A command's top-level envelope (a budget stop or exception line: why it stopped) is
# recorded whatever the spec declares: scalars only, strings only identifier-shaped.
ENVELOPE_KEYS = ("status", "phase", "stage", "error", "code", "elapsed_seconds",
                 "need_bytes", "free_bytes")
ENVELOPE_STRING = re.compile(r"[A-Za-z_][A-Za-z0-9_.:\-]{0,63}")
# A SQLite stop line (the projection probes' ``error_line`` / ``build_failure``) also names
# the ``store`` it was writing (kept when identifier-shaped, else dropped) and the ``disk``
# space where it stopped: these file systems' byte counts, integers only; anything else in
# it is dropped, so a low-disk failure shows in the verdict and never a path or message.
# A projection probe's ``disk.peak`` (scripts/ontology_projection_probe.py ``PEAK_KEYS``):
# what its largest build took (the next run's free-space need when its ``layout`` is the
# projector's), the free space it started from and its object count; ``disk.stages``
# (``STAGE_KEYS``): what each projector stage held on disk when it ended.
ENVELOPE_DISK = {"store_dir": ("free_bytes", "total_bytes"),
                 "sqlite_temp_dir": ("free_bytes", "total_bytes"),
                 "peak": ("build_bytes", "store_bytes", "mirror_bytes", "scratch_bytes",
                          "work_bytes", "free_bytes", "objects", "layout"),
                 "stages": ("load", "links", "lineage", "versions", "values", "conflicts",
                            "events", "links_written")}
# A Python traceback (the last one in stderr is the exception that ended the command): its
# frames, and after the last frame the exception's first line, ``<class>[: <message>]``.
TRACEBACK_HEADER = "Traceback (most recent call last):"
TRACEBACK_FRAME = re.compile(r'  File "([^"]+)", line (\d+), in ')
EXCEPTION_CLASS = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
REPO_FRAME_PATH = re.compile(r"[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*\.py")
SPEC_KEYS = {"id", "description", "command", "expect", "source", "timeout_seconds",
             "requires_env", "requires_env_any", "record", "record_keys", "record_enums",
             "disabled", "legacy_ids", "notes", "profile", "cadence", "requires",
             "expect_blocked", "complete_when", "tier", "answer_source",
             "blocked_when", "blocked_cause", "optional_env", "blocked_without_env",
             "requires_relations",
             "registry_names", "session_audience", "metric", "artifacts",
             "carry_previous_measured", "session_reads", "session_required",
             "blocked_causes"}
# ``registry_names``: the source registry's reading of the names a check records
# (scripts/source_registry.py LiveIndex).
REGISTRY_NAME_KINDS = ("pass", "fail", "unexercised")
# ``artifacts``: a declared {tmp} file of a schema-only kind, published per digest.
ARTIFACT_PATH = re.compile(r"\{tmp\}/([A-Za-z0-9_][A-Za-z0-9_\-]{0,59})\.([a-z]{2,8})")
ARTIFACTS_DIR_NAME = "artifacts"
ARTIFACT_INDEX = "index.json"
ARTIFACT_INDEX_SCHEMA = "plx-live-artifacts/v1"
MAX_ARTIFACTS = 4
MAX_ARTIFACT_BYTES = 16_000_000
MAX_ARTIFACT_TEXT = 2000
_EDMX_NS = "http://docs.oasis-open.org/odata/ns/edmx"
_EDM_NS = "http://docs.oasis-open.org/odata/ns/edm"
# OData CSDL XML 4.01: every element a $metadata document may hold. An entity instance
# (Atom entry, m:properties, d:<field>) or anything else outside these refuses the file.
CSDL_EDMX = frozenset({"Edmx", "DataServices", "Reference", "Include", "IncludeAnnotations"})
CSDL_STRUCTURE = frozenset({
    "Schema", "EntityType", "ComplexType", "EnumType", "Member", "Key", "PropertyRef",
    "Property", "NavigationProperty", "ReferentialConstraint", "OnDelete", "TypeDefinition",
    "Action", "Function", "Parameter", "ReturnType", "EntityContainer", "EntitySet",
    "Singleton", "ActionImport", "FunctionImport", "NavigationPropertyBinding", "Term",
    "Annotations", "Annotation"})
# Annotation expressions: only below an Annotation; text only in the constant/path ones.
CSDL_TEXT_EXPRESSIONS = frozenset({
    "Binary", "Bool", "Date", "DateTimeOffset", "Decimal", "Duration", "EnumMember", "Float",
    "Guid", "Int", "String", "TimeOfDay", "AnnotationPath", "ModelElementPath",
    "NavigationPropertyPath", "Path", "PropertyPath", "LabeledElementReference"})
CSDL_EXPRESSIONS = CSDL_TEXT_EXPRESSIONS | {
    "Apply", "Cast", "Collection", "If", "Eq", "Ne", "Gt", "Ge", "Lt", "Le", "And", "Or",
    "Not", "Has", "In", "IsOf", "LabeledElement", "Null", "Record", "PropertyValue", "UrlRef"}
CSDL_ATTRIBUTE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}")
EXPECT_KEYS = {"exit_code", "json_fields", "json_equals", "json_min", "json_max", "json_same",
               "json_empty", "json_absent"}
RESULTS = ("pass", "fail", "error", "blocked", "skipped")
TIERS = ("access", "capture", "projected", "deterministic_tools", "eval", "real_agent",
         "employee_usable", "harness")
# Tier of a spec that declares none, by id prefix (first match wins; else access).
TIER_BY_PREFIX = (("capture.", "capture"), ("bc.capture", "capture"), ("graph.mail_", "capture"),
                  ("ingest.", "capture"), ("mirror.", "capture"), ("ao-originals", "capture"),
                  ("ontology.", "projected"), ("case.", "deterministic_tools"),
                  ("eval.real_agent", "real_agent"), ("eval.", "eval"))
CAUSES = ("upstream_outage", "projection_timeout", "missing_bindings", "host_misconfiguration",
          "capture_gap", "relation_unavailable", "source_gap", "later_ask")
# The Portal reporting API's code for a relation it lists but does not serve.
RELATION_UNAVAILABLE_CODES = ("relation-unavailable", "relation_unavailable",
                              "mirror.relation_unavailable")
BLOCKED_RED_HOURS = 24
# The blocked-age store (results/blocked-history.json beside the published results).
HISTORY_SCHEMA, HISTORY_NAME = "plx-live-verify-history/v1", "blocked-history.json"
HISTORY_STAMPS = 48
MAX_BLOCKED_PARTS = 20
ANSWER_COUNTS = ("sampled", "blocked_upstream", "not_held", "unscored_other",
                 "product_error", "scored", "correct", "wrong", "abstain_ok", "missed")
DEFAULT_TIMEOUT, MAX_TIMEOUT = 300, 1800
CADENCES = ("hourly", "daily", "weekly")
DEFAULT_CADENCE = "hourly"
# The hourly run must finish inside its hour; a daily or weekly check gets a longer ceiling.
MAX_TIMEOUT_BY_CADENCE = {"hourly": MAX_TIMEOUT, "daily": 7200, "weekly": 7200}
# A weekly spec runs in the daily set only once its held result is this old; a held result of
# any verdict (blocked included) counts, so a blocked weekly spec waits out the week.
WEEKLY_DAYS = 7
MAX_DETAIL_CHARS = 4000
MAX_STRING_CHARS = 300
MAX_ITEMS = 50
MAX_ARG_CHARS = 300
DAILY_REPORT_ARG = "--previous-report={daily_report}"
SESSION_STATE = "{session_state}"
# ``{session_state}/<relative path>`` ending an argument (module docstring, "Session state").
SESSION_ARG = re.compile(r"(?:--[a-z0-9][a-z0-9-]*=)?\{session_state\}"
                         r"((?:/[A-Za-z0-9_][A-Za-z0-9_.\-]*)+)")
SESSION_PREFIX = "live-verify-session-"
# A session directory a verifier process names for itself (``SessionState.directory``).
SESSION_DIR = re.compile(re.escape(SESSION_PREFIX) + r"(\d+)-[A-Za-z0-9_]+")
CHECK_PREFIX = "live-verify-"  # a check's own {tmp} (run_spec), named for the verifier's pid
CHECK_DIR = re.compile(re.escape(CHECK_PREFIX) + r"(\d+)-[A-Za-z0-9_]+")
SESSION_AUDIENCE_KEYS = {"sources", "env", "reader"}
# A session's store writer kept as written (``SessionState.record``).
SESSION_WRITES = ("produced", "cold_not_run", "cold_audience")
# Given to a check with session paths: the instant its session directory was made, so a
# capture consumer reuses only what this session completed (integrations/bc_read.py
# ``FRESH_SINCE_ENV``); removed from every other check's environment.
CAPTURE_FRESH_SINCE_ENV = "PLX_CAPTURE_FRESH_SINCE"
PREVIOUS_DAILY_MAX_HOURS = 48  # an older daily set is never a probe's previous run
# A probe's last measured run this old still carries its memory past a blocked or timed-out
# one (``measured_previous``; scripts/mirror_measure.py ROTATION_WINDOW_HOURS).
CARRIED_PREVIOUS_MAX_HOURS = 96
EARLIER_RUNS = 5  # runs of a check a daily set keeps when it is re-run (merge_reports)
EARLIER_KEYS = ("result", "checked_at", "main_sha", "exit_code", "duration_s",
                "spec_digest")
# Daily rotation value order (spec_value): answers first, then these tiers, capture last.
ANSWER_TIERS = ("eval", "real_agent")
VALUE_TIERS = ("employee_usable", "deterministic_tools", "projected", "access", "capture")
STARVED_RUNS = 2  # a budget deferral this many runs in a row starts the next run
RUN_LOG = 20  # runs a daily set lists in ``runs`` (newest last)
DEFAULT_BUDGET_SECONDS = 7000  # --resume-daily: under the runtime's 7200 s limit per command
COMMAND_LIMIT_SECONDS = MAX_TIMEOUT_BY_CADENCE["daily"]  # the runtime kills a command there
KILL_GRACE_SECONDS = 10
LOCK_NAME = "running.json"
LOCK_LIVE_HOURS = 2
HEARTBEAT_SECONDS = 300  # a marker reads as live for 2 h; refreshed well inside 10 min
LOCKED_EXIT = 75  # EX_TEMPFAIL: another session's run holds results/running.json
# Another attempt is writing this daily set: wait for it
DAILY_RUNNING_EXIT = 76
# No results/running.json on LIVE_REF names a session: the marker push was skipped
UNCLAIMED_EXIT = 78  # EX_CONFIG
DAILY_LOCK_PREFIX = "plx-live-verify-daily-"
SESSION_ENV = "CLAUDE_CODE_REMOTE_SESSION_ID"
LIVE_REF = "origin/verify-live"  # the pushed results/running.json
# The agent evals the verifier provisions a host for (module docstring, "Agent-eval host"):
# each setting a spec declares and the run leaves unset, for that check's run only.
AGENT_HOST_CHECKS = ("eval.real_agent", "eval.ask_agent", "eval.case_cards_answers",
                     "eval.reader_coverage")
AGENT_SECRET_ENV = "PLX_WORKBENCH_SESSION_SECRET"  # fresh per run, held only in memory
AGENT_STATE_ENV = "PLX_WORKBENCH_STATE_DIR"
AGENT_CONFIG_ENV = "PLX_WORKBENCH_HOST_CONFIG"
AGENT_HOST_CONFIG = Path("examples") / "host.live-check.json"  # under the checkout's root
AGENT_STATE_DIR = "agent-host-state"  # in the run's session directory
# The names of the host settings the run made for a check, comma-separated (never a value):
# scripts/eval_real_agent.py PROVISIONED_ENV reads it into its result.
AGENT_PROVISIONED_ENV = "PLX_WORKBENCH_HOST_PROVISIONED"

# Scrubbing: credential-named variables (their values are replaced wherever they
# appear) and key/secret/token-shaped text.
SECRET_ENV_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|COOKIE|DSN|CREDENTIAL|SESSION",
                             re.IGNORECASE)
# What a spec's command never sees unless it declares the name (requires_env or
# requires_env_any): credential-shaped names and git's injected config, whose values can
# carry an http.extraheader.
CREDENTIAL_ENV_NAME = re.compile(SECRET_ENV_NAME.pattern + r"|^GIT_CONFIG_", re.IGNORECASE)
# Every spec command runs under this guard (python -c GUARD <hidden names> <module> args):
# reading a credential that is set but not declared (hidden) ends the command with
# UNDECLARED_ENV_EXIT and one JSON line naming it, instead of running without it.
UNDECLARED_ENV_EXIT = 86
# Stripped like any credential-shaped name but never guarded: the ssl module reads it
# for every TLS context, declared or not.
UNGUARDED_ENV = frozenset({"SSLKEYLOGFILE"})
ENV_GUARD = f"""import json, os, runpy, sys
hidden = frozenset(filter(None, sys.argv[1].split(",")))
module, sys.argv = sys.argv[2], [sys.argv[2], *sys.argv[3:]]
class Guarded(type(os.environ)):
    def __getitem__(self, key):
        if key in hidden:
            print(json.dumps({{"error": "UndeclaredEnv", "name": key}}), file=sys.stderr, flush=True)
            os._exit({UNDECLARED_ENV_EXIT})
        return super().__getitem__(key)
    def __contains__(self, key):
        if key in hidden:
            self[key]
        return super().__contains__(key)
os.environ.__class__ = Guarded
runpy.run_module(module, run_name="__main__", alter_sys=True)
"""
_SCRUBBERS = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"), "<redacted-jwt>"),
    (re.compile(r"(?i)\b(plxwb|brn|sk|ghp|gho|ghs|xox[a-z])[_-][A-Za-z0-9_\-]{6,}"),
     "<redacted-key>"),
    (re.compile(r"(?i)([A-Za-z0-9_\-]*(?:key|secret|token|password|passwd|pwd|signature|sig|"
                r"cookie|authorization|credential|code)[A-Za-z0-9_\-]*\"?\s*[:=]\s*\"?)"
                r"[^\s\"',;&}]+"), r"\1<redacted>"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "<email>"),
    # Long opaque tokens mixing letters and digits (snake_case field names have no digits).
    (re.compile(r"(?<![A-Za-z0-9+/_=\-])(?=[A-Za-z0-9+/_=\-]*\d)(?=[A-Za-z0-9+/_=\-]*[A-Za-z])"
                r"[A-Za-z0-9+/_=\-]{32,}"), "<redacted-opaque>"),
)


class SpecError(ValueError):
    """A spec that the runner refuses to run."""


# ------------------------------------------------------------------ scrubbing

def secret_values(environ: Mapping[str, str]) -> list[str]:
    """Values of credential-named variables, longest first (never printed)."""
    values = {value.strip() for name, value in environ.items()
              if SECRET_ENV_NAME.search(name) and isinstance(value, str)
              and len(value.strip()) >= 6}
    return sorted(values, key=len, reverse=True)


def scrub(text: str, secrets: Sequence[str] = ()) -> str:
    for value in secrets:
        text = text.replace(value, "<redacted>")
    for pattern, replacement in _SCRUBBERS:
        text = pattern.sub(replacement, text)
    if len(text) > MAX_STRING_CHARS:
        text = text[:MAX_STRING_CHARS] + "...<truncated>"
    return text


def scrub_tree(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, Mapping):
        return {scrub(str(k), secrets): scrub_tree(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_tree(v, secrets) for v in value]
    return value


# ------------------------------------------------------------------ JSON paths

def parse_path(path: str) -> list[tuple[str, int]]:
    if not isinstance(path, str) or not path:
        raise SpecError("path must be a non-empty string")
    parts = []
    for segment in path.split("."):
        match = PATH_SEGMENT.fullmatch(segment)
        if match is None:
            raise SpecError(f"invalid path segment {segment!r} in {path!r}")
        parts.append((match.group(1), match.group(2).count("[*]")))
    return parts


def resolve(document: Any, path: str) -> list[Any]:
    """Every value at ``path`` (``[*]`` fans out over lists); [] when absent."""
    current = [document]
    for key, stars in parse_path(path):
        nxt = []
        for node in current:
            if isinstance(node, Mapping) and key in node:
                nxt.append(node[key])
        for _ in range(stars):
            nxt = [item for node in nxt if isinstance(node, list) for item in node]
        current = nxt
    return current


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    return left == right


def summarize(value: Any, *, enums: bool, keys: bool = False, depth: int = 0) -> Any:
    """Counts-only view: numbers, booleans and null pass; strings only as enums.

    Mapping keys are shown only when ``keys`` or ``enums`` allows it and every key is
    enum-shaped; otherwise the mapping is reduced to its key count."""
    keys = keys or enums
    if value is None or isinstance(value, bool) or _is_number(value):
        return value
    if isinstance(value, str):
        if enums and ENUM_STRING.fullmatch(value):
            return value
        return f"<str len={len(value)}>"
    if isinstance(value, list):
        if (len(value) <= MAX_ITEMS and depth < 3 and all(
                x is None or isinstance(x, bool) or _is_number(x)
                or (enums and isinstance(x, str)) for x in value)):
            return [summarize(x, enums=enums, keys=keys, depth=depth + 1) for x in value]
        return {"count": len(value)}
    if isinstance(value, Mapping):
        if (depth >= 3 or len(value) > MAX_ITEMS or not keys
                or not all(isinstance(k, str) and ENUM_STRING.fullmatch(k) for k in value)):
            return {"keys": len(value)}
        return {k: summarize(v, enums=enums, keys=keys, depth=depth + 1)
                for k, v in value.items()}
    return f"<{type(value).__name__}>"


# ------------------------------------------------------------------ specs

def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise SpecError(f"{name} must be a list of non-empty strings")
    return value


def _validate_session_audience(spec: Mapping[str, Any]) -> None:
    """Every ``{session_state}`` path declares its capture audience (module docstring,
    "Session state"), from what the spec itself declares: its ``source``, its credential
    names and the files its command passes; anything else refuses the spec."""
    paths, declared = session_paths(spec), spec.get("session_audience", {})
    if not isinstance(declared, Mapping) or set(declared) != set(paths):
        raise SpecError("session_audience must declare exactly the {session_state} paths "
                        f"of the command: {', '.join(paths) or 'none'}")
    sources = set(str(spec.get("source", "")).split("+"))
    env = {name for key in ("requires_env", "requires_env_any", "optional_env")
           for name in spec.get(key, [])}
    for path, audience in declared.items():
        if not isinstance(audience, Mapping) or set(audience) != SESSION_AUDIENCE_KEYS:
            raise SpecError(f"session_audience {path!r} must be {{sources, env, reader}}")
        if not set(_string_list(audience["sources"], "session_audience sources")) <= sources:
            raise SpecError(f"session_audience {path!r} sources must be in the spec's source")
        if not set(_string_list(audience["env"], "session_audience env")) <= env:
            raise SpecError(f"session_audience {path!r} env must be credentials the spec "
                            "declares")
        reader = audience["reader"]
        if reader != "service" and not set(_string_list(
                reader, "session_audience reader")) <= set(spec["command"][3:]):
            raise SpecError(f"session_audience {path!r} reader must be 'service' or files "
                            "the command passes")
        if not audience["sources"] or not audience["env"] or not reader:
            raise SpecError(f"session_audience {path!r} must name sources, env and reader")


def validate_spec(spec: Any, *, allowed_modules: Iterable[str] = ALLOWED_MODULES,
                  stem: str | None = None) -> list[str]:
    """Raise SpecError if the spec may not run; return warnings (unknown keys)."""
    if not isinstance(spec, Mapping):
        raise SpecError("spec must be a JSON object")
    spec_id = spec.get("id")
    if not isinstance(spec_id, str) or ID_SHAPE.fullmatch(spec_id) is None:
        raise SpecError("id must match " + ID_SHAPE.pattern)
    if stem is not None and stem != spec_id:
        raise SpecError(f"file name {stem!r}.json does not match id {spec_id!r}")
    if not isinstance(spec.get("description"), str) or not spec["description"].strip():
        raise SpecError("description must be a non-empty string")
    command = spec.get("command")
    if (not isinstance(command, list) or len(command) < 3
            or not all(isinstance(arg, str) for arg in command)):
        raise SpecError("command must be an argv list: python3 -m <module> [args...]")
    if command[0] != "python3" or command[1] != "-m":
        raise SpecError("command must start with python3 -m (no shell, no other program)")
    module = command[2]
    if MODULE_SHAPE.fullmatch(module) is None:
        raise SpecError(f"module {module!r} is not an integrations.*, operating.*, "
                        "scripts.* or verify.* module")
    if module.startswith("verify.") and module != PROFILE_MODULE:
        raise SpecError(f"module {module!r}: the only verify module is {PROFILE_MODULE}")
    if "profile" in spec and spec["profile"] is not True:
        raise SpecError("profile must be true when present")
    if (spec.get("profile") is True) != (module == PROFILE_MODULE):
        raise SpecError(f"profile specs (and only they) run {PROFILE_MODULE}")
    if (spec.get("profile") is True) != spec_id.startswith(PROFILE_ID_PREFIX):
        raise SpecError(f"profile spec ids (and only they) start with {PROFILE_ID_PREFIX!r}")
    if module not in set(allowed_modules) and not spec.get("disabled"):
        raise SpecError(f"module {module!r} is not in the live-verify allowlist")
    for arg in command[3:]:
        if not arg or len(arg) > MAX_ARG_CHARS or any(ord(c) < 32 for c in arg):
            raise SpecError("command arguments must be 1..300 printable characters")
        if "{daily_report}" in arg and arg != DAILY_REPORT_ARG:
            raise SpecError(f"{{daily_report}} is allowed only as {DAILY_REPORT_ARG!r}")
        if SESSION_STATE in arg and SESSION_ARG.fullmatch(arg) is None:
            raise SpecError(f"{{session_state}} is allowed only as {{session_state}}/<path> "
                            f"ending an argument, not {arg!r}")
        if re.search(r"\{(?!tmp\}|daily_report\}|session_state\})[^}]*\}", arg):
            raise SpecError(f"unknown placeholder in {arg!r}; only {{tmp}}, {{session_state}} "
                            "and {daily_report} are supported")
        if arg in ("-c", "--shell"):
            raise SpecError(f"argument {arg!r} refused")
    cadence = spec.get("cadence", DEFAULT_CADENCE)
    if cadence not in CADENCES:
        raise SpecError("cadence must be one of: " + ", ".join(CADENCES))
    ceiling = MAX_TIMEOUT_BY_CADENCE[cadence]
    timeout = spec.get("timeout_seconds", DEFAULT_TIMEOUT)
    if not _is_number(timeout) or not 0 < timeout <= ceiling:
        raise SpecError(f"timeout_seconds must be in (0, {ceiling}] for a {cadence} spec")
    for upstream in _string_list(spec.get("requires", []), "requires"):
        if ID_SHAPE.fullmatch(upstream) is None or upstream == spec_id:
            raise SpecError(f"requires entry {upstream!r} must be another spec id")
    for relation in _string_list(spec.get("requires_relations", []), "requires_relations"):
        if RELATION_NAME.fullmatch(relation) is None:
            raise SpecError(f"requires_relations entry {relation!r} must be schema.relation")
    if spec.get("requires_relations") and not spec.get("requires"):
        raise SpecError("requires_relations needs the upstream spec in requires")
    for key in ("requires_env", "requires_env_any", "optional_env"):
        for name in _string_list(spec.get(key, []), key):
            if ENV_NAME.fullmatch(name) is None:
                raise SpecError(f"{key} name {name!r} invalid")
    if not set(_string_list(spec.get("blocked_without_env", []), "blocked_without_env")) <= set(
            spec.get("optional_env", [])):
        raise SpecError("blocked_without_env names must also be in optional_env")
    for key in ("record", "record_keys", "record_enums"):
        for path in _string_list(spec.get(key, []), key):
            parse_path(path)
    _validate_session_audience(spec)
    reads = spec.get("session_reads", [])
    if not set(_string_list(reads, "session_reads")) <= set(session_paths(spec)):
        raise SpecError("session_reads must name {session_state} paths of the command")
    if "session_required" in spec and (spec["session_required"] is not True or not reads):
        raise SpecError("session_required must be true beside session_reads")
    _validate_artifacts(spec)
    if "carry_previous_measured" in spec and (
            spec["carry_previous_measured"] is not True
            or DAILY_REPORT_ARG not in spec["command"]):
        raise SpecError(f"carry_previous_measured must be true beside {DAILY_REPORT_ARG!r}")
    if "blocked_when" in spec:
        if not isinstance(spec["blocked_when"], str):
            raise SpecError("blocked_when must be one path")
        parse_path(spec["blocked_when"])
    if "blocked_cause" in spec and (spec["blocked_cause"] not in CAUSES
                                    or "blocked_when" not in spec):
        raise SpecError(f"blocked_cause must be one of {', '.join(CAUSES)} beside blocked_when")
    causes = spec.get("blocked_causes")
    if causes is not None and (
            "blocked_when" not in spec or not isinstance(causes, dict) or not causes
            or any(not isinstance(k, str) or not k or not ENUM_STRING.fullmatch(k)
                   or v not in CAUSES
                   for k, v in causes.items())):
        raise SpecError("blocked_causes maps blocked strings to causes beside blocked_when")
    if "disabled" in spec and (not isinstance(spec["disabled"], str)
                               or not spec["disabled"].strip()):
        raise SpecError("disabled must be a non-empty reason")
    for legacy in _string_list(spec.get("legacy_ids", []), "legacy_ids"):
        if len(legacy) > 64:
            raise SpecError("legacy_ids entries must be <= 64 characters")
    _validate_expect(spec.get("expect"), "expect")
    if "expect_blocked" in spec:
        _validate_expect(spec["expect_blocked"], "expect_blocked")
    if "complete_when" in spec:
        _validate_equals(spec["complete_when"], "complete_when")
        if not spec["complete_when"]:
            raise SpecError("complete_when must name at least one path")
    if "registry_names" in spec:
        kinds = spec["registry_names"]
        if not isinstance(kinds, Mapping) or not kinds or set(kinds) - set(REGISTRY_NAME_KINDS):
            raise SpecError("registry_names must map some of " + ", ".join(REGISTRY_NAME_KINDS)
                            + " to recorded paths")
        recorded = set(spec.get("record") or ())
        for kind, paths in kinds.items():
            for path in _string_list(paths, f"registry_names.{kind}"):
                if path not in recorded:
                    raise SpecError(f"registry_names.{kind} path {path!r} is not in record")
    if "tier" in spec and spec["tier"] not in TIERS:
        raise SpecError("tier must be one of: " + ", ".join(TIERS))
    if "answer_source" in spec and spec["answer_source"] is not True:
        raise SpecError("answer_source must be true when present")
    if "metric" in spec and spec["metric"] is not True:
        raise SpecError("metric must be true when present")
    return [f"unknown spec key {key!r} ignored" for key in sorted(set(spec) - SPEC_KEYS)]


def _validate_artifacts(spec: Mapping[str, Any]) -> None:
    if "artifacts" not in spec:
        return
    declared = spec["artifacts"]
    if not isinstance(declared, list) or not 0 < len(declared) <= MAX_ARTIFACTS:
        raise SpecError(f"artifacts must list 1..{MAX_ARTIFACTS} {{path, kind}} objects")
    paths = set()
    for item in declared:
        if not isinstance(item, Mapping) or set(item) != {"path", "kind"}:
            raise SpecError("each artifact must be exactly {path, kind}")
        kind, path = item["kind"], item["path"]
        if not isinstance(kind, str) or kind not in ARTIFACT_KINDS:
            raise SpecError("artifact kind must be one of: " + ", ".join(sorted(ARTIFACT_KINDS)))
        found = ARTIFACT_PATH.fullmatch(path) if isinstance(path, str) else None
        if found is None or found.group(2) != ARTIFACT_KINDS[kind][0]:
            raise SpecError(f"artifact path must be {{tmp}}/<name>.{ARTIFACT_KINDS[kind][0]}")
        if path in paths:
            raise SpecError(f"artifact path {path!r} declared twice")
        paths.add(path)
        if not any(arg == path or arg.endswith("=" + path) for arg in spec["command"][3:]):
            raise SpecError(f"artifact path {path!r} is not a command argument")


def _validate_equals(mapping: Any, name: str) -> None:
    if not isinstance(mapping, Mapping):
        raise SpecError(f"{name} must be an object")
    for path, value in mapping.items():
        parse_path(path)
        if not (value is None or isinstance(value, (bool, str)) or _is_number(value)):
            raise SpecError(f"{name} values must be scalars")


def _validate_expect(expect: Any, name: str) -> None:
    if not isinstance(expect, Mapping):
        raise SpecError(f"{name} must be an object")
    unknown = set(expect) - EXPECT_KEYS
    if unknown:  # an unenforced expectation would be a silent pass
        raise SpecError(f"unknown {name} keys: " + ", ".join(sorted(unknown)))
    if not isinstance(expect.get("exit_code"), int) or isinstance(expect["exit_code"], bool):
        raise SpecError(f"{name}.exit_code must be an integer")
    for path in _string_list(expect.get("json_fields"), f"{name}.json_fields"):
        parse_path(path)
    for key in ("json_empty", "json_absent"):
        for path in _string_list(expect.get(key, []), f"{name}.{key}"):
            parse_path(path)
    _validate_equals(expect.get("json_equals", {}), f"{name}.json_equals")
    for key in ("json_min", "json_max"):
        mapping = expect.get(key, {})
        if not isinstance(mapping, Mapping):
            raise SpecError(f"{name}.{key} must be an object")
        for path, value in mapping.items():
            parse_path(path)
            if not _is_number(value):
                raise SpecError(f"{name}.{key} values must be numbers")
    mapping = expect.get("json_same", {})
    if not isinstance(mapping, Mapping):
        raise SpecError(f"{name}.json_same must be an object")
    for path, other in mapping.items():
        parse_path(path)
        if not isinstance(other, str) or "[*]" in path or "[*]" in other:
            raise SpecError(f"{name}.json_same maps one path to one other path")
        parse_path(other)


def spec_tier(spec: Mapping[str, Any] | None, check_id: str = "") -> str:
    """The tier a spec declares, else the one its id prefix maps to (``TIER_BY_PREFIX``)."""
    declared = spec.get("tier") if isinstance(spec, Mapping) else None
    if declared in TIERS:
        return declared
    spec_id = str((spec or {}).get("id") or check_id)
    return next((tier for prefix, tier in TIER_BY_PREFIX if spec_id.startswith(prefix)),
                "access")


def load_specs(specs_dir: Path, *, allowed_modules: Iterable[str] = ALLOWED_MODULES
               ) -> list[dict[str, Any]]:
    """Every ``*.json`` in the directory: {id, path, spec, error, warnings}."""
    entries = []
    for path in sorted(specs_dir.glob("*.json")):
        entry: dict[str, Any] = {"id": path.stem, "path": path, "spec": None, "error": None,
                                 "warnings": []}
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
            entry["warnings"] = validate_spec(spec, allowed_modules=allowed_modules,
                                              stem=path.stem)
            entry["spec"] = spec
        except (OSError, ValueError) as exc:  # SpecError and JSON errors are ValueErrors
            entry["error"] = str(exc)[:MAX_STRING_CHARS]
        entries.append(entry)
    ids = [e["id"] for e in entries]
    for entry in entries:
        if ids.count(entry["id"]) > 1 and entry["error"] is None:
            entry["error"] = "duplicate id"
    graph = {e["id"]: [u for u in e["spec"].get("requires", []) if isinstance(u, str)]
             for e in entries if e["spec"] is not None and e["error"] is None}
    cyclic = _requires_cycle_members(graph)
    for entry in entries:
        if entry["id"] in cyclic and entry["error"] is None:
            entry["error"] = "requires cycle: " + " -> ".join(sorted(cyclic))
    return entries


def _requires_cycle_members(graph: Mapping[str, Sequence[str]]) -> set[str]:
    """Ids that sit on a ``requires`` cycle (a spec that reaches itself), iteratively."""
    members = set()
    for start in graph:
        seen: set[str] = set()
        stack = list(graph[start])
        while stack:
            node = stack.pop()
            if node == start:
                members.add(start)
                break
            if node not in seen:
                seen.add(node)
                stack.extend(graph.get(node, ()))
    return members


# ------------------------------------------------------------------ running

DAILY_STALE_HOURS = 26


def daily_status(path: Path, now: datetime,
                 required: Iterable[str] = (), weekly: Iterable[str] = ()
                 ) -> dict[str, Any]:
    """Freshness of the last daily set for the hourly report.

    ``daily_oldest_check_at``: the oldest ``checked_at`` of the set's checks (a set that
    took days to complete is as old as its oldest check); a check of unknown origin (no
    ``main_sha`` or ``checked_at``: made before checks carried them; every check made now
    carries both, skipped, blocked and refused ones included) is listed in
    ``daily_origin_unknown`` and was made no earlier than its set began, so the set's
    ``started_at`` stands in for it when older. ``daily_age_hours`` reads from it and
    ``daily_last_run_at`` is its alias, kept for earlier readers. ``daily_completed_at`` is
    the file's own ``generated_at`` when the set is complete (null for a partial set, whose
    ``pending`` ids are listed in ``daily_pending``); it never sets the age.
    ``daily_never_passed``: the file's ``never_passed`` (its ids with no pass yet).
    ``daily_stale``: the file absent, unreadable or not daily, no checks, a pending id the
    file holds no result for, the oldest check over 26 h, an unknown-origin check in a set
    without ``started_at`` (its age is unbounded), or one that another spec ``requires``
    (listed in ``daily_required_origin_unknown``); any other unknown-origin check marks only
    itself. A check of a weekly id (one ``weekly_of`` the specs in ``SPECS_DIR_NAME`` names,
    joined by any ``weekly`` given) is left out of the age and the origin: it is due by
    ``WEEKLY_DAYS``, not by the daily clock, so no reader (precheck, ``previous_daily``) ages
    the set by it; a weekly id pending again with a held result is not unrun.
    Nothing reruns from ``daily_stale``: it is a reading for people and agents."""
    oldest, completed, unknown, unbounded, pending, unrun = None, None, [], False, [], False
    never_passed: list[str] = []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = None
    if (isinstance(value, Mapping) and value.get("schema") == SCHEMA
            and value.get("cadence") in ("daily", "all")):
        weekly = set(weekly) | set(weekly_of(load_specs(ROOT / SPECS_DIR_NAME)))
        held = [c for c in value.get("checks") or [] if isinstance(c, Mapping)]
        checks = [c for c in held if c.get("id") not in weekly]
        stamps = [_stamp(c.get("checked_at")) if isinstance(c.get("main_sha"), str) else None
                  for c in checks]
        unknown = sorted(str(c.get("id")) for c, at in zip(checks, stamps) if at is None)
        started = _stamp(value.get("started_at")) if unknown else None
        unbounded = bool(unknown) and started is None
        oldest = min([at for at in (*stamps, started) if at is not None], default=None)
        listed = value.get("pending") if value.get("complete") is False else None
        pending = sorted(p for p in listed if isinstance(p, str)) if isinstance(
            listed, list) else []
        completed = None if value.get("complete") is False else _stamp(value.get("generated_at"))
        unrun = bool(set(pending) - {str(c.get("id")) for c in held})
        never = value.get("never_passed")
        never_passed = sorted(i for i in never if isinstance(i, str)) if isinstance(
            never, list) else []
    required_unknown = sorted(set(unknown) & set(required))
    age = None if oldest is None else round((now - oldest).total_seconds() / 3600, 2)
    at = None if oldest is None else _utc(oldest)
    return {"daily_oldest_check_at": at, "daily_last_run_at": at,
            "daily_completed_at": None if completed is None else _utc(completed),
            "daily_age_hours": age,
            "daily_stale": (age is None or age > DAILY_STALE_HOURS or unbounded or unrun
                            or bool(required_unknown)),
            **({"daily_never_passed": never_passed} if never_passed else {}),
            **({"daily_pending": pending} if pending else {}),
            **({"daily_origin_unknown": unknown} if unknown else {}),
            **({"daily_required_origin_unknown": required_unknown}
               if required_unknown else {})}


SET_CADENCES = ("daily", "weekly")  # the cadences the daily set holds


def weekly_of(entries: Iterable[Mapping[str, Any]]) -> list[str]:
    """The ids of the valid specs that declare the ``weekly`` cadence."""
    return [e["id"] for e in entries
            if e["spec"] is not None and spec_cadence(e["spec"]) == "weekly"]


def spec_cadence(spec: Mapping[str, Any] | None) -> str:
    """The cadence a (valid) spec declares; ``hourly`` when absent or unreadable."""
    value = spec.get("cadence") if isinstance(spec, Mapping) else None
    return value if value in CADENCES else DEFAULT_CADENCE


def check_expectations(spec: Mapping[str, Any], exit_code: int, document: Any,
                       expect: Mapping[str, Any] | None = None) -> list[str]:
    expect = spec["expect"] if expect is None else expect
    failures = []
    if exit_code != expect["exit_code"]:
        failures.append(f"exit_code {exit_code} != {expect['exit_code']}")
    needs_json = any(expect.get(k) for k in EXPECT_KEYS - {"exit_code"})
    if needs_json and document is None:
        return failures + ["stdout is not a JSON object"]
    for path in expect["json_fields"]:
        found = resolve(document, path)
        if not found:
            failures.append(f"missing {path}")
        elif any(value is None for value in found):  # a field the run did not measure
            failures.append(f"null {path}")
    for path, wanted in expect.get("json_equals", {}).items():
        found = resolve(document, path)
        if not found:
            failures.append(f"missing {path}")
        elif not all(_same(value, wanted) for value in found):
            failures.append(f"{path} != expected")
    for path, minimum in expect.get("json_min", {}).items():
        found = resolve(document, path)
        if not found:
            failures.append(f"missing {path}")
        elif not all(_is_number(v) and v >= minimum for v in found):
            failures.append(f"{path} < {minimum}")
    for path, maximum in expect.get("json_max", {}).items():
        found = resolve(document, path)
        if not found:
            failures.append(f"missing {path}")
        elif not all(_is_number(v) and v <= maximum for v in found):
            failures.append(f"{path} > {maximum}")
    for path, other in expect.get("json_same", {}).items():
        left, right = resolve(document, path), resolve(document, other)
        if not left or not right:
            failures.append(f"missing {path if not left else other}")
        elif left[0] is None or not _same(left[0], right[0]):
            failures.append(f"{path} != {other}")
    for path in expect.get("json_empty", []):
        if not all(v is None or v == [] or v == {} for v in resolve(document, path)):
            failures.append(f"{path} not empty")
    for path in expect.get("json_absent", []):
        if resolve(document, path):
            failures.append(f"{path} present")
    return failures


def blocked_cause(http_status: Any = None, error_code: Any = None, declared: Any = None) -> str:
    """One of ``CAUSES``: a cause the command declared, else ``relation_unavailable`` for
    the service's ``relation-unavailable`` code, else ``host_misconfiguration`` for
    a refused credential or route (401/403/407, ``not_configured``), else
    ``upstream_outage`` (5xx, throttling, transport, timeout)."""
    if declared in CAUSES:
        return declared
    if error_code in RELATION_UNAVAILABLE_CODES:
        return "relation_unavailable"
    if (isinstance(http_status, int) and http_status in (401, 403, 407)) or \
            error_code in ("not_configured", "unauthorized", "forbidden"):
        return "host_misconfiguration"
    return "upstream_outage"


def blocked_parts(document: Any) -> list[dict[str, Any]]:
    """The command's ``blocked_upstream`` parts, rebuilt from names, statuses and codes
    only (``[]`` when absent or empty); each carries its ``cause``."""
    parts = document.get("blocked_upstream") if isinstance(document, Mapping) else None
    out = []
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, Mapping):
            continue
        status, code = part.get("http_status"), part.get("error_code")
        entry: dict[str, Any] = {
            key: part[key] for key in ("part", "source")
            if isinstance(part.get(key), str) and ENUM_STRING.fullmatch(part[key])}
        if isinstance(status, int) and not isinstance(status, bool):
            entry["http_status"] = status
        if isinstance(code, str) and ERROR_CODE.fullmatch(code):
            entry["error_code"] = code
        entry["cause"] = blocked_cause(entry.get("http_status"), entry.get("error_code"),
                                       part.get("cause"))
        reason = part.get("reason")
        if entry["cause"] == "relation_unavailable" and isinstance(reason, str) and REASON_TEXT.fullmatch(reason):
            entry["reason"] = reason
        out.append(entry)
    return out[:MAX_BLOCKED_PARTS]


def document_causes(document: Any) -> list[str]:
    """The distinct enum-safe ``cause`` values of the command's top-level ``causes`` list
    (``[{part, cause}]``, e.g. ``credential_refused`` for a refused key), in order."""
    causes = document.get("causes") if isinstance(document, Mapping) else None
    found = [c.get("cause") for c in causes if isinstance(c, Mapping)] \
        if isinstance(causes, list) else []
    return list(dict.fromkeys(c for c in found if isinstance(c, str) and c
                              and ENUM_STRING.fullmatch(c)))[:MAX_BLOCKED_PARTS]


def completeness(spec: Mapping[str, Any], document: Any) -> dict[str, Any]:
    """``complete_when`` over the command's output: ``complete`` and the paths that are not
    (missing or another value); ``{}`` for a spec without it."""
    wanted = spec.get("complete_when")
    if not wanted:
        return {}
    if document is None:
        return {"complete": False, "incomplete": sorted(wanted)}
    missing = [path for path, value in wanted.items()
               if not (found := resolve(document, path)) or
               not all(_same(v, value) for v in found)]
    return {"complete": not missing, **({"incomplete": missing} if missing else {})}


def record_paths(spec: Mapping[str, Any]) -> list[str]:
    """The paths a check records: ``record``, else every expected path."""
    expect = spec["expect"]
    return list(spec.get("record") or dict.fromkeys(
        [*expect["json_fields"], *expect.get("json_equals", {}), *expect.get("json_min", {}),
         *expect.get("json_max", {}), *expect.get("json_same", {}),
         *expect.get("json_same", {}).values(),
         *expect.get("json_empty", []), *expect.get("json_absent", [])]))


def under(path: str, prefixes: Sequence[str]) -> bool:
    """``path`` is one of ``prefixes`` or below one: how a recorded path takes a
    ``record_enums`` / ``record_keys`` entry (an entry below a recorded path has no
    effect)."""
    return any(path == e or path.startswith(e + ".") or path.startswith(e + "[")
               for e in prefixes)


def _recorded(spec: Mapping[str, Any], document: Any) -> dict[str, Any]:
    paths = record_paths(spec)
    enum_paths = spec.get("record_enums", [])
    key_paths = spec.get("record_keys", [])

    fields = _envelope(document)
    for path in paths:
        enums, keys = under(path, enum_paths), under(path, key_paths)
        if path in fields and not (enums or keys):  # the spec's wider view wins
            continue
        found = resolve(document, path)
        if not found:
            continue
        values = [summarize(v, enums=enums, keys=keys) for v in found[:MAX_ITEMS]]
        wildcard = "[*]" in path
        fields[path] = values if wildcard else values[0]
        if wildcard and len(found) > MAX_ITEMS:
            fields[path + ".count"] = len(found)
    return fields


def _envelope(document: Any) -> dict[str, Any]:
    """The ``ENVELOPE_KEYS`` the command printed at top level: numbers, booleans and null
    as they are, an identifier-shaped string as it is, anything else typed (a long or
    free-text string by its length, a list by its count, a mapping by its key count)."""
    out: dict[str, Any] = {}
    for key in ENVELOPE_KEYS:
        if not isinstance(document, Mapping) or key not in document:
            continue
        value = document[key]
        if isinstance(value, str):
            out[key] = value if ENVELOPE_STRING.fullmatch(value) else f"<str len={len(value)}>"
        else:  # depth 3: a list or mapping is reduced to its size, never its items
            out[key] = summarize(value, enums=False, depth=3)
    if not isinstance(document, Mapping):
        return out
    store = document.get("store")
    if isinstance(store, str) and ENVELOPE_STRING.fullmatch(store):
        out["store"] = store
    disk = _envelope_disk(document.get("disk"))
    if disk:
        out["disk"] = disk
    return out


def _envelope_disk(value: Any) -> dict[str, dict[str, int]]:
    """``ENVELOPE_DISK`` counts of a stop line's ``disk``: non-negative integers only."""
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, dict[str, int]] = {}
    for name, counts in ENVELOPE_DISK.items():
        measured = value.get(name)
        if not isinstance(measured, Mapping):
            continue
        kept = {k: measured[k] for k in counts
                if type(measured.get(k)) is int and measured[k] >= 0}
        if kept:
            out[name] = kept
    return out


def _stderr_error(stderr: str, root: Path = ROOT) -> dict[str, Any] | None:
    """What a failure may publish from stderr: an error code or exception class only.

    Messages and tracebacks can carry row values, so they are never copied; a JSON
    ``{"error": ...}`` line (the adapters print one) gives its code, otherwise the last
    traceback its exception (``_traceback_error``), otherwise a fixed marker."""
    from verify.data_profile import http_error_code_ok, http_status_ok

    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    if not lines:
        return None
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, Mapping) and "error" in value:
            code = value["error"]
            message, reason = value.get("message"), value.get("reason")
            return {"error": code if isinstance(code, str) and ERROR_CODE.fullmatch(code)
                    else "error_unparsed",
                    **{key: value[key] for key, ok in (("http_status", http_status_ok),
                                                       ("error_code", http_error_code_ok))
                       if ok(value.get(key))},
                    **({"message": summarize(message, enums=False)}
                       if message is not None else {}),
                    **({"reason": reason} if code in REASONED_ERRORS and isinstance(
                        reason, str) and REASON_TEXT.fullmatch(reason) else {})}
    return {**(_traceback_error(stderr.splitlines(), root)
               or {"error": "stderr_unparsed"}), "lines": len(lines)}


def _traceback_error(lines: Sequence[str], root: Path) -> dict[str, str] | None:
    """The last traceback of ``lines`` (raw stderr lines): ``error_class``, the class token
    of its exception line, whatever the class is named and however many lines its message
    has (never the message), and ``frame``, its innermost frame in a file of the
    repository at ``root`` as ``path:line``. The exception line is the first line after
    the frames that is ``Class: message``; without one, a bare ``Class`` (a capitalised
    last name part) only when it is the one bare word there, since a bare word may be
    another thread's print. None without a traceback; no exception line is
    ``stderr_unparsed`` beside the frame."""
    starts = [i for i, line in enumerate(lines) if line.rstrip() == TRACEBACK_HEADER]
    if not starts:
        return None
    error_class = repo_frame = None
    bare: list[str] = []
    for line in lines[starts[-1] + 1:]:
        frame = TRACEBACK_FRAME.match(line)
        if frame is not None:
            path = _repo_frame_path(frame.group(1), root)
            if path is not None:
                repo_frame = f"{path}:{frame.group(2)}"
            continue
        if not line.strip() or line[:1].isspace():   # a frame's source and caret lines
            continue
        token, colon, _ = line.partition(":")
        token = token.strip()
        if len(token) > 80 or not EXCEPTION_CLASS.fullmatch(token):
            break   # neither an exception line nor a bare word: never read further
        if colon:
            error_class = token
            break
        bare.append(token)
    if error_class is None:
        names = [token for token in bare if token.rsplit(".", 1)[-1][:1].isupper()]
        error_class = names[0] if len(names) == 1 else None
    return {**({"error_class": error_class} if error_class else {"error": "stderr_unparsed"}),
            **({"frame": repo_frame} if repo_frame else {})}


def _repo_frame_path(path: str, root: Path) -> str | None:
    """``path`` relative to the repository ``root`` when it names a source file there (an
    installed package or a frozen module is no repository frame), else None."""
    if path.startswith("<"):
        return None
    try:
        relative = (root / path).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    if not REPO_FRAME_PATH.fullmatch(relative) or len(relative) > 160 or any(
            part in ("site-packages", "dist-packages") for part in relative.split("/")):
        return None
    return relative


def _command_text(command: Sequence[str]) -> str:
    return " ".join(arg.replace("{tmp}", "<tmp>").replace("{daily_report}", "<daily_report>")
                    .replace(SESSION_STATE, "<session_state>") for arg in command)


def _expand(args: Sequence[str], tmp: str, daily_report: Path | None,
            session: Mapping[str, str] | None = None) -> list[str]:
    """Fill the placeholders; ``--previous-report={daily_report}`` is dropped without a
    readable daily report file (the probe then has no previous run). ``session``: the
    directory each ``{session_state}`` path expands in (``session_paths`` keys); without
    it every one expands in ``tmp``."""
    have = daily_report is not None and Path(daily_report).is_file()
    out = []
    for arg in args:
        if not have and arg == DAILY_REPORT_ARG:
            continue
        found = SESSION_ARG.search(arg)
        base = (session or {}).get(found.group(1), tmp) if found else tmp
        out.append(arg.replace("{tmp}", tmp).replace("{daily_report}", str(daily_report))
                   .replace(SESSION_STATE, base))
    return out


def session_paths(spec: Mapping[str, Any] | None) -> list[str]:
    """The ``{session_state}`` paths a (valid) spec's command names, in order."""
    command = spec.get("command") if isinstance(spec, Mapping) else None
    found = (SESSION_ARG.fullmatch(arg) for arg in command[3:]
             if isinstance(arg, str)) if isinstance(command, list) else ()
    return list(dict.fromkeys(match.group(1) for match in found if match))


def session_key(spec: Mapping[str, Any], path: str) -> str:
    """The session store of one ``{session_state}`` path of a (valid) spec: the path and its
    declared capture audience (``session_audience``), as a 16-hex key."""
    audience = spec["session_audience"][path]
    reader = audience["reader"]
    document = {"path": path, "sources": sorted(audience["sources"]),
                "env": sorted(audience["env"]),
                "reader": reader if reader == "service" else sorted(reader)}
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()[:16]


def session_producers(spec: Mapping[str, Any] | None,
                      specs: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    """Per ``{session_state}`` path of the spec, the specs it ``requires`` that name the same
    path under the same capture audience (its session producers); a path no required spec
    names so is the spec's own."""
    required = [up for up in (spec or {}).get("requires", []) if up in specs]
    return {path: [up for up in required if path in session_paths(specs[up])
                   and session_key(specs[up], path) == session_key(spec, path)]
            for path in session_paths(spec)}


def blocking_requires(spec: Mapping[str, Any] | None,
                      specs: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The ``requires`` entries that block the spec when they do not pass: all but its
    session producers (a consumer of a failed producer captures cold instead)."""
    producers = {up for ups in session_producers(spec, specs).values() for up in ups}
    return [up for up in (spec or {}).get("requires", []) if up not in producers]


def reads_producers(spec: Mapping[str, Any] | None,
                    specs: Mapping[str, Mapping[str, Any]]) -> bool:
    """The spec cannot run cold (``session_required``) and only reads (``session_reads``)
    every path a session producer of it names: it never runs cold for want of budget."""
    consumed = [path for path, ups in session_producers(spec, specs).items() if ups]
    return ((spec or {}).get("session_required") is True and bool(consumed)
            and set(consumed) <= set(spec.get("session_reads", ())))


def filled_session(check: Mapping[str, Any] | None) -> bool:
    """A producer's run filled its session paths: it ran to its own exit (not timed out,
    refused, skipped or blocked before running) and printed a JSON report that names no
    ``error``. A missed expectation does not void what it captured."""
    detail = (check or {}).get("detail") or {}
    fields = detail.get("fields")
    return ((check or {}).get("result") in ("pass", "fail", "blocked")
            and (check or {}).get("exit_code") is not None and not detail.get("timed_out")
            and isinstance(fields, Mapping) and not fields.get("error"))


class SessionState:
    """The verifier session's ``{session_state}`` directory: one store per path and capture
    audience (``session_key``) and who wrote each (module docstring, "Session state").
    ``close`` removes it. ``warm``: the ``operating.warm_store.WarmStore`` this session
    holds (the directory is made in it, and its ``WARM_ITEMS`` stores are restored on first
    use and kept at ``close`` when their last writer filled them, or when a consumer
    adopted them: ``plan``'s ``warm``), else None (a temporary directory); ``environ``: the
    values its audience stamps read."""

    def __init__(self, root: Path, warm: Any = None,
                 environ: Mapping[str, str] | None = None) -> None:
        self.root = Path(root)
        self.warm = warm
        self.environ = os.environ if environ is None else environ
        # session key -> (store, item, audience stamp) of each warm store it restored
        self.carried: dict[str, tuple[Path, str, str]] = {}
        # session key -> the kept writer (operating.warm_store ``writer``) of each warm store
        # a consumer adopted: restored into this session, written by no spec of it
        self.adopted: dict[str, dict[str, Any]] = {}
        self.path: Path | None = None
        # session key -> {producer, ok, result, run, inputs}: the store's writer, whether
        # every spec that wrote it since filled it, and the write count (``writes``) of each
        # store it read when it ran
        self.written: dict[str, dict[str, Any]] = {}
        self.writes: dict[str, int] = {}   # session key -> specs that wrote it this session
        self.ran: set[str] = set()  # every spec this session has run or recorded
        self.started_at: str | None = None  # when the directory was made (UTC)

    def directory(self) -> Path:
        """The session directory, made on first use outside the repository (0700)."""
        if self.path is None:
            started = _now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            made = (self.warm.session_dir() if self.warm is not None else
                    Path(tempfile.mkdtemp(prefix=f"{SESSION_PREFIX}{os.getpid()}-")))
            if made.resolve().is_relative_to(self.root.resolve()):
                shutil.rmtree(made, ignore_errors=True)
                raise SpecError("session_state would be inside the repository")
            os.chmod(made, 0o700)
            self.path, self.started_at = made, started
        return self.path

    def bases(self, spec: Mapping[str, Any], plan: dict[str, str]) -> dict[str, str]:
        """The directory each path of ``plan`` expands in: its session store, unless the
        store's writer failed (``cold_failed``: the check's own ``{tmp}``). A ``warm`` path
        not yet in this session is restored now; one the warm store no longer restores
        ``warm`` is ``cold_not_run`` in ``plan`` (the store starts empty)."""
        out = {}
        for path, outcome in plan.items():
            if outcome != "cold_failed":
                key = session_key(spec, path)
                store = self.directory() / key
                made = not store.exists()
                store.mkdir(mode=0o700, exist_ok=True)
                writer = self._restore(made, spec, path, key, store, outcome == "warm")
                if outcome == "warm" and key not in self.adopted:
                    if writer is None:
                        plan[path] = "cold_not_run"
                    else:
                        self.adopted[key] = writer
                out[path] = str(store)
        return out

    def _stamp(self, spec: Mapping[str, Any], path: str) -> tuple[str, str] | None:
        """(item, audience stamp) of a ``WARM_ITEMS`` path in a warm session, else None."""
        if self.warm is None:
            return None
        from operating.warm_store import WARM_ITEMS, audience_material

        item = path.strip("/")
        if item not in WARM_ITEMS:
            return None
        return item, self.warm.stamp(audience_material(spec["session_audience"][path],
                                                       self.environ, self.root))

    def _restore(self, made: bool, spec: Mapping[str, Any], path: str, key: str,
                 store: Path, adopt: bool = False) -> dict[str, Any] | None:
        """A warm session's store of a ``WARM_ITEMS`` path, made now: the one the last run
        kept under the same audience stamp, when there is one. ``adopt``: a consumer takes
        it as it is (``plan``'s ``warm``): its kept writer when it was restored ``warm``."""
        held = self._stamp(spec, path) if made else None
        if held is None:
            return None
        item, stamp = held
        writer = self.warm.peek(key, item, stamp) if adopt else None
        restored = self.warm.restore(key, store, item, stamp)
        self.carried[key] = (store, item, stamp)
        return writer if restored == "warm" else None

    def _kept_writer(self, spec: Mapping[str, Any], path: str) -> dict[str, Any] | None:
        """The writer of the store the warm store kept for ``path`` (not yet in this
        session) when it would restore ``warm``, and no store it read was written again
        after it (``coherent``); else None."""
        key = session_key(spec, path)
        held = self._stamp(spec, path)
        if held is None or key in self.carried:
            return None
        writer = self.warm.peek(key, *held)
        return writer if writer is not None and writer["coherent"] else None

    def _adopted_current(self, key: str) -> bool:
        """An adopted store no spec of this session has rewritten a store it was made from."""
        return not any(name in self.writes for name in self.adopted[key]["inputs"])

    def plan(self, spec: Mapping[str, Any],
             specs: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
        """Each session path's outcome for this run of ``spec``: ``shared``, ``warm``,
        ``produced``, ``cold_not_run``, ``cold_audience`` or ``cold_failed``.

        Warm adoption (the one rule for every session consumer, module docstring): a path
        the spec only reads (``session_reads``) and a session producer of it names, with no
        writer in this session, is ``warm`` when every such path of the spec can be taken
        from one earlier session's writers: the store a consumer already adopted in this
        session (none of its inputs rewritten since), or the one the warm store kept that it
        would restore ``warm`` (same audience, fresh), whose writer filled it and whose
        inputs were not rewritten after it (``coherent``), all written in one verifier
        session, each store such a writer read being adopted with it (this spec reads it
        too, or a consumer adopted it in this session): a store is never opened without
        what it was made from. Else nothing new is adopted and the producers run as
        before."""
        out = {}
        producers = session_producers(spec, specs)
        required = [specs[up] for up in spec.get("requires", []) if up in specs]
        reads = set(spec.get("session_reads", ()))
        for path in session_paths(spec):
            key = session_key(spec, path)
            held = self.written.get(key)
            if held is not None:
                out[path] = "shared" if held["ok"] else "cold_failed"
            elif (key in self.adopted and path in reads and producers[path]
                  and self._adopted_current(key)):
                out[path] = "warm"
            elif producers[path]:
                out[path] = "cold_not_run"
            elif any(path in session_paths(up) for up in required):
                out[path] = "cold_audience"
            else:
                out[path] = "produced"
        consumed = [path for path in out if producers[path] and path in reads]
        keys = {session_key(spec, path) for path in consumed}
        sessions, kept = set(), []
        for path in consumed:
            key = session_key(spec, path)
            if out[path] == "shared":
                sessions.add(self.started_at)
            elif out[path] == "warm":
                sessions.add(self.adopted[key]["session_started_at"])
            elif out[path] == "cold_not_run" and key not in self.adopted:
                writer = self._kept_writer(spec, path)
                if writer is None:
                    return out
                for name in writer["inputs"]:  # each store its writer read comes along
                    if name in self.adopted and name not in self.writes \
                            and self._adopted_current(name):
                        sessions.add(self.adopted[name]["session_started_at"])
                    elif name not in keys:
                        return out
                sessions.add(writer["session_started_at"])
                kept.append(path)
            else:
                return out
        if kept and len(sessions) == 1:
            for path in kept:
                out[path] = "warm"
        return out

    def consumes(self, spec: Mapping[str, Any], specs: Mapping[str, Mapping[str, Any]],
                 plan: Mapping[str, str]) -> list[str]:
        """The spec's consumer paths: each a session producer names, less, when every such
        path it reads (``session_reads``) is adopted (``warm``), the paths it writes itself
        (it captures those in its own session store, ``cold_not_run``, as a consumer of the
        adopted generation does)."""
        reads = set(spec.get("session_reads", ()))
        named = [path for path, ups in session_producers(spec, specs).items() if ups]
        consumed = [path for path in named if path in reads]
        if consumed and all(plan.get(path) == "warm" for path in consumed):
            return consumed
        return named

    def needs_producers(self, spec: Mapping[str, Any],
                        specs: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
        """``session_producers`` less what warm adoption makes needless: the producers of
        an adopted (``warm``) path and of a path ``consumes`` leaves out."""
        plan = self.plan(spec, specs)
        kept = set(self.consumes(spec, specs, plan))
        return {path: ups if path in kept and plan[path] != "warm" else []
                for path, ups in session_producers(spec, specs).items()}

    def since(self, spec: Mapping[str, Any], plan: Mapping[str, str]) -> str | None:
        """The check's ``CAPTURE_FRESH_SINCE_ENV``: the session's start, or, when it adopted
        a store, shares none it reads from this session and produces none itself (a
        ``produced`` path: a producer captures afresh even over an adopted input), the
        start of the session that wrote what it adopted (it opens that session's
        generation)."""
        reads = set(spec.get("session_reads", ()))
        adopted = [self.adopted[session_key(spec, path)]["session_started_at"]
                   for path, outcome in plan.items()
                   if outcome == "warm" and session_key(spec, path) in self.adopted]
        if not adopted or "produced" in plan.values() \
                or any(plan[path] == "shared" for path in plan if path in reads):
            return self.started_at
        return min([self.started_at, *adopted]) if self.started_at else min(adopted)

    def detail(self, spec: Mapping[str, Any], plan: Mapping[str, str],
               specs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        """The check's ``session_state`` record: each path's outcome and, for a consumer
        (a path a session producer names, or one it shared or adopted), ``shared_store``
        and the verdict and run of each consumed store's writer (an adopted store's: the
        run of the earlier session that wrote it)."""
        detail: dict[str, Any] = {"paths": dict(plan)}
        producers = session_producers(spec, specs)
        named = set(self.consumes(spec, specs, plan))
        consumed = [path for path in plan if path in named
                    or not producers.get(path) and plan[path] in ("shared", "warm")]
        if consumed:
            detail["shared_store"] = all(plan[path] in ("shared", "warm") for path in consumed)
            held = {}
            for path in consumed:
                key = session_key(spec, path)
                if plan[path] == "warm" and key in self.adopted:
                    held[path] = self.adopted[key]
                elif key in self.written:
                    held[path] = self.written[key]
            if held:
                detail["producer_verdict"] = {path: w["result"] for path, w in held.items()}
                detail["producer_run"] = {path: w["run"] for path, w in held.items()}
        return detail

    def record(self, spec: Mapping[str, Any], plan: Mapping[str, str],
               check: Mapping[str, Any]) -> None:
        """After the run (``check`` stamped with its origin): every store it wrote keeps
        its writer, and whether every writer filled it (one that did not spoils it). A
        path it only reads (``session_reads``) never makes it a writer: a store it shared
        keeps its writer's outcome, and one it captured cold is never shared."""
        filled = filled_session(check)
        reads = set(spec.get("session_reads", ()))
        inputs = {session_key(spec, path) for path, outcome in plan.items()
                  if path in reads and outcome in ("shared", "warm")}
        for path, outcome in plan.items():
            key = session_key(spec, path)
            if outcome in SESSION_WRITES and path not in reads:
                self.written[key] = {
                    "producer": check["id"], "ok": filled, "result": check.get("result"),
                    "run": {name: check.get(name) for name in ("id", "checked_at", "main_sha")},
                    "inputs": {name: self.writes.get(name, 0) for name in inputs - {key}}}
                self.writes[key] = self.writes.get(key, 0) + 1
            elif outcome == "shared" and path not in reads:
                self.written[key]["ok"] = self.written[key]["ok"] and filled
                self.writes[key] = self.writes.get(key, 0) + 1
        self._journal()

    def begin(self, spec: Mapping[str, Any], plan: Mapping[str, str]) -> None:
        """Before the run: the stores it will write leave the warm store's journal until it
        is recorded, so a run killed while it writes keeps none of them."""
        reads = set(spec.get("session_reads", ()))
        self._journal({session_key(spec, path) for path, outcome in plan.items()
                       if path not in reads and outcome in (*SESSION_WRITES, "shared")})

    def _keeps(self, busy: set[str] = frozenset()) -> list[tuple[str, Path, str, str,
                                                                    dict[str, Any], bool]]:
        """What ``close`` keeps now: (key, store, item, stamp, writer, carried) of each warm
        store its last writer filled and of each one a consumer adopted and no spec wrote,
        less the ``busy`` keys."""
        out = []
        for key, (store, item, stamp) in sorted(self.carried.items()):
            if key in busy:
                continue
            if key in self.written:
                if self.written[key].get("ok"):
                    out.append((key, store, item, stamp, self._writer(key), False))
            elif key in self.adopted:
                writer = dict(self.adopted[key])
                writer["coherent"] = writer["coherent"] and self._adopted_current(key)
                out.append((key, store, item, stamp, writer, True))
        return out

    def _journal(self, busy: set[str] = frozenset()) -> None:
        """Write ``_keeps`` to the warm store's journal (``operating.warm_store``), so a
        killed run's next session keeps them; a journal that cannot be written loses only
        that (the run goes on)."""
        if self.warm is not None:
            try:
                self.warm.journal([(key, item, stamp, writer, carried) for
                                   key, _, item, stamp, writer, carried in self._keeps(busy)])
            except OSError:
                pass

    def _writer(self, key: str) -> dict[str, Any]:
        """The kept writer of a store written this session (operating.warm_store)."""
        held = self.written[key]
        return {"producer": held["producer"], "result": held["result"], "run": held["run"],
                "session_started_at": self.started_at,
                "coherent": all(self.writes.get(name, 0) == count
                                for name, count in held["inputs"].items()),
                "inputs": sorted(held["inputs"])}

    def close(self) -> None:
        """Keep each warm store its last writer filled (``written`` ``ok``) with that writer,
        and each one a consumer adopted and no spec wrote (``carried``: its kept writer and
        age), then remove the directory (and release the warm store)."""
        if self.warm is not None:
            try:
                for key, store, item, stamp, writer, carried in self._keeps():
                    self.warm.keep(key, store, item, stamp, writer=writer, carried=carried)
            finally:
                self.warm.close()
                self.path = None
        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)
            self.path = None


def tree_bytes(path: str | Path) -> int:
    """The bytes the files under ``path`` take on disk (allocated blocks; links not
    followed); 0 for what cannot be read."""
    total = 0
    for top, dirs, files in os.walk(path):
        for name in (*dirs, *files):
            try:
                total += os.lstat(os.path.join(top, name)).st_blocks * 512
            except OSError:
                continue
    return total


def free_bytes(directory: str | Path) -> int | None:
    """Bytes available to this user on the file system holding ``directory``."""
    try:
        st = os.statvfs(directory)
    except (OSError, AttributeError):
        return None
    return st.f_bavail * st.f_frsize


def sweep_leftovers(directory: str | Path | None = None) -> dict[str, Any]:
    """Count the ``live-verify-*`` directories in the temp directory (this user's, never a
    link) and their bytes, then remove those a verifier process no longer alive named for
    its pid (``SESSION_DIR`` or ``CHECK_DIR``, mode 0700), the ones a killed run left
    behind. Counts only: ``free_bytes_start`` (before removing), ``leftover_dirs``,
    ``leftover_bytes``, ``swept_dirs``, ``swept_bytes`` and ``session_dirs_swept`` (the
    session directories among them). A live pid keeps its directory (another run, or a
    reused pid); a directory named for no pid is counted and kept."""
    base = Path(directory if directory is not None else tempfile.gettempdir())
    out = {"free_bytes_start": free_bytes(base), "leftover_dirs": 0, "leftover_bytes": 0,
           "swept_dirs": 0, "swept_bytes": 0, "session_dirs_swept": 0}
    for entry in sorted(base.glob(CHECK_PREFIX + "*")):
        try:
            info = entry.lstat()
        except OSError:
            continue
        if not stat_is_dir(info.st_mode) or info.st_uid != os.getuid():
            continue
        size = tree_bytes(entry)
        out["leftover_dirs"] += 1
        out["leftover_bytes"] += size
        match = SESSION_DIR.fullmatch(entry.name) or CHECK_DIR.fullmatch(entry.name)
        if match is None or info.st_mode & 0o777 != 0o700 or _alive(int(match.group(1))):
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            out["swept_dirs"] += 1
            out["swept_bytes"] += size
            out["session_dirs_swept"] += entry.name.startswith(SESSION_PREFIX)
    return out


def _alive(pid: int) -> bool:
    """A process with this pid exists (this one, another user's or one not to judge)."""
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError, ValueError):
        return True
    return True


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        process.kill()


def unmet_env(spec: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """The skip detail of a spec whose ``requires_env`` / ``requires_env_any`` is unmet
    (names only), else empty. One rule for the run and for the budget deferral."""
    missing = [name for name in spec.get("requires_env", []) if not env.get(name)]
    any_of = spec.get("requires_env_any", [])
    detail = {"missing_env": missing} if missing else {}
    if any_of and not any(env.get(name) for name in any_of):
        detail["missing_env_any"] = list(any_of)
    return detail


def absent_credentials(spec: Mapping[str, Any], env: Mapping[str, str]) -> list[str]:
    """The ``blocked_without_env`` names unset in ``env`` (names only, never a value)."""
    return [name for name in spec.get("blocked_without_env", []) if not env.get(name)]


def run_spec(spec: Mapping[str, Any], *, root: Path = ROOT,
             environ: Mapping[str, str] | None = None,
             allowed_modules: Iterable[str] = ALLOWED_MODULES,
             python: str = sys.executable,
             daily_report: Path | None = None,
             session: Mapping[str, str] | None = None,
             session_since: str | None = None,
             artifacts_dir: Path | None = None,
             main_sha: str | None = None) -> dict[str, Any]:
    """Run one spec and return its result entry (redacted, counts only). ``session``: the
    session directory of each ``{session_state}`` path it shares or produces
    (``SessionState.plan``); every other one is cold, in the check's own ``{tmp}``.
    ``session_since``: the session's start (``SessionState.started_at``), the command's
    ``CAPTURE_FRESH_SINCE_ENV`` when ``session`` names a directory (never inherited).
    ``artifacts_dir``: where its declared artifacts publish (results/artifacts), cited with
    ``main_sha``; without it they are validated and digested but not written."""
    env = dict(os.environ if environ is None else environ)
    env.pop(CAPTURE_FRESH_SINCE_ENV, None)
    if session and session_since:
        env[CAPTURE_FRESH_SINCE_ENV] = session_since
    secrets = secret_values(env)
    entry: dict[str, Any] = {"id": spec.get("id") if isinstance(spec, Mapping) else None}
    try:
        validate_spec(spec, allowed_modules=allowed_modules)
    except SpecError as exc:
        entry.update(result="error", detail={"refused": str(exc)[:MAX_STRING_CHARS]})
        return scrub_tree(entry, secrets)
    entry.update({"source": spec.get("source", "n/a"), "command": _command_text(spec["command"]),
                  "spec": f"{SPECS_DIR_NAME}/{spec['id']}.json", "tier": spec_tier(spec)})
    if spec.get("answer_source"):
        entry["answer_source"] = True
    if spec.get("legacy_ids"):
        entry["legacy_ids"] = list(spec["legacy_ids"])
    if spec.get("disabled"):
        entry.update(result="skipped", detail={"disabled": spec["disabled"]})
        return scrub_tree(entry, secrets)
    detail = unmet_env(spec, env)
    if detail:
        entry.update(result="skipped", detail=detail)
        return scrub_tree(entry, secrets)
    absent = absent_credentials(spec, env)
    if absent:
        entry.update(result="blocked", detail={
            "reason": f"upstream {spec.get('source', 'n/a')} credentials_absent",
            "cause": "later_ask", "missing_settings": absent})
        return scrub_tree(entry, secrets)
    any_of = spec.get("requires_env_any", [])
    declared = {*spec.get("requires_env", []), *any_of, *spec.get("optional_env", [])}
    hidden = sorted(name for name in env
                    if name not in declared and CREDENTIAL_ENV_NAME.search(name))
    env = {name: value for name, value in env.items() if name not in hidden}
    tmp = tempfile.mkdtemp(prefix=f"{CHECK_PREFIX}{os.getpid()}-")  # CHECK_DIR
    bases = {path: (session or {}).get(path) or os.path.join(tmp, "session-state")
             for path in session_paths(spec)}
    for path, base in bases.items():
        os.makedirs(os.path.dirname(base + path), exist_ok=True)
    argv = [python, "-c", ENV_GUARD,
            ",".join(n for n in hidden if ENV_NAME.fullmatch(n) and n not in UNGUARDED_ENV),
            spec["command"][2],
            *_expand(spec["command"][3:], tmp, daily_report, bases)]
    timeout = spec.get("timeout_seconds", DEFAULT_TIMEOUT)
    started = time.monotonic()
    detail: dict[str, Any] = {}
    try:
        process = subprocess.Popen(argv, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            try:
                process.communicate(timeout=KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:  # a pipe held open outside the group
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
            entry.update(result="fail", exit_code=None,
                         duration_s=round(time.monotonic() - started, 2),
                         detail={"timeout_seconds": timeout, "timed_out": True,
                                 "failures": ["timeout"]})
            return scrub_tree(entry, secrets)
        except BaseException:  # interrupted or terminated: the spec never outlives the run
            _kill_group(process)
            raise
        captured = capture_artifacts(spec, tmp, secrets)  # before {tmp} is removed
    except OSError as exc:
        entry.update(result="error", detail={"runner_error": type(exc).__name__})
        return scrub_tree(entry, secrets)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    entry["exit_code"] = process.returncode
    entry["duration_s"] = round(time.monotonic() - started, 2)
    undeclared = _undeclared_read(process.returncode, stderr, hidden)
    if undeclared:
        entry.update(result="error", detail={"undeclared_env": undeclared})
        return scrub_tree(entry, secrets)
    text = stdout.decode("utf-8", "replace")
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if not isinstance(document, Mapping):
        document = None
    failures = check_expectations(spec, process.returncode, document)
    parts = blocked_parts(document) if spec.get("expect_blocked") else []
    causes = document_causes(document)
    if parts:
        # Upstream parts did not run: only the rest is checked, and it is never a pass.
        detail["blocked"] = {"parts": parts, "cause": parts[0]["cause"]}
        if causes:  # the command's own headline: a refused key beside an outage
            detail["blocked"]["causes"] = list(dict.fromkeys(
                [*causes, *(p["cause"] for p in parts)]))
        if failures:
            detail["unverified"] = failures
        failures = check_expectations(spec, process.returncode, document,
                                      spec["expect_blocked"])
    if document is not None:
        detail["fields"] = _recorded(spec, document)
    else:
        detail["stdout_json"] = False
    detail.update(completeness(spec, document))
    if not failures and not parts and detail.get("complete") is False:
        # every expectation held over a run its own complete_when calls incomplete:
        # green would claim a whole capture the run did not make
        failures = [f"incomplete {path}" for path in detail["incomplete"]]
    if failures:
        # The command's causes lead the failures: the headline names why, not only what.
        detail["failures"] = [f"causes {', '.join(causes)}", *failures] if causes else failures
        error = _stderr_error(stderr.decode("utf-8", "replace"), root)
        if error:
            detail["stderr"] = error
    entry["result"] = "fail" if failures else "blocked" if parts else "pass"
    blocked = (resolve(document, spec["blocked_when"]) if document is not None
               and spec.get("blocked_when") and process.returncode == spec["expect"]["exit_code"]
               else [])
    if blocked and isinstance(blocked[0], str) and blocked[0] \
            and ENUM_STRING.fullmatch(blocked[0]):
        detail.pop("failures", None)
        detail.pop("stderr", None)
        detail["reason"] = f"upstream {spec.get('source', 'n/a')} {blocked[0]}"
        detail["cause"] = spec.get("blocked_causes", {}).get(
            blocked[0], spec.get("blocked_cause", "upstream_outage"))
        entry["result"] = "blocked"
    elif parts and not failures and _positive_minimum(spec["expect_blocked"]):
        # the rest ran and measured something: a positive minimum was required and met
        detail["blocked"]["measured"] = True
    entry["detail"] = _cap(scrub_tree(detail, secrets), spec)
    if spec.get("profile") and document is not None:
        # Published even on a failed expectation (a partial profile is still shape
        # evidence); rebuilt from the closed profile shape, then scrubbed like the rest.
        entry["profile"] = _sanitize_profile(document)
    entry = scrub_tree(entry, secrets)
    if captured:  # after the scrub: digests are hex, names and kinds the spec's own
        entry["artifacts"] = publish_artifacts(spec["id"], captured, artifacts_dir, main_sha)
    return entry


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
    (``starved``), the most deferred first, then never run, then the oldest last stored
    result (ahead of value), then, with a ``now``, the daily specs whose last stored result
    is over ``DAILY_STALE_HOURS`` old (each keeps latest.json ``daily_stale``; a weekly one
    is due by ``WEEKLY_DAYS`` instead, one never run keeps its value place), the oldest
    first, then the rest; each by value
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
        due = ((0, -held["count"], last(entry) != "", last(entry)) if starved(held)
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
    return out


def upstream_cause(check: Mapping[str, Any]) -> str:
    """The ``CAUSES`` entry for a dependent of a failed or blocked upstream check."""
    detail = check.get("detail") or {}
    if isinstance(detail.get("cause"), str) and detail["cause"] in CAUSES:
        return detail["cause"]
    blocked = detail.get("blocked") if isinstance(detail.get("blocked"), Mapping) else {}
    if blocked.get("cause") in CAUSES:
        return blocked["cause"]
    stderr = detail.get("stderr") if isinstance(detail.get("stderr"), Mapping) else {}
    return blocked_cause(stderr.get("http_status"), stderr.get("error_code"))


def upstream_reason(check: Mapping[str, Any]) -> str:
    """``upstream <source> <http_status/error_code>`` for a failed upstream check: only the
    status, code or error class the check recorded, else ``failed``/``timeout``."""
    detail = check.get("detail") or {}
    stderr = detail.get("stderr") if isinstance(detail.get("stderr"), Mapping) else {}
    parts = [str(stderr[k]) for k in ("http_status", "error_code", "error", "error_class")
             if stderr.get(k) is not None]
    if not parts:
        parts = ["timeout" if detail.get("timed_out") else check.get("result") or "failed"]
    return f"upstream {check.get('source') or 'n/a'} {' '.join(parts[:2])}"


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


def previous_daily(daily_report: Path | None, merge_into: Path | None,
                   now: datetime | None = None) -> tuple[Path | None, dict[str, Any] | None]:
    """The report ``{daily_report}`` names and the gap when none could be: ``--daily-report``
    when that file exists, else the newest complete daily report beside the ``--merge-into``
    file (never that file) unless it is older than ``PREVIOUS_DAILY_MAX_HOURS`` by its oldest
    check (``daily_status``; refused, a ``daily_report_stale`` gap: a probe never compares
    with a stale run), else ``daily_report`` (the argument is then dropped)."""
    if daily_report is not None and Path(daily_report).is_file() or merge_into is None:
        return daily_report, None
    best: tuple[tuple[str, str], Path] | None = None
    for candidate in Path(merge_into).parent.glob("*.json"):
        if candidate.resolve() == Path(merge_into).resolve():
            continue
        report = load_report(candidate)
        if (report is None or report.get("cadence") != "daily"
                or report.get("complete") is not True):
            continue
        key = (str(report.get("generated_at") or ""), candidate.name)
        if best is None or key > best[0]:
            best = (key, candidate)
    if best is None:
        return daily_report, None
    age = daily_status(best[1], now or _now())["daily_age_hours"]
    if age is None or age > PREVIOUS_DAILY_MAX_HOURS:
        return daily_report, {"gap": "daily_report_stale", "report": best[1].name,
                              "age_hours": age, "max_age_hours": PREVIOUS_DAILY_MAX_HOURS}
    return best[1], None


def measured_previous(spec_id: str, daily_report: Path | None, merge_into: Path | None,
                      now: datetime | None = None) -> tuple[Path | None, str | None]:
    """The report a spec's ``{daily_report}`` names and, when carried, the ``checked_at`` of
    the run it carries. ``daily_report`` when its check of ``spec_id`` ran to its own exit
    with a JSON report (``filled_session``) or the file is absent; else (that run was blocked,
    timed out or refused, so it recorded no fields) the daily report beside it (never the
    ``merge_into`` file) whose check of ``spec_id`` did, the newest by that check's
    ``checked_at`` and at most ``CARRIED_PREVIOUS_MAX_HOURS`` old, so an empty run never
    erases a probe's memory (the rotation of ``mirror.relation_counts``); else
    ``daily_report``."""
    report = load_report(Path(daily_report)) if daily_report is not None else None
    if report is None:
        return daily_report, None

    def measured(document: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        checks = (document or {}).get("checks")
        return next((c for c in checks if isinstance(c, Mapping) and c.get("id") == spec_id
                     and filled_session(c)), None) if isinstance(checks, list) else None
    if measured(report) is not None:
        return daily_report, None
    at = now or _now()
    skip = {Path(merge_into).resolve()} if merge_into is not None else set()
    best: tuple[datetime, str, Path] | None = None
    for candidate in Path(daily_report).parent.glob("*.json"):
        if candidate.resolve() in skip:
            continue
        document = load_report(candidate)
        check = measured(document) if (document or {}).get("cadence") == "daily" else None
        stamp = _stamp(check.get("checked_at")) if check is not None else None
        # -0.1 h: a run stamped up to 6 min ahead (host clock skew) still counts.
        if stamp is None or check.get("origin") == "unknown" or not (
                -0.1 <= (at - stamp).total_seconds() / 3600 <= CARRIED_PREVIOUS_MAX_HOURS):
            continue
        if best is None or (stamp, candidate.name) > best[:2]:
            best = (stamp, candidate.name, candidate)
    if best is None:
        return daily_report, None
    return best[2], best[0].strftime("%Y-%m-%dT%H:%M:%SZ")


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


def csdl_refusal(raw: bytes) -> str | None:
    """None when ``raw`` is a schema-only OData CSDL document (``bc_metadata_csdl``), else
    the fixed refusal code. Every element is CSDL (an entity instance is not), attributes
    are plain names, text sits only in annotation constants; no DTD, comment or PI."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "not_utf8"
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        return "doctype"
    body = re.sub(r"\A\ufeff?\s*<\?xml[^>]*\?>", "", text)
    if "<!--" in body or "<?" in body:  # outside the root ElementTree drops them unseen
        return "comment_or_pi"
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return "not_xml"
    if root.tag != f"{{{_EDMX_NS}}}Edmx":
        return "not_csdl"
    if root.find(f"{{{_EDMX_NS}}}DataServices/{{{_EDM_NS}}}Schema") is None:
        return "no_schema"
    stack = [(root, False)]
    while stack:
        element, in_annotation = stack.pop()
        ns, _, local = element.tag[1:].partition("}") if element.tag[:1] == "{" else ("", "", "")
        expression = ns == _EDM_NS and local in CSDL_EXPRESSIONS
        if not ((ns == _EDMX_NS and local in CSDL_EDMX)
                or (ns == _EDM_NS and local in CSDL_STRUCTURE)
                or (expression and in_annotation)):
            return "foreign_element"
        if not all(CSDL_ATTRIBUTE.fullmatch(name) for name in element.attrib):
            return "foreign_attribute"
        value = (element.text or "").strip()
        if (value and not (expression and local in CSDL_TEXT_EXPRESSIONS)) \
                or (element.tail or "").strip():
            return "text_content"
        if len(value) > MAX_ARTIFACT_TEXT:
            return "text_length"
        stack.extend((child, in_annotation or local == "Annotation") for child in element)
    return None


# kind -> (file extension, validator: None when the bytes are that kind, else a code)
ARTIFACT_KINDS = {"bc_metadata_csdl": ("xml", csdl_refusal)}


def capture_artifacts(spec: Mapping[str, Any], tmp: str, secrets: Sequence[str]
                      ) -> list[tuple[dict[str, Any], bytes | None]]:
    """Read and validate a (valid) spec's declared artifacts in its ``{tmp}``, after its
    command exited: per path its row and the bytes, only when they passed its kind."""
    out: list[tuple[dict[str, Any], bytes | None]] = []
    for item in spec.get("artifacts") or ():
        found = ARTIFACT_PATH.fullmatch(item["path"])
        row: dict[str, Any] = {"name": f"{found.group(1)}.{found.group(2)}",
                               "kind": item["kind"]}
        path = os.path.join(tmp, row["name"])
        try:
            regular = stat_is_reg(os.lstat(path).st_mode)
            if regular:  # never a symlink; a FIFO never blocks the run
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as handle:
                    regular = stat_is_reg(os.fstat(handle.fileno()).st_mode)
                    raw = handle.read(MAX_ARTIFACT_BYTES + 1) if regular else b""
        except FileNotFoundError:
            out.append(({**row, "status": "absent"}, None))
            continue
        except OSError:
            regular = False
        if not regular:
            out.append(({**row, "status": "refused", "reason": "not_regular_file"}, None))
            continue
        reason = ("size" if not raw or len(raw) > MAX_ARTIFACT_BYTES
                  else "secret_value" if any(value.encode() in raw for value in secrets)
                  else ARTIFACT_KINDS[item["kind"]][1](raw))
        if reason:
            out.append(({**row, "status": "refused", "reason": reason,
                         "bytes": min(len(raw), MAX_ARTIFACT_BYTES)}, None))
            continue
        out.append(({**row, "status": "captured", "sha256": hashlib.sha256(raw).hexdigest(),
                     "bytes": len(raw), "captured_at": _utc(_now())}, raw))
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
    except ImportError:  # run as a script: verify/ itself is sys.path[0]
        from data_profile import sanitize_profile  # type: ignore[no-redef]
    return sanitize_profile(document)


def _cap(detail: dict[str, Any], spec: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``detail`` within ``MAX_DETAIL_CHARS``. The recorded fields that hold a path
    ``spec``'s expectations assert (the path itself, above or below it) are kept first:
    the asserted part of each (all of it when a value at it or above it is asserted, else
    the asserted paths below it projected out of the recorded value, else all of it),
    smallest first, then each of those whole where it still fits, then the rest in order,
    each one that still fits. ``fields_truncated`` counts the paths dropped (``projected`` the fields kept
    only in their asserted parts). Verdicts are already computed and ``detail`` already
    scrubbed: this only decides which parts of the record stay, never adds a value."""
    if len(json.dumps(detail, sort_keys=True)) <= MAX_DETAIL_CHARS:
        return detail

    def fits(trial: Mapping[str, Any]) -> bool:
        return len(json.dumps(trial, sort_keys=True)) <= MAX_DETAIL_CHARS

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
    (absent, unfetched or unreadable), ``LOCKED_EXIT`` when another one. The scheduler pushes
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


def _now() -> datetime:
    """The one clock every stamp of a run reads (started_at, checked_at, generated_at, the
    markers), so a test can step it."""
    return datetime.now(timezone.utc)


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
    ``results_dir`` and its ``daily/`` dir (the published results), so a lost
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
    """B1's one answer rate as counts (decision 2026-10-19-b1-one-answer-rate,
    ``evals.score.answer_rates``): accuracy correct/answered, coverage answered over the
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


def build_report(entries: Sequence[Mapping[str, Any]], checks: Sequence[Mapping[str, Any]], *,
                 root: Path, main_sha: str | None, previous_main_sha: str | None,
                 now: datetime | None = None,
                 previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
    now = now or _now()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    sha = main_sha or _git(root, "rev-parse", "HEAD")
    ref = sha or "HEAD"
    report: dict[str, Any] = {
        "schema": SCHEMA, "run": "live-" + stamp, "main_sha": sha,
        "generated_at": stamp, "mode": MODE,
        "live_verify_py_on_main": _on_ref(root, ref, "verify/live_verify.py"),
        "live_checks_dir_on_main": _on_ref(root, ref, SPECS_DIR_NAME)}
    derived = summarize_checks(checks, now, previous)
    report.update(summary=derived.pop("summary"), checks=list(checks), **derived)
    if previous_main_sha:
        report["previous_main_sha"] = previous_main_sha
    return report


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


def write_latest(path: Path, text: str) -> bool:
    """Publish a latest.json copy; a failed write is reported (stderr), never ends the run:
    the report it copies is already written and stays as it finished."""
    try:
        write_atomic(path, text)
    except OSError as exc:
        print(json.dumps({"warning": "latest_write_failed", "path": path.name,
                          "error": type(exc).__name__}), file=sys.stderr)
        return False
    return True


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


def advance_latest(results_dir: Path, root: Path, main_ref: str = MAIN_REF) -> Path | None:
    """Copy the newest publishable report in ``results_dir`` whose ``main_sha`` is
    ``main_ref`` or its ancestor (in ``root``) to ``latest.json`` there, when it is newer
    than the report latest.json holds; returns the report copied, else None (fails closed:
    an unresolvable SHA, or a ref that cannot be fetched first, never advances)."""
    latest = results_dir / "latest.json"
    held = load_report(latest)
    floor = _stamp((held or {}).get("generated_at"))
    found = []
    for path in results_dir.glob("*.json"):
        if path.name in ("latest.json", HISTORY_NAME, LOCK_NAME):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            report = json.loads(text)
        except (OSError, ValueError):
            continue
        if isinstance(report, dict) and report.get("schema") == SCHEMA and _publishable(report):
            stamp = _stamp(report["generated_at"])
            if floor is None or stamp > floor:
                found.append((stamp, path.name, path, text, report["main_sha"]))
    if found and _fetched(root, main_ref) is None:
        return None
    for _, _, path, text, sha in sorted(found, reverse=True):
        if _git(root, "merge-base", "--is-ancestor", sha, main_ref) is not None:
            return path if write_latest(latest, text) else None
    return None


def merge_reports(base: Mapping[str, Any] | None, part: Mapping[str, Any],
                  daily_ids: Sequence[str],
                  prior: Mapping[str, Mapping[str, Any]] | None = None,
                  pending: Iterable[str] | None = None,
                  run: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Fold a partial run into the daily report: its checks replace same-id checks, the
    other checks stay, and the envelope (stamp, daily status, stop reason) is the newest
    run's; ``started_at`` stays the set's. Every check keeps its own origin (``main_sha``,
    ``checked_at``, stamped when it was made) and is never restamped; one without it is
    marked ``origin`` ``unknown``. The result is ``complete`` once every daily spec has a
    result, from the write right after its last check on (a spec of another cadence this
    run left waiting, e.g. an upstream deferred for budget, is not the set's); until then
    ``pending`` lists the rest and ``deferred_budget`` those a run deferred for budget.
    ``prior``: the set's checks from before this run, by id; a check of this run that
    replaces one keeps it in ``earlier_runs`` (``with_earlier``).
    ``pending``: the set's pending ids before this run (``daily_set``), when its checks
    carry results of an earlier set: this run's checks leave it, the rest stays. Without it
    (a plain ``--merge-into``) a daily spec the run has not finished (``part``'s
    ``pending``: running, waiting or interrupted) stays pending too, though the file holds
    an earlier result for it.
    ``run``: this run's entry in the set's ``runs`` (its ``id``, ``started_at``,
    ``main_sha``, ``stop_reason``, the ids it ``ran`` and those it ``deferred`` with why),
    replacing an entry of the same ``id``; the newest ``RUN_LOG`` are kept."""
    base = base or {}
    by_id = {c["id"]: c for c in base.get("checks", [])}
    by_id.update({c["id"]: with_earlier(c, (prior or {}).get(c["id"]))
                  for c in part["checks"]})
    checks = [check if isinstance(check.get("main_sha"), str) and _stamp(check.get("checked_at"))
              else {**check, "origin": "unknown"} for check in by_id.values()]
    merged = {k: v for k, v in part.items()
              if k not in ("not_run", "pending", "deferred_budget", *DERIVED_KEYS)}
    now = _stamp(part.get("generated_at")) or _now()
    if pending is None:
        unfinished = part.get("pending")
        unfinished = {i for i in unfinished if isinstance(i, str)} if isinstance(
            unfinished, list) else set()
        pending = sorted((set(daily_ids) - set(by_id)) | unfinished.intersection(daily_ids))
    else:
        pending = sorted(set(pending).intersection(daily_ids)
                         - {c["id"] for c in part["checks"]})
    before = base.get("deferred_budget")
    deferred = {**(before if isinstance(before, Mapping) else {}),
                **(part.get("deferred_budget") or {})}
    merged.update({"cadence": "daily", "checks": checks,
                   "started_at": base.get("started_at") or base.get("generated_at")
                   or part.get("started_at") or part.get("generated_at"),
                   "complete": not pending,
                   **summarize_checks(checks, now, base or None)})
    listed = base.get("runs")
    runs = [r for r in listed if isinstance(r, Mapping)
            and (run is None or r.get("id") != run.get("id"))] if isinstance(listed, list) else []
    runs = [*runs, *([dict(run)] if run is not None else [])][-RUN_LOG:]
    if runs:
        merged["runs"] = runs
    if pending:
        merged["pending"] = pending
        held = {i: deferred[i] for i in pending if isinstance(deferred.get(i), Mapping)}
        if held:
            merged["deferred_budget"] = held
    return merged


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


def daily_set(held: Mapping[str, Any] | None, new_set: bool, daily_ids: Sequence[str],
              upstream: Iterable[str] = (), weekly: Iterable[str] = (),
              now: datetime | None = None
              ) -> tuple[dict[str, Any], list[str], dict[str, Mapping[str, Any]]]:
    """The base a resumed run merges into, the set's pending ids and its results so far.
    A continued set keeps its ``pending`` and only shrinks it (a spec changed or unskipped
    since waits for the next set), except that a daily spec it holds no result for (one
    landed since) is pending; a new set (or an incomplete one whose ``pending`` is not a list) has every daily spec
    pending and carries ``held``'s checks (and its answer stamps) but none of its envelope,
    so it starts at this run. Either keeps only the checks of current daily specs and the
    specs they require (``upstream``): a check of a spec deleted, renamed or no longer daily
    never runs again, so it is dropped rather than aging the set forever. ``daily_ids``
    holds the ``weekly`` ids too; a
    weekly id is pending only when the set holds no result for it or its ``checked_at`` is
    ``WEEKLY_DAYS`` old (or unreadable, or ahead of now), in a new set as in a continued one."""
    weekly = set(weekly) & set(daily_ids)
    at = now or _now()
    cutoff = at - timedelta(days=WEEKLY_DAYS)
    stamps = {c["id"]: _stamp(c.get("checked_at")) for c in (held or {}).get("checks") or []
              if isinstance(c, Mapping)}
    # A checked_at ahead of now (host clock skew) is due too: it would otherwise hold for weeks.
    due = {i for i in weekly if stamps.get(i) is None or not cutoff < stamps[i] <= at}
    keep = set(daily_ids) | set(upstream)
    checks = [c for c in (held or {}).get("checks") or []
              if isinstance(c, Mapping) and c.get("id") in keep]
    listed = (held or {}).get("pending")
    if new_set or held is None or not isinstance(listed, list):  # unreadable: run them all
        before = (held or {}).get("answer_correctness")
        return ({"checks": checks, **({"answer_correctness": before} if before else {})},
                sorted((set(daily_ids) - weekly) | due), {})
    # A daily spec the set holds no result for is pending whenever it landed: left out, it
    # would be neither a result nor pending, and the set would complete without it.
    unheld = set(daily_ids) - weekly - {c["id"] for c in checks}
    pending = ({p for p in listed if isinstance(p, str)} | due | unheld) & set(daily_ids)
    return ({**held, "checks": checks}, sorted(pending),
            {c["id"]: c for c in checks if c["id"] not in pending})


def _instant(value: Any) -> datetime | None:
    """An ISO-8601 UTC stamp (``Z`` or an offset; naive reads as UTC), else None."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
                ).astimezone(timezone.utc)
    except (ValueError, OverflowError):  # 0001-01-01T00:00:00+01:00 has no UTC instant
        return None


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def marker_live(marker: Any, mtime: datetime | None, now: datetime) -> bool:
    """A run marker is live while ``updated_at`` is under 2 h old; a legacy marker without
    it, while ``started_at`` is; an unreadable one, while the file is."""
    value = marker if isinstance(marker, Mapping) else {}
    stamp = _instant(value.get("updated_at")) or _instant(value.get("started_at")) or mtime
    return stamp is None or now - stamp < timedelta(hours=LOCK_LIVE_HOURS)


class RunLock:
    """``results/running.json`` held for one run (module docstring, "One run at a time")."""

    def __init__(self, path: Path, session: str, sha: str | None, phase: str,
                 claim: Mapping[str, Any] | None = None) -> None:
        """``claim``: this session's pushed marker, put back on exit when no marker of the
        session is here."""
        self.path, self.session, self.token = path, session, uuid.uuid4().hex
        self.claim = dict(claim) if claim is not None else None
        self.marker: dict[str, Any] = {"session": session, "token": self.token, "sha": sha,
                                       "phase": phase}
        self.held_before: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @contextlib.contextmanager
    def _exclusive(self):
        """Hold an flock on the marker's directory: a read-then-write of the marker is one
        step for every run on this checkout (closing the descriptor releases it)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _held(self) -> tuple[Any, datetime | None]:
        try:
            return (json.loads(self.path.read_text(encoding="utf-8")),
                    datetime.fromtimestamp(self.path.stat().st_mtime, timezone.utc))
        except (OSError, ValueError):
            return None, None

    def _own(self) -> bool:
        held, _ = self._held()
        return isinstance(held, Mapping) and held.get("token") == self.token

    def acquire(self) -> str | None:
        """Write the marker and start the heartbeat; the refusal reason instead when
        another session's marker is live."""
        now = _now()
        with self._exclusive():
            if self.path.exists():
                held, mtime = self._held()
                if isinstance(held, Mapping) and held.get("session") == self.session:
                    self.held_before = dict(held)
                elif marker_live(held, mtime, now):
                    who = held.get("session") if isinstance(held, Mapping) else "unreadable"
                    return f"live run marker {self.path.name} of session {who}"
            if self.held_before is None and self.claim is not None:
                self.held_before = self.claim
            self.marker["started_at"] = (self.held_before or {}).get("started_at") or _utc(now)
            self._write()
        self._thread = threading.Thread(target=self._beat, name="live-verify-heartbeat",
                                        daemon=True)
        self._thread.start()
        return None

    def _write(self) -> None:
        self.marker["updated_at"] = _utc(_now())
        write_atomic(self.path, json.dumps(self.marker) + "\n")

    def _beat(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                with self._exclusive():
                    if self._own():  # not once another run of this session took it over
                        self._write()
            except OSError as exc:
                print(json.dumps({"warning": "heartbeat_write_failed",
                                  "error": type(exc).__name__}), file=sys.stderr)

    def release(self) -> None:
        """Stop the heartbeat; while the marker carries this run's token, remove it, or put
        back, refreshed, the one of its own session it found; else leave it alone."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self._exclusive():
            if not self._own():
                return
            if self.held_before is not None:
                self.marker = dict(self.held_before)
                self._write()
            else:
                self.path.unlink(missing_ok=True)


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


def process_started() -> float:
    """The ``time.monotonic`` instant this process started (``/proc/self/stat`` against
    ``/proc/uptime``, never later than now), so the command limit counts the interpreter's
    startup too; now when either is unreadable."""
    now = time.monotonic()
    try:
        ticks = int(Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19])
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        return now - max(0.0, uptime - ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return now


def main(argv: Sequence[str] | None = None, *, root: Path = ROOT,
         environ: Mapping[str, str] | None = None,
         allowed_modules: Iterable[str] = ALLOWED_MODULES,
         reviewed_main_only: bool = False, started: float | None = None) -> int:
    """``started``: the ``time.monotonic`` instant the command started (the script passes
    ``process_started``), from which ``COMMAND_LIMIT_SECONDS`` counts; default this call."""
    command_started = time.monotonic() if started is None else started
    parser = argparse.ArgumentParser(prog="python3 verify/live_verify.py",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="validate every spec; run nothing")
    parser.add_argument("--only", action="append", default=[], metavar="ID",
                        help="run only this spec id (repeatable)")
    parser.add_argument("--cadence", choices=(*CADENCES, "all"), default=None,
                        help="without --only: run the specs of this cadence "
                             f"(default {DEFAULT_CADENCE}; all = every spec)")
    parser.add_argument("--daily-report", type=Path, default=None,
                        help="last daily report: record its age (daily_stale > 26 h) and "
                             "pass it to --previous-report={daily_report} spec arguments")
    parser.add_argument("--specs-dir", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None, help="also write the report here")
    parser.add_argument("--merge-into", type=Path, default=None,
                        help="daily report to fold this run's checks into (created when "
                             "absent); lets the daily set run as several commands")
    parser.add_argument("--resume-daily", type=Path, default=None, metavar="FILE",
                        help="continue the newest incomplete daily set beside FILE "
                             "(results/daily/<sha>.json), whatever SHA it started on, else "
                             "start one carrying the last set's checks: run only its pending "
                             "ids (starved budget deferrals, then by value: metric, answers, "
                             "tiers, capture last; upstream first), writing <this run's "
                             "main_sha>.json beside FILE and latest.json before the first "
                             "spec and after each one; implies --merge-into")
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="--resume-daily: start no spec whose 1.5 x last duration "
                             "(else timeout) exceeds the budget left (default "
                             f"{DEFAULT_BUDGET_SECONDS}); "
                             "the first spec of a command always runs")
    parser.add_argument("--profiles-dir", type=Path, default=None,
                        help="profile files dir (default: <output dir>/profiles)")
    parser.add_argument("--previous", type=Path, default=None, metavar="FILE",
                        help="the previous report (default: results/latest.json beside an "
                             "--output in results/; for a daily merge the --daily-report "
                             "file, else latest.json beside it): carries blocked_since and the "
                             "answer-correctness stamps over")
    parser.add_argument("--blocked-history", type=Path, default=None, metavar="FILE",
                        help="blocked-age store, read and rewritten after every spec "
                             f"(default: {HISTORY_NAME} in the results/ dir of --output, "
                             "--merge-into or --daily-report; every report there is folded in)")
    parser.add_argument("--main-sha", default=None, help="default: git rev-parse HEAD")
    parser.add_argument("--session", default=None,
                        help=f"session id in results/running.json (default: env {SESSION_ENV})")
    parser.add_argument("--previous-main-sha", default=None)
    args = parser.parse_args(argv)
    specs_dir = args.specs_dir or root / SPECS_DIR_NAME
    entries = load_specs(specs_dir, allowed_modules=allowed_modules)
    unknown = sorted(set(args.only) - {e["id"] for e in entries})
    if unknown:
        print(json.dumps({"error": "unknown_spec_id", "ids": unknown}), file=sys.stderr)
        return 2
    if args.only and args.cadence is not None:
        parser.error("--only runs the named ids whatever their cadence; drop --cadence")
    if args.resume_daily is not None and (args.only or args.cadence or args.dry_run
                                          or args.merge_into):
        parser.error("--resume-daily replaces --only, --cadence, --dry-run and --merge-into")
    if args.budget_seconds is None:
        args.budget_seconds = DEFAULT_BUDGET_SECONDS
    resume = args.resume_daily is not None
    cadence = "daily" if resume else args.cadence or DEFAULT_CADENCE
    not_run: dict[str, list[str]] = {}
    all_entries = entries
    if args.only:
        entries = [e for e in entries if e["id"] in args.only]
    elif not resume and not args.dry_run and cadence != "all":
        # A spec that failed to load runs (as an error) in the default cadence so
        # a broken daily spec is still reported every hour.
        for e in entries:
            own = spec_cadence(e["spec"])
            if own != cadence:
                not_run.setdefault(own, []).append(e["id"])
        entries = [e for e in entries if spec_cadence(e["spec"]) == cadence]
    if args.dry_run:
        rows = [{"id": e["id"], "valid": e["error"] is None,
                 **({"error": e["error"]} if e["error"] else {}),
                 **({"warnings": e["warnings"]} if e["warnings"] else {}),
                 **({"module": e["spec"]["command"][2],
                     "requires_env": e["spec"].get("requires_env", []),
                     "requires_env_any": e["spec"].get("requires_env_any", []),
                     "optional_env": e["spec"].get("optional_env", []),
                     "requires": e["spec"].get("requires", []),
                     "disabled": bool(e["spec"].get("disabled")),
                     "cadence": spec_cadence(e["spec"]), "tier": spec_tier(e["spec"]),
                     "profile": bool(e["spec"].get("profile"))} if e["spec"] else {})}
                for e in entries]
        invalid = sum(1 for r in rows if not r["valid"])
        print(json.dumps({"schema": SCHEMA, "dry_run": True, "specs_dir": SPECS_DIR_NAME,
                          "summary": {"specs": len(rows), "valid": len(rows) - invalid,
                                      "invalid": invalid}, "specs": rows},
                         indent=1, sort_keys=True))
        return 1 if invalid or not rows else 0
    refused = unreviewed_head(root) if reviewed_main_only else None
    if refused:
        print(json.dumps({"error": "not_reviewed_main", "reason": refused}), file=sys.stderr)
        return 2
    sha = args.main_sha or _git(root, "rev-parse", "HEAD")
    session_id = args.session or os.environ.get(SESSION_ENV) or f"pid-{os.getpid()}"
    daily_file = args.resume_daily or args.merge_into
    claim = None
    if reviewed_main_only and (resume or results_dir_of(
            args.output, daily_file, args.daily_report) is not None):
        claim, refusal = unclaimed_daily(root, session_id)
        if refusal:
            code, error, reason = refusal
            print(json.dumps({"error": error, "reason": reason}), file=sys.stderr)
            return code
    held_daily, refused = hold_daily(daily_file.parent, {
        "session": session_id, "pid": os.getpid(), "sha": sha, "file": daily_file.name,
        "started_at": _utc(_now())}) if daily_file else (None, None)
    if refused:
        print(json.dumps({"error": "daily_running", "reason": refused}), file=sys.stderr)
        return DAILY_RUNNING_EXIT
    try:
        return _main_daily_held(args, entries, all_entries, not_run, cadence, resume, root=root,
                                environ=environ, allowed_modules=allowed_modules, sha=sha,
                                session_id=session_id, claim=claim,
                                command_started=command_started)
    finally:
        if held_daily is not None:
            os.close(held_daily)


def results_dir_of(output: Path | None, *daily: Path | None) -> Path | None:
    """The results/ dir a run holds ``results/running.json`` in: ``output``'s directory when
    named results, else the one above a daily file's ``daily/`` dir."""
    return next((path for path in (
        output.parent if output is not None else None,
        *(p.parent.parent for p in daily if p is not None and p.parent.name == "daily"))
        if path is not None and path.name == "results"), None)


def _main_daily_held(args: argparse.Namespace, entries: list[dict[str, Any]],
                     all_entries: list[dict[str, Any]], not_run: dict[str, list[str]],
                     cadence: str, resume: bool, *, root: Path,
                     environ: Mapping[str, str] | None, allowed_modules: Iterable[str],
                     sha: str | None, session_id: str, claim: Mapping[str, Any] | None,
                     command_started: float) -> int:
    """``main`` under the daily set's flock when it writes one: the set it
    continues is read only here."""
    set_base: dict[str, Any] | None = None
    set_pending: list[str] | None = None
    if resume:
        args.merge_into, held, new_set = resume_target(args.resume_daily, sha)
        daily_ids = [e["id"] for e in all_entries
                     if e["spec"] is not None and spec_cadence(e["spec"]) in SET_CADENCES]
        set_base, set_pending, known = daily_set(
            held, new_set, daily_ids, {up for e in all_entries if e["id"] in daily_ids
                                       for up in (e["spec"] or {}).get("requires", [])},
            weekly_of(all_entries))
        wanted = [e for e in entries if e["spec"] and e["id"] in set_pending]
        # A session producer is not re-run here: it runs for its consumer (_run).
        upstream = {up for e in wanted for up in blocking_requires(e["spec"], valid_specs(
            entries))}
        entries = wanted + [e for e in entries if e["id"] in upstream and e not in wanted
                            and known.get(e["id"], {}).get("result") != "pass"]
    else:
        # Results already known for upstream skips: this run's checks, then the daily report's.
        known = {c["id"]: c for c in (load_report(args.merge_into) or {}).get("checks", [])
                 } if args.merge_into else {}
    # Externalize only into a known results tree: an explicit --profiles-dir, or an
    # --output inside a directory named "results" (results/<sha>.json -> results/profiles/).
    # Any other --output (a temp path the caller copies) keeps the profiles inline, so a
    # copied report never points at files that were left behind.
    profiles_dir = args.profiles_dir or (
        args.output.parent / "profiles"
        if args.output is not None and args.output.parent.name == "results" else None)
    report_dir = args.output.parent if args.output is not None else None
    # A daily merge (--merge-into, --resume-daily) without --previous carries blocked_since
    # over from the last daily report: --daily-report, else latest.json beside the merge file.
    previous_path = args.previous or (
        args.output.parent / "latest.json"
        if args.output is not None and args.output.parent.name == "results"
        and args.output.name != "latest.json" else None) or (
        args.daily_report or (args.merge_into.parent / "latest.json"
                              if args.merge_into.name != "latest.json" else None)
        if args.merge_into is not None else None)
    previous = load_report(previous_path) if previous_path is not None else None
    # blocked_since counts from each check's last pass in every stored result, never only
    # from the previous report (a partial run between two blocks must not reset it).
    results_dir = results_dir_of(args.output, args.merge_into, args.daily_report)
    history_path = args.blocked_history or (
        results_dir / HISTORY_NAME if results_dir is not None else None)
    lock = RunLock(results_dir / LOCK_NAME, session_id, sha,
                   "daily" if args.merge_into or cadence == "daily" else "scheduled", claim
                   ) if results_dir is not None else None
    refused = lock.acquire() if lock is not None else None
    if refused:
        print(json.dumps({"error": "run_locked", "reason": refused}), file=sys.stderr)
        return LOCKED_EXIT
    term = (signal.signal(signal.SIGTERM, _terminated)
            if threading.current_thread() is threading.main_thread() else None)
    try:
        return _run(args, entries, all_entries, known, not_run, cadence, resume, set_base,
                    set_pending, root=root, environ=environ, allowed_modules=allowed_modules,
                    sha=sha, previous=previous, history_path=history_path, results_dir=results_dir,
                    profiles_dir=profiles_dir, report_dir=report_dir,
                    command_started=command_started)
    finally:
        if term is not None:
            signal.signal(signal.SIGTERM, term)
        if lock is not None:
            lock.release()


def _terminated(signum: int, frame: Any) -> None:
    raise SystemExit(128 + signum)  # SIGTERM unwinds: the report and the marker are settled


def _stop_of(exc: BaseException) -> tuple[str, int]:
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted", 130
    if isinstance(exc, SystemExit):
        return "terminated", exc.code if isinstance(exc.code, int) else 1
    return f"error {type(exc).__name__}", 1


def _run(args: argparse.Namespace, entries: list[dict[str, Any]],
         all_entries: list[dict[str, Any]], known: dict[str, Any],
         not_run: dict[str, list[str]], cadence: str, resume: bool,
         set_base: dict[str, Any] | None, set_pending: list[str] | None, *,
         root: Path, environ: Mapping[str, str] | None, allowed_modules: Iterable[str],
         sha: str | None, previous: dict[str, Any] | None, history_path: Path | None,
         results_dir: Path | None, profiles_dir: Path | None, report_dir: Path | None,
         command_started: float) -> int:
    """Run the selected specs under the lock; the report is written before the first spec,
    after each one and once more with the run's exit code and stop reason."""
    history = load_history(history_path, results_dir)
    base = set_base if resume else load_report(args.merge_into) if args.merge_into else None
    # The set's checks before this run: one this run re-runs keeps them (merge_reports).
    prior = dict(known) if resume else {
        c["id"]: c for c in (base or {}).get("checks", []) if isinstance(c, Mapping)}
    for report_before in (previous, base):
        fold_report(history, report_before)
    started = _now().replace(microsecond=0)
    daily_ids = sorted(e["id"] for e in all_entries
                       if e["spec"] is not None and spec_cadence(e["spec"]) in SET_CADENCES)
    carried = (base or {}).get("deferred_budget") if resume else None
    carried = carried if isinstance(carried, Mapping) else {}
    deferred: dict[str, dict[str, Any]] = {}
    run_id = uuid.uuid4().hex[:12]  # this run's entry in the set's ``runs``

    def emit(checks: list[dict[str, Any]], pending: Sequence[str], stop: str = "running",
             code: int | None = None) -> dict[str, Any]:
        # Checks are scrubbed where they are made; the envelope holds only shas and stamps.
        report = build_report(entries, checks, root=root, main_sha=sha,
                              previous_main_sha=args.previous_main_sha, previous=previous)
        report["cadence"] = "only" if args.only else cadence
        report["started_at"] = _utc(started)
        report["complete"] = not pending
        if pending:
            report["pending"] = list(pending)
        if deferred:
            report["deferred_budget"] = dict(deferred)
        if daily_gap is not None:
            report["previous_daily_gap"] = dict(daily_gap)
        if swept["session_dirs_swept"]:
            report["session_dirs_swept"] = swept["session_dirs_swept"]
        if session.path is not None and session.path.is_dir():
            host_disk["session_dir_bytes"] = tree_bytes(session.path)
            host_disk["session_dir_peak_bytes"] = max(
                host_disk.get("session_dir_peak_bytes", 0), host_disk["session_dir_bytes"])
        report["host_disk"] = {k: v for k, v in host_disk.items() if v is not None}
        report["warm_store"] = dict(warm_detail)
        if not_run:
            report["not_run"] = {name: sorted(ids) for name, ids in sorted(not_run.items())}
        if args.daily_report is not None:
            report.update(daily_status(args.daily_report, datetime.strptime(
                report["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc),
                required={up for e in all_entries if e["spec"]
                          for up in e["spec"].get("requires", [])},
                weekly=weekly_of(all_entries)))

        def settle(doc: dict[str, Any]) -> dict[str, Any]:
            doc["stop_reason"] = stop
            doc["exit_code"] = (None if stop == "running" else code if code is not None
                                else 1 if doc.get("failing") else 0)
            return doc
        if args.output:
            write_atomic(args.output, json.dumps(settle(report), indent=1) + "\n")
        if history_path is not None:
            write_atomic(history_path, json.dumps({"schema": HISTORY_SCHEMA, "checks": dict(
                sorted(history.items()))}, indent=1) + "\n")
        if not args.merge_into:
            return settle(report)
        merged = settle(merge_reports(
            base if resume else load_report(args.merge_into), report, daily_ids, prior,
            set_pending, run={"id": run_id, "started_at": report["started_at"],
                              "main_sha": sha, "stop_reason": stop,
                              "ran": [c["id"] for c in checks],
                              "deferred": {i: d["reason"] for i, d in deferred.items()}}))
        if resume:  # the set's checks that never passed, before every other check list
            passed = {c["id"] for c in merged.get("checks") or [] if c.get("result") == "pass"}
            merged = {"never_passed": sorted(
                i for i in daily_ids
                if i not in passed and not (history.get(i) or {}).get("last_pass")),
                **{k: v for k, v in merged.items() if k != "never_passed"}}
        text = json.dumps(merged, indent=1) + "\n"
        write_atomic(args.merge_into, text)
        if resume and args.merge_into.name != "latest.json":
            write_latest(args.merge_into.with_name("latest.json"), text)
        return merged

    secrets = secret_values(os.environ if environ is None else environ)
    specs, by_id = valid_specs(all_entries), {e["id"]: e for e in all_entries}
    daily_report, daily_gap = previous_daily(args.daily_report, args.merge_into)
    swept = sweep_leftovers()  # what killed runs left behind, before this one's
    host_disk: dict[str, Any] = {k: v for k, v in swept.items() if k != "session_dirs_swept"}
    if (os.environ if environ is None else environ).get("PLX_WORKBENCH_STATE_DIR"):
        from operating.warm_store import WarmStore   # a repository import, only when set
        warm, warm_detail = WarmStore.open(os.environ if environ is None else environ)
    else:   # operating.warm_store.state_dir's refusal, naming the setting
        warm, warm_detail = None, {"status": "state_dir_unset",
                                   "setting": "PLX_WORKBENCH_STATE_DIR"}
    session = SessionState(root, warm=warm, environ=environ)
    checks: list[dict[str, Any]] = []
    waiting: list[str] = []
    in_flight: list[str] = []
    report: dict[str, Any] = {}

    host_secret: list[str] = []  # this run's agent-eval session secret, made once, in memory

    def agent_host(spec_id: str, spec_body: Mapping[str, Any]
                   ) -> tuple[Mapping[str, str] | None, dict[str, str]]:
        """The environment an agent eval runs in (module docstring, "Agent-eval host"): each
        host setting it declares in ``optional_env`` and the run leaves unset, provisioned
        for this check only: a fresh random session secret (added to the run's scrubbed
        values, never recorded), a state directory in the run's session directory, the
        checkout's example host config (named ``not_provisioned`` when it is missing, never
        invented); the names it set go to the check in ``AGENT_PROVISIONED_ENV``. Any other
        check, or none unset: the run's environment as it is, less a stale
        ``AGENT_PROVISIONED_ENV`` (only this run says what it provisioned)."""
        base_env = os.environ if environ is None else environ
        if AGENT_PROVISIONED_ENV in base_env:
            base_env = {k: v for k, v in base_env.items() if k != AGENT_PROVISIONED_ENV}
        declared = set(spec_body.get("optional_env") or [])
        unset = [name for name in (AGENT_SECRET_ENV, AGENT_STATE_ENV, AGENT_CONFIG_ENV)
                 if name in declared and not base_env.get(name)]
        if spec_id not in AGENT_HOST_CHECKS or not unset:
            return (environ if base_env is (os.environ if environ is None else environ)
                    else base_env), {}
        env, made = dict(base_env), {}
        if AGENT_SECRET_ENV in unset:
            if not host_secret:
                host_secret.append(os.urandom(32).hex())
                secrets[:] = sorted({*secrets, host_secret[0]}, key=len, reverse=True)
            env[AGENT_SECRET_ENV], made[AGENT_SECRET_ENV] = host_secret[0], "generated"
        if AGENT_STATE_ENV in unset:
            state = session.directory() / AGENT_STATE_DIR
            state.mkdir(mode=0o700, exist_ok=True)
            env[AGENT_STATE_ENV], made[AGENT_STATE_ENV] = str(state), "session_dir"
        if AGENT_CONFIG_ENV in unset:
            config = root / AGENT_HOST_CONFIG
            if config.is_file():
                env[AGENT_CONFIG_ENV], made[AGENT_CONFIG_ENV] = str(config), str(
                    AGENT_HOST_CONFIG)
            else:
                made[AGENT_CONFIG_ENV] = f"not_provisioned:{AGENT_HOST_CONFIG} missing"
        env[AGENT_PROVISIONED_ENV] = ",".join(sorted(
            name for name, kind in made.items() if not kind.startswith("not_provisioned")))
        return env, made

    def execute(entry: Mapping[str, Any], bounded: bool = False,
                settled: Mapping[str, Any] | None = None) -> None:
        """Run (or record as refused or blocked) one spec, then write the report.
        ``bounded``: its timeout was lowered to the budget left (``run_within``).
        ``settled``: not run, recorded as a skip, ``unmeasured`` ``budget``, with this detail
        (a consumer whose session producers and shared run fit no command, or one that
        cannot run cold whose need no longer fits after its producers): never a fail, and,
        a plain skip, never a fresh result in the history the stale-age order reads."""
        nonlocal report
        spec_body = entry["spec"]
        down = failed_upstream(spec_body, known, specs) if spec_body else None
        gap = (unavailable_relations_needed(spec_body, known)
               if spec_body and down is None else None)
        if entry["id"] in waiting:
            waiting.remove(entry["id"])
        deferred.pop(entry["id"], None)
        session.ran.add(entry["id"])
        in_flight[:] = [entry["id"]]
        ran = False
        if settled is not None:
            check = scrub_tree({"id": entry["id"], "source": spec_body.get("source", "n/a"),
                                "spec": f"{SPECS_DIR_NAME}/{entry['id']}.json",
                                "tier": spec_tier(spec_body), "result": "skipped",
                                "detail": {"unmeasured": "budget", **settled}}, secrets)
        elif entry["error"] is not None:
            check = scrub_tree({"id": entry["id"],
                                "spec": f"{SPECS_DIR_NAME}/{entry['id']}.json",
                                "result": "error", "detail": {"refused": entry["error"]}},
                               secrets)
        elif down is not None or gap is not None:
            detail = ({"reason": upstream_reason(down), "upstream": down.get("id"),
                       "cause": upstream_cause(down)} if down is not None else
                      {"reason": f"upstream {gap[0].get('source') or 'n/a'} "
                                 "relation_unavailable",
                       "upstream": gap[0].get("id"), "cause": "relation_unavailable",
                       "relations": gap[1]})
            check = scrub_tree({"id": entry["id"],
                                "source": spec_body.get("source", "n/a"),
                                "spec": f"{SPECS_DIR_NAME}/{entry['id']}.json",
                                "tier": spec_tier(spec_body), "result": "blocked",
                                **({"answer_source": True}
                                   if spec_body.get("answer_source") else {}),
                                "detail": detail}, secrets)
        else:
            plan = session.plan(spec_body, specs)
            previous_run, carried = (measured_previous(entry["id"], daily_report,
                                                       args.merge_into)
                                     if spec_body.get("carry_previous_measured")
                                     else (daily_report, None))
            provisioned: dict[str, str] = {}
            try:
                bases = session.bases(spec_body, plan)
                session.begin(spec_body, plan)
                host_env, provisioned = agent_host(entry["id"], spec_body)
                check = run_spec(spec_body, root=root, environ=host_env,
                                 allowed_modules=allowed_modules, daily_report=previous_run,
                                 session=bases, session_since=session.since(spec_body, plan),
                                 artifacts_dir=(results_dir / ARTIFACTS_DIR_NAME
                                                if results_dir is not None else None),
                                 main_sha=sha)
            except SpecError as exc:  # the session directory would sit in the repository
                check = {"id": entry["id"], "result": "error",
                         "spec": f"{SPECS_DIR_NAME}/{entry['id']}.json",
                         "detail": {"refused": str(exc)[:MAX_STRING_CHARS]}}
            except Exception as exc:  # one broken spec never ends the run
                check = scrub_tree({"id": entry["id"], "result": "error",
                                    "spec": f"{SPECS_DIR_NAME}/{entry['id']}.json",
                                    "detail": {"runner_error": type(exc).__name__}},
                                   secrets)
            if bounded and (check.get("detail") or {}).get("timed_out") is True:
                # Killed at the budget left, never at its own timeout: no verdict, so it
                # stays pending for a later command, which gives it its full timeout.
                waiting.append(entry["id"])
                defer(entry, f"bounded run killed at {spec_body.get('timeout_seconds')} s; "
                             "needs its full timeout", full_timeout=True)
                in_flight.clear()
                report = emit(checks, waiting)
                return
            if carried is not None:  # the measured run its previous report carries
                check["detail"] = _cap({**(check.get("detail") or {}),
                                        "previous_report_carried": carried})
            if provisioned:  # which host settings this run made (names and kinds only)
                check["detail"] = {**(check.get("detail") or {}),
                                   "host_provisioned": dict(provisioned)}
            ran = bool(plan) and (check.get("exit_code") is not None
                                  or bool((check.get("detail") or {}).get("timed_out")))
            if ran:
                check.setdefault("detail", {})["session_state"] = session.detail(
                    spec_body, plan, specs)
        # The previous report's same check, or this daily file's when it is newer.
        check = stamp_blocked(check, {"generated_at": None, "checks": [known[entry["id"]]]}
                              if entry["id"] in known and args.merge_into else previous,
                              started, history)
        history[entry["id"]] = folded(history.get(entry["id"]), check,
                                       started.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if profiles_dir is not None:
            check = externalize_profile(check, profiles_dir, report_dir)
        # Its origin, kept by every later merge (merge_reports never restamps it), and the
        # spec it ran under as loaded (never a budget-bounded copy): which version a result measured.
        check = {**check, "main_sha": sha, "checked_at": _utc(_now()),
                 **({"spec_digest": spec_digest(specs[entry["id"]])}
                    if entry["id"] in specs else {})}
        if ran:  # the stores it wrote cite this run
            session.record(spec_body, plan, check)
        checks.append(check)
        in_flight.clear()
        known[entry["id"]] = check
        # After every spec, so a run killed by the runtime keeps every finished spec.
        report = emit(checks, waiting)

    def producers_to_run(spec_body: Mapping[str, Any]) -> list[str]:
        """The daily rotation's session producers of a consumer that did not run in this
        session, each after its own such producers (deepest first)."""
        out: list[str] = []

        def visit(body: Mapping[str, Any], seen: frozenset[str]) -> None:
            for ups in session.needs_producers(body, specs).values():
                for up in ups:
                    if (up not in session.ran and up not in out and up not in seen
                            and up in by_id and not specs[up].get("disabled")):
                        visit(specs[up], seen | {up})
                        if up not in out:
                            out.append(up)
        visit(spec_body, frozenset())
        return out

    # Each spec's last check, a pending one's included (``known`` holds only done ones).
    measured = {c.get("id"): c for c in (base or {}).get("checks", [])
                if isinstance(c, Mapping)}

    earlier = {c.get("id"): c for c in (previous or {}).get("checks", [])
               if isinstance(c, Mapping)}
    # A non-build phase budget flag (``--budget-seconds`` alone is the build's) and the
    # whole run's, which stands alone.
    phase_flag = re.compile(r"--[a-z][a-z0-9]*(?:-[a-z0-9]+)*-budget-seconds")
    total_flag = "--total-budget-seconds"

    def shared_store(check: Any) -> bool | None:
        detail = check.get("detail") if isinstance(check, Mapping) else None
        state = detail.get("session_state") if isinstance(detail, Mapping) else None
        return state.get("shared_store") if isinstance(state, Mapping) else None

    def phase_budget(spec_id: str) -> float | None:
        """The sum of the spec's non-build budgets (``--<phase>-budget-seconds``; a bare
        ``--budget-seconds`` is its build's), or its ``--total-budget-seconds`` alone (its
        whole run's); None when it names none or one is unreadable."""
        argv, total = [str(a) for a in specs[spec_id].get("command", [])], None
        whole = total_flag in {a.partition("=")[0] for a in argv}
        for i, arg in enumerate(argv):
            flag, _, value = arg.partition("=")
            if not phase_flag.fullmatch(flag) or whole and flag != total_flag:
                continue
            value = value if value else argv[i + 1] if i + 1 < len(argv) else ""
            try:
                seconds = float(value)
            except ValueError:
                return None  # unreadable: no estimate from it
            if not math.isfinite(seconds) or seconds <= 0:
                return None
            total = (total or 0) + seconds
        return total

    def newest_measured(spec_id: str) -> Mapping[str, Any] | None:
        """The spec's newest run with a positive measured ``duration_s``: this set's check, then
        the runs it replaced (``earlier_runs``), then the previous report's. A settled
        (``session_over_budget``) or other unrun result has none and is passed over, so it
        never puts a measured spec back on its timeout (the estimate ratchet)."""
        runs: list[Mapping[str, Any]] = []
        for check in (known.get(spec_id) or measured.get(spec_id), earlier.get(spec_id)):
            if isinstance(check, Mapping):
                runs += [check, *(r for r in check.get("earlier_runs") or []
                                  if isinstance(r, Mapping))]
        return next((r for r in runs if _is_number(r.get("duration_s"))
                     and r["duration_s"] > 0), None)

    def timed_out(run: Mapping[str, Any]) -> bool:
        # an ``earlier_runs`` entry keeps no detail: a run with a duration and no exit code
        # was killed at its timeout (run_spec)
        detail = run.get("detail")
        return (detail.get("timed_out") is True if isinstance(detail, Mapping)
                else "exit_code" in run and run["exit_code"] is None)

    def shared_run(spec_id: str) -> Mapping[str, Any] | None:
        """The spec's newest check that shared its producers' stores, else None."""
        return next((c for c in (known.get(spec_id), measured.get(spec_id),
                                 earlier.get(spec_id)) if shared_store(c) is True), None)

    def estimate(spec_id: str, shared: bool = False) -> float:
        # A timed-out run's duration is only a lower bound: its timeout stands instead.
        last = known.get(spec_id) or measured.get(spec_id) or {}
        if not shared or not last or shared_store(last) is True:  # its own measured runs
            run = newest_measured(spec_id)
            return (run["duration_s"] if run is not None and not timed_out(run)
                    else specs[spec_id].get("timeout_seconds", DEFAULT_TIMEOUT))
        # Shared, its last run cold: a cold run's duration counts the sync and build its
        # producers do instead, and a settled (session_over_budget) or other unrun result has
        # none: the last shared run's stands, else its non-build budget, else its timeout.
        # The non-build budget leaves out opening the session projection: no report
        # records that time apart (a shared run's duration includes it), and a run
        # slower than its estimate is killed at the budget left (run_within).
        last = shared_run(spec_id)
        if last is None:
            phase = phase_budget(spec_id)
            return phase if phase is not None else specs[spec_id].get(
                "timeout_seconds", DEFAULT_TIMEOUT)
        took = last.get("duration_s")
        return took if (_is_number(took) and took > 0
                        and (last.get("detail") or {}).get("timed_out") is not True) \
            else specs[spec_id].get("timeout_seconds", DEFAULT_TIMEOUT)

    def fit(spec_id: str, timeout: float, shared: bool = False) -> float:
        """The budget a spec needs to start: 1.5 x its last measured duration, never more
        than its timeout (a spec never measured, last timed out, or killed at a bounded
        timeout: its timeout). ``shared``: it will share its producers' stores."""
        held = carried.get(spec_id)
        if isinstance(held, Mapping) and held.get("full_timeout") is True:
            return timeout
        if shared and shared_run(spec_id) is None and (bound := phase_budget(spec_id)):
            # No shared run to measure: the command ends inside its own budgets, so they
            # are the need (1.5 x them kept a check with a 1,800 s budget off a 2,222 s slot).
            return min(timeout, math.ceil(bound))
        return min(timeout, math.ceil(1.5 * estimate(spec_id, shared)))

    def will_share(spec_body: Mapping[str, Any], producers: Sequence[str]) -> bool:
        """Every path a session producer names will be shared: its store is filled in this
        session, or one of its producers runs first."""
        plan = session.plan(spec_body, specs)
        named = session_producers(spec_body, specs)
        consumed = {path: named[path] for path in session.consumes(spec_body, specs, plan)}
        return bool(consumed) and all(plan.get(path) in ("shared", "warm")
                                      or set(ups) & set(producers)
                                      for path, ups in consumed.items())

    def last_verdicts() -> dict[str, Mapping[str, Any]]:
        """Each spec's newest stored check (this set's, a pending one's, the previous report's)."""
        newest: dict[str, Mapping[str, Any]] = {}
        for source in (earlier, measured, known):
            for check_id, check in source.items():
                if isinstance(check_id, str) and isinstance(check, Mapping) and (
                        check_id not in newest
                        or str(check.get("checked_at") or "") >= str(
                            newest[check_id].get("checked_at") or "")):
                    newest[check_id] = check
        return newest

    entries = order_entries(
        entries, history if resume else None, carried, {
            e["id"]: fit(e["id"], specs[e["id"]].get("timeout_seconds", DEFAULT_TIMEOUT))
            for e in entries if e["id"] in specs} if resume else None,
        fixed_since_verdict(entries, last_verdicts(), root, sha, carried) if resume else (),
        started)
    waiting[:] = [e["id"] for e in entries]

    def basis(needs: float, timeout: float) -> str:
        return (f"timeout {timeout} s" if needs == timeout
                else f"1.5 x last duration {needs} s")

    def run_within(entry: Mapping[str, Any], timeout: float, left: float) -> None:
        # Fits on its measured duration but not its timeout: killed at the budget left,
        # so a slower run than measured still never overruns the command.
        if timeout <= left:
            execute(entry)
        else:
            execute({**entry, "spec": {**entry["spec"], "timeout_seconds": int(left)}},
                    bounded=True)

    def defer(entry: Mapping[str, Any], reason: str, after_producers: bool = False,
              full_timeout: bool = False) -> None:
        # Does not fit the budget left (or was killed at it): stays pending.
        held = carried.get(entry["id"])
        held = held if isinstance(held, Mapping) else {}
        count = held.get("count")
        deferred[entry["id"]] = {
            "count": (count if isinstance(count, int) and count > 0 else 0) + 1,
            "reason": reason,
            **({"after_producers": True}
               if after_producers or held.get("after_producers") is True else {}),
            **({"full_timeout": True}
               if full_timeout or held.get("full_timeout") is True else {})}

    try:
        report = emit(checks, waiting)  # the file exists before the first spec runs
        begun = time.monotonic()
        deadline = begun + args.budget_seconds
        # The runtime kills the command 7200 s after it started (startup included); a spec
        # killed at its timeout still takes KILL_GRACE_SECONDS to settle.
        ceiling = command_started + COMMAND_LIMIT_SECONDS - KILL_GRACE_SECONDS
        head_taken = False  # the set's first starved deferral to reach this run has run
        for entry in entries:
            spec_body = entry["spec"]
            down = failed_upstream(spec_body, known, specs) if spec_body else None
            gap = (unavailable_relations_needed(spec_body, known)
                   if spec_body and down is None else None)
            runs = (entry["error"] is None and down is None and gap is None
                    and not spec_body.get("disabled")
                    and not unmet_env(spec_body, os.environ if environ is None else environ)
                    and not absent_credentials(spec_body,
                                               os.environ if environ is None else environ))
            timeout = (spec_body or {}).get("timeout_seconds", DEFAULT_TIMEOUT)
            left = deadline - time.monotonic()
            held = carried.get(entry["id"])
            # The first spec of a command, or the first starved deferral carried here that
            # runs and whose timeout fits what the command limit leaves after the specs
            # before it (an upstream of its own included): it runs whatever budget is left,
            # so a long spec is never deferred forever, nor started where the runtime would
            # kill it.
            first = (not checks or (runs and not head_taken and starved(held)
                                    and timeout <= ceiling - time.monotonic()))
            head_taken = head_taken or (first and runs and starved(held))
            reader = runs and resume and reads_producers(spec_body, specs)
            cold = (not reader and isinstance(held, Mapping)
                    and held.get("after_producers") is True)
            producers = producers_to_run(spec_body) if resume and runs and not cold else []
            shares = runs and resume and not cold and will_share(spec_body, producers)
            needs = fit(entry["id"], timeout, shares) if runs else timeout
            if producers:
                need = sum(estimate(up) for up in producers) + needs
                if need <= left:
                    cut = []  # a reader's producers each run within the budget left
                    for up in producers:
                        room = deadline - time.monotonic()
                        if not reader:
                            execute(by_id[up])
                        elif room >= 1:
                            run_within(by_id[up], specs[up].get("timeout_seconds",
                                                                DEFAULT_TIMEOUT), room)
                        if reader and (room < 1 or up in deferred):
                            cut.append(up)
                            break  # a later producer would read what this one did not fill
                    left = deadline - time.monotonic()
                    if reader and (cut or needs > left):  # never cold: settled instead
                        reason = (f"{basis(needs, timeout)} not left after session "
                                  f"producers {', '.join(cut or producers)}")
                        execute(entry, settled={
                            "cause": "session_over_budget", "failures": [reason],
                            "need_seconds": int(need),
                            "budget_seconds": int(args.budget_seconds),
                            "producers": list(producers)})
                        continue
                    if needs > left:  # slower than they last ran: never overrun
                        defer(entry, f"{basis(needs, timeout)} over the "
                                     f"{max(0, int(left))} s budget left after its "
                                     "session producers", after_producers=True)
                        continue
                    run_within(entry, timeout, left)
                    continue
                # Never cold for want of budget: that records cold_not_run and keeps its
                # estimate cold, so the next run is cold again. One that fits no command
                # is settled unmeasured for budget (deferred, it would hold the set open).
                # Not settled while a producer of this set has no result: it is pending and
                # runs on its own, so the wait ends (one outside the set never would).
                if (need <= args.budget_seconds
                        or any(up in waiting and not (known.get(up) or measured.get(up))
                               for up in producers)):
                    defer(entry, f"session producers and spec {int(need)} s over the "
                                 f"{max(0, int(left))} s budget left")
                else:
                    reason = (f"session producers and spec {int(need)} s over the "
                              f"{int(args.budget_seconds)} s command budget")
                    execute(entry, settled={
                        "cause": "session_over_budget", "failures": [reason],
                        "need_seconds": int(need),
                        "budget_seconds": int(args.budget_seconds),
                        "producers": list(producers)})
                continue
            if resume and not first and runs:
                if needs > left:
                    defer(entry, f"{basis(needs, timeout)} over the "
                                 f"{max(0, int(left))} s budget left")
                    continue
                run_within(entry, timeout, left)
                continue
            execute(entry)
        report = emit(checks, waiting, "budget" if deferred else "finished")
    except BaseException as exc:
        stop, code = _stop_of(exc)
        with contextlib.suppress(Exception):  # the run's own exception is the one raised
            emit(checks, [*in_flight, *waiting], stop, code)
        raise
    finally:
        session.close()  # the session ends with the run: its captures never outlive it
    # After the try: the report is final, so publishing it never turns it into an error stop.
    if report_dir is not None and report_dir == results_dir and args.output.name != "latest.json":
        advance_latest(results_dir, root)
    print(json.dumps(report, indent=1, sort_keys=False))
    return report["exit_code"]


if __name__ == "__main__":
    # The runner runs only reviewed main; --dry-run runs nothing, so any checkout.
    raise SystemExit(main(reviewed_main_only=True, started=process_started()))
