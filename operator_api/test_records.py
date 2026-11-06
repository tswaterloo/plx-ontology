"""The pages' view of the one test-record rule (``operating.test_records``): what a page
labels is judged by the same rules the proactive engine drops claims by, never a second
copy; this module adds only the "N test records hidden" words."""

from __future__ import annotations

from typing import Optional

from operating.test_records import (  # noqa: F401 - the one rule, re-exported for the pages
    ENVIRONMENT_PREFIX, LIST_FILE, NAME_RULE, TEST_MARK, TEST_NAME, is_test, listed)


def hidden_words(count: int) -> Optional[str]:
    """"2 test records hidden" (None for none)."""
    if not count:
        return None
    return f"{count} test record{'s' if count != 1 else ''} hidden"
