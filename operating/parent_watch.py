"""``ontology.parent_watch`` under its earlier name (the helper lives in the lowest layer,
so every package's worker pools can use it)."""

from ontology.parent_watch import (EXIT_CODE, POLL_SECONDS, die_with, gone,  # noqa: F401
                                   pool_init, spawned_parent)
