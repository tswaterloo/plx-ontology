"""Fixture world of the case console: SYNTHETIC data over injected transports only.

``python3 -m operator_ui.tests.case_fixtures --fixture --seed-fixtures`` (the case console
with this world as its ``fixtures``) writes, into a new state directory, the cases this module investigates through the real MCP case tools:

* the six AO packet cases (AO-26005B, -26005C, -26005E, -26006C, -26007D, -26013C)
  over the AO-26005B rows of the live trace (``operating/tests/test_ao_26005b_case.py``;
  the five siblings are clones of its rows under other ids) and the PO5447 purchase-order
  case over the synthetic ``operating/tests/fixtures/packaging_case_fixture.json`` rows,
  each opened offline through the generic path (``operating.case_workspace_live``:
  ``plx.find``, ``plx.object``, ``plx.investigate``) after the fixture Workbench filled
  the source mirror; every AO case gets its synthetic SharePoint packet attached through
  the GET-only Graph reader over the in-process ``FakeSender``
  (``operating/tests/test_ao_originals.py``), and the PO case one synthetic supplier
  confirmation mail read through ``plx.read`` and cited by its body-pinned ``cite_as``;
* a proposal on AO-26005B with a checkable precondition and an expected effect,
  registered on the goal-2 action path (``operating.action_path``; state ``proposed``,
  never dispatched), and its playbook evaluation.

Evidence label: ``injected-transport`` (the real read-only clients over in-process
fakes) and ``isolated-fixture`` (rows, packets, mail): no real PLX data and no network.
A marker file (``case_console.FIXTURE_MARKER``) is written before anything else, so a
console that later opens this directory, with or without the seed flag, labels it
fixture.

The case identifiers and expected values here are evaluation data, so this demo seed
lives with the tests (the no-hardcoded-cases gate): product code never imports it; this
module's entry point hands it to ``case_console.main``. The fixture-state marker and labels are product code
(``operator_ui.case_console``) and are re-exported here for the tests.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from operator_ui.case_console import (ACTIONS_FILE, EVIDENCE_LABEL, EVIDENCE_NOTE,
                                      FIXTURE_MARKER, LIVE_AUDIENCE_FILE, MARKER_SCHEMA,
                                      MIRROR_AUDIENCE_FILE, REPORTS_FILE, report_store_for)
from operator_ui.case_console import fixture_marker as marker
from operating.session_identity import CHANNEL_CONSOLE, fixture_session

AO_CODES = ("AO-26005B", "AO-26005C", "AO-26005E", "AO-26006C", "AO-26007D", "AO-26013C")
PO_NUMBER = "PO5447"
OPERATIONS, QUALITY, PURCHASING = "staff:operations", "staff:quality", "staff:purchasing"
CONFIRMATION_ID = "m-synth-5447-confirm"
_ROOT = Path(__file__).resolve().parents[2]


def build_world() -> SimpleNamespace:
    """The in-process sources: one FakeWorkbench, one FakeSender (SharePoint + mail)."""
    from integrations.tests.test_live_source_adapters import (DRIVE, ENV, MAILBOX, MSG_PATH, j)
    from integrations.tests.test_source_mirror import FakeWorkbench
    from operating.tests import test_ao_26005b_case as ao_tests
    from operating.tests.test_ao_originals import FOLDER, SharePointFolder

    fake = FakeWorkbench()
    packaging = json.loads((_ROOT / "operating" / "tests" / "fixtures" /
                            "packaging_case_fixture.json").read_text("utf-8"))
    fake.catalog = copy.deepcopy(packaging["catalog"])
    fake.tables = copy.deepcopy(packaging["tables"])
    fake.catalog.update(copy.deepcopy(ao_tests.AO_CATALOG))
    for name, rows in ao_tests.AO_TABLES.items():
        fake.tables[name] = [dict(row) for row in rows]
    holder = SimpleNamespace(fake=fake)
    for index, code in enumerate(AO_CODES[1:], start=1):
        ao_tests.Ao26005bCaseTest.add_sibling_ao(holder, code, offset=10_000 * index,
                                                 line_id=1140 + index)
    folder = SharePointFolder()
    confirmation = {
        "id": CONFIRMATION_ID, "changeKey": "ck-synth-1",
        "subject": "SYNTHETIC TEST FIXTURE - Order confirmation PO#5447",
        "parentFolderId": "INBOX-V", "lastModifiedDateTime": "2025-11-04T15:10:00Z",
        "receivedDateTime": "2025-11-04T15:10:00Z", "sentDateTime": "2025-11-04T15:09:00Z",
        "from": {"emailAddress": {"address": "orders@synthetic-vendor-a.example",
                                  "name": "Synthetic Bottle Vendor A"}},
        "toRecipients": [{"emailAddress": {"address": MAILBOX}}],
        "webLink": "https://outlook.office365.com/owa/?ItemID=synthetic",
        "bodyPreview": "SYNTHETIC TEST FIXTURE", "hasAttachments": False,
        "body": {"contentType": "text", "content": (
            "SYNTHETIC TEST FIXTURE - not a real message.\n"
            "We confirm your PO#5447 for 50,000 pieces.\n"
            "Our order no. SC-77120, estimated ship date 2025-12-01.\n")}}
    messages = {CONFIRMATION_ID: confirmation}
    folder.sender.routes[MSG_PATH] = lambda url: j(200, {"value": [
        {key: value for key, value in item.items() if key != "body"}
        for item in messages.values()]})
    folder.sender.routes[f"{MSG_PATH}/{CONFIRMATION_ID}"] = lambda url: j(200, confirmation)
    folder.sender.routes[f"{MSG_PATH}/{CONFIRMATION_ID}/attachments"] = j(200, {"value": []})
    return SimpleNamespace(fake=fake, folder=folder, sender=folder.sender, drive=DRIVE,
                           ao_folder=FOLDER, mailbox=MAILBOX, env=dict(ENV),
                           messages=messages)


def write_audiences(state_dir: Path, world: SimpleNamespace, tenant: str) -> tuple[Path, Path]:
    """The fixture audiences (examples, not PLX grants): mirror relations and live sources."""
    from integrations.source_mirror import AUDIENCE_SCHEMA

    mirror = {}
    for name in ("ao_case_audience.example.json", "packaging_case_audience.example.json"):
        data = json.loads((_ROOT / "examples" / name).read_text("utf-8"))
        for relation, principals in data["audiences"].items():
            mirror[relation] = sorted(set(mirror.get(relation, [])) | set(principals))
    mirror_path = Path(state_dir) / MIRROR_AUDIENCE_FILE
    mirror_path.write_text(json.dumps({
        "schema": AUDIENCE_SCHEMA, "tenant": tenant, "default_classification": "internal",
        "note": "FIXTURE audience of python3 -m operator_ui.tests.case_fixtures --seed-fixtures "
                "(the AO and packaging example audiences, merged); not a PLX grant.",
        "audiences": mirror}, indent=1, sort_keys=True), "utf-8")
    live_path = Path(state_dir) / LIVE_AUDIENCE_FILE
    live_path.write_text(json.dumps({
        "schema": "plx-live-source-audience/v1", "tenant": tenant,
        "default_classification": "internal",
        "note": "FIXTURE live-source audience (synthetic drive and mailbox); not a PLX grant.",
        "audiences": {
            f"sharepoint:{world.drive}:{world.ao_folder}": [QUALITY],
            f"sharepoint:{world.drive}:{world.ao_folder}/*": [QUALITY],
            f"mail:{world.mailbox}": [PURCHASING]}}, indent=1, sort_keys=True), "utf-8")
    return mirror_path, live_path


def live_readers(world: SimpleNamespace, live_audience: Path):
    """The read-only Graph readers of this world (``FakeSender``: injected transport)."""
    from operating.case_runtime import build_live_readers

    return build_live_readers(live_audience, world.env, graph_sender=world.sender,
                              sharepoint_drives=[world.drive])


def runtime_for(state_dir: Path, world: SimpleNamespace, mirror_audience: Path,
                live_audience: Path, tenant: str, *, online: bool):
    """A case runtime over this world; ``online`` reads the fake Workbench (seeding)."""
    from integrations.tests.test_source_mirror import BASE, KEY
    from integrations.workbench_read_api import WorkbenchReadApiClient
    from operating.case_runtime import CaseRuntime
    from operating.tests.test_ao_originals import PAGES, extractor

    client = WorkbenchReadApiClient(BASE, KEY, fetch=world.fake) if online else None
    return CaseRuntime(state_dir, client=client, audience=mirror_audience, tenant=tenant,
                       live=live_readers(world, live_audience),
                       mirror_options={"retry_delay_seconds": 0},
                       originals_options={"extractor": extractor(dict(PAGES))})


def _sessions(runtime: Any, actor: Any, name: str) -> Callable[[str, dict], dict]:
    """``call`` over MCP case-only sessions; a new session when one's per-minute call
    budget is spent (the server refuses before executing, so nothing is repeated).
    ``call.close()`` ends every session it opened (each releases its case host)."""
    from operating.mcp_server import MCPServer, local_client

    servers: list[Any] = []

    def session() -> Callable[[str, dict], dict]:
        servers.append(MCPServer(case_runtime=runtime, actor=actor))
        return local_client(servers[-1], name)
    current = [session()]

    def call(tool: str, arguments: dict) -> dict:
        try:
            return current[0](tool, arguments)
        except RuntimeError as error:
            if "rate_limit_exceeded" not in str(error):
                raise
            current[0] = session()
            return current[0](tool, arguments)

    def close() -> None:
        while servers:
            servers.pop().close()
    call.close = close  # type: ignore[attr-defined]
    return call


def seed(state_dir: str | Path, tenant: str = "plx") -> dict[str, Any]:
    """Open the fixture cases into a new ``state_dir`` the way a live console does; returns
    what was written. The fixture Workbench first fills the source mirror (the ingestion
    step of a live host); every case then opens offline through the generic path
    (``operating.case_workspace_live.CaseOpener``: plx.find, plx.object, plx.investigate)."""
    from integrations.source_mirror import AudienceMap
    from ontology.core import Actor
    from operating import playbook_eval
    from operating.action_path import ActionPath, ActionPathError
    from operating.case_workspace_live import CaseOpener
    from operating.cases import actor_label
    from operating.sharepoint_originals import folder_source
    from operating.playbooks import PlaybookRegistry

    state_dir = Path(state_dir)
    if (state_dir / "case-runtime").exists() or (state_dir / FIXTURE_MARKER).exists():
        raise ValueError("seed_requires_new_state_dir")
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / FIXTURE_MARKER).write_text(json.dumps({
        "schema": MARKER_SCHEMA, "evidence_label": EVIDENCE_LABEL, "note": EVIDENCE_NOTE,
        "aos": list(AO_CODES), "po": PO_NUMBER}, indent=1), "utf-8")
    world = build_world()
    mirror_path, live_path = write_audiences(state_dir, world, tenant)
    expected_path = state_dir / "fixture-ao-originals-expected.json"
    world.folder.expected(state_dir).replace(expected_path)
    originals = folder_source(expected_path, {}, drive_id=world.drive)
    filler = runtime_for(state_dir, world, mirror_path, live_path, tenant, online=True)
    try:
        filler.mirror.sync(Actor(tenant, frozenset({OPERATIONS, QUALITY, PURCHASING})))
    finally:
        filler.close()
    runtime = runtime_for(state_dir, world, mirror_path, live_path, tenant, online=False)
    audience = AudienceMap.from_file(mirror_path)
    quality = Actor(tenant, frozenset({OPERATIONS, QUALITY}))
    purchasing = Actor(tenant, frozenset({OPERATIONS, PURCHASING}))
    written: dict[str, Any] = {"aos": {}, "po": None}

    def opened(opener: Any, kind: str, identifier: str) -> str:
        result = opener.open_case(kind, identifier)
        if result["status"] != "opened":
            raise RuntimeError(f"seed_case_not_opened:{identifier}:{result.get('missing')}")
        return result["case_id"]
    sessions: list[Any] = []
    try:
        opener = CaseOpener(runtime, quality, state_dir, audience=audience, originals=originals)
        for code in AO_CODES:
            written["aos"][code] = opened(opener, "ao", code)
        case_id = written["aos"][AO_CODES[0]]
        # A proposal on the first AO citing its FM row and one of its quality item rows.
        [ao_row] = runtime.mirror.rows(quality, "public.assembly_order",
                                       where=[["nameamo_c", "eq", AO_CODES[0]]])["rows"]
        item = runtime.mirror.rows(quality, "silver.assembly_order_items",
                                   where=[["assembly_order_id", "eq", ao_row["row"]["id"]],
                                          ["qty_per_batch", "eq", "1.0"]],
                                   order=["id"], limit=1)["rows"][0]
        call = _sessions(runtime, quality, "case-console-fixture-seed")
        sessions.append(call)
        proposal = call("plx.case_propose", {
            "case_id": case_id,
            "capability": "portal.assembly_order.set_header_quantities",
            # Each written value names the cited evidence field that holds it.
            "payload": {"assembly_order": AO_CODES[0], "qtytotal": "1509", "finalqty": "1503",
                        "evidence": {
                            "qtytotal": {"citation": item["citation"], "field": "quantity"},
                            "finalqty": {"citation": item["citation"],
                                         "field": "actual_amount"}}},
            "preconditions": [
                "Quality confirms the counts on the signed AO packet",
                {"check": "field_empty", "citation": ao_row["citation"], "field": "qtytotal"}],
            "citations": [ao_row["citation"], item["citation"]],
            "expected_effect": {"relation": "public.assembly_order",
                                "key": {"id": ao_row["row"]["id"]},
                                "values": {"qtytotal": "1509", "finalqty": "1503"}}})
        actions = ActionPath(state_dir / ACTIONS_FILE, runtime.cases)
        # The proposal's action type is not in the typed action catalog
        # (operating/action_catalog.json; no Portal write surface sets these AO header
        # fields), so the action path refuses to register it: the case keeps the
        # proposal and the console shows it as not registered.
        try:
            intent = actions.register(quality, case_id, proposal["proposal"]["proposal_id"],
                                      session=fixture_session(tenant, quality.principals,
                                                              channel=CHANNEL_CONSOLE))
            written["intent"], written["intent_refused"] = intent["intent_id"], None
        except ActionPathError as exc:
            written["intent"], written["intent_refused"] = None, exc.code
        # The shipped AO playbook scored on the generic case, stored as the console shows it.
        registry = PlaybookRegistry().load_directory()
        report = playbook_eval.evaluate(registry.get("ao_quantity_reconciliation"), call,
                                        case_id)
        store = report_store_for(runtime, state_dir / REPORTS_FILE, [quality])
        store.record(report, evaluated_by=actor_label(quality), transcript_source="none")

        buyer = CaseOpener(runtime, purchasing, state_dir, audience=audience)
        written["po"] = opened(buyer, "petra_po", PO_NUMBER)
        call = _sessions(runtime, purchasing, "case-console-fixture-seed")
        sessions.append(call)
        message = runtime.mail_message(purchasing, world.mailbox, CONFIRMATION_ID)
        mail = call("plx.read", {"citation": message["metadata_citation"]})
        call("plx.case_add_finding", {
            "case_id": written["po"], "kind": "fact", "status": "supported",
            "statement": "The supplier's confirmation mail, as read for this employee.",
            "citations": [mail["cite_as"]["citation"]]})
    finally:
        for opened_session in sessions:
            opened_session.close()
        runtime.close()
    data = marker(state_dir) or {}
    data.update(cases=written)
    (state_dir / FIXTURE_MARKER).write_text(json.dumps(data, indent=1), "utf-8")
    return written


def scripted_ask(request: dict) -> tuple[dict, None]:
    """A scripted Ask planner for the fixture console (``--scripted-ask``): it reads the
    records the question names (an objective's root and candidate lines, or an Ask's
    object id), then answers with one structured claim per card that has one: the first
    identifier its first cited record holds, by reference (value, citation, field), named
    by the prose; and leaves one unknown. No model; nothing is invented."""
    if not request["transcript"]:
        question, ids = request["question"], []
        for line in question.splitlines():
            if "(plx object id): " in line:
                ids.append(line.split("(plx object id): ", 1)[1])
            elif "none chosen: " in line:
                ids += [part.strip() for part in
                        line.split("none chosen: ", 1)[1].split(". Read", 1)[0].split(";")]
        if ids:
            return {"action": "call", "calls": [
                {"tool": "plx.object", "arguments": {"object_id": object_id}}
                for object_id in ids[:3]]}, None
    claims = []
    for step in request["transcript"][:1]:
        for observation in step["observations"]:
            card = observation["result"]
            values = [value for value in card.get("values") or [] if value.get("citation")]
            if not values:
                continue
            handle = values[0]["citation"]["l1_handle"]
            named = next((value for value in values if value.get("value_type") == "identifier"
                          and isinstance(value.get("value"), str)
                          and value["citation"]["l1_handle"] == handle), None)
            if named is not None:
                claims.append({"id": f"c{len(claims)}", "value": named["value"],
                               "citation": handle, "role": "identifier",
                               "field": named["citation"]["field_path"]})
    return {"action": "answer", "claims": claims,
            "text": " ".join(f"The record is {{{claim['id']}}}." for claim in claims),
            "unknowns": ["not in the record: a freight quote for the shipment"]}, None


__all__ = ["ACTIONS_FILE", "AO_CODES", "EVIDENCE_LABEL", "EVIDENCE_NOTE", "FIXTURE_MARKER",
           "LIVE_AUDIENCE_FILE", "MARKER_SCHEMA", "MIRROR_AUDIENCE_FILE", "PO_NUMBER",
           "REPORTS_FILE", "build_world", "live_readers", "marker", "runtime_for", "scripted_ask",
           "seed",
           "write_audiences"]


if __name__ == "__main__":
    # The fixture console: ``case_console`` arguments, served with this fixture world.
    import sys

    from operator_ui import case_console
    from operator_ui.tests import case_fixtures

    sys.stdout.reconfigure(line_buffering=True)
    # ``--scripted-ask``: Ask and the objective box run ``scripted_ask``, not Claude Code.
    scripted = "--scripted-ask" in sys.argv
    argv = [arg for arg in sys.argv[1:] if arg != "--scripted-ask"]
    raise SystemExit(case_console.main(argv, fixtures=case_fixtures,
                                       planner=case_fixtures.scripted_ask if scripted else None))
