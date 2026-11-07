"""The link-coverage family: hard questions for every link kind of the served schema.

The graph-walk family (``evals.graph_walks``, F31) draws random walks in proportion to the
link counts, so a link kind a schema migration adds can go untested. This family asks, for
**every** link type of the schema (relation mirrors left out), one question per template
(``TEMPLATES``), mixing easy and hard:

* ``lookup`` (easy): the records one hop away, by name ("Which suppliers ... of PO X?");
* ``count`` (easy): how many records one hop away;
* ``extreme`` (medium): the latest or earliest date (else the highest or lowest amount)
  over the records one hop away, and which record holds it;
* ``twin`` (medium): an identity link (``same_record_as``, ``bc_posted_as``) asks which
  record of the other system is the same record; any other link starts from the start
  record's twin in another system and crosses over before the hop;
* ``path`` (hard): two or three hops, the later ones leaving the start's system where the
  links allow, answered as a count or a list (or "none" when the walk ends empty);
* ``without`` (hard): which (or how many) of the records one hop away have no link of
  another kind ("Which of the PO lines of PO X have no receiving line?").

A record people do not name (a line, an entry) is reached from the named record it belongs
to, so its question starts one hop earlier. Every answer is the template's own execution
over **all** current firm links of the records (``Records`` from ``evals.graph_walks``), with
the evidence records' handles; a template the records cannot settle (an unnamed or
ambiguous start, a set too large, a tie, values that disagree, a person the exclusion
excludes) is dropped with its reason. Items are ``plx-eval-item/v1`` rubric items of family
``F32`` with live-ops keys (suite ``graph``, so ``scripts.eval_live_ops`` runs and rescores
them unchanged); each key's ``strata`` carries the link kind, the template and its
difficulty, which ``grade_report`` reports by. Pure: reads no source, clock or environment.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import hashlib
import json
import random
import re
from typing import Any, Iterable, Mapping, Sequence

from evals import graph_walks as gw

FAMILY = "F32"
SUITE = gw.SUITE
KEY_FAMILY = "link_coverage"
ORACLE_ID = "oracle:link-coverage"
CARD_PREFIX = "link-"
DEV_SHARE = 0.3
# template -> difficulty, in the order they are tried for each link kind
TEMPLATES = {"lookup": "easy", "count": "easy", "extreme": "medium", "twin": "medium",
             "path": "hard", "without": "hard"}
DIFFICULTIES = ("easy", "medium", "hard")
TARGET_PER_LINK = 3          # the fewest items a link kind with firm links should get
TRIES = 8                    # sampled links tried per template before it is dropped
# a link kind under TARGET_PER_LINK after one round asks these again (other starts)
TOP_UP = ("count", "path", "extreme", "lookup", "without")
TOP_UP_ROUNDS = 2
MAX_SET = gw.MAX_SET
MAX_LIST = gw.MAX_LIST
GRADES = ("correct", "incomplete", "wrong", "abstain")


@dataclass
class Question:
    text: str
    facts: list[dict[str, Any]]
    link: str
    template: str
    answer: str
    hops: list[str]
    systems: str
    start: str
    label: str
    reads: list[str]
    rule: str
    touched: list[str] = field(default_factory=list)

    @property
    def difficulty(self) -> str:
        return TEMPLATES[self.template]


def link_types(raw: Mapping[str, Mapping[str, Any]], counts: Mapping[str, int] | None = None
               ) -> list[gw.Link]:
    """The schema's link types (``load_schema().link_types`` shape), relation mirrors left
    out, each with its count of current firm links when ``counts`` has it."""
    out = []
    for link_id, link in sorted(raw.items()):
        if gw.mirror_type(link["from"]) or gw.mirror_type(link["to"]):
            continue
        out.append(gw.Link(link_id, link["name"], link.get("reverse_name") or link["name"],
                           link["from"], link["to"], int((counts or {}).get(link_id, 0))))
    return out


class Generator:
    """Composes each template's question for one link kind (``for_link``)."""

    def __init__(self, links: Sequence[gw.Link], records: gw.Records, as_of: date) -> None:
        # every link kind may be walked, whatever its census count: a sample decides
        self.links = {l.id: l for l in links}
        self.records = records
        self.as_of = as_of
        self.walker = gw.Composer([gw.Link(l.id, l.name, l.reverse_name, l.source, l.target,
                                           max(l.count, 1)) for l in links], records, as_of)
        self.by_type = self.walker.by_type

    # ---- reads
    def rows(self, ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        return self.walker.rows(ids)

    def step(self, ids: Sequence[str], hop: gw.Hop) -> list[str]:
        return self.walker.execute(ids, hop)

    def label(self, oid: str) -> tuple[str, str] | None:
        return gw.label_of(self.rows([oid])[oid])

    def unique(self, oid: str) -> tuple[str, bool] | None:
        """(the start's name, whether the name also answers in another system) when the
        name picks out exactly this record in its own system."""
        named = self.label(oid)
        if not named:
            return None
        hits = self.records.lookup(gw.object_type(oid), named[0])
        if hits is None:
            return None
        same = [h for h in hits if gw.binding_of(h) == gw.binding_of(oid)]
        if same != [oid]:
            return None
        return named[0], len(hits) > 1

    # ---- starts
    def anchors(self, link: gw.Link, rng: random.Random
                ) -> Iterable[tuple[str, str, bool, list[gw.Hop]]]:
        """(start, its name, say its system, the hops to the link's far end) for sampled
        links of ``link``: a named end, else the named record an end belongs to."""
        sample = list(self.records.sample(link.id))
        rng.shuffle(sample)
        for a, b in sample[:TRIES]:
            ends = [(a, "out"), (b, "in")]
            rng.shuffle(ends)
            found = None
            for oid, direction in ends:
                got = self.unique(oid)
                if got:
                    found = (oid, got, [gw.Hop(link, direction)])
                    break
            if found is None:
                for oid, direction in ends:
                    parents = [h for h in self.by_type.get(gw.object_type(oid), [])
                               if h.direction == "out" and not h.link.identity
                               and h.link.id != link.id]
                    for hop in sorted(parents, key=lambda h: h.link.id)[:4]:
                        for parent in self.step([oid], hop)[:2]:
                            got = self.unique(parent)
                            if got:
                                found = (parent, got, [gw.Hop(hop.link, "in"),
                                                       gw.Hop(link, direction)])
                                break
                        if found:
                            break
                    if found:
                        break
            if found:
                oid, (label, elsewhere), path = found
                yield oid, label, elsewhere or rng.random() < 0.5, path

    # ---- phrasing
    def ref(self, start: str, label: str, say_system: bool) -> str:
        kind = gw.object_type(start)
        word = gw.SHORT.get(kind) if re.search(r"\d", label) else None
        out = f"{word or gw.noun(kind)} {label}"
        system = gw.SYSTEM_WORDS.get(gw.system_of(gw.binding_of(start)), "")
        system = system.removeprefix("the ")
        if say_system and system and not out.startswith(system):
            out = f"the {system} {out}"
        return out

    def chain(self, ref: str, path: Sequence[gw.Hop], sets: Sequence[Sequence[str]]
              ) -> tuple[str, list[str]]:
        """("the B of the A of <ref>", the noun of each hop)."""
        nouns = [self.walker._hop_noun(h, sets[i + 1] or sets[i]) for i, h in enumerate(path)]
        return self.owner(ref, nouns, len(nouns)), nouns

    @staticmethod
    def owner(ref: str, nouns: Sequence[str], upto: int) -> str:
        """The chain of the first ``upto`` hops ("the A of <ref>"; ``ref`` for none)."""
        out = ref
        for n in nouns[:upto]:
            out = f"the {n} of {out}"
        return out

    def evidence(self, ids: Sequence[str]) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
        versions = self.records.versions(sorted(ids))
        handles = [versions[i][0] for i in ids if i in versions]
        allowed = sorted({s for h in handles if (s := gw.fact_system(h))})
        if len(allowed) != len({h.split("://", 1)[0] for h in handles}) or \
                (ids and not allowed):
            raise gw.Dropped("handle_system_unknown")
        marks = gw.markings({"markings": versions[i][1]} for i in ids if i in versions)
        return {"records": handles[:60], "allowed": allowed, **gw._marks(marks)}, versions

    def systems(self, start: str, sets: Sequence[Sequence[str]]) -> str:
        reached = next((s for s in reversed(sets) if s), [start])
        ends = sorted({gw.system_of(gw.binding_of(i)) for i in reached})
        start_system = gw.system_of(gw.binding_of(start))
        return "~".join(sorted({start_system, ends[0] if len(ends) == 1 else "+".join(ends)}))

    def run_path(self, start: str, path: Sequence[gw.Hop]) -> list[list[str]]:
        sets = [[start]]
        for hop in path:
            got = self.step(sets[-1], hop) if sets[-1] else []
            if len(got) > MAX_SET:
                raise gw.Dropped("fanout")
            sets.append(got)
        return sets

    def answer_set(self, final: Sequence[str], nouns: Sequence[str], target: str,
                   prefer_list: bool) -> tuple[str, list[dict[str, Any]]]:
        """A list of the final records' names when they are few, named and distinct (or
        asked for), else their count; "none" when empty."""
        labels = list(dict.fromkeys(l for l in [nouns[-1], gw.plural(gw.noun(target)),
                                                nouns[-1].split()[-1]] if l))
        if not final:
            return "none", []
        base, _v = self.evidence(final)
        names = [self.label(i) for i in final]
        if prefer_list and len(final) <= MAX_LIST and all(names) and \
                len({n[0].casefold() for n in names}) == len(names):   # type: ignore[index]
            return "list", [{"id": "list", "kind": "set",
                             "members": [[n[0]] for n in sorted(names)], **base}]  # type: ignore[index]
        return "count", [{"id": "count", "kind": "number", "value": str(len(final)),
                          "label": labels, **base}]

    def none_fact(self, start: str, what: str) -> list[dict[str, Any]]:
        base, _v = self.evidence([start])
        return [{"id": "none", "kind": "absent", "value": f"no {what}",
                 "accept": [f"no {what}", *gw.NONE_FORMS], "records": base["records"],
                 "allowed": base["allowed"]}]

    # ---- templates
    def for_link(self, link_id: str, rng: random.Random
                 ) -> tuple[list[Question], Counter]:
        link = self.links[link_id]
        made: list[Question] = []
        dropped: Counter = Counter()
        if not self.records.sample(link_id):
            dropped["no_firm_links"] += 1
            return made, dropped
        rounds = [list(TEMPLATES)] + [list(TOP_UP)] * TOP_UP_ROUNDS
        for n, templates in enumerate(rounds):
            if n and len(made) >= TARGET_PER_LINK:
                break
            self._round(link, templates, made, dropped, rng, n)
        return made, dropped

    def _round(self, link: gw.Link, templates: Sequence[str], made: list[Question],
               dropped: Counter, rng: random.Random, n: int) -> None:
        for template in templates:
            if n and len(made) >= TARGET_PER_LINK:
                return
            if template == "twin" and not link.identity and not any(
                    h.link.identity for t in (link.source, link.target)
                    for h in self.by_type.get(t, [])):
                dropped["twin:no_identity_link"] += 1
                continue
            last = "no_named_start"
            for start, label, say, path in self.anchors(link, rng):
                try:
                    q = getattr(self, f"_{template}")(link, start, label, say, path, rng)
                except gw.Dropped as why:
                    last = str(why)
                    continue
                if not self.records.cleared(sorted(set(q.touched) | {q.start})):
                    last = "people_excluded"
                    continue
                if any(q.text == m.text or (q.template, q.start, q.hops) ==
                       (m.template, m.start, m.hops) for m in made):
                    last = "duplicate"
                    continue
                made.append(q)
                break
            else:
                if not n:   # a top-up round's misses are not the template's reasons
                    dropped[f"{template}:{last}"] += 1

    def _q(self, link: gw.Link, template: str, body: str, facts: list[dict[str, Any]],
           answer: str, path: Sequence[gw.Hop], start: str, label: str,
           sets: Sequence[Sequence[str]], rule: str) -> Question:
        text = f"As of {gw.long_day(self.as_of)}, {body[0].lower() + body[1:] if not body.startswith(('PO ', 'AO ')) else body}"
        return Question(text, facts, link.id, template, answer, [h.link.id for h in path],
                        self.systems(start, sets), start, label,
                        [f"{h.link.id} ({h.direction})" for h in path], rule,
                        sorted({i for s in sets for i in s}))

    def _lookup(self, link, start, label, say, path, rng) -> Question:
        sets = self.run_path(start, path)
        final = sets[-1]
        if not final:
            raise gw.Dropped("empty")
        inner, nouns = self.chain(self.ref(start, label, say), path, sets)
        answer, facts = self.answer_set(final, nouns, path[-1].target, True)
        if answer != "list":
            raise gw.Dropped("unnamed_records")
        body = rng.choice([f"Name {inner}.", f"List {inner} by name or number."])
        return self._q(link, "lookup", body, facts, answer, path, start, label, sets,
                       "the names of the records the hop reaches")

    def _count(self, link, start, label, say, path, rng) -> Question:
        sets = self.run_path(start, path)
        inner, nouns = self.chain(self.ref(start, label, say), path, sets)
        body = rng.choice([f"How many {nouns[-1]} are there for "
                           f"{self.owner(self.ref(start, label, say), nouns, len(nouns) - 1)}?",
                           f"Count {inner}."])
        if not sets[-1]:
            return self._q(link, "count", body, self.none_fact(start, nouns[-1]), "none",
                           path, start, label, sets, "the hop reaches no record")
        answer, facts = self.answer_set(sets[-1], nouns, path[-1].target, False)
        return self._q(link, "count", body, facts, answer, path, start, label, sets,
                       "count of distinct records the hop reaches")

    def _extreme(self, link, start, label, say, path, rng) -> Question:
        sets = self.run_path(start, path)
        final = sets[-1]
        if not final:
            raise gw.Dropped("empty")
        rows = self.rows(final)
        inner, _nouns = self.chain(self.ref(start, label, say), path, sets)
        base, versions = self.evidence(final)
        dated = self.walker._dated(final, rows)
        if dated:
            prop, days, marks = rng.choice(dated)
            pick = rng.choice([max, min])
            top = pick(days.values())
            winners = [i for i, d in days.items() if d == top]
            word = "latest" if pick is max else "earliest"
            facts = [{"id": word, "kind": "date", "value": top.isoformat(),
                      "records": [versions[i][0] for i in winners if i in versions],
                      "allowed": base["allowed"], **gw._marks(marks)}]
            named = self.label(winners[0]) if len(winners) == 1 else None
            if len(final) > 1 and named:
                facts.append({"id": f"{word}_record", "kind": "text", "value": named[0],
                              "records": facts[0]["records"], "allowed": base["allowed"]})
            body = f"Among {inner}, which has the {word} {gw.words(prop)}, and when?" \
                if len(final) > 1 else f"What is the {gw.words(prop)} of {inner}?"
            return self._q(link, "extreme", body, facts, word, path, start, label, sets,
                           f"{pick.__name__} of {prop} over the records the hop reaches")
        numeric = self.walker._numeric(final, rows)
        if not numeric:
            raise gw.Dropped("no_date_or_amount")
        prop, values, marks = rng.choice(numeric)
        pick = rng.choice([max, min])
        got = pick(values.values())
        word = "highest" if pick is max else "lowest"
        facts = [{"id": word, "kind": "number", "value": gw.fmt(got),
                  "label": [gw.words(prop), prop.split("_")[-1]], "tolerance_pct": 1,
                  **base, **gw._marks(marks | set(base.get("markings", ())))}]
        body = f"What is the {word} {gw.words(prop)} across {inner}?" if len(final) > 1 \
            else f"What is the {gw.words(prop)} of {inner}?"
        return self._q(link, "extreme", body, facts, word, path, start, label, sets,
                       f"{pick.__name__} of {prop} over the records the hop reaches")

    def _twin(self, link, start, label, say, path, rng) -> Question:
        if link.identity:
            if len(path) != 1:
                raise gw.Dropped("twin_unnamed")
            sets = self.run_path(start, path)
            if not sets[-1]:
                raise gw.Dropped("empty")
            other = sorted({gw.SYSTEM_WORDS.get(gw.system_of(gw.binding_of(i)), "?")
                            for i in sets[-1]})
            answer, facts = self.answer_set(sets[-1], [gw.noun(path[-1].target)],
                                            path[-1].target, True)
            if answer != "list":
                raise gw.Dropped("unnamed_records")
            body = (f"Which {gw.noun(path[-1].target)} in {' and '.join(other)} is the same "
                    f"record as {self.ref(start, label, True)}? Give its number or name.")
            return self._q(link, "twin", body, facts, "twin", path, start, label, sets,
                           f"the records {link.id} ties to the start")
        # cross over: name the start by its twin in another system
        hops = [h for h in self.by_type.get(gw.object_type(start), []) if h.link.identity]
        for hop in sorted(hops, key=lambda h: h.link.id):
            for twin in self.step([start], hop)[:3]:
                if gw.system_of(gw.binding_of(twin)) == gw.system_of(gw.binding_of(start)):
                    continue
                got = self.unique(twin)
                if not got or self.step([twin], gw.Hop(hop.link, "in" if hop.direction ==
                                                       "out" else "out")) != [start]:
                    continue
                back = gw.Hop(hop.link, "in" if hop.direction == "out" else "out")
                full = [back, *path]
                sets = self.run_path(twin, full)
                if not sets[-1]:
                    continue
                system = gw.SYSTEM_WORDS[gw.system_of(gw.binding_of(start))]
                _inner, nouns = self.chain("", path, sets[1:])
                answer, facts = self.answer_set(sets[-1], nouns, path[-1].target, False)
                body = (f"{self.ref(twin, got[0], True)} is the same record as a "
                        f"{gw.noun(gw.object_type(start))} in {system}. How many "
                        f"{nouns[-1]} does that record have?")
                return self._q(link, "twin", body, facts, answer, full, twin, got[0], sets,
                               "cross the identity link, then count the hop")
        raise gw.Dropped("no_named_twin")

    def _path(self, link, start, label, say, path, rng) -> Question:
        path = list(path)
        sets = self.run_path(start, path)
        if not sets[-1]:
            raise gw.Dropped("empty")
        home = gw.system_of(gw.binding_of(start))
        want = len(path) + rng.choice([1, 1, 2])
        while len(path) < min(want, 3):
            options = [h for h in self.by_type.get(path[-1].target, [])
                       if not (h.link.id == path[-1].link.id and h.direction != path[-1].direction)]
            rng.shuffle(options)
            # leave the start's system first, where the links allow it
            options.sort(key=lambda h: gw.system_of(gw.binding_of(
                h.link.target if h.direction == "out" else h.link.source)) == home)
            for hop in options[:10]:
                try:
                    got = self.step(sets[-1], hop)
                except Exception:   # noqa: BLE001 - a store that cannot read the hop
                    continue
                if got and len(got) <= MAX_SET:
                    path.append(hop)
                    sets.append(got)
                    break
            else:
                break
        if len(path) < 2:
            raise gw.Dropped("no_second_hop")
        inner, nouns = self.chain(self.ref(start, label, say), path, sets)
        answer, facts = self.answer_set(sets[-1], nouns, path[-1].target, rng.random() < 0.5)
        body = f"List {inner}." if answer == "list" else \
            f"How many {nouns[-1]} are there for " \
            f"{self.owner(self.ref(start, label, say), nouns, len(nouns) - 1)}?"
        return self._q(link, "path", body, facts, answer, path, start, label, sets,
                       "the records the walk reaches, every hop over all links")

    def _without(self, link, start, label, say, path, rng) -> Question:
        sets = self.run_path(start, path)
        members = sets[-1]
        if len(members) < 2:
            raise gw.Dropped("too_few_to_split")
        last = path[-1]
        options = [h for h in self.by_type.get(last.target, []) if not h.link.identity
                   and h.link.id != last.link.id]
        rng.shuffle(options)
        for hop in options[:12]:
            pairs = self.records.step(sorted(members), hop.link.id, hop.direction)
            having = {a for a, _b in pairs}
            lacking = [m for m in members if m not in having]
            if not lacking or len(lacking) == len(members):
                continue
            inner, nouns = self.chain(self.ref(start, label, say), path, sets)
            other = self.walker._hop_noun(hop, sorted({b for _a, b in pairs}))
            answer, facts = self.answer_set(lacking, nouns, last.target, True)
            body = f"Which of {inner} have no {other}?" if answer == "list" else \
                f"How many of {inner} have no {other}?"
            q = self._q(link, "without", body, facts, answer, [*path, hop], start, label,
                        sets, f"records the hop reaches with no {hop.link.id} link")
            q.reads[-1] = f"{hop.link.id} ({hop.direction}) absent"
            q.touched = sorted(set(q.touched) | {b for _a, b in pairs})
            return q
        raise gw.Dropped("no_splitting_link")


# --------------------------------------------------------------------------- build

def _hash(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()


def item_and_key(tag: str, q: Question, *, now: str, as_of: date,
                 generations: Mapping[str, str], digest: str) -> tuple[dict, dict]:
    walk = gw.Walk(q.text, q.facts, q.answer, q.hops, q.systems, q.start,
                   gw.object_type(q.start), q.label, q.reads, q.rule)
    item, key = gw.item_and_key(tag, walk, now=now, as_of=as_of, generations=generations,
                                digest=digest)
    item["family"] = FAMILY
    item["root"]["identifier_kind"] = KEY_FAMILY
    item["as_known_at"]["l1_generations"] = {KEY_FAMILY: digest}
    item["oracle"]["id"] = f"{ORACLE_ID}@{digest[:16]}"
    item["label"]["note"] = (f"link coverage ({q.link}, {q.template}, {q.difficulty}): graded "
                             "by its key (evals.live_ops), never by this label")
    key["family"] = KEY_FAMILY
    key["card"] = f"{CARD_PREFIX}{q.template}"
    key["strata"].update(link_kind=q.link, template=q.template, difficulty=q.difficulty)
    return item, key


def compose(links: Sequence[gw.Link], records: gw.Records, *, seed: int, as_of: date,
            only: Iterable[str] | None = None
            ) -> tuple[list[Question], dict[str, dict[str, int]]]:
    """Each link kind's questions (of ``only``, else all), each from its own random
    stream (seed and link id), so any split of the link kinds over workers gives the same
    questions; and why each template was dropped."""
    gen = Generator(links, records, as_of)
    questions: list[Question] = []
    dropped: dict[str, dict[str, int]] = {}
    for link_id in sorted(only if only is not None else [l.id for l in links]):
        made, why = gen.for_link(link_id, random.Random(_hash(seed, link_id)))
        questions += made
        if why:
            dropped[link_id] = dict(sorted(why.items()))
    return questions, dropped


def build(links: Sequence[gw.Link], records: gw.Records | None, *, seed: int, as_of: date,
          now: str, generations: Mapping[str, str], dev_share: float = DEV_SHARE,
          composed: tuple[list[Question], dict[str, dict[str, int]]] | None = None
          ) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """One question per template for every link kind (``compose``, or ``composed`` by
    the caller's workers); items and keys dealt dev/held-out by start record (items
    sharing a start are never split); and the counts."""
    from evals import blind_mined as bm
    if composed is None:
        composed = compose(links, records, seed=seed, as_of=as_of)   # type: ignore[arg-type]
    questions = sorted(composed[0], key=lambda q: (q.link, list(TEMPLATES).index(q.template)))
    dropped = composed[1]
    digest = _hash(*sorted(q.text for q in questions))
    items, keys = [], []
    for n, q in enumerate(questions):
        item, key = item_and_key(f"link#{n}", q, now=now, as_of=as_of,
                                 generations=generations, digest=digest)
        items.append(item)
        keys.append(key)
    by_start: dict[str, list[int]] = defaultdict(list)
    for n, q in enumerate(questions):
        by_start[q.start].append(n)
    dealt = bm.deal(list(by_start.values()), dev_share, digest)
    counters = {"dev": 0, "held_out": 0}
    for n, (item, key) in enumerate(zip(items, keys)):
        split = dealt[n]
        counters[split] += 1
        item["split"] = split
        item["id"] = f"{'dev' if split == 'dev' else 'ho'}-{FAMILY}-{counters[split]:04d}"
        key["item_id"] = item["id"]
    per_link = Counter(q.link for q in questions)
    stats = {"items": len(items), "dev": counters["dev"], "held_out": counters["held_out"],
             "link_kinds": len(links),
             "link_kinds_covered": sum(1 for l in links if per_link[l.id]),
             "link_kinds_at_target": sum(1 for l in links if per_link[l.id] >= TARGET_PER_LINK),
             "link_kinds_without_firm_links": sorted(
                 l.id for l in links if (dropped.get(l.id) or {}).get("no_firm_links")),
             "link_kinds_below_target": {l.id: per_link[l.id] for l in links
                                         if 0 < per_link[l.id] < TARGET_PER_LINK},
             "per_link_kind": {l.id: per_link[l.id] for l in links},
             "templates": dict(sorted(Counter(q.template for q in questions).items())),
             "difficulty": dict(sorted(Counter(q.difficulty for q in questions).items())),
             "answers": dict(sorted(Counter(q.answer for q in questions).items())),
             "dropped": dropped, "digest": digest}
    return items, keys, stats


def sample(pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]], count: int,
           seed: Any) -> list[str]:
    """``count`` item ids, one link kind at a time (kinds in SHA-256 order), each kind's
    items easy, medium, hard in turn: every link kind gets an item before any gets two."""
    buckets: dict[str, list[tuple[int, str, str]]] = {}
    for item, key in pairs:
        strata = key.get("strata") or {}
        rank = DIFFICULTIES.index(strata.get("difficulty", "hard"))
        buckets.setdefault(str(strata.get("link_kind")), []).append(
            (rank, _hash(seed, item["id"]), item["id"]))
    for ids in buckets.values():
        ids.sort()
        by_rank = [[x for x in ids if x[0] == r] for r in range(len(DIFFICULTIES))]
        ids[:] = [x for group in zip(*[g + [None] * (len(ids) - len(g)) for g in by_rank])
                  for x in group if x is not None]
    order = sorted(buckets, key=lambda k: _hash(seed, k))
    out: list[str] = []
    while len(out) < count and any(buckets.values()):
        for kind in order:
            if buckets[kind] and len(out) < count:
                out.append(buckets[kind].pop(0)[2])
    return out


