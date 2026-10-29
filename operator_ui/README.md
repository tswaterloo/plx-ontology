# Local operator console

`operator_ui.server.make_server()` exposes an HTTP console on an ephemeral loopback
port; `serve(port=8765, ...)` runs it until interrupted. It is launched by
`python3 -m operator_ui.case_console`. It serves the static page, GET `/api/session`,
the read-only case workspace and the object workspace (below). It does not import a
connector, call a production URL, or infer access from a typed actor name.

A random session token from GET `/api/session` is required in `X-Operator-Token` on
every POST. This token prevents cross-site browser submits; it is not a business
authorization credential. The server rejects non-loopback bindings and foreign Host
headers.

The `UnifiedPlatform` capability routes (engineering, episodes, objectives,
campaigns, work inbox, autonomy, business watches, transactions, streams, replica
reads) were retired with the platform in deletion-8, and the injected-service views
(`/api/overview`, `/api/findings`, `/api/recover`, `/api/entity/<id>`,
`/api/workflow/<id>`) and actions (POST `/api/approve/<plan_id>`,
`/api/execute/<plan_id>`) in deletion-18 (`devloop/BLOAT-MAP.md`): the console never
approves or executes; approval is a person's decision in Mission Control. Any other
path answers 404 `route_not_found`. Test: `python3 -m unittest tests.test_operator_ui`.

## A named employee's live session

