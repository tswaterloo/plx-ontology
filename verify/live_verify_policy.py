"""Constants of the live verifier: allowed modules, spec shapes, tiers, causes, cadences,
limits and the secret-scrubbing patterns (``verify.live_verify``, which re-exports them)."""

from __future__ import annotations

from pathlib import Path
import re


SCHEMA = "plx-live-verify/v1"
# results/latest.json advances only to a report whose main_sha is this ref or its ancestor.
MAIN_REF = "origin/main"
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SPECS_DIR_NAME = "devloop/live-checks"
MODE = ("read-only (specs from devloop/live-checks: GET-only adapters, OAuth token POSTs, "
        "MC MCP initialize+tools/list and read-tool tools/call only)")

# Read-only probes and case scripts reviewed for live use. Adding a module here is a
# reviewed change: it must only read (GET-only client / guarded MC and brain reads).
ALLOWED_MODULES = frozenset({
    "integrations.graph_read",
    "integrations.graph_directory",  # Entra user + transitive groups, GET only
    "integrations.capture_org",  # org chart (users, managers), GET only; token POST only
    "integrations.shared_link",  # /shares/{u!..}/driveItem + item /content, GET only
    "integrations.bc_read",
    "integrations.bc_purchase",
    # FBA external sources: Freightcom test host
    # GET only; SP-API GETs plus the one LWA token POST; counts and enums only.
    "integrations.freightcom_read",
    "integrations.zendesk_read",
    "integrations.shopify_read",
    "integrations.granola_read",
    "integrations.edi_read",
    # Graph change notifications (AUD-05): Event Hubs partition ids (receive-only metadata)
    # and the receiver's own status file; counts, ages and hours only.
    "integrations.graph_notifications",
    "integrations.amazon_sp_read",
    "integrations.mc_brain_probe",  # MC read tools only via tools/call
    # MC operator-scope capture into {tmp}: read tools only (CAPTURE_TOOLS), counts only.
    "integrations.mc_sources",
    "integrations.knowledge_source",  # brain agent/items GET only, namespace-trimmed (GOV-13)
    # The card-action send gate read without sending: one token request, booleans/counts.
    "integrations.graph_send",
    # The autopilot write path's switches, gate passes and adapter states, read without
    # performing anything (no business system is read); counts, booleans and states only.
    "operating.autopilot_write",
    # The write readiness record read back (``show``): no token, no system call; per-system
    # status enums and missing-permission names only.
    "operating.autopilot_write_readiness",
    # Deferred mail attachments through the GET-only Graph mail reader; its JSON $batch
    # bodies carry GETs only (live_source_guard.check_batch_body); counts only.
    "integrations.attach_fetch",
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
    # Capture cycles into {tmp} through the host's GET-only capture modules, or in ingest
    # mode every source's own GET-only capture CLI (ingest.perpetual_cycle), the projection
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
    # Reads public.app_department and public.app_user_department_membership through the
    # GET-only WorkbenchReadApiClient; the PROC department's member count only.
    "scripts.purchasing_grant_probe",
    # Sage AP duty/freight actuals and HS proposals via the GET-only WorkbenchReadApiClient;
    # writes its artifacts to OUT_DIR / the host state, prints counts only.
    "scripts.duty_actual_report",
    # Data-shape profiler (profile.* specs): reads through WorkbenchReadApiClient,
    # bc_read and graph_read only; prints a sanitized shape profile, never values.
    "devloop.data_profile",
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
    # Customer orders a supplier lot touches over the served host (process.lot_impact);
    # counts and gap types only.
    "scripts.lot_impact_probe",
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
    # Objective-investigation eval (eval.objective_agent, the acceptance bar): headless Claude Code
    # sessions as plx.agent.investigate on the fixture case host behind the objective gate
    # (read-only tools, the case-store writers and plx.case_propose recorded only; plx.act
    # and every MC tool refused, nothing sent); stdout is counts, rates and enums.
    "scripts.eval_objective_agent",
    # Home's objective box (workspace.objective_box): three free-form objectives through
    # operator_api.workspace.objective_box.answer on the reader's console workspace of the persistent
    # host, a headless Claude Code planner, read-only; stdout is counts and enums.
    "scripts.eval_objective_box",
    # The console question box's one path (workspace.console_question_one_path): one
    # two-part question about an order of the reader's home gaps through
    # operator_api.workspace.objective_box.answer on the reader's console workspace of the persistent
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
PROFILE_MODULE = "devloop.data_profile"
PROFILE_ID_PREFIX = "profile."
MODULE_SHAPE = re.compile(r"(integrations|operating|scripts|devloop)(\.[a-z_][a-z0-9_]*)+")
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
             "blocked_causes", "max_detail_chars"}
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
# The blocked-age store (results/blocked-history.json on the live-results branch).
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
# ``max_detail_chars``: a spec's own cap, above the default and at most this. For a check whose
# asserted name lists cannot fit the default (schema.portal_queryable names every bound Portal
# relation: 248 of them, 4,769 characters of queryable names alone on 2026-11-10), so the
# cap never drops the names the source registry credits.
MAX_DETAIL_CHARS_CEILING = 16000
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
# A backticked check id in a section 1 "Done when" cell: dotted (``eval.dev_baseline``), or a
# bare lowercase name (``denied_leaks``); an upper-case env var (``PLX_EVAL_READER``) is no id.
ROADMAP_CHECK_ID = re.compile(r"`([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*)`")
# Added to the roadmap's ids: A1 "Capture complete" is the whole capture family (a prefix over
# the spec ids), which includes capture.breadth, whose weekly run the budget defers by itself.
MILESTONE_FAMILIES = ("capture.*",)


def roadmap_check_ids(text: str) -> tuple[str, ...]:
    """The check ids ROADMAP.md section 1 names in its tables' "Done when" cells, in order,
    each once. A bare name inside parentheses is a qualifier (``app_receipt``), not a check.
    No spec is left out for the run budget here: ``--milestones`` defers a spec that does not
    fit into ``milestone_deferred`` by name, so the budget never silently shrinks the list."""
    section = re.search(r"^## 1\..*?(?=^## )", text, re.M | re.S)
    ids: list[str] = []
    for line in (section.group(0) if section else "").splitlines():
        cells = line.split("|")
        if len(cells) < 4 or not line.lstrip().startswith("|"):
            continue
        cell = re.sub(r"\([^()]*\)", lambda m: re.sub(r"`[a-z0-9_]+`", "", m.group(0)), cells[2])
        ids += [i for i in ROADMAP_CHECK_ID.findall(cell) if i not in ids]
    if not ids:
        raise ValueError("ROADMAP.md section 1 names no check ids; --milestones cannot run")
    return tuple(ids)


MILESTONE_BUDGET_SECONDS = 2400  # --milestones default: a 1800 s capture spec fits, in the hour
MILESTONE_HOUR_SECONDS = 3600  # --milestones refuses a larger --budget-seconds
COMMAND_LIMIT_SECONDS = MAX_TIMEOUT_BY_CADENCE["daily"]  # the runtime kills a command there
KILL_GRACE_SECONDS = 10
LOCK_NAME = "running.json"
LOCK_LIVE_HOURS = 2
LOCKED_EXIT = 75  # EX_TEMPFAIL: another session's run holds results/running.json
NOT_ADMITTED_EXIT = 77  # EX_NOPERM: the session started before state.admission_epoch; final
# Another attempt is writing this daily set: wait for it (never final, unlike 77)
DAILY_RUNNING_EXIT = 76
# No results/running.json on LIVE_REF names a session: the routine skipped its marker push
UNCLAIMED_EXIT = 78  # EX_CONFIG
DAILY_LOCK_PREFIX = "plx-live-verify-daily-"
SESSION_ENV = "CLAUDE_CODE_REMOTE_SESSION_ID"
SESSION_START_ENV = "SESSION_START"  # an explicit session start; LIVE-VERIFIER.md unsets it
STATE_REF = "origin/claude/devloop-state"
LIVE_REF = "origin/claude/devloop-live"  # the routine's pushed results/running.json
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
# Host-scoped checks (module docstring, "Host-scoped checks"): they read the state directory
# a host cycle wrote, never the run's own setting. The producer's ``--state-dir {tmp}/...``
# is kept in the session directory (HOST_STATE_DIR) when its run finishes.
HOST_STATE_PRODUCER = "host.cycles"
HOST_STATE_CONSUMERS = ("access.control_proof",)
HOST_STATE_DIR = "host-cycle-state"
# A capture check red this many records in a row (the check and its newest earlier runs),
# for one cause on one spec digest, is skipped by --milestones runs (``known_red``).
KNOWN_RED_RUNS = 3
AFTER_LANDING_DAYS = 3
LANE_SPEC_ID = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")

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
