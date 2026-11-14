"""Novel cross-system "find" and "connection" questions, drawn fresh from current data.

The tuned families ask a named path ("count the X of the Y of Z"): the question says
which links to walk. A person testing the Workbench asks what nobody wrote a path for:
"what do we have on Z outside Sage?", "what connects A and B?". This family draws such
questions from the records and links as they stand, with a seed (a night's date), so each
batch is new and untouched: it is built as held-out only, never looked at item by item,
and reported apart from the familiar families (``scripts.eval_novel_walks``).

Two cards, each over every declared link type of the records it touches (the question
names none of them):

* ``elsewhere``: a named record of one system; the truth is every record directly linked
  to it (any link type, either direction, identity links too) that lives in another
  system, each named, one to ``MAX_MEMBERS`` of them, but for a copy under the name the
  question already writes. Asked as "what do we have on ...
  outside <system>", "where else does ... show up", "find everything tied to ...". When
  those are too many or not all named, the same over one other system that holds one to
  ``MAX_MEMBERS`` named ones ("what do we have on Croda in Sage?", ``novel-elsewhere-in``).
* ``bridge``: two named records of different systems that no link joins directly; the
  truth is every record linked to both (any link types), each named, one to
  ``MAX_BRIDGES``, but for a copy of an end under its name. Asked as "what connects A and B?", "how are A and B related?".

A start or end must be named by its own label (``graph_walks.label_of``) and be the only
record of that label in its binding; a neighbourhood wider than ``graph_walks.MAX_SET``
on any link type, a member without a name, two members of one name, or any record a
person exclusion touches (``Records.cleared``) drops the draw; no start type takes more
than ``TYPE_SHARE`` of a batch (one well-named type would otherwise fill it) (fail closed: never a
partial truth). Items are ``plx-eval-gen/v1`` (``evals.generative``), graded by
``evals.generative.grade``: a set fact's members must each be named.

Evidence: isolated-fixture (evals/tests/test_novel_walks.py); a built batch is
generated-from-live-Postgres but its answers are graded only when run.
"""

from __future__ import annotations

import random
import re
from datetime import date
from typing import Any, Mapping, Sequence

from evals import graph_walks as gw
from evals.generative import ID_PROPS

CARDS = ("elsewhere", "bridge")
MAX_MEMBERS = 6
MAX_BRIDGES = 4
TRIES = 40   # draws per item before the build moves on
UNNAMED_AFTER = 12   # records of a type tried without one named before the type is skipped
TYPE_SHARE = 0.12    # of a batch's items at most, per start type (at least 2)


def _system(oid: str) -> str:
    return gw.system_of(gw.binding_of(oid))


def _said(oid: str) -> str:
    return gw.SYSTEM_WORDS.get(_system(oid), _system(oid))


