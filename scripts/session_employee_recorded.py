"""Live check ``session.employee_recorded``: counts from the console journal (see
``operator_ui.session_journal.employee_recorded``) over ``PLX_WORKBENCH_STATE_DIR``."""

from __future__ import annotations

import sys

from operator_ui.session_journal import main

if __name__ == "__main__":
    sys.exit(main(["check"]))
