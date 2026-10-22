"""``ReviewsIfPresent`` reads the review store and never writes it (evidence: isolated-fixture).

Every projection build and the MCP host read ``<state>/reviews.sqlite`` through it: a current
store is read as it is, a v1 store through an in-memory copy (the file keeps its format, bytes
and mtime; only the writer's own open upgrades it), even one the process may not write; no
file reads as no reviews. A file that is there but cannot be read (locked past the retries, a
hot journal, no review store) is tried a bounded number of times and never written: a build
fails with ``ReviewStoreUnreadable`` rather than serve a generation without its decisions (a
rejected link back as a plain lead), and the MCP readers read it as no reviews (their answers
then carry a ``review_store_unreadable`` gap, ``tests/test_host_followups.py``). A lock released
within the retries reads the decisions. Nothing here depends on the clock or on the user: a
read-only file or directory is checked by the opens themselves as well as by the bytes, so a
run as root (which 0444 does not stop) proves the same.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
import shutil
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ontology import reviews
from operating import build_steps
from tests import test_host_followups as hf
from tests.test_review_allocation import LINK, MAIL, MAIL_HANDLE, P1, SO1, v1_reviews

AUTHOR = {"principal": "employee:x"}
CITED = [{"object_id": MAIL, "l1_handle": MAIL_HANDLE, "digest": "0" * 64}]
MENTION = hf.mail("M-1", f"Shipping {hf.PO_NUMBER}")   # a lead link to the PO over [T1, T2)
EMAIL = 'Email:graph_mail.messages:{"id":"M-1","mailbox":"orders@plx.test"}'


class Builder:
    """The part of a ``StoreBuilder`` the ``reviews`` step reads: a generation holding no
    object (a decision's ends are not projected)."""

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE TABLE objects (object_id TEXT, object_type TEXT)")


def record_reject(state: str | Path) -> None:
    """A person rejects the mail's mention link to the PO."""
    reviews.ReviewStore(reviews.store_path(state), clock=lambda: dt.datetime(
        2026, 9, 6, tzinfo=dt.timezone.utc)).record(
        tenant="plx", author=AUTHOR, decision="reject_link", subject=EMAIL, target=hf.PO_ID,
        link_type="Email.mentions_purchase_order",
        evidence=[{"object_id": EMAIL, "l1_handle": MENTION.l1_handle,
                   "digest": MENTION.digest, "field_path": "row.subject"}])


def reviews_step(state: str | Path) -> dict:
    return build_steps.post_project(state, "plx")["reviews"](Builder(), None)


def snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    """Every file in ``directory``: its bytes and mtime (a journal or WAL file would show)."""
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(directory.iterdir())}


def lock(path: Path) -> sqlite3.Connection:
    """A writer holding ``path``'s exclusive lock until it rolls back."""
    writer = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    writer.execute("BEGIN EXCLUSIVE")
    return writer


class ReviewsIfPresentTests(unittest.TestCase):
    def read_all(self, path: Path) -> tuple:
        self.store = store = build_steps.ReviewsIfPresent(path)
        return (store.link_history("plx"), store.alias_history("plx", None),
                store.kind_history("plx"), store.history("plx", P1))

    def test_reads_a_current_or_v1_store_without_writing_it(self):
        """A read-only file or directory (0444 / 0555; as root nothing is refused, so every
        open is checked to be read-only, ``mode=ro``) is read and left as it was."""
        clock = lambda: dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)  # noqa: E731
        for (name, module), locked in itertools.product(
                (("current", reviews), ("v1", v1_reviews())), ("file", "directory")):
            label = f"{name}, read-only {locked}"
            with tempfile.TemporaryDirectory() as tmp:
                path = reviews.store_path(tmp)
                writer = module.ReviewStore(path, clock=clock)
                link, _ = writer.record(tenant="plx", author=AUTHOR, decision="accept_link",
                                        subject=P1, target=SO1, link_type=LINK, evidence=CITED)
                os.chmod(path, 0o444)
                os.chmod(path.parent, 0o555 if locked == "directory" else 0o755)
                before = snapshot(path.parent)
                try:
                    with mock.patch.object(build_steps.sqlite3, "connect",
                                           wraps=sqlite3.connect) as connect:
                        links, aliases, kinds, history = self.read_all(path)
                finally:
                    os.chmod(path.parent, 0o755)   # the temporary directory is removable
                opened = [str(c.args[0]) for c in connect.call_args_list
                          if c.args[0] != ":memory:"]
                self.assertTrue(opened and all(o.endswith("?mode=ro") for o in opened),
                                (label, opened))
                self.assertEqual([(r["review_id"], r["decision"]) for r in links],
                                 [(link["review_id"], "accept_link")], label)
                self.assertEqual(([r["review_id"] for r in history], aliases, kinds),
                                 ([link["review_id"]], [], []), label)
                self.assertEqual(snapshot(path.parent), before, label)
                with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                    columns = {row[1] for row in db.execute("PRAGMA table_info(reviews)")}
                self.assertEqual("quantity" in columns, name == "current", label)

    def test_a_file_that_is_no_review_store_is_tried_bounded_and_never_written(self):
        def other_table(path: Path) -> None:
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE notes (body TEXT)")

        makers = {"not_a_database": lambda p: p.write_bytes(b"not a database\n" * 256),
                  "empty_file": lambda p: p.write_bytes(b""),
                  "no_review_table": other_table}
        for name, make in makers.items():
            with tempfile.TemporaryDirectory() as tmp:
                path = reviews.store_path(tmp)
                path.parent.mkdir(parents=True, exist_ok=True)
                make(path)
                os.chmod(path, 0o444)
                before = snapshot(path.parent)
                for strict in (False, True):
                    store = build_steps.ReviewsIfPresent(path, strict=strict)
                    with mock.patch.object(build_steps.sqlite3, "connect",
                                           wraps=sqlite3.connect) as connect:
                        if strict:
                            with self.assertRaises(build_steps.ReviewStoreUnreadable) as raised:
                                store.link_history("plx")
                            self.assertEqual(raised.exception.code, "review_store_unreadable")
                        else:
                            self.assertEqual(store.link_history("plx"), [], name)
                    opened = [c for c in connect.call_args_list
                              if "mode=ro" in str(c.args[0])]
                    self.assertEqual(len(opened), build_steps._READ_ATTEMPTS, (name, strict))
                self.assertEqual(snapshot(path.parent), before, name)
                with self.assertRaises(reviews.ReviewError):
                    build_steps.ReviewsIfPresent(path).record(
                        tenant="plx", author=AUTHOR, decision="accept_link", subject=P1,
                        target=SO1, link_type=LINK, evidence=CITED)


