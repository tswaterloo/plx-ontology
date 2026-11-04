"""A document step's failures, one document at a time.

The document steps of a build (``mentions``, ``text_index``: ``operating/build_steps.py``)
read every captured document of a generation; one document whose text or derivation
raises (a character a codec refuses, a reading the parser cannot take) once failed the
whole generation after hours of projection (gen 8 attempt 1: one lone surrogate, 5.7 h).
A :class:`DocumentFailures` lets the step go on without that document: its links or index
fields are not written, and the step's report counts it (``documents_failed``, by error
class, never its text). The citations are logged (operators), never put in the report.

Fails closed where it is no longer one document: a database error or an exhausted memory
is the build's (re-raised as it is), and a step where more than ``MAX_SHARE`` of its
documents failed (beyond ``MIN_FAILED``) raises :class:`TooManyFailed` at its end (or as
soon as ``EARLY_FAILED`` failed with most of the documents seen failing): a defect of the
step's code, never a document's.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable, TypeVar

MAX_SHARE = 0.01
MIN_FAILED = 100
EARLY_FAILED = 1000
LOGGED = 20   # failures logged by citation per step run

log = logging.getLogger(__name__)
T = TypeVar("T")


class TooManyFailed(RuntimeError):
    """More documents failed than a document's own defect explains."""

    def __init__(self, step: str, failed: int, seen: int) -> None:
        super().__init__(f"document_step_failed: {step}: {failed} of {seen} documents")
        self.code, self.step, self.failed, self.seen = ("document_step_failed", step,
                                                        failed, seen)


class DocumentFailures:
    """The failed documents of one run of the document step ``step``."""

    def __init__(self, step: str) -> None:
        self.step = step
        self.seen = 0
        self.failed = 0
        self.errors: dict[str, int] = {}
        self._parts: set[str | None] = set()   # documents a failed part counted

    def run(self, citation: str | None, work: Callable[[], T], *, documents: int = 1,
            part: bool = False) -> tuple[bool, T | None]:
        """``(True, work())``, or ``(False, None)`` when it raised for this document (or
        ``documents`` of a group read together); a database or memory error, and an
        interrupt, are raised as they are. ``part``: a part of document ``citation``
        already seen (a field): its failure counts that document failed, once."""
        if not part:
            self.seen += documents
        try:
            return True, work()
        except (sqlite3.Error, MemoryError):
            raise
        except Exception as exc:  # noqa: BLE001 - one document's, recorded
            if part:
                if citation in self._parts:
                    return False, None
                self._parts.add(citation)
                documents = 1
            self.failed += documents
            name = type(exc).__name__
            self.errors[name] = self.errors.get(name, 0) + documents
            if self.failed <= LOGGED:
                log.warning("document step %s skipped %s: %s", self.step,
                            citation or "a group", name)
            if self.failed >= EARLY_FAILED and self.failed * 2 > self.seen:
                raise TooManyFailed(self.step, self.failed, self.seen) from exc
            return False, None

    def check(self) -> None:
        """Raise :class:`TooManyFailed` when the failures are the step's, not documents'."""
        if self.failed > max(MIN_FAILED, MAX_SHARE * self.seen):
            raise TooManyFailed(self.step, self.failed, self.seen)

    def report(self) -> dict[str, Any]:
        """``documents_failed`` and ``failed_errors`` (count per error class) for the step's
        report; empty when nothing failed (a report like before)."""
        self.check()
        if not self.failed:
            return {}
        return {"documents_failed": self.failed,
                "failed_errors": dict(sorted(self.errors.items()))}


__all__ = ["DocumentFailures", "TooManyFailed"]