A live console takes each request's employee from a signed session ticket (author
`human`, `operating.session_identity`). Until the Portal mints one,
`python3 -m operator_ui.console_ticket mint --state-dir DIR --host-config host.json --out FILE`
mints it on the host for the employee whose identity claims (`uid`, `email`,
`entraTenantId`, `entraObjectId`) `PLX_EMPLOYEE_READER` holds (`mint` and `preflight` read
it from `--reader-env`; `PLX_EVAL_READER` holds the eval reader's claims instead and must be
set where `session.employee_recorded` runs, else it reads blocked `eval_reader_unset`),
signed with
`PLX_WORKBENCH_SESSION_SECRET`, at most two hours, and only after the console's own check
(employee policy and Portal access snapshot grant that employee, the ticket's e-mail is
the Portal row's) passes; the console URL with the ticket goes to FILE (new, mode 0600)
and is never printed. `revoke --state-dir DIR --ticket-file FILE` ends it on every host
over DIR. Each mint and revoke appends one row to `DIR/console-ticket-audit.jsonl` (0600,
append-only: session reference, `employee:<object id>`, expiry, lifetime, OS user,
`minted_by: host`, `revoked_by` on a revoke row; never the ticket or an e-mail); a mint that cannot write it refuses
with `audit_unwritable` and writes no FILE. Every answered `/api/*` request of a live console is journaled in
`DIR/console-journal.sqlite` (`operator_ui.session_journal`: route, status, the opened
object's type and each panel's item count, never a value or identifier);
`python3 -m operator_ui.session_journal summary --state-dir DIR --session REF` prints one
session's counts and `minted_by` (from the audit file). The run book for a first recorded employee session (WS-16) is
`docs/runbooks/ws16-first-employee-session.md`. Tests:
`operating.tests.test_session_identity.ConsoleHttpTests`,
`tests.test_object_workspace_http.PersonControlsTest`.

**Known limitation.** Anyone holding `PLX_WORKBENCH_SESSION_SECRET` can mint a `human`
ticket for any employee the resolver (employee policy and Portal access snapshot) accepts:
the ticket proves the secret's holder named that employee, not that the employee signed
in. This exposure is accepted and stated in
`devloop/DECISIONS/2026-10-07-live-agent-metric-reader-ticket.md`. Host minting is
interim, until the Portal's sign-in mints the console ticket.

## The page

`index.html` loads `app.js` (the shell and the case workspace) and `workspace.js` (the
landing view, "Find & open"). The landing view has an objective box (a question in the
employee's words) and an identifier search; both call `/api/objects`, which finds the
objects the text names (`plx.find`, ranked, never merged) and lists, per object type, the
question families of `ontology/plans` it can answer. Picking one runs `/api/investigate`
(`plx.investigate`: chain, cited answer, gaps, conflicts, answerability); opening a
candidate shows the object workspace below. Two controls write, only to the Workbench's
own stores and never to a business system: **Confirm / Reject link** on each link
(`POST /api/object-review`: one `plx.review` decision in `DIR/reviews.sqlite`, append-only,
citing the record versions shown; only a person signed in as an employee records one, a
fixture or agent session is refused; the open object lists its links' reviews, and a link
decision changes the tier at the next projection build) and **Propose a follow-up** on an
object or an investigation (`POST /api/object-follow-up`: a new case with one
`workbench.follow_up.record` proposal citing the object's record; it is never executed,
registered or approved here). Both need the session's CSRF token and take the author from
the session, never from the body. Every `/api/` target the page's scripts name is requested
from a real server by `python3 -m unittest tests.test_operator_ui_routes` (an unserved one
fails it); `tests.test_case_console_smoke` starts the console from the command line and
uses the landing page over loopback HTTP.

## Case workspace (read-only)

`make_server(case_workspace=CaseWorkspace(runtime, actor, report_store=...))`
adds GET `/api/cases?status=&limit=`, `/api/case/<case_id>` and
`/api/case-citation?handle=<api:// or original://>&text_offset=`. Every read goes
through `CaseStore.get/list` and `CaseRuntime.resolve_citation/citation_status/read_original`
for the one host-configured `Actor`, so case, relation and original audiences apply
exactly as for the MCP case tools; a case or citation the actor cannot read is a
plain 404 `not_found`. A configured case workspace always requires the access token.
Lists are capped at 200 items with a `truncated` map; `?section=<name>&offset=&limit=` pages any list. The stored playbook evaluation is shown only when nothing on the case is withheld from or changed for the reader (otherwise `{"status": "withheld"}`), and never names its recorder. There are no case write routes. `python3 -m operator_ui.case_console` serves a
case-only console over an offline mirror (`python3 -m operator_ui.tests.case_fixtures
--fixture --seed-fixtures ...` writes the six AO packet cases and the PO5447 packaging case
from fixtures first, `operator_ui/tests/case_fixtures.py`: evaluation data, kept out of
product code, which never imports it). Test: `python3 -m unittest tests.test_case_workspace_http`.

One case is one view computed only from what the actor can resolve: an evidence banner (a
seeded directory always reads **FIXTURE**), the conflicts recorded on the case with both
sides cited (e.g. FM damage row against the paper page; `case_workspace.conflicts`), gaps
by state, the records a recorded item cites together (what that item declares, not a
verified key), the timeline (case events with the citations of the item they recorded,
dated source values, mail received dates, SharePoint item dates), originals with their
re-hashed SHA-256 and current SharePoint eTag status, page links, PDF and Portal metadata
proposals (`/api/case-document-metadata`, proposals only), proposals with their goal-2
action-path state (none is ever approved or dispatched here) and the ingestion coverage
(`case_context_tools.ingest_status`) when the host configured them. Mail for a cited PO has
one path, no case mail panel: the case's `case_mail` findings and the PO's stored mail links
in the object workspace (`plx.around`). The case view draws no
relationship map of its own: typed relationships and their identity tiers are the
ontology's (the object workspace below). A withheld proposal shows only its action-path
state, never its capability or intent id (read through `ActionPath.list_for_case`; a broken
intent chain or a bounded listing reads as unknown, never "not registered").

Live mirror: `python3 -m operating.case_workspace_live` (and `case_console --open
KIND=IDENTIFIER`) opens real cases (`ao=`, `petra_po=`, `sales_order=`, `customer_po=`,
`pick=`) offline over a state directory the ingestion runner filled and renders the
workspace per reader with a row-data leak check. The case audience is narrowed to the
principals that may read the relations the case's own words came from. The example sources
cover the AO and PO chains; `sales_order=`/`customer_po=` also need the sales-order,
customer and shipment relations ingested. Spec
`devloop/live-checks/case.workspace_live.json` (live-unverified until the verifier runs it).

## Object workspace (generic ontology tools)

Object routes follow the session identity (`operating.session_identity`): live,
`ticket_sessions.object_workspace(session)` gives each authenticated employee
(`employee:<oid>`) its own `ObjectWorkspace` (`operator_ui/object_workspace.py`), whose
grant the case runtime's employee gate resolves again at the start of every request;
fixture (`--fixture`), `make_server(..., case_workspace=, session=, object_workspace=)`
serves the fixture session's, behind the same access token. They add GET
`/api/objects?q=&type=&cursor=` (`plx.find`: ranked candidates, never a merged winner, and
the question families per candidate type), `/api/object?id=<object id>`,
`/api/object-source?handle=<L1 handle>&text_offset=` and
`/api/investigate?family=<plan id>&root=<object id>` (`plx.investigate`).
One object is one joined answer of the case host's generic tools for the request's
reader (10-DECISION #4): the `plx.object` card (every value with its L1
citation, authority rank, unit/currency, markings, conflicts, identity edges), the
`plx.around` relationship map (one hop per link type the card lists, weakest tier floor,
each edge with its tier, reason and why-not), the `plx.timeline` of the object and its
neighbour sets (what each call withheld is shown per call), `plx.process` (stage
states, current stage, three-way-match comparisons with `agree: null` and the reason when
undecided), the quantities and money by role (card and comparison values grouped by role
or currency, mixed units flagged, never summed), every gap and conflict labelled with
the call it came from, the originals (every capture any answer cites, one entry per
handle with its version, SHA-256 row digests, source system taken from its scheme and
what cites it; an ungranted scheme is listed as withheld with the reason; past 200
captures the answer carries `total`, `truncated` and per-scheme shown/total counts and
the panel says "showing N of M" with the withheld shown/total count; withheld captures
take at most half the slots (the rest are counted, not listed), slots go round-robin
across source families, and inside a family the object's own card captures come first,
then process and conflicts, link reasons, neighbours and events; the mail, SharePoint and
other documents `plx.documents` answers around the object, each opening only when the
item gate admits the reader, with the tool's withheld marker), and the goal-2 intents
(`ActionPath.list_for_case`, same reader) with their approval stage
(`object_workspace.APPROVAL_STAGES` over every `action_path.STATES`; decided in Mission
Control, never here) whose cited evidence is one of the object's source records (a withheld intent is only
counted, across all cases the reader can read, as unmatchable; one open reads at most 50
cases). Nothing is computed beyond the tool answers, so the tools' permission
monotonicity carries over; withheld counts stay `null` when the policy does not reveal
them. The tools, grants and per-item gates are the case host's
(`object_workspace.host_access` over `operating.case_tools_host.CaptureCaseHostTools`, the
host MCP clients use; option (b) of devloop/OPEN-DECISIONS.md `object-workspace-sources`):
the reader's policy is decided once per request (Portal relations from the mirror
audience, BC units from the live audience, mail and SharePoint units from
`--mail-capture-dir`/`--sharepoint-capture-dir`), so a revocation refuses find, card and
source on the next request and one answer never mixes grants; every mail, SharePoint and
attached-original record is re-gated per item by the host's item gate. A cited record the
reader may not open is listed as withheld with the reason ("not granted to you on this
console", or "this record's own permissions do not admit you"), never dropped; a family
the reader holds no grant in is named in `withheld_sources`. A Portal row or attached
original opens through `CaseWorkspace.citation` (the case runtime's L1 resolver, same
reader), a mail, SharePoint or BC record through the host's `plx.read` (as-of-capture
labels and refusals included); an unreadable or unknown record is 404. Every answer
carries the store's build time and mirror generation; a store from another mirror
generation, or one whose mirror cannot be read, is refused (403), one older than the mirror's captures is labelled stale. The UI is
`operator_ui/workspace.js` ("Find & open"), styled only with the Portal brand tokens
mirrored at the top of `styles.css` (`design-system/tokens.css@c3ffda3`), one column at
phone width. `case_console --ontology-store FILE` serves it; `--project-ontology` builds
`DIR/ontology.sqlite` from DIR's source mirror first (offline); `--seed-fixtures` does so
for the fixture seed (`--fixture`); `--marking` (fixture only) names a marking the fixture
reader holds; a live employee holds none here. Tests:
`python3 -m unittest tests.test_object_workspace_http`, `node --test operator_ui/workspace.test.js`.
Known limitations: a withheld document's count is shown only when the host's policy
reveals it (otherwise "the policy does not reveal how many"); the projection the console
builds (`--project-ontology`) holds Portal captures only, so mail and SharePoint records
appear through `plx.documents` and attached originals, BC through the live audience.
