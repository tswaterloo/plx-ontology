"""The three non-GET egress paths found on 2026-10-01 stay retired (hard rule).

1. Portal pallet-builder quote POST (``HttpPortalQuoteReader``): the route can
   rate with live carriers (FedEx/UPS, ``carrierQuotes``) - third-party contact.
2. OpenAI Responses POSTs (``operating/cloud_*``): PLX business documents and
   facts sent to a third-party model. The investigating agent is Claude via MCP.
3. Portal Swarm bridge ``POST /api/external/swarm/v1/query``: outside the
   GET-only guard, UAT-scoped, key never granted. Its fixture capture client
   (``integrations/portal_bridge.py``) went with the named-stream family and
   ``UnifiedPlatform`` in deletion-8.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RETIRED_MODULES = ("operating.cloud_document_tags", "operating.cloud_goal_plans",
                   "operating.cloud_transaction_answer", "operating.host_config",
                   # deletion-8: the platform, its launcher and the bridge/stream family.
                   "operating.unified", "operating.__main__", "integrations.portal_bridge",
                   "integrations.stream_host", "integrations.stream_service",
                   "integrations.stream_config")
RETIRED_FLAGS = ("--enable-cloud-document-tags", "--cloud-tag-model",
                 "--enable-cloud-goal-plans", "--cloud-goal-model",
                 "--enable-cloud-transaction-answers", "--cloud-answer-model",
                 "--enable-portal-quote-read", "--portal-quote-base-url",
                 "--portal-quote-auth-env")
RETIRED_NAMES = ("HttpPortalQuoteReader", "CloudDocumentTagger", "CloudGoalPlanner",
                 "CloudTransactionAnswerer", "api.openai.com", "suggest_transaction_answer",
                 "suggest_campaign_plan", "generate_document_tags", "request_freight_quote")
PRODUCT_DIRS = ("ontology", "integrations", "operating", "operator_ui")
# The one embeddings sender may name its provider's endpoint: scripts/check_http_egress.py
# pins that file to that URL and function, so the retired cloud answerers stay banned.
ALLOWED_MARKERS = {"operating/embed_sender.py": frozenset({"api.openai.com"})}


def _env() -> dict[str, str]:
    # Never hand credentials to a child process (no business-system calls here).
    return {key: value for key, value in os.environ.items()
            if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_CONFIG_COUNT")) or
            not any(mark in key for mark in ("KEY", "SECRET", "TOKEN", "PASSWORD"))}


def _product_files():
    for top in PRODUCT_DIRS:
        for path in sorted((ROOT / top).rglob("*")):
            relative = path.relative_to(ROOT)
            if (path.suffix in {".py", ".js"} and "tests" not in relative.parts and
                    "__pycache__" not in relative.parts and not path.name.endswith(".test.js")):
                yield relative, path.read_text(encoding="utf-8")


class EgressRetiredTests(unittest.TestCase):
    def test_retired_modules_do_not_exist(self):
        for name in RETIRED_MODULES:
            self.assertIsNone(importlib.util.find_spec(name), name)

    def test_no_product_code_names_a_retired_path_or_flag(self):
        files = list(_product_files())
        self.assertGreater(len(files), 50)
        for relative, text in files:
            allowed = ALLOWED_MARKERS.get(relative.as_posix(), frozenset())
            for marker in RETIRED_NAMES + RETIRED_FLAGS:
                if marker not in allowed:
                    self.assertNotIn(marker, text, f"{relative}: {marker}")

    def test_case_host_rejects_the_retired_flags_and_the_platform_launcher_is_gone(self):
        # deletion-8: `python3 -m operating workbench` (UnifiedPlatform's launcher) is
        # gone; the case host is the only agent entry point and the console is
        # `python3 -m operator_ui.case_console`.
        with tempfile.TemporaryDirectory() as tmp:
            for flag in ("--enable-cloud-goal-plans", "--enable-cloud-transaction-answers",
                         "--enable-portal-quote-read", "--enable-cloud-document-tags"):
                command = [sys.executable, "-m", "operating.mcp_server",
                           "--state-dir", tmp, "--source-mirror-audience",
                           str(ROOT / "examples" / "source_mirror_audience.example.json"),
                           flag]
                result = subprocess.run(command, cwd=ROOT, env=_env(), stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 2, (command, result.stderr[-400:]))
                self.assertIn(f"unrecognized arguments: {flag}", result.stderr)
            result = subprocess.run([sys.executable, "-m", "operating", "workbench",
                                     "--state-dir", tmp], cwd=ROOT, env=_env(),
                                    stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, timeout=120)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("No module named operating.__main__", result.stderr)

    def test_stream_host_loader_and_its_portal_get_opener_are_retired(self):
        # deletion-7: the binding loader built Portal export/receipt GETs over its
        # own urllib opener and Graph clients from token variables; only the
        # retired platform CLI loaded it. The named Portal streams it bound were
        # deleted with UnifiedPlatform (deletion-8).
        self.assertIsNone(importlib.util.find_spec("integrations.stream_host"))
        for relative, text in _product_files():
            for marker in ("load_stream_host", "plx.stream-host/v1", "--stream-bindings"):
                self.assertNotIn(marker, text, f"{relative}: {marker}")


if __name__ == "__main__":
    unittest.main()
