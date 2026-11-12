"""The one test-record rule: test and placeholder records are no business claim and stay out
of a business reader's default view. The proactive engine drops every claim about one
before any page reads it (``operating.proactive_engine.test_records``); the pages read the
same rules on what they label (``operator_api.test_records`` re-exports them).

What counts as one, and nothing else:

* **a name that says so**: a record whose own name carries the exact marker
  ``(DO NOT USE)``, or a record, supplier or customer whose name is exactly ``TEST`` or ends
  in `` TEST``, in capitals (``Laura TEST``); a real name with the word in it (``Test
  Kitchen Inc``, ``XYZ Lab Test``, ``Covid test``, ``... Materials Testing``) is not. A
  supplier or customer card marked ``(DO NOT USE)`` is a retired card (``Lorpon Labels (DO
  NOT USE)``, its orders migrated from FileMaker), not a test: an open order placed with it
  is still the business's order, and a late one is still late;
* **a test mark**: a record whose own label carries a test, UAT, demo or proof mark
  (``TEST_MARK``: ``INVOICETEST``, ``UAT-SH100034``, ``DEMO-``, ``pipeline-proof``,
  ``PLX-VAL-``, an ``E2E-`` run's record ...; a word such as "UAT-ready" is none). The
  proactive engine also reads it on the documents an order owns (its invoices, receipts,
  lines) and on an item's name (``V1 Demo 60 mL``);
* **the Portal's environment prefix**: a number issued by a prefix-setting Portal database
  (``UAT-SO100004``, ``UAT-AO-...``; ``ontology/schema/identifiers.json``,
  ``environment_prefixes``) is a staging record, not the business's;
* **a go-live first**: in a BC number series, the first number the series issued and the
  series' documents dated that same day, when that day is the BC go-live day the
  deployment states (``PLX_BC_GO_LIVE_DATE``, an ISO date; the proactive engine's
  ``series_firsts``). A series begun on any other day is ordinary business, lines or not:
  numbers alone never prove a test. Without a stated (or with an unreadable) day nothing is
  marked go-live (nothing is hidden on a guess);
* **the deployment's own list**: record numbers an owner names as tests in
  ``test-records.json`` in the app's data directory (``--app-data-dir``), or the file
  ``PLX_APP_TEST_RECORDS`` names: ``{"records": ["<number>", ...], "note": "..."}``. It
  lives with the deployment, never in the repository: a real case's number is data, not
  code. Matched on the record's label (its number or name) as shown, whole and case-folded.

Only names and numbers are judged (a record's label, its supplier's or customer's name),
never free text such as a summary or a reason: "the supplier says do not use lot 4" is
about a real order.

Hiding is a view, never a deletion: the record's own page, search and a question that
names it still open it; Home, the briefing and a profitability answer say how many test
records they left out ("2 test records hidden").
"""

from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Optional

# Marks of a test, proof, UAT or demo record in a document number, reference or line text:
# a UAT mark is a UAT-prefixed or -suffixed document number or a bare trailing UAT, never a
# word such as "UAT-ready"
TEST_MARK = re.compile(r"(?:\binvoice ?test|\btest ?invoice|(?:\b|-)uat(?:-[a-z0-9-]*\d|\s*$)|"
                       r"\bdemo-|[- ]demo\b|pipeline[- ]proof|\bo2c[- ]proof|\bbanktest|"
                       r"\bproof line\b|\bplx-val-|\btest ?po\b|\be2e[- ])", re.I)
NAME_RULE = re.compile(r"\(\s*DO\s+NOT\s+USE\s*\)", re.I)
# "TEST" exactly as the placeholder names write it, upper case only: a real "XYZ Lab Test"
# or "Covid test" stays
TEST_NAME = re.compile(r"(?:^|\s)TEST\s*$")
ENVIRONMENT_PREFIX = re.compile(r"^\s*UAT-", re.I)
LIST_FILE = "test-records.json"
GO_LIVE_ENV = "PLX_BC_GO_LIVE_DATE"
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def go_live_day(environ: Any = os.environ) -> Optional[date]:
    """The BC go-live day the deployment states (``PLX_BC_GO_LIVE_DATE``, ``YYYY-MM-DD``);
    None without one or with anything else (nothing is marked go-live on a guess)."""
    said = str((environ.get(GO_LIVE_ENV) if environ is not None else None) or "").strip()
    if not _ISO_DAY.match(said):
        return None
    try:
        return date.fromisoformat(said)
    except ValueError:
        return None


def listed(data_dir: Optional[Path], environ: Any = os.environ) -> frozenset[str]:
    """The deployment's named test records (case-folded labels); empty without a file or
    with one that is unreadable (nothing is hidden on a guess)."""
    named = environ.get("PLX_APP_TEST_RECORDS") if environ is not None else None
    path = Path(named) if named else (Path(data_dir) / LIST_FILE if data_dir else None)
    if path is None or not path.is_file():
        return frozenset()
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    records = got.get("records") if isinstance(got, dict) else None
    return frozenset(str(r).strip().casefold() for r in records or ()
                     if isinstance(r, (str, int)) and str(r).strip())


def is_test(texts: Iterable[Any], named: frozenset[str] = frozenset(),
            parties: Iterable[Any] = ()) -> bool:
    """Whether any of ``texts`` (a record's own label or number) or ``parties`` (its
    supplier's or customer's name; never free text) marks a test or placeholder record
    (module doc). A party's ``(DO NOT USE)`` marks a retired card, never a test."""
    for text, party in [*((t, False) for t in texts), *((p, True) for p in parties)]:
        if not isinstance(text, str) or not text.strip():
            continue
        if (not party and (NAME_RULE.search(text) or TEST_MARK.search(text))) \
                or TEST_NAME.search(text) or ENVIRONMENT_PREFIX.match(text):
            return True
        if text.strip().casefold() in named:
            return True
    return False
