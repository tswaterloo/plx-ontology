"""The live verifier and its helpers, shipped with the product.

``live_verify`` (the live-check runner), ``data_profile`` (the profile sanitiser it and the
live probes use), ``import_graph`` (a literal copy of ``tools/import_graph.py``, a test holds them equal) and
``gap_id``.
"""

from pathlib import Path as _Path

LIVE_VERIFY_ABSENT = "verify/live_verify.py is not in this copy of the code"


def live_verify_or_none():
    """``verify.live_verify``, or None in a copy of the code that leaves the live-check
    runner out (the public code push keeps it internal). Only the file's absence reads as
    None: where the file is present, an import error is raised, never hidden."""
    if not _Path(__file__).with_name("live_verify.py").is_file():
        return None
    from verify import live_verify
    return live_verify


def require_live_verify(test):
    """``verify.live_verify`` for a test that checks a live-check spec, or skip the test
    (``unittest.SkipTest``) in a copy of the code without the live-check runner."""
    live_verify = live_verify_or_none()
    if live_verify is None:
        test.skipTest(LIVE_VERIFY_ABSENT)
    return live_verify
