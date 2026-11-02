"""A full-read grantee and a captured permission list past the capture-age bound.

A Teams chat whose captured member list is older than ``UNIT_ACL_MAX_AGE`` opens for a
reader holding a declared full-read grant for Teams only when (a) the list names no excluded
person, (b) its members are recorded, checkable (object ids) and cleared, and (c) the
exclusion list is loaded; every open is journalled with the list's age. Every other reader,
a member included, keeps the strict bound. A SharePoint file past the bound opens for the
grantee only with a recorded, checkable, cleared creator and last modifier. Synthetic ids
(the injected-transport Teams fixture of ``integrations.tests.test_capture_teams``)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest import mock

from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AudienceMap
from integrations.tests import test_capture_teams as tct
from ontology.types import Actor
from operating.case_runtime import CaseRuntime, LiveReaders, _EmployeeItemAcl
from operating.employee_permissions import (EmployeeEntitlement, EmployeePrincipal,
                                            FullReadAccess, FullReadGrant)
from operator_ui.session_journal import (JOURNAL_DIR_ENV, SessionJournal,
                                         record_full_read_stale)

GRANTEE = "0000aaaa-0000-0000-0000-0000000000f1"   # not a member of the chat
CLEAR = "0e0e0e0e-0000-4000-8000-00000000e0e1"     # the fixture's excluded id (no member)


def grant(scopes=("teams", "sharepoint")) -> FullReadGrant:
    return FullReadGrant(GRANTEE, "owner", "2026-10-31", "test", frozenset(scopes))


def entitlement(exclusion=None, *, full_read=True, identities=("outsider@petrasoap.com",)):
    reader = Actor("plx", frozenset({"staff:teams-outsider", "employee:" + GRANTEE}),
                   "restricted")
    guard = PeopleExclusion([CLEAR]) if exclusion is None else exclusion
    return EmployeeEntitlement(
        EmployeePrincipal(tct.ENV["MICROSOFT_GRAPH_TENANT_ID"], GRANTEE), reader, None,
        source_identities=frozenset(identities),
        full_read=FullReadAccess(grant(), guard) if full_read else None)


class StaleTeamsChatTest(tct.CaptureCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertTrue(self.capture.run(tct.scope())["complete"])
        self.runtime = CaseRuntime(self.tmp.name, client=None,
                                   audience=AudienceMap({}, tenant="plx"), tenant="plx",
                                   live=LiveReaders(self.audience),
                                   people_exclusion=PeopleExclusion.from_env(tct.ENV))
        self.addCleanup(self.runtime.close)

    def age(self, hours: float) -> None:
        stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE unit_acl SET recorded_at = ? WHERE kind = 'teams_chat'", (stamp,))

    def chat(self, held: EmployeeEntitlement) -> list:
        audience = _EmployeeItemAcl(self.audience, held, "teams-chat",
                                    stale=self.runtime.full_read_stale_allows)
        return [r for r in self.store.read_as(audience).records(held.actor, *self.unit(tct.CHAT))
                ["items"] if "/conversation@" not in r["citation"]]

    def stale_rows(self) -> list:
        return SessionJournal.for_state_dir(self.tmp.name).summary()["full_read_stale_opens"]

    def open(self, held: EmployeeEntitlement, handle: str) -> dict | None:
        """The runtime's open of one captured Teams message (``_from_capture``, what
        ``resolve_citation`` and the claim gate read), None when refused."""
        from operating.case_runtime import CitationUnresolvable
        self.runtime.employee_gate = SimpleNamespace(entitlement=lambda actor: held)
        try:
            return self.runtime._from_capture(handle, held.actor)
        except CitationUnresolvable:
            return None

    def test_a_stale_member_list_opens_for_the_grantee_only_when_a_to_c_hold(self):
        listed = self.chat(entitlement())                     # fresh: the grant reads it
        self.assertTrue(listed)
        handle = listed[0]["citation"]
        self.assertTrue(self.open(entitlement(), handle))
        self.assertEqual(self.stale_rows(), [])              # not stale: nothing journalled
        self.age(72)
        # Listed only (the store's per-record decision): stale, (a)-(c) hold, and nothing
        # is journalled, since nothing was opened.
        self.assertTrue(self.chat(entitlement()))
        self.assertEqual(self.stale_rows(), [])
        # A status check reads no content: not an open either.
        self.runtime.employee_gate = SimpleNamespace(entitlement=lambda actor: entitlement())
        self.runtime._capture_resolve(handle, entitlement().actor)
        self.assertEqual(self.stale_rows(), [])
        # Opened: one row, after every check passed.
        self.assertTrue(self.open(entitlement(), handle))
        rows = self.stale_rows()
        self.assertEqual(len(rows), 1)                       # one row per open
        self.assertEqual(rows[0]["source"], "teams_chat")
        self.assertRegex(rows[0]["note"],
                         r"^opened on a permission list 7[12] h old under full-read$")
        # Refused (an excluded person on the list, the exclusion not loaded, no grant):
        # never journalled, before or after the refusal.
        for held in (entitlement(PeopleExclusion([tct.U_COS])), entitlement(PeopleExclusion([])),
                     entitlement(full_read=False)):
            self.assertIsNone(self.open(held, handle))
        self.assertEqual(len(self.stale_rows()), 1)
        # The stale membership decided open, then the item refused by a later check (the
        # runtime's own exclusion names a chat member now): no row either.
        held_guard = self.runtime.people_exclusion
        self.runtime.people_exclusion = PeopleExclusion([CLEAR, tct.U_OTHER])
        self.assertIsNone(self.open(entitlement(), handle))
        self.runtime.people_exclusion = held_guard
        self.assertEqual(len(self.stale_rows()), 1)

    def test_never_for_a_non_grantee_even_a_member(self):
        self.age(72)
        self.assertEqual(self.chat(entitlement(full_read=False)), [])
        member = entitlement(full_read=False, identities=(tct.COS, tct.U_COS))
        self.assertEqual(self.chat(member), [])
        self.age(1)                                          # fresh: the member reads it
        self.assertTrue(self.chat(member))
        self.assertEqual(self.stale_rows(), [])

    def test_never_when_the_stale_list_names_an_excluded_person(self):
        self.age(72)
        self.assertEqual(self.chat(entitlement(PeopleExclusion([tct.U_COS]))), [])
        self.assertEqual(self.stale_rows(), [])

    def test_never_when_the_exclusion_list_is_not_loaded(self):
        self.age(72)
        for guard in (PeopleExclusion([]),
                      PeopleExclusion(unavailable="excluded_people_file_unreadable")):
            self.assertEqual(self.chat(entitlement(guard)), [])
        self.assertEqual(self.stale_rows(), [])


class StaleSharePointTest(unittest.TestCase):
    """``CaseRuntime.full_read_stale_allows`` for a SharePoint file (the check the capture
    host's age bound and the recorded-capture original gate both make)."""

    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()

    def allows(self, held, acl, at=None, **people) -> bool:
        runtime = SimpleNamespace(employee_gate=None, state_dir=self.tmp.name)
        runtime.journal_full_read_stale = lambda *a: CaseRuntime.journal_full_read_stale(
            runtime, *a)
        return CaseRuntime.full_read_stale_allows(runtime, held, "sharepoint", acl,
                                                  at or self.old, **people)

    @staticmethod
    def directory(*excluded) -> PeopleExclusion:
        """The exclusion with a captured directory: COS and VINCE listed (and any
        ``excluded`` id with its own address), an outside address unlisted."""
        guard = PeopleExclusion([CLEAR, *excluded])
        users = [(tct.U_COS, tct.COS, []), (tct.U_VINCE, tct.VINCE, [])]
        users += [(oid, f"gone{n}@petrasoap.com", []) for n, oid in enumerate(excluded)]
        guard.attach_directory(users)
        return guard

    def test_no_creator_needed_but_every_person_must_be_decidable_and_cleared(self):
        oids = {"status": "read", "identities": [tct.U_COS]}
        self.assertTrue(self.allows(entitlement(), oids))                  # no fields needed
        self.assertTrue(self.allows(entitlement(), oids, modifier=tct.U_VINCE))
        self.assertFalse(self.allows(entitlement(full_read=False), oids))   # non-grantee
        # A recorded modifier the exclusion cannot decide: withheld (never opened blind);
        # the captured directory decides it.
        self.assertFalse(self.allows(entitlement(), oids, modifier=tct.VINCE))
        self.assertTrue(self.allows(entitlement(self.directory()), oids, modifier=tct.VINCE))
        self.assertFalse(self.allows(entitlement(self.directory()), oids, modifier=tct.OTHER))
        # The list is checked as a fresh item's: what the exclusion decides must be clear,
        # an entry it cannot name (an unlisted address, a link scope) is no person to it.
        addresses = {"status": "read", "identities": [tct.COS, "link-scope:organization"]}
        self.assertTrue(self.allows(entitlement(self.directory()), addresses))
        # An excluded person, on the list, as modifier, or as OneDrive owner: withheld.
        gone = self.directory(tct.U_OTHER)
        self.assertFalse(self.allows(entitlement(gone), {"status": "read",
                                                         "identities": ["gone0@petrasoap.com"]}))
        self.assertFalse(self.allows(entitlement(gone), oids, modifier="gone0@petrasoap.com"))
        self.assertFalse(self.allows(entitlement(gone), oids,
                                     onedrive_owner="gone0_petrasoap_com"))
        self.assertTrue(self.allows(entitlement(gone), oids,
                                    onedrive_owner="cos_petrasoap_com"))
        self.assertFalse(self.allows(entitlement(gone), oids,
                                     onedrive_owner="nobody_example_com"))
        # The exclusion not loaded: nothing.
        self.assertFalse(self.allows(entitlement(PeopleExclusion([])), oids))
        future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        self.assertFalse(self.allows(entitlement(), oids, future))
        self.assertFalse(self.allows(entitlement(), oids, "undated"))
        notes = SessionJournal.for_state_dir(self.tmp.name).summary()["full_read_stale_opens"]
        self.assertEqual({n["note"] for n in notes},
                         {"opened on a permission list 100 h old under full-read"})
        self.assertEqual(len(notes), 5)   # one row per open above


class EvalRunJournalTest(unittest.TestCase):
    """An evaluation run reads the host's state but journals in its own run directory,
    never in the host's state journal,
    and the hosts it starts are
    told where."""

    def setUp(self) -> None:
        import os
        import tempfile
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state, self.run_dir = root / "plx-state", root / "runs" / "r1"
        self.state.mkdir()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(JOURNAL_DIR_ENV, None)

    def written(self) -> list:
        """Journal one stale open for the host state from inside the run (what a host
        the run started does) and say where it went."""
        import os
        record_full_read_stale(self.state, employee=GRANTEE, access="full_read:teams",
                               source="teams-chat", hours=60)
        return [os.environ.get(JOURNAL_DIR_ENV),
                SessionJournal.for_state_dir(self.state).path]

    def test_every_scoreboard_journals_in_its_run_directory(self):
        import os
        from unittest import mock
        from scripts import (eval_case_cards, eval_objective_agent, eval_real_agent,
                             eval_retrieval)
        dirs = {name: self.run_dir.parent / name for name in (
            "cards", "agent", "objective", "retrieval")}
        cases = {
            "eval_case_cards": (lambda: eval_case_cards.main(
                ["run", "--work-dir", str(dirs["cards"])]),
                "scripts.eval_case_cards.card_sets", dirs["cards"] / "journal"),
            "eval_real_agent": (lambda: eval_real_agent.main(
                ["run", "--work-dir", str(dirs["agent"])]),
                "scripts.eval_real_agent.shutil.which", dirs["agent"] / "journal"),
            "eval_objective_agent": (lambda: eval_objective_agent.main(
                ["run", "--work-dir", str(dirs["objective"])]),
                "scripts.eval_objective_agent.shutil.which", dirs["objective"] / "journal"),
            "eval_retrieval": (lambda: eval_retrieval.main(
                ["--out", str(dirs["retrieval"] / "retrieval.json")]),
                "scripts.eval_case_cards.card_sets",
                dirs["retrieval"] / "retrieval.json.journal"),
        }
        for name, (run, hook, expected) in cases.items():
            with self.subTest(name):
                seen: list = []

                def during(*args, **kwargs):
                    seen.append(self.written())
                    raise ValueError("stop_here")
                with mock.patch(hook, side_effect=during), \
                        mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                    try:
                        run()
                    except ValueError:
                        pass
                self.assertEqual(seen, [[str(expected.resolve()),
                                         expected.resolve() / "console-journal.sqlite"]])
                self.assertEqual(oct(expected.stat().st_mode & 0o777), "0o700")
                rows = SessionJournal(expected / "console-journal.sqlite").summary()
                self.assertEqual(len(rows["full_read_stale_opens"]), 1)
                # never the host's own journal; the setting ends with the run
                self.assertFalse((self.state / "console-journal.sqlite").exists())
                self.assertIsNone(os.environ.get(JOURNAL_DIR_ENV))

    def test_the_hosts_a_run_starts_are_told_and_a_bad_setting_writes_nothing(self):
        import os
        from operating.agent_session import ReaderHost
        from operator_ui.session_journal import isolate_journal
        journal = isolate_journal(self.run_dir / "journal")
        host = ReaderHost(self.state, self.state / "host.json", {}, "s", tenant="plx")
        host.ticket = lambda: "ticket"
        self.assertEqual(host.command("operating.host", "mcp")["env"][JOURNAL_DIR_ENV],
                         str(journal))
        # Relative or empty: refused (OSError, which every journal writer catches), so
        # nothing is written anywhere, the host's state directory included.
        for bad in ("journal", ""):
            os.environ[JOURNAL_DIR_ENV] = bad
            with self.assertRaises(OSError):
                self.written()
        self.assertFalse((self.state / "console-journal.sqlite").exists())
        # Unset (the host's own service): its state directory, as before.
        os.environ.pop(JOURNAL_DIR_ENV)
        self.assertEqual(self.written()[1], self.state / "console-journal.sqlite")


class CapturedDirectoryExclusionTest(unittest.TestCase):
    """The people exclusion decided offline by a captured directory: an address or OneDrive
    owner it maps to one listed user is decided by that user's object id; anything it does
    not map, or maps to two ids, stays undecided; without it nothing changes."""

    def test_directory_decides_only_what_it_maps(self):
        from integrations.people_exclusion import ExclusionUnavailable, PeopleExcluded
        guard = PeopleExclusion([tct.U_OTHER])
        with self.assertRaises(ExclusionUnavailable):
            guard.is_excluded(tct.COS)                  # unbound, no directory
        guard.attach_directory([(tct.U_COS, tct.COS, ["shared@petrasoap.com"]),
                                (tct.U_OTHER, tct.OTHER, []),
                                (tct.U_VINCE, tct.VINCE, ["shared@petrasoap.com"])])
        self.assertFalse(guard.is_excluded(tct.COS))
        self.assertTrue(guard.is_excluded(tct.OTHER))
        for unknown in ("shared@petrasoap.com", "x@example.com"):   # clash, unlisted
            self.assertFalse(guard.decides(unknown))
            with self.assertRaises(ExclusionUnavailable):
                guard.is_excluded(unknown)
        guard.refuse_onedrive_path("cos_petrasoap_com")
        with self.assertRaises(PeopleExcluded):
            guard.refuse_onedrive_path("guest_example-plx_com")
        with self.assertRaises(ExclusionUnavailable):
            guard.refuse_onedrive_path("nobody_example_com")

    def test_a_captured_original_in_a_onedrive_is_decided_by_the_directory(self):
        from operating.captured_originals import FAILED, REFUSED
        guard = PeopleExclusion([tct.U_OTHER])
        # No Graph listing (address_exclusion None): the runtime's own guard decides only
        # with a captured directory attached.
        runtime = SimpleNamespace(exclusion_refusal=lambda: None, address_exclusion=lambda: None,
                                  people_exclusion=guard)
        decide = CaseRuntime._captured_original_exclusion
        self.assertEqual(decide(runtime, "cos_petrasoap_com"), FAILED)   # no directory
        guard.attach_directory([(tct.U_COS, tct.COS, []), (tct.U_OTHER, tct.OTHER, [])])
        self.assertIs(decide(runtime, "cos_petrasoap_com", [tct.U_COS, tct.COS]), True)
        self.assertEqual(decide(runtime, "guest_example-plx_com"), REFUSED)
        self.assertEqual(decide(runtime, "cos_petrasoap_com", [tct.OTHER]), REFUSED)
        self.assertEqual(decide(runtime, "cos_petrasoap_com", ["x@example.com"]), FAILED)

    def test_a_onedrive_path_is_decided_by_a_upn_only(self):
        """The excluded person is not in the captured directory (excluded people's
        mailboxes are never captured); a cleared user's alias ``ann_lee@...`` has the path
        form of the excluded UPN ``ann.lee@...``. The alias never clears that OneDrive:
        only the UPN a path is made from decides it."""
        from integrations.people_exclusion import ExclusionUnavailable, onedrive_path
        excluded_upn, alias = "ann.lee@petrasoap.com", "ann_lee@petrasoap.com"
        self.assertEqual(onedrive_path(alias), onedrive_path(excluded_upn))
        guard = PeopleExclusion([tct.U_OTHER])
        guard.attach_directory([(tct.U_COS, tct.COS, [alias])])
        self.assertFalse(guard.is_excluded(alias))            # the address itself: COS's
        with self.assertRaises(ExclusionUnavailable):         # the path: undecided
            guard.refuse_onedrive_path(onedrive_path(excluded_upn))
        guard.refuse_onedrive_path(onedrive_path(tct.COS))    # COS's own UPN: cleared

    def test_a_directory_past_its_age_bound_is_not_loaded_and_fails_closed(self):
        import tempfile
        from integrations.people_exclusion import (DIRECTORY_MAX_AGE, mail_capture_directory,
                                                   with_directory)
        from operating.captured_originals import FAILED
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Path(tmp.name) / "mail-capture" / "capture.db"
        store.parent.mkdir()
        now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        with sqlite3.connect(store) as db:
            db.execute("CREATE TABLE mailboxes (mailbox_id TEXT, upn TEXT, identities TEXT, "
                       "identity_checked_at TEXT)")
            db.executemany("INSERT INTO mailboxes VALUES (?, ?, ?, ?)", [
                (tct.U_COS, tct.COS, "[]", (now - timedelta(days=1)).isoformat()),
                (tct.U_VINCE, tct.VINCE, "[]",
                 (now - DIRECTORY_MAX_AGE - timedelta(minutes=1)).isoformat()),
                (tct.U_OTHER, tct.OTHER, "[]", "undated")])
        users = mail_capture_directory(tmp.name, now=now)
        self.assertEqual([u[0] for u in users], [tct.U_COS])   # the recent one only
        # Every row past the bound: not loaded, so nothing is decided by it.
        self.assertIsNone(mail_capture_directory(tmp.name,
                                                 now=now + timedelta(days=30)))
        guard = PeopleExclusion([tct.U_OTHER])
        with mock.patch("integrations.people_exclusion.mail_capture_directory",
                        lambda state_dir: mail_capture_directory(
                            state_dir, now=now + timedelta(days=30))):
            held = with_directory(guard, tmp.name)
        self.assertFalse(held.directory_loaded)
        runtime = SimpleNamespace(exclusion_refusal=lambda: None, address_exclusion=lambda: None,
                                  people_exclusion=held)
        self.assertEqual(CaseRuntime._captured_original_exclusion(
            runtime, "cos_petrasoap_com", [tct.U_COS, tct.COS]), FAILED)


if __name__ == "__main__":
    unittest.main()