class NovelComposer:
    """Draws ``elsewhere`` and ``bridge`` items over ``records`` (``graph_walks.Records``)."""

    def __init__(self, links: Sequence[gw.Link], records: gw.Records, as_of: date) -> None:
        self.walks = gw.Composer(links, records, as_of)
        self.records = records
        self.as_of = as_of
        self.tried: dict[str, tuple[int, int]] = {}   # type -> (starts tried, named)

    # ---- reads
    def neighbours(self, oid: str) -> set[str]:
        """Every record linked to ``oid`` over any declared link type, either direction;
        a link type reaching more than ``MAX_SET`` drops the draw (its truth would not be
        listed whole)."""
        out: set[str] = set()
        for hop in self.walks.by_type.get(gw.object_type(oid), []):
            got = self.walks.execute([oid], hop)
            if len(got) > gw.MAX_SET:
                raise gw.Dropped("fanout")
            out.update(got)
        out.discard(oid)
        return out

    def named(self, oid: str) -> str | None:
        found = gw.label_of(self.walks.rows([oid])[oid])
        return found[0] if found else None

    def unique(self, oid: str, label: str) -> bool:
        """Whether ``label`` names ``oid`` alone among its binding's records."""
        hits = self.records.lookup(gw.object_type(oid), label)
        return hits is not None and \
            [h for h in hits if gw.binding_of(h) == gw.binding_of(oid)] == [oid]

    def start(self, link_id: str, rng: random.Random) -> tuple[str, str]:
        """A named start among the link's sampled records: every sampled end is tried, and
        a type none of whose first ``UNNAMED_AFTER`` tried records carried a unique name is
        not tried again (lines, entries: records people do not name)."""
        sample = list(self.records.sample(link_id))
        if not sample:
            raise gw.Dropped("no_sample")
        rng.shuffle(sample)
        for pair in sample:
            ends = list(dict.fromkeys(pair))   # a fresh start is sampled as (oid, oid)
            rng.shuffle(ends)
            for oid in ends:
                kind = gw.object_type(oid)
                tried, named = self.tried.get(kind, (0, 0))
                if gw.mirror_type(kind) or (tried >= UNNAMED_AFTER and not named):
                    continue
                label = self.named(oid)
                ok = bool(label) and self.unique(oid, label)   # type: ignore[arg-type]
                self.tried[kind] = (tried + 1, named + ok)
                if ok:
                    return oid, label   # type: ignore[return-value]
        raise gw.Dropped("start_unnamed")

    def _members(self, ids: Sequence[str], cap: int, asked: Sequence[str]) -> dict[str, str]:
        """The members' names; a member of a name the question writes (a copy of an end
        under the same name) is left out: naming it again says nothing found."""
        names = {i: self.named(i) for i in ids}
        if any(n is None for n in names.values()):
            raise gw.Dropped("member_unnamed")
        said = {a.casefold() for a in asked}
        names = {i: n for i, n in names.items() if n.casefold() not in said}   # type: ignore[union-attr]
        if not 1 <= len(names) <= cap:
            raise gw.Dropped("size")
        if len({n.casefold() for n in names.values()}) != len(names):   # type: ignore[union-attr]
            raise gw.Dropped("member_ambiguous")
        return names   # type: ignore[return-value]

    def forms(self, oid: str, name: str, asked: Sequence[str] = ()) -> list[str]:
        """``name`` and the record's own identifiers (``generative.ID_PROPS``: its number or
        code, never a free-text name nor a key of another system's record such as an
        ``erp_code``): every way a reader may rightly name the member (dev 11-11: the Sage
        item named by its item code graded as missing). A form the question already writes
        (an end's own name) is never one: restating the question names nothing found."""
        out, rows = [name], self.walks.rows([oid]).get(oid) or []
        said = {a.casefold() for a in asked}
        for row in rows:
            value = re.sub(r"\s+", " ", str(row.get("value") or "")).strip()
            if row.get("property") in ID_PROPS and 3 <= len(value) <= 40 \
                    and value.casefold() not in {f.casefold() for f in out} \
                    and value.casefold() not in said and gw._one(rows, row["property"]):
                out.append(value)
        return out

    def _fact(self, fid: str, names: Mapping[str, str], asked: Sequence[str]
              ) -> dict[str, Any]:
        versions = self.records.versions(sorted(names))
        handles = [versions[i][0] for i in sorted(names) if i in versions]
        if len(handles) != len(names):
            raise gw.Dropped("no_version")
        allowed = sorted({s for h in handles if (s := gw.fact_system(h))})
        if len(allowed) != len({h.split("://", 1)[0] for h in handles}):
            raise gw.Dropped("handle_system_unknown")
        marks = gw.markings({"markings": versions[i][1]} for i in names)
        fact = {"id": fid, "kind": "set",
                "members": [self.forms(i, names[i], asked)
                            for i in sorted(names, key=names.get)],
                "records": handles, "allowed": allowed}
        if marks:
            fact["markings"] = sorted(marks)
        return fact

    def _ref(self, oid: str, label: str) -> str:
        return f"the {gw.noun(gw.object_type(oid))} {label} in {_said(oid)}"

    # ---- cards
    def elsewhere(self, oid: str, label: str, rng: random.Random) -> dict[str, Any]:
        """Every linked record of another system; when those are too many or not all
        named, every linked record of one other system that has one to ``MAX_MEMBERS``
        named ones ("what do we have on Croda in Sage?")."""
        home = _system(oid)
        others = sorted(i for i in self.neighbours(oid) if _system(i) != home)
        ref = f"the {gw.noun(gw.object_type(oid))} {label}"
        where = gw.SYSTEM_WORDS.get(home, home)
        try:
            names = self._members(others, MAX_MEMBERS, [label])
            question = rng.choice([
                f"What do we have on {ref} outside {where}?",
                f"Where else does {ref} show up, apart from {where}?",
                f"Find everything in our other systems that is tied to {ref} in {where}.",
                f"Is {ref} in {where} connected to anything in another system? What?"])
            card, reached = "novel-elsewhere", others
        except gw.Dropped as why:
            if str(why) not in ("size", "member_unnamed"):
                raise   # two neighbours of one name: no answer could tell them apart
            systems = sorted({_system(i) for i in others})
            rng.shuffle(systems)
            for there in systems:
                reached = [i for i in others if _system(i) == there]
                try:
                    names = self._members(reached, MAX_MEMBERS, [label])
                except gw.Dropped:
                    continue
                said = gw.SYSTEM_WORDS.get(there, there)
                question = rng.choice([
                    f"What do we have on {ref} in {said}?",
                    f"Does {ref} from {where} show up in {said}? Where?",
                    f"Find what {said} holds that is tied to {ref} in {where}."])
                card = "novel-elsewhere-in"
                break
            else:
                raise why
        if not self.records.cleared(sorted({oid, *reached})):
            raise gw.Dropped("people_excluded")
        return {"question": question, "facts": [self._fact("elsewhere", names, [label])],
                "card": card, "start": oid, "label": label,
                "systems": sorted({home, *(_system(i) for i in reached)}),
                "rule": "every record linked to the start, any link type, in another system"
                        + (" (the one named)" if card == "novel-elsewhere-in" else "")}

    def bridge(self, oid: str, label: str, rng: random.Random) -> dict[str, Any]:
        home = _system(oid)
        near = self.neighbours(oid)
        seconds: dict[str, None] = {}
        for mid in sorted(near):
            try:
                reach = self.neighbours(mid)
            except gw.Dropped:
                continue
            for far in sorted(reach - near - {oid}):
                if _system(far) != home and not gw.mirror_type(gw.object_type(far)):
                    seconds[far] = None
        candidates = list(seconds)
        rng.shuffle(candidates)
        for far in candidates[:8]:
            far_label = self.named(far)
            if not far_label or not self.unique(far, far_label):
                continue
            try:
                via = sorted(near & self.neighbours(far))
                names = self._members(via, MAX_BRIDGES, [label, far_label])
            except gw.Dropped:
                continue
            if not self.records.cleared(sorted({oid, far, *via})):
                raise gw.Dropped("people_excluded")
            a, b = self._ref(oid, label), self._ref(far, far_label)
            question = rng.choice([f"What connects {a} and {b}?",
                                   f"How are {a} and {b} related?",
                                   f"Is there a link between {a} and {b}? Through what?"])
            return {"question": question, "facts": [self._fact("bridge", names, [label, far_label])],
                    "card": "novel-bridge", "start": oid, "label": label,
                    "systems": sorted({home, _system(far), *(_system(i) for i in via)}),
                    "rule": "every record linked to both ends, any link types"}
        raise gw.Dropped("no_bridge")


