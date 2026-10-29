"""Which captured case originals have a source gate, and which cards ask about a
document no captured source serves (owner decision 2026-10-27,
``devloop/DECISIONS/2026-10-27-case-originals-served.md``). Read-only, counts and ids only:

    python3 -m scripts.eval_case_originals_coverage --state-dir <state> [--cards-dir evals/dev]

Evaluation tooling (it reads the card sets); the product reads the store through
``operating.captured_originals``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence

from operating.captured_originals import ENTRY_ID, CapturedOriginals, SourceLookup


def card_dependencies(cards_dir: str | Path, states: Mapping[str, str], *,
                      watched: Iterable[str] = ()) -> dict[str, Any]:
    """Per card set, which cards ask about a document no source serves. A card citing a
    captured original is ``servable`` when one of them has a source gate, else
    ``unknown_acl`` (or ``not_held``: no receipt). A card citing no captured original is
    ``in_watched_source`` when its set's documents are mapped to a watched source folder
    (``watched``: the card files of the registry's crosswalk, served by the SharePoint
    capture), ``advisory_only`` when it cites an advisory record and nothing else holds
    the document it asks about, else ``no_document_named``."""
    from scripts.eval_case_cards import _cards, card_sets
    watched_cases = {Path(p).parent.name for p in watched}
    out: dict[str, Any] = {}
    for case, (doc, _sha) in card_sets(Path(cards_dir)).items():
        rows: dict[str, list[str]] = {}
        for card in _cards(doc):
            if not isinstance(card, Mapping) or not isinstance(card.get("id"), str):
                continue
            cited = [str(c) for k in ("cites", "originals", "live_sources")
                     for c in card.get(k) or []]
            originals = sorted({c for c in cited if ENTRY_ID.match(c)})
            if originals:
                found = [states.get(c, "not_held") for c in originals]
                state = ("servable" if any(s.startswith("servable") for s in found)
                         else "unknown_acl" if any(s.startswith("unknown_acl") for s in found)
                         else "not_held")
            elif case in watched_cases:
                state = "in_watched_source"
            elif any(c.startswith("advisory:") for c in cited):
                state = "advisory_only"
            else:
                state = "no_document_named"
            rows.setdefault(state, []).append(card["id"])
        out[case] = {state: {"cards": len(ids), "ids": ids} for state, ids in sorted(rows.items())}
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Coverage of captured case originals "
                                     "(counts and ids only, read-only).")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--sharepoint-dir", type=Path, default=None,
                        help="the SharePoint capture's state directory (default: state dir)")
    parser.add_argument("--cards-dir", type=Path, default=None)
    parser.add_argument("--registry", type=Path,
                        default=Path(__file__).resolve().parents[1] / "examples"
                        / "case_originals.json")
    args = parser.parse_args(argv)
    lookup = SourceLookup(args.state_dir, sharepoint_dir=args.sharepoint_dir or args.state_dir)
    store = CapturedOriginals(args.state_dir, gate=lambda unit, actor: False, lookup=lookup)
    report = store.coverage()
    states = report.pop("states")
    if args.cards_dir is not None:
        registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
        watched = [str(i.get("path")) for i in
                   ((registry.get("crosswalk") or {}).get("inputs") or [])
                   if isinstance(i, Mapping)]
        report["cards"] = card_dependencies(args.cards_dir, states, watched=watched)
    json.dump(report, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
