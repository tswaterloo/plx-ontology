"""Citation handle schemes: the one list every handle grammar is built from.

A citation handle is ``<scheme>://<rest>``. The schemes a Workbench answer may cite are
the Portal reporting mirror (``api``), operator-supplied originals (``original``), Graph
mail (``graph-mail``), SharePoint (``sharepoint``), Business Central (``bc``), Mission
Control (``mc``), brain knowledge (``brain``), Teams channel and chat message
(``teams-channel``, ``teams-chat``) and SharePoint list item (``sharepoint-list``) captures.
``claim_checks.HANDLE``, the injection guard's ``handle`` grammar and ``ontology_tools``
build their patterns from ``HANDLE_PREFIX``; the JSON contracts (``evals/item.schema.json``
and ``operating/ontology_tool_contracts.json`` ``$defs/handle``) cannot import it
and are tested against it (``tests/test_injection_guard.py``). ``fixture://`` handles name
isolated-fixture rows (``action_path``, ``goal2_exec_owner``): only the injection guard
accepts them.

Which schemes a case runtime can *resolve* is a narrower, separate list
(``operating/case_runtime.SUPPORTED_SCHEMES``).
"""

from __future__ import annotations

import re

CITATION_SCHEMES = ("api", "bc", "graph-mail", "sharepoint", "original", "mc", "brain",
                    "teams-channel", "sharepoint-list", "teams-chat")
FIXTURE_SCHEME = "fixture"


def handle_prefix(*extra: str) -> str:
    """Regex for ``<scheme>://`` over the citation schemes plus ``extra``."""
    schemes = CITATION_SCHEMES + extra
    if len(set(schemes)) != len(schemes) or not all(
            re.fullmatch(r"[a-z][a-z0-9-]{0,31}", scheme) for scheme in schemes):
        raise ValueError(f"citation schemes must be distinct lowercase names: {schemes}")
    return "(?:" + "|".join(schemes) + ")://"


HANDLE_PREFIX = handle_prefix()

__all__ = ["CITATION_SCHEMES", "FIXTURE_SCHEME", "HANDLE_PREFIX", "handle_prefix"]