def build(links: Sequence[gw.Link], records: gw.Records, *, items: int, seed: Any,
          as_of: date, now: str, split: str = "held",
          exclude: Sequence[str] = ()) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``items`` ``plx-eval-gen/v1`` items of ``split`` (half of each card, as drawn), over
    link types weighted as ``graph_walks.weights``, and the build's counts. A ``dev``
    batch (its own seed) is the one read item by item; a ``held`` batch never is, and
    starts no item of ``exclude`` (another batch's starts) starts."""
    if split not in ("dev", "held"):
        raise ValueError(f"split is dev or held, not {split!r}")
    from evals import generative as gen
    rng = random.Random(f"novel:{split}:{seed}")
    composer = NovelComposer(links, records, as_of)
    weights = gw.weights(links)
    kinds, shares = list(weights), list(weights.values())
    out: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    seen: set[str] = set(exclude)
    per_type: dict[str, int] = {}
    most = max(2, round(items * TYPE_SHARE))
    # Draws run until the batch holds ``items`` or ``items * TRIES`` draws are spent: a
    # slot whose draws all drop no longer leaves the batch short while draws remain (held1
    # 11-13: 32 of 60 built, 28 slots spent their 40 draws each and were skipped).
    # A slot whose card drew nothing in TRIES draws turns to the other card, so a card no
    # record can give never takes the whole budget.
    draws = items * TRIES
    while len(out) < items and draws > 0:
        tried = 0
        while draws > 0:
            card = CARDS[(len(out) + tried // TRIES) % len(CARDS)]
            draws, tried = draws - 1, tried + 1
            try:
                oid, label = composer.start(rng.choices(kinds, weights=shares)[0], rng)
                if oid in seen:
                    raise gw.Dropped("start_seen")
                if per_type.get(gw.object_type(oid), 0) >= most:
                    raise gw.Dropped("type_quota")
                drawn = getattr(composer, card)(oid, label, rng)
            except gw.Dropped as why:
                dropped[str(why)] = dropped.get(str(why), 0) + 1
                continue
            seen.add(oid)
            per_type[gw.object_type(oid)] = per_type.get(gw.object_type(oid), 0) + 1
            style = gen.pick_style(f"{seed}:{len(out)}")
            if card == "bridge" and style == "misspelled":
                style = "plain"   # a slip may fall in the second record's name
            text = gen.styled(drawn["question"], style, label, f"{seed}:{oid}")
            facts = drawn["facts"]
            out.append({
                "format": gen.FORMAT, "generation": "postgres", "as_of": as_of.isoformat(),
                "truth_as_of": now, "id": f"nv-{split}-{len(out) + 1:04d}", "split": split,
                "question": text, "category": drawn["card"], "difficulty": "hard",
                "style": style, "facts": facts,
                "citations": sorted({h for f in facts for h in f["records"]}),
                "markings": sorted({m for f in facts for m in f.get("markings", ())}),
                "hops": 1 if card == "elsewhere" else 2, "link_types": [],
                "rule": drawn["rule"], "start": oid, "start_type": gw.object_type(oid),
                "systems": drawn["systems"], "tags": ["novel", card], "sensitive": False,
                "set_digest": gw._hash("novel", split, seed, as_of)[:16]})
            break
    cards = {c: sum(1 for i in out if i["tags"][1] == c) for c in CARDS}
    return out, {"items": len(out), "cards": cards, "dropped": dropped, "seed": str(seed),
                 "split": split}
