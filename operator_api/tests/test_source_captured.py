"""The source page opens a captured Teams message, SharePoint list item or MC task (a Home
link to a Teams chat message was a 404 for every reader: the route took only the L1
schemes). It is decided like any source (its capture unit, then the host's gated
``plx.read``), and a source the reader may not open still reads exactly like one that
does not exist (``KeyError``: 404), before anything is read for a withheld unit."""

from __future__ import annotations

import contextlib
import unittest

from ontology.types import Actor
from operator_api.workspace.object_workspace import (CAPTURED_SOURCE_SCHEMES, ObjectWorkspace,
                                                     audience_unit)

CHAT = ("teams-chat://19%3Ab52e3565-9862-4f79-b42a-2acbbd81c6b9_e1dd02bd-0017-4203-95ef-"
        "585f1c801321%40unq.gbl.spaces/1794318594727@c7f46f0e7f4f3f3b-258118")


class Access:
    """The host access: ``withheld`` handles are refused at the unit, ``refused`` ones by
    the per-item read (membership, exclusion), the rest read."""

    def __init__(self, *, withheld=(), refused=(), holders=()):
        self.withheld, self.refused, self.holders = set(withheld), set(refused), holders
        self.reads, self.decided = [], []

    def request(self):
        return contextlib.nullcontext()

    def decide(self, handles):
        self.decided.extend(handles)
        return {h: ({"reason": "not granted to you on this console"} if h in self.withheld
                    else None) for h in handles}

    def withheld_sources(self):
        return []

    def records_of(self, handle):
        return frozenset(self.holders)

    def read(self, handle, *, text_offset=0):
        self.reads.append(handle)
        if handle in self.refused:
            return {"error": {"code": "citation_unresolvable"}}
        return {"status": "resolved", "row": {"body_text": "Is PO 106001 shipping Monday?",
                                               "from": "someone"}}


def workspace(access):
    none = {"handler": lambda arguments, actor: {}}
    tools = {name: none for name in ("plx.find", "plx.object", "plx.around", "plx.timeline",
                                     "plx.process")}
    return ObjectWorkspace(tools, Actor("plx", frozenset({"staff:a"})), access=access)


class CapturedSources(unittest.TestCase):
    def test_a_teams_chat_message_opens_through_the_hosts_read(self):
        access = Access()
        got = workspace(access).source(CHAT)
        self.assertEqual((got["handle"], got["status"], got["kind"]),
                         (CHAT, "resolved", "source_read"))
        self.assertEqual(access.reads, [CHAT])
        # its unit is the chat's, so the reader's grant decides it first
        self.assertTrue(str(audience_unit(CHAT)).startswith("teams:chat:"))

    def test_a_hidden_source_reads_exactly_like_a_missing_one(self):
        withheld = Access(withheld={CHAT})
        with self.assertRaises(KeyError) as hidden:
            workspace(withheld).source(CHAT)
        self.assertEqual(withheld.reads, [], "nothing is read for a withheld unit")
        refused = Access(refused={CHAT})
        with self.assertRaises(KeyError) as item:
            workspace(refused).source(CHAT)
        missing = CHAT.replace("258118", "999999")
        with self.assertRaises(KeyError) as absent:
            workspace(Access(refused={missing})).source(missing)
        self.assertEqual({str(hidden.exception), str(item.exception), str(absent.exception)},
                         {str(absent.exception)})
        # the records citing it are no oracle either
        with self.assertRaises(KeyError):
            workspace(Access(withheld={CHAT}, holders=["X:1"])).cited_by(CHAT)

    def test_every_captured_scheme_the_host_reads_is_routed_and_nothing_else(self):
        from operating.mcp_server import CAPTURED_RECORD_SCHEMES
        self.assertEqual(set(CAPTURED_SOURCE_SCHEMES), set(CAPTURED_RECORD_SCHEMES))
        for bad in ("teams://x/y", "file:///etc/passwd", "https://example.com/a", "",
                    "teams-chat:" + "x" * 5000):
            access = Access()
            with self.assertRaises(ValueError, msg=bad):
                workspace(access).source(bad)
            self.assertEqual((access.reads, access.decided), ([], []))


if __name__ == "__main__":
    unittest.main()
