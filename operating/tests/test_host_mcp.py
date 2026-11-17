"""The host's MCP startup, freshness bound, engine reader and resumed intents (split from
test_host so each module stays under the suite's per-module limit)."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys

from operating.tests.test_host import (
    ActionPathTestCase, Actor, BASE, CATALOG, CaseStore, Directory, EFFECT, ENV,
    Fixture, HTTP_500, HostCase, INIT, Lines, OID_A, OPS, Path, REL, SECRET, SESSION,
    SVC, ap, call, host_module, patch, roster_upns, si, timedelta)


class FreshnessBoundTests(HostCase):
    """Each captured source's freshness
    bound is the config's ``freshness_hours`` for it, else the larger of the default (a
    capture cycle and its projection build take hours) and the capture cadence; the MCP
    host and the console are started with it."""

    def test_bounds_fit_the_cycle_unless_the_config_names_them(self):
        from operating import mcp_server
        day = host_module.DEFAULT_FRESHNESS_HOURS * 3600
        portal = {"base_url": BASE, "relations": sorted(CATALOG), "budget_seconds": 500}
        # no setting: the default for every captured source (the cadence is below it)
        plain = self.write_config(interval_seconds=900, portal=portal)
        # (a family the config does not capture, Mission Control here, is bounded too)
        self.assertEqual(host_module.freshness_bounds(host_module.HostConfig.from_file(plain)),
                         dict.fromkeys(host_module.CAPTURED_FAMILIES, day))
        # a cadence longer than the default keeps its own, larger bound
        slow = self.write_config(interval_seconds=86400, portal=portal)
        self.assertEqual(host_module.freshness_bounds(host_module.HostConfig.from_file(
            slow))["portal_api"], 2.0 * (86400 + 500))
        # one number for every source, or hours per source section (the rest default)
        every = self.write_config(interval_seconds=900, portal=portal, freshness_hours=6)
        self.assertEqual(host_module.freshness_bounds(host_module.HostConfig.from_file(
            every)), dict.fromkeys(host_module.CAPTURED_FAMILIES, 6 * 3600.0))
        some = self.write_config(interval_seconds=900, portal=portal,
                                 freshness_hours={"portal": 12, "mail": 0.5})
        bounds = host_module.freshness_bounds(host_module.HostConfig.from_file(some))
        self.assertEqual((bounds["portal_api"], bounds["graph_mail"], bounds["bc"]),
                         (12 * 3600.0, 1800.0, day))
        host = host_module.Host(self.state, host_module.HostConfig.from_file(some),
                                environ=ENV, sender=self.router, clock=self.clock)
        argv = host_module.mcp_arguments(host, session_ticket_env="T")
        given = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--freshness-bound"]
        self.assertEqual(mcp_server.freshness_bounds(given), bounds)
        # fail closed: an unknown source, zero, negative, NaN, infinite, a boolean, text
        self.assertEqual(host_module.freshness_bounds(host_module.HostConfig.from_file(
            self.write_config(interval_seconds=900, portal=portal, freshness_hours=720)))
            ["bc"], 720 * 3600.0)
        for bad in ({"nowhere": 1}, {}, 0, -1, float("nan"), float("inf"), True, "36",
                    720.5, {"bc": 100000},
                    {"portal": 0}, {"portal": True}, [36]):
            refused = self.write_config(interval_seconds=900, portal=portal,
                                        freshness_hours=bad)
            with self.assertRaises(host_module.HostError, msg=repr(bad)):
                host_module.HostConfig.from_file(refused)

    def test_the_portal_access_age_bound_reaches_every_child_and_shows_in_status(self):
        """SEC-004: ``portal_access_max_age_hours`` is passed to the MCP host and the
        console that load the snapshot; ``status --config`` shows its read time, age and
        bound; a bad bound (or one without a snapshot) is refused."""
        from operating.employee_permissions import parse_read_at
        plain = host_module.HostConfig.from_file(self.write_config())
        self.assertIsNone(plain.portal_access_max_age_hours)
        host = host_module.Host(self.state, plain, environ=ENV, sender=self.router,
                                clock=self.clock)
        self.assertNotIn("--portal-access-max-age-hours",
                         host_module.mcp_arguments(host, session_ticket_env="T"))
        bounded = self.write_config(portal_access_max_age_hours=48)
        host = host_module.Host(self.state, host_module.HostConfig.from_file(bounded),
                                environ=ENV, sender=self.router, clock=self.clock)
        for argv in (host_module.mcp_arguments(host, session_ticket_env="T"),
                     host_module.console_arguments(host, 0)):
            self.assertEqual(argv[argv.index("--portal-access-max-age-hours") + 1], "48.0")
        snapshot_path = Path(json.loads(bounded.read_text("utf-8"))["portal_access"])
        read_at = parse_read_at(json.loads(snapshot_path.read_text("utf-8"))["read_at"])

        def status_at(when):
            self.clock.now = when
            code, out, err = self.main("status", "--state-dir", str(self.state),
                                       "--config", str(bounded))
            self.assertEqual(code, 0, err)
            return json.loads(out)["portal_access"]

        # 10 h after the snapshot was read: fresh, with its read time, age and bound
        shown = status_at(read_at + timedelta(hours=10))
        self.assertEqual(shown["read_at"], read_at.isoformat())
        self.assertEqual(shown["max_age_hours"], 48.0)
        self.assertAlmostEqual(shown["age_hours"], 10.0, places=3)
        self.assertEqual(shown["state"], "fresh")
        # past the bound: stale (the children refuse it per call)
        shown = status_at(read_at + timedelta(hours=49))
        self.assertAlmostEqual(shown["age_hours"], 49.0, places=3)
        self.assertEqual(shown["state"], "stale")
        # a read time more than 5 min in the clock's future is refused, never fresh
        shown = status_at(read_at - timedelta(hours=1))
        self.assertEqual(shown["state"], "invalid")
        self.assertIn("read_at_future", shown["reason"])
        code, out, err = self.main("status", "--state-dir", str(self.state))
        self.assertEqual(code, 0, err)
        self.assertIsNone(json.loads(out)["portal_access"])
        for bad in (0, -1, float("nan"), float("inf"), True, "48", 721):
            with self.assertRaises(host_module.HostError, msg=repr(bad)):
                host_module.HostConfig.from_file(self.write_config(
                    portal_access_max_age_hours=bad))
        with self.assertRaises(host_module.HostError):
            host_module.HostConfig.from_file(self.write_config(
                portal_access=None, portal_access_max_age_hours=48))

    def test_bounds_follow_the_configured_cadence_in_both_modes(self):
        from operating import ingest_forever as forever
        from operating import mcp_server
        cycle = self.write_config(interval_seconds=80000,
                                  portal={"base_url": BASE, "relations": sorted(CATALOG),
                                          "budget_seconds": 500})
        host = host_module.Host(self.state, host_module.HostConfig.from_file(cycle),
                                environ=ENV, sender=self.router, clock=self.clock)
        # 2 x (interval + one capture's budget, else the default timeout), above the
        # default bound
        self.assertEqual(host_module.freshness_bounds(host.config), {
            "portal_api": 161000.0, "bc": 161200.0, "graph_mail": 161200.0,
            "sharepoint": 161200.0, "teams": 161200.0,
            "mission_control": host_module.DEFAULT_FRESHNESS_HOURS * 3600,
            "meetings": host_module.DEFAULT_FRESHNESS_HOURS * 3600})
        argv = host_module.mcp_arguments(host, session_ticket_env="T")
        given = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--freshness-bound"]
        self.assertEqual(mcp_server.freshness_bounds(given),
                         host_module.freshness_bounds(host.config))
        ingest = self.write("ingest-bounds.json", {
            "schema": host_module.CONFIG_SCHEMA, "capture_principal": SVC,
            "capture_audience": "capture-audience.json",
            "mirror_audience": "mirror-audience.json",
            "ingest": {"mode": forever.MODE, "grace_seconds": 60},
            "portal": {"relations": sorted(CATALOG), "budget_seconds": 1500,
                       "min_interval_seconds": 300},
            "sharepoint": {"scope": "sharepoint-scope.json", "budget_seconds": 100},
            "sharepoint_lists": {"scope": "sharepoint-scope.json", "timeout_seconds": 900},
            "teams": {"scope": "teams-scope.json"}})
        # 2 x (min_interval (default 120) + budget, timeout or default 600 + grace), each
        # far below one cycle: the default bound for every projected family
        self.assertEqual(host_module.freshness_bounds(host_module.HostConfig.from_file(
            ingest)), dict.fromkeys(host_module.CAPTURED_FAMILIES,
                                    host_module.DEFAULT_FRESHNESS_HOURS * 3600))
        for refused in (["portal_api=0"], ["nowhere=60"], ["bc=60", "bc=70"], ["bc=x"],
                        ["bc=nan"], ["bc=1e10"]):
            with self.assertRaises(ValueError, msg=refused):
                mcp_server.freshness_bounds(refused)


class McpStartupTests(HostCase):
    """``host mcp``: the real start-up path resolves the employee through the Entra
    directory and consumes the start-up ticket in the durable replay guard."""

    def setUp(self) -> None:
        super().setUp()
        self.config = self.write_config(entra_directory=True)
        self.run_cycle()
        self.clock.advance(3600)
        self.portal.down = HTTP_500
        self.run_cycle()   # the last generation now serves the Portal from its copy
        self.directory = Directory()
        self.ticks = [0.0]
        self.ticket = Fixture.ticket(author=si.AUTHOR_AGENT)

    def mcp(self, lines, *, ticket=None):
        output = io.BytesIO()
        env = {"T": ticket or self.ticket, si.SESSION_TICKET_SECRET_ENV: SECRET}
        with patch.dict(os.environ, env):
            code, _out, err = self.main(
                "mcp", "--state-dir", str(self.state), "--config", str(self.config),
                "--session-ticket-env", "T", sender=self.directory,
                monotonic=lambda: self.ticks[0], input_stream=lines, output_stream=output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        return code, {r["id"]: r for r in replies if "id" in r}, err

    def test_session_resolves_through_the_directory_and_the_tools_use_it(self):
        def revoke():
            self.directory.enabled = False
            self.ticks[0] += 3600   # past the directory cache

        code, replies, err = self.mcp(Lines(
            INIT + [call(2, "plx.find", {"query": "SO100305", "object_type": "SalesOrder"}),
                    call(3, "plx.case_get", {}), call(4, "plx.case_get", {})],
            hooks={4: revoke}))
        self.assertEqual(code, 0, err)
        self.assertGreaterEqual(self.directory.reads(), 1)
        found = replies[2]["result"]
        self.assertFalse(found["isError"], found)
        (fresh,) = [f for f in found["structuredContent"]["meta"]["freshness"]
                    if f["source"] == "portal_api"]
        self.assertEqual((fresh["served_from"], fresh["stale"]), ("persisted_capture", True))
        self.assertIn("live source unavailable (500, internal)", fresh["label"])
        self.assertFalse(replies[3]["result"]["isError"], replies[3])
        # The served tools resolve the employee through the same directory provider.
        refused = replies[4]["result"]
        self.assertTrue(refused["isError"], refused)
        self.assertIn("entra_directory:account_disabled", refused["content"][0]["text"])

    def test_a_configured_portal_never_captured_is_unavailable_never_unconfigured(self):
        """No cycle ever captured the Portal: a Portal-only question names it unavailable
        on a host that configures it, and not configured only on a host that does not."""
        reasons = {}
        for configured in (True, False):
            self.state = self.root / f"never-captured-{configured}"
            self.portal.down_all = HTTP_500
            self.config = self.write_config(entra_directory=True,
                                            **({} if configured else {"portal": None}))
            self.run_cycle()
            code, replies, err = self.mcp(Lines(INIT + [call(2, "plx.find", {
                "query": "SO100305", "object_type": "SalesOrder"})]),
                ticket=Fixture.ticket(author=si.AUTHOR_AGENT))
            self.assertEqual(code, 0, err)
            found = replies[2]["result"]["structuredContent"]
            self.assertNotEqual(found["status"], "found", found)
            reasons[configured] = {(g["state"], g["reason"]) for g in found["gaps"]
                                   if g["subject"] == "portal_api"}
        self.assertIn(("inaccessible", "source_unavailable"), reasons[True])
        self.assertNotIn("source_not_configured", {r for _s, r in reasons[True]})
        self.assertEqual(reasons[False], {("not_ingested", "source_not_configured")})

    def test_the_configured_agent_definition_is_the_one_the_session_runs(self):
        self.config = self.write_config(entra_directory=True, agent="plx.agent.investigate")
        code, replies, err = self.mcp(Lines(
            INIT + [{"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    call(3, "plx.case_get", {}),
                    call(4, "plx.act", {"mode": "status", "case_id": "c"})]))
        self.assertEqual(code, 0, err)
        self.assertNotIn("plx.act", {tool["name"] for tool in replies[2]["result"]["tools"]})
        stamp = replies[3]["result"]["_meta"]["plx/agent"]
        self.assertEqual((stamp["id"], stamp["version"]), ("plx.agent.investigate", 4))
        self.assertEqual(replies[4]["result"]["content"][0]["text"],
                         "tool_not_allowed_for_agent:plx.agent.investigate")
        with self.assertRaises(host_module.HostError):
            host_module.HostConfig({"schema": host_module.CONFIG_SCHEMA,
                                    "mirror_audience": "m.json", "agent": "plx.agent.root"},
                                   self.state)

    def test_plx_act_is_served_only_when_the_config_enables_the_action_path(self):
        status = call(2, "plx.act", {"mode": "status", "intent_id": "intent-unknown"})
        texts = {}
        for enabled in (None, True):
            if enabled:
                self.config = self.write_config(entra_directory=True, action_path=True)
            code, replies, err = self.mcp(Lines(INIT + [status]),
                                          ticket=Fixture.ticket(author=si.AUTHOR_AGENT))
            self.assertEqual(code, 0, err)
            texts[enabled] = replies[2]["result"]["content"][0]["text"]
            said = [json.loads(line)["approver_roster"] for line in err.splitlines()
                    if "approver_roster" in line]
            if not enabled:
                self.assertEqual(said, [])
                continue
            # Host start resolved the roster by UPN; the directory has no such user, so
            # the approver stays unverified and the host says so (counts only).
            self.assertEqual([(a["status"], a["reasons"]) for a in said],
                             [("unverified", {"user_not_found": len(roster_upns())})])
            for upn in roster_upns():
                self.assertNotIn(upn.lower(), err.lower())
        self.assertIn("action_path_not_configured", texts[None])   # default deny
        self.assertNotIn("action_path_not_configured", texts[True])
        for value in ("true", 1, None):
            with self.assertRaises(host_module.HostError):
                host_module.HostConfig({"schema": host_module.CONFIG_SCHEMA,
                                        "mirror_audience": "m.json", "action_path": value},
                                       self.state)

    def test_an_open_session_reads_each_new_projection_generation(self):
        """The host rebuilds the projection while a session is open: the session's next
        query reads the new generation and its labels (Portal back, then down again)."""
        def cycle(down):
            def run():
                self.clock.advance(3600)
                self.portal.down = down
                self.run_cycle()
            return run

        find = {"query": "SO100305", "object_type": "SalesOrder"}
        code, replies, err = self.mcp(Lines(
            INIT + [call(2, "plx.find", find), call(3, "plx.find", find),
                    call(4, "plx.find", find)],
            hooks={3: cycle(None), 4: cycle(HTTP_500)}))
        self.assertEqual(code, 0, err)
        fresh = []
        for request in (2, 3, 4):
            found = replies[request]["result"]
            self.assertFalse(found["isError"], found)
            (portal,) = [f for f in found["structuredContent"]["meta"]["freshness"]
                         if f["source"] == "portal_api"]
            fresh.append(portal)
        self.assertEqual([(f.get("served_from"), f["stale"]) for f in fresh],
                         [("persisted_capture", True), (None, False),
                          ("persisted_capture", True)])
        # The later outage's own label, not the first generation's.
        self.assertNotEqual(fresh[0]["label"], fresh[2]["label"])

    def test_a_replayed_ticket_is_refused_after_a_restart(self):
        code, replies, err = self.mcp(Lines(INIT))
        self.assertEqual(code, 0, err)
        self.assertIn(1, replies)
        code, _replies, err = self.mcp(Lines(INIT))
        self.assertEqual(code, 2)
        self.assertIn("session_ticket_replayed", err)
        # A fresh ticket for the same employee starts.
        code, _replies, err = self.mcp(Lines(INIT), ticket=Fixture.ticket(
            author=si.AUTHOR_AGENT))
        self.assertEqual(code, 0, err)

    def test_a_directory_that_does_not_grant_refuses_the_start(self):
        self.directory.enabled = False
        code, _replies, err = self.mcp(Lines(INIT))
        self.assertEqual(code, 2)
        self.assertIn("entra_directory:", err)
        code, _replies, err = self.mcp(Lines(INIT), ticket=Fixture.ticket(
            author=si.AUTHOR_AGENT))
        self.assertEqual(code, 2)
        # Without Graph credentials the directory is never silently dropped.
        code, _out, err = self.main(
            "mcp", "--state-dir", str(self.state), "--config", str(self.config),
            "--session-ticket-env", "T", environ={}, sender=self.directory)
        self.assertEqual(code, 2)
        self.assertIn("entra_directory_credentials_unavailable", err)
        # The host's live state is never opened by a command-line employee.
        code, _out, err = self.main(
            "mcp", "--state-dir", str(self.state), "--config", str(self.config),
            "--employee-oid", OID_A, sender=self.directory)
        self.assertEqual(code, 2)
        self.assertIn("required: --session-ticket-env", err)


    def seed_brain(self) -> dict[str, set[str]]:
        """A brain capture in the host's state directory (``knowledge_source.capture`` as
        the capture principal over the fake brain), projected by the next cycle: the
        projected object ids per audience unit."""
        from integrations.capture_store import CaptureStore
        from integrations.knowledge_source import NamespaceAuthority, capture, store_path
        from integrations.tests.test_knowledge_source import brain

        authority = NamespaceAuthority({"swarm/rollup/company": [SVC], "swarm/rag/*": [SVC]},
                                       tenant="plx")
        store_path(self.state).parent.mkdir(parents=True, exist_ok=True)
        with CaptureStore(store_path(self.state), authority) as store:
            capture(brain()[0], store, authority, Actor("plx", frozenset({SVC})),
                    ["swarm/rollup/company", "swarm/rag/*"])
        self.assertIn("brain", self.run_cycle()["projection"]["sources"])
        units: dict[str, set[str]] = {}
        with sqlite3.connect(host_module.projection_path(self.state)) as conn:
            for object_id, key in conn.execute("SELECT object_id, audience_key FROM objects "
                                               "WHERE audience_key LIKE 'brain:%'"):
                units.setdefault(key, set()).add(object_id)
        return units

    def test_a_brain_namespace_is_served_only_to_the_readers_the_authority_grants(self):
        """GOV-13: the host grants ``brain:<namespace>`` from its namespace authority, re-read
        per call: a reader sees exactly its namespaces' items, nobody without a grant, and
        nothing when the document is missing, malformed, unknown or names a personal one."""
        units = self.seed_brain()
        self.assertEqual(set(units), {"brain:swarm/rollup/company", "brain:swarm/rag/portal",
                                      "brain:swarm/rag/finance"})   # never a personal one
        namespaces = self.write("namespaces.json", {
            "schema": "plx-knowledge-namespaces/v1", "tenant": "plx", "namespaces": {
                "swarm/rag/portal": ["staff:engineering", "staff:operations"],
                "swarm/rag/*": [SVC], "swarm/rollup/company": [SVC]}})
        self.config = self.write_config(entra_directory=True,
                                        knowledge_namespaces=namespaces.name)
        unset = json.loads(self.config.read_text())
        del unset["knowledge_namespaces"]
        unset = self.write("host-no-brain.json", unset)
        with sqlite3.connect(host_module.projection_path(self.state)) as conn:
            every = [r[0] for r in conn.execute("SELECT object_id FROM objects")]

        def shown(principal: str, config: Path | None = None) -> set[str]:
            host = host_module.Host(self.state, host_module.HostConfig.from_file(
                config or self.config), environ=ENV, sender=self.router, clock=self.clock)
            actor = Actor("plx", frozenset({principal}), "restricted")
            with host.served_tools(actor) as served:
                return {i for i in every
                        if "error" not in served.call("plx.object", {"object_id": i}, actor)}

        # staff:engineering holds no other grant: the one namespace's items, nothing else.
        self.assertEqual(shown("staff:engineering"), units["brain:swarm/rag/portal"])
        self.assertEqual(shown(SVC) & set().union(*units.values()),
                         units["brain:swarm/rollup/company"] | units["brain:swarm/rag/finance"])
        self.assertEqual(shown("staff:nobody"), set())
        # A revocation applies on the next call of a session already open.
        host = host_module.Host(self.state, host_module.HostConfig.from_file(self.config),
                                environ=ENV, sender=self.router, clock=self.clock)
        actor = Actor("plx", frozenset({"staff:engineering"}), "restricted")
        granted_document = namespaces.read_text()
        revoked = json.loads(granted_document)
        revoked["namespaces"]["swarm/rag/portal"] = ["staff:operations"]
        with host.served_tools(actor) as served:
            def visible() -> set[str]:
                return {i for i in every
                        if "error" not in served.call("plx.object", {"object_id": i}, actor)}
            self.assertEqual(visible(), units["brain:swarm/rag/portal"])
            namespaces.write_text(json.dumps(revoked), "utf-8")
            self.assertEqual(visible(), set())
        namespaces.write_text(granted_document, "utf-8")
        # The served MCP session: the employee finds its namespace's item, not another's.
        query = call(2, "plx.find", {"query": "runbook", "object_type": "KnowledgeItem"})
        code, replies, err = self.mcp(Lines(INIT + [query]))
        self.assertEqual(code, 0, err)
        found = {c["object"]["object_id"] for c in
                 replies[2]["result"]["structuredContent"].get("candidates") or ()}
        self.assertEqual(found, units["brain:swarm/rag/portal"])
        # No authority configured: no brain item for anyone.
        self.assertEqual(shown(SVC, unset) & set().union(*units.values()), set())
        granted, self.config = self.config, unset
        code, replies, err = self.mcp(Lines(INIT + [query]), ticket=Fixture.ticket(
            author=si.AUTHOR_AGENT))
        self.config = granted
        self.assertEqual(code, 0, err)
        self.assertFalse(replies[2]["result"]["structuredContent"].get("candidates"))
        # Fail closed per call: a document that no longer loads (malformed, a personal
        # namespace, another tenant) or names only an unknown namespace grants nothing.
        document = json.loads(namespaces.read_text())
        eng = ["staff:engineering"]
        for broken in ("{not json", *(json.dumps({**document, **change}) for change in (
                {"namespaces": {**document["namespaces"], "swarm/personal-notes": eng}},
                {"namespaces": {"swarm/rag/portal": "staff:engineering"}},
                {"tenant": "other"}, {"namespaces": {"swarm/lessons/*": eng}}))):
            config = self.config
            namespaces.write_text(broken, "utf-8")
            if "lessons" not in broken:   # one that does not load is refused at start too
                with self.assertRaises(host_module.HostError, msg=broken) as refused:
                    host_module.HostConfig.from_file(config)
                self.assertEqual(refused.exception.code,
                                 "host_config_invalid:knowledge_namespaces")
                namespaces.write_text(json.dumps(document), "utf-8")
                host = host_module.Host(self.state, host_module.HostConfig.from_file(config),
                                        environ=ENV, sender=self.router, clock=self.clock)
                namespaces.write_text(broken, "utf-8")   # broken while served
                actor = Actor("plx", frozenset({"staff:engineering"}), "restricted")
                with host.served_tools(actor) as served:
                    self.assertEqual({i for i in every if "error" not in served.call(
                        "plx.object", {"object_id": i}, actor)}, set(), broken)
            else:
                self.assertEqual(shown("staff:engineering"), set())
        self.assertIn("--knowledge-namespaces", host_module.mcp_arguments(
            host, session_ticket_env=None))


class EngineReaderTests(HostCase):
    """The proactive engine and the vendor-mail pass read as the capture principal granted
    what it captured (live 2026-11-02: a host config with a capture audience and no live
    audience ran the engine on the Portal mirror audience alone, blind to BC: 0 late POs,
    201 undetermined)."""

    def test_the_engine_reads_with_the_capture_audience_and_builds_no_reader(self):
        from operating.case_runtime import LiveReaders

        config = host_module.HostConfig.from_file(self.write_config(live_audience=None))
        readers = host_module.engine_readers(config, None)
        self.assertIsInstance(readers, LiveReaders)
        self.assertIn("bc", readers.audience.granted_sources())
        # grants only: no source reader, so nothing is requested and no credential read
        self.assertEqual(readers.configured(), {"graph-mail": False,
                                                "sharepoint_drives": [], "bc": False})
        # a host with a live audience keeps its own live readers
        live = object()
        self.assertIs(host_module.engine_readers(
            host_module.HostConfig.from_file(self.config), live), live)


class IntentResumeTests(ActionPathTestCase):
    """EXE-06: a ``plx.act`` intent's state and approval survive a process killed while it
    holds the action store's write lock, and a restart; nothing is dispatched until the
    approved intent is executed (fixture owner, test mode)."""

    def killed_holding_the_store(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-I", "-c",
             "import sqlite3, sys, time; db = sqlite3.connect(sys.argv[1], isolation_level=None);"
             " db.execute('BEGIN IMMEDIATE'); print('held', flush=True); time.sleep(600)",
             str(self.dir / "actions.db")], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "held")
        finally:
            child.kill()
            child.wait(timeout=30)
            child.stdout.close()
        self.assertEqual(child.returncode, -9)

    def resumed(self, iid: str) -> dict:
        self.cases = CaseStore(self.dir / "cases.db", citation_resolver=self.resolve)
        self.path = self.new_path()
        return self.path.view(OPS, iid)

    def test_a_parked_or_approved_intent_survives_a_kill_and_resumes_undispatched(self):
        parked = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        before = dict(self.fx.rows[(REL, '{"id":3}')])
        self.killed_holding_the_store()
        view = self.resumed(parked["intent_id"])
        self.assertEqual((view["state"], host_module.approval_state(view), view["dispatches"],
                          view["outcome"], view["chain_ok"]),
                         ("proposed", "none", [], "not_dispatched", True))

        approved = self.path.register(OPS, self.case_id, self.propose(
            effect={**EFFECT, "equals": "PO-9999"}), session=SESSION)
        iid = approved["intent_id"]
        self.approve(iid, self.call_record(approved))
        self.killed_holding_the_store()
        view = self.resumed(iid)
        self.assertEqual((view["state"], host_module.approval_state(view), view["dispatches"],
                          view["outcome"], view["chain_ok"]),
                         ("call_approved", "call_approved", [], "not_dispatched", True))
        self.assertEqual((self.fx.calls, self.fx.rows[(REL, '{"id":3}')]), (0, before))
        # Approved is not authorized: no dispatch before the gate is requested and decided.
        with self.assertRaises(ap.ActionPathError):
            self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"])
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.killed_holding_the_store()
        view = self.resumed(iid)
        self.assertEqual((view["state"], host_module.approval_state(view), view["dispatches"]),
                         ("gate_authorized", "decided", []))
        self.assertEqual(self.fx.calls, 0)
        # Executed: the one dispatch, after the restart.
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry, session=SESSION)["state"],
                         "dispatched")
        self.assertEqual((self.fx.calls, self.fx.rows[(REL, '{"id":3}')]["po_reference"]),
                         (1, "PO-9999"))
        self.assertEqual(self.resumed(parked["intent_id"])["state"], "proposed")



if __name__ == "__main__":
    import unittest
    unittest.main()