class BuildReviewsTests(unittest.TestCase):
    """A build over a state directory: only a missing review store means no reviews; one it
    cannot read fails the build, never a generation where a rejected link is a plain lead."""

    def build(self, state: Path) -> list[tuple[str, str]]:
        """The tiers of the mail's links to the PO in a build over ``state``."""
        conn = hf.BuildStepTests.build(self, state, [MENTION])
        found = sorted(conn.execute(
            "SELECT tier, link_type FROM links WHERE from_object = ? AND to_object = ?",
            (EMAIL, hf.PO_ID)))
        conn.close()
        (state / "projection.sqlite").unlink()
        return found

    def test_an_unreadable_store_fails_the_build_and_a_missing_one_is_none(self):
        lead = ("lead", "Email.mentions_purchase_order")
        contradicted = ("contradicted", "Email.mentions_purchase_order")
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertEqual(self.build(state), [lead])
            self.assertEqual(os.listdir(state), [])
            record_reject(state)
            self.assertEqual(self.build(state), [contradicted, lead])

        def other_table(path: Path) -> None:
            path.unlink()
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE notes (body TEXT)")

        makers = {"not_a_database": lambda p: p.write_bytes(b"not a database\n" * 256),
                  "no_review_table": other_table, "locked": lambda p: self.addCleanup(
                      lock(p).close)}
        for name, make in makers.items():
            with tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                record_reject(state)
                make(reviews.store_path(state))
                before = snapshot(state)
                with mock.patch.object(build_steps, "_BUSY_TIMEOUT", 0.01), \
                        self.assertRaises(build_steps.ReviewStoreUnreadable, msg=name):
                    hf.BuildStepTests.build(self, state, [MENTION])
                self.assertFalse((state / "projection.sqlite").exists(), name)
                self.assertEqual(snapshot(state), before, name)

    def test_a_lock_released_within_the_retries_reads_the_decisions(self):
        """The writer holds the lock through the first attempt (it fails on the busy
        timeout) and releases it before the second: the build reads the rejection. The
        release is ordered by the attempts, never by the clock."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            record_reject(state)
            writer = lock(reviews.store_path(state))
            self.addCleanup(writer.close)
            attempts = []
            read_only = build_steps.ReviewsIfPresent._read_only

            def attempt(store):
                attempts.append(writer.in_transaction)
                if len(attempts) == 2:
                    writer.execute("ROLLBACK")
                return read_only(store)
            with mock.patch.object(build_steps, "_BUSY_TIMEOUT", 0.01), \
                    mock.patch.object(build_steps.ReviewsIfPresent, "_read_only", autospec=True,
                                      side_effect=attempt):
                tiers = self.build(state)
            self.assertIn(("contradicted", "Email.mentions_purchase_order"), tiers)
            self.assertEqual(attempts[:2], [True, True])

    def test_a_hot_journal_fails_the_build_and_is_left_as_it_is(self):
        with tempfile.TemporaryDirectory() as live, tempfile.TemporaryDirectory() as crashed:
            reviews.ReviewStore(reviews.store_path(live)).record(
                tenant="plx", author=AUTHOR, decision="accept_link", subject=P1, target=SO1,
                link_type=LINK, evidence=CITED)
            path = reviews.store_path(live)
            writer = sqlite3.connect(path, isolation_level=None)
            self.addCleanup(writer.close)
            writer.execute("CREATE TABLE filler (body TEXT)")
            writer.executemany("INSERT INTO filler VALUES (?)", [("x" * 500,)] * 200)
            writer.execute("PRAGMA cache_size = 1")   # spill: the journal header is synced
            writer.execute("BEGIN")
            writer.execute("UPDATE filler SET body = 'y' || body")
            for name in os.listdir(path.parent):   # the files a crash mid-write leaves
                shutil.copy(path.parent / name, Path(crashed) / name)
            writer.execute("ROLLBACK")
            before = snapshot(Path(crashed))
            self.assertIn(path.name + "-journal", before)
            with self.assertRaises(build_steps.ReviewStoreUnreadable):
                reviews_step(crashed)
            self.assertEqual(snapshot(Path(crashed)), before)
            self.assertEqual(reviews_step(live)["counts"]["decisions"], 1)


if __name__ == "__main__":
    unittest.main()
