"""Write the continuous-ingest host config from a cycle-mode one (``operating.host serve``).

    python3 scripts/ingest_continuous_config.py --from COLD.json --out CONTINUOUS.json \\
        [--priority-mailbox ADDRESS ...]

Same sources, scopes, audiences and budgets as ``--from``; what changes is how they run
(``operating.ingest_forever.Continuous``): every source in its own loop, all at once
(``max_parallel`` one per source), each re-run ``min_interval_seconds`` after it ends
(:data:`CADENCE`), and the projection built from snapshots of the stores on its own thread
(``ingest.build_inputs`` ``snapshot``), so no capture ever waits for a build. One writer
per store still: each source is one capture process writing its own store, and the host
alone writes its ledger and the projection.

``--priority-mailbox`` (repeatable, in order): the mailboxes the mail capture reads first
(``integrations.capture_mail.MailPriority``); the mail scope is then written beside
``--out`` as ``<out stem>.mail-scope.json`` with that ``priority`` section. Addresses stay in
the deployment's own files, never in Git. Prints the paths written, nothing else.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Seconds between the end of one run of a source and the start of its next. Mail and
# SharePoint run back to back (each run resumes from its cursors); Teams re-reads every
# conversation's members each run (a run is about 400 s), so its member lists are never
# older than about 40 minutes; the rest follow their upstream's pace.
CADENCE = {"mail": 60, "sharepoint": 60, "teams": 1800, "bc": 1800, "portal": 7200,
           "mc": 3600, "sharepoint_lists": 3600, "meetings": 1800}


def continuous(document: dict, base: Path) -> dict:
    """The continuous config of a cycle-mode ``plx-host-config/v1`` document: paths made
    absolute against ``base`` (the source file's directory), every capture section given
    its cadence, ingest set to run every source at once and build from snapshots."""
    out = json.loads(json.dumps(document))
    for key in ("capture_audience", "mirror_audience"):
        if isinstance(out.get(key), str):
            out[key] = str((base / out[key]).resolve())
    for name, seconds in CADENCE.items():
        section = out.get(name)
        if not isinstance(section, dict):
            continue
        for key in ("scope", "live_audience", "registry", "audience"):
            if isinstance(section.get(key), str):
                section[key] = str((base / section[key]).resolve())
        section.setdefault("min_interval_seconds", seconds)
    ingest = dict(out.get("ingest") or {})
    configured = sum(1 for name in CADENCE if isinstance(out.get(name), dict))
    ingest.update({"mode": "capture_clis", "max_parallel": max(1, min(8, configured)),
                   "build_inputs": "snapshot"})
    ingest.setdefault("grace_seconds", 900)
    out["ingest"] = ingest
    out["note"] = ("Continuous ingest (scripts/ingest_continuous_config.py): every source in "
                   "its own loop at once, the projection built from store snapshots on its "
                   "own thread; written from a cycle-mode config. " + str(out.get("note", "")))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="source", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--priority-mailbox", action="append", default=[])
    args = parser.parse_args(argv)
    document = json.loads(args.source.read_text(encoding="utf-8"))
    out = continuous(document, args.source.resolve().parent)
    written = [args.out]
    mail = out.get("mail")
    if args.priority_mailbox:
        if not isinstance(mail, dict) or not mail.get("scope"):
            print(json.dumps({"error": "mail_scope_not_configured"}), file=sys.stderr)
            return 2
        scope = json.loads(Path(mail["scope"]).read_text(encoding="utf-8"))
        scope["priority"] = {**(scope.get("priority") or {}),
                             "mailboxes": list(dict.fromkeys(args.priority_mailbox))}
        target = args.out.with_name(args.out.stem + ".mail-scope.json")
        target.write_text(json.dumps(scope, indent=1) + "\n", encoding="utf-8")
        target.chmod(0o600)
        mail["scope"] = str(target.resolve())
        written.append(target)
    args.out.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    args.out.chmod(0o600)
    print(json.dumps({"written": [str(p) for p in written]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