# --------------------------------------------------------------------------- grading

MONTHS = gw.MONTHS
WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}
ABSTAIN = re.compile(r"\b(?:could ?n[o']t (?:find|determine|answer|tell)|cannot (?:find|"
                     r"determine|answer|tell)|can't (?:find|determine|answer|tell)|unable to|"
                     r"not able to|no (?:data|information|records?) (?:available|found)|"
                     r"i don't (?:know|have)|not enough information)\b", re.I)
ABSTAIN_OUTCOMES = frozenset({"not_found", "refused", "abstained", "unanswerable",
                              "not_served", "error", "time_limit"})


def _texts(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            if k not in ("open_url", "handle"):   # a number inside a URL is no answer
                yield from _texts(v)
    elif isinstance(value, list):
        for v in value:
            yield from _texts(v)


def _numbers(text: str) -> list[Decimal]:
    text = re.sub(r"\b(" + "|".join(WORDS) + r")\b", lambda m: WORDS[m.group(1).lower()],
                  text, flags=re.I)
    out = []
    for raw in re.findall(r"(?<![\w.])-?\d[\d,]*\.?\d*", text):
        clean = raw.replace(",", "").rstrip(".")
        try:
            out.append(Decimal(clean))
        except ArithmeticError:
            continue
    return out


def _has_number(text: str, value: str, tolerance_pct: float) -> bool:
    want = Decimal(str(value))
    slack = abs(want) * Decimal(str(tolerance_pct)) / 100 if tolerance_pct else Decimal(0)
    return any(abs(n - want) <= slack for n in _numbers(text))


def _date_forms(value: str) -> list[str]:
    day = date.fromisoformat(value[:10])
    month = MONTHS[day.month - 1]
    return [day.isoformat(), f"{month} {day.day}, {day.year}", f"{month[:3]} {day.day}, {day.year}",
            f"{day.day} {month} {day.year}", f"{month} {day.day} {day.year}"]


def grade(answer: Mapping[str, Any] | None, key: Mapping[str, Any]) -> tuple[str, str]:
    """(``correct`` | ``incomplete`` | ``wrong`` | ``abstain``, per-fact detail) of one final
    answer event (``direct_answer``, ``facts``, ``outcome``) against its key. A fact kind
    this grader does not know refuses (ValueError), never a silent pass."""
    if answer is None:
        return "abstain", "no_answer"
    direct = str(answer.get("direct_answer") or "")
    text = direct + "\n" + "\n".join(_texts(answer.get("facts")))
    folded = text.casefold()
    marks = []
    for fact in key["facts"]:
        kind = fact["kind"]
        if kind == "number":
            ok = _has_number(text, fact["value"], fact.get("tolerance_pct", 0))
            short = ok and re.search(r"at least\s+" + re.escape(str(fact["value"])), folded)
            marks.append("partial" if short else ("ok" if ok else "miss"))
        elif kind == "date":
            marks.append("ok" if any(f.casefold() in folded for f in
                                     _date_forms(fact["value"])) else "miss")
        elif kind == "text":
            marks.append("ok" if str(fact["value"]).casefold() in folded else "miss")
        elif kind == "set":
            members = fact["members"]
            hit = sum(any(str(m).casefold() in folded for m in forms) for forms in members)
            marks.append("ok" if hit == len(members) else ("partial" if hit else "miss"))
        elif kind == "absent":
            low = direct.casefold()
            marks.append("ok" if any(a.casefold() in low for a in fact.get("accept") or ())
                         else "miss")
        else:
            raise ValueError(f"fact_kind_unknown:{kind}")
    detail = ",".join(f"{f['id']}={m}" for f, m in zip(key["facts"], marks))
    if marks and all(m == "ok" for m in marks):
        return "correct", detail
    if any(m != "miss" for m in marks):
        return "incomplete", detail
    if str(answer.get("outcome") or "") in ABSTAIN_OUTCOMES or not direct.strip() or \
            ABSTAIN.search(direct):
        return "abstain", detail
    return "wrong", detail


def grade_report(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per link kind, per difficulty and per template: the count of each grade and the
    seconds (total, mean, max) over ``rows`` of {item_id, key, grade, seconds}."""
    def table(by: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            strata = row["key"]["strata"]
            name = str(strata.get(by) or "?")
            cell = out.setdefault(name, {**{g: 0 for g in GRADES}, "items": 0,
                                         "seconds": 0.0, "max_seconds": 0.0, "timed": 0})
            if row["grade"] not in GRADES:
                raise ValueError(f"grade_unknown:{row['grade']}")
            cell[row["grade"]] += 1
            cell["items"] += 1
            if row.get("seconds") is not None:
                cell["timed"] += 1
                cell["seconds"] = round(cell["seconds"] + float(row["seconds"]), 1)
                cell["max_seconds"] = max(cell["max_seconds"], float(row["seconds"]))
        for cell in out.values():
            cell["mean_seconds"] = round(cell["seconds"] / cell["timed"], 1) if cell["timed"] else None
        return dict(sorted(out.items()))
    totals = Counter(r["grade"] for r in rows)
    return {"items": len(rows), **{g: totals.get(g, 0) for g in GRADES},
            "by_difficulty": table("difficulty"), "by_template": table("template"),
            "by_link_kind": table("link_kind")}


def final_answer(sse_text: str) -> dict[str, Any] | None:
    """The last ``data:`` event carrying a ``direct_answer`` of an /api/ask SSE stream."""
    out = None
    for line in sse_text.splitlines():
        if line.startswith("data:"):
            try:
                doc = json.loads(line[5:])
            except ValueError:
                continue
            if isinstance(doc, dict) and "direct_answer" in doc:
                out = doc
    return out
