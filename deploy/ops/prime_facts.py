"""Compute and keep the store facts of the served generation (TraverseTools.prime), once,
with no session time limit, so every later MCP/harness/console process loads them from
beside the store file (ontology_tools_traverse.facts_path). Read-only on the store; the
facts are reader-independent (each call applies its reader's grants later)."""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.environ.get("PLX_CURRENT", "/data/plx-releases/current"))
from operating import ontology_tools, ontology_tools_evidence, ontology_tools_traverse  # noqa: E402

STORE = sys.argv[1] if len(sys.argv) > 1 else "/data/plx-state/projection/ontology.sqlite"
conn = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True, check_same_thread=False)
reader = ontology_tools.StoreReader(conn)
policy = ontology_tools.ReadPolicy(tenant="plx", audiences={})
tools = ontology_tools.OntologyTools(reader, policy)
started = time.monotonic()
ontology_tools_traverse.TraverseTools(tools).prime(
    cell_families=ontology_tools_evidence.STORE_DOCUMENT_FAMILIES)
print(f"primed in {time.monotonic() - started:.0f}s; facts at",
      ontology_tools_traverse.facts_path(STORE))
