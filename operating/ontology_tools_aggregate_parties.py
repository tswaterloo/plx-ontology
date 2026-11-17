"""Party names and the counterparty's currency for ``plx.aggregate`` groups keyed by a
party's code (Ask shows customers and vendors by name, and every
amount with its currency: the document's, else the counterparty's Business Central
record's, said so; never guessed or converted). A mixin of ``AggregateTools``: it reads
through that class's store, schema, visibility rules and value reads."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Sequence

from operating.ontology_tools import _envelope
from operating.ontology_tools_traverse import _live, _visible_object


def _plain_key(value: Any) -> str | None:
    """A group key's text: the envelope's value, or the value itself."""
    if isinstance(value, Mapping):
        value = value.get("untrusted_text")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _one_party(names: Sequence[str]) -> bool:
    """Whether every name names one party: the name rules say so
    (``identity_scoring.party_names_agree``: the same words, a misspelling, a long prefix)
    or, the code being shared already, one's words begin the other's ("Diversified" of
    "Diversified Hospitality Solutions, Ltd."). Never a guess between two parties."""
    from ontology.identity_scoring import normalize_name, party_names_agree

    def same(a: str, b: str) -> bool:
        if party_names_agree(a, b) in ("exact", "close", "prefix"):
            return True
        short, long_ = sorted((normalize_name(a), normalize_name(b)), key=len)
        return bool(short) and (long_ + " ").startswith(short + " ")

    distinct = sorted({" ".join(n.split()) for n in names})
    return all(same(a, b) for i, a in enumerate(distinct) for b in distinct[i + 1:])


def _best_spelling(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Of name value rows naming one party: the most common spelling (ties: the fuller
    name, then the first)."""
    if not rows:
        return None
    spelled = [" ".join(str(row["value"]).split()) for row in rows]
    best = max(range(len(rows)), key=lambda i: (spelled.count(spelled[i]), len(spelled[i]),
                                                -i))
    return rows[best]


_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")
# the system whose party record states a counterparty's currency (business rule 11-16: the
# document's currency first, else the customer's or vendor's Business Central currency)
_COUNTERPARTY_CURRENCY_SYSTEM = "bc"


class AggregateParties:
    """``plx.aggregate``'s party names and counterparty currencies (see the module)."""

    def _party_kind(self, types: Iterable[str], name: str) -> tuple[str, str] | None:
        """(identifier kind, party type) when ``name`` is, on every one of ``types`` that
        declares it, a reference to one party type by the same identifier kind (a code
        such as a customer or vendor number: the identifier registry's ``object_type`` for
        the kind is a type the schema gives the ``Party`` interface and a ``display``);
        else None. Read from the schema only, never a named type or kind."""
        from ontology import identifiers
        kinds = {self._declared(t).get(name, {}).get("identifier_kind")
                 for t in types if name in self._declared(t)}
        if len(kinds) != 1 or None in kinds:
            return None
        kind = next(iter(kinds))
        try:
            party = identifiers.kind_spec(kind).get("object_type")
        except (KeyError, ValueError):
            return None
        spec = (self.schema.object_types.get(party) if self.schema and isinstance(
            party, str) else None) or {}
        if "Party" not in (spec.get("interfaces") or ()) or not spec.get("display"):
            return None
        return kind, party

    def _parties(self, scope: Any, types: Iterable[str], group_by: Sequence[str],
                 groups: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        """Per group_by property whose key is a party's code (``_party_kind``): code ->
        {"ids": the party records this reader may read that hold it, "unique": whether
        every record holding it, readable or not, names one party (``_one_party``), "names":
        readable name rows per readable record, "currency": the one currency code the
        readable Business Central records holding it state, else None}. A code no record
        holds, or whose holders name different parties (the code is not unique), names
        nobody; the names of records this reader may not read only decide that and are
        never returned. One read per property, for every group (not only those shown)."""
        from ontology import identifiers
        from operating.ontology_tools_aggregate import _pick, record_system
        types = sorted(types)
        out: dict[str, dict[str, Any]] = {}
        for name in group_by:
            found = self._party_kind(types, name)
            if found is None:
                continue
            kind, party = found
            codes: dict[str, str] = {}
            for group in groups:
                raw = _plain_key((group.get("key") or {}).get(name))
                normal = identifiers.normalize(kind, raw) if raw is not None else None
                if normal:
                    codes[raw] = normal
            if not codes:
                continue
            wanted = json.dumps(sorted(set(codes.values())))
            every: dict[str, set[str]] = {}
            for row in self.store.conn.execute(
                    "SELECT DISTINCT i.normalized, i.object_id FROM identifiers i JOIN objects o "
                    "ON o.object_id = i.object_id WHERE i.identifier_kind = :k AND i.normalized "
                    "IN (SELECT value FROM json_each(:codes)) AND o.object_type = :party AND "
                    f"{_live('i')}", scope.params(k=kind, party=party, codes=wanted)):
                every.setdefault(row["normalized"], set()).add(row["object_id"])
            readable = {row["object_id"] for row in self.store.conn.execute(
                "SELECT o.object_id FROM objects o WHERE o.object_id IN (SELECT value FROM "
                f"json_each(:ids)) AND {_visible_object('o', scope.gate)}",
                scope.params(ids=json.dumps(sorted({o for ids in every.values()
                                                    for o in ids}))))}
            display = list(self.schema.object_types[party]["display"])[0]
            all_ids = sorted({o for ids in every.values() for o in ids})
            # every holder's names, read or not (only to tell whether the code is unique)
            any_names: dict[str, list[str]] = {}
            if all_ids:
                for row in self.store.conn.execute(
                        "SELECT v.object_id, v.value FROM object_values v WHERE v.object_id IN "
                        "(SELECT value FROM json_each(:ids)) AND v.property = :p AND "
                        f"{_live('v')}", scope.params(ids=json.dumps(all_ids), p=display)):
                    if isinstance(row["value"], str) and row["value"].strip():
                        any_names.setdefault(row["object_id"], []).append(row["value"])
            seen_ids = sorted(readable)
            props = [display] + (["currency"] if "currency" in self._declared(party) else [])
            values, _withheld = self._values(scope, seen_ids, props)
            per: dict[str, Any] = {}
            for raw, normal in codes.items():
                holders = every.get(normal, set())
                names = [n for oid in holders for n in any_names.get(oid, ())]
                ids = sorted(holders & readable)
                named = {}
                for oid in ids:
                    pick = _pick(values.get((oid, display), []), False)
                    if pick.row is not None and isinstance(pick.row["value"], str) \
                            and pick.row["value"].strip():
                        named[oid] = pick.row
                stated = set()
                for oid in ids:
                    if record_system(oid) != _COUNTERPARTY_CURRENCY_SYSTEM:
                        continue
                    pick = _pick(values.get((oid, "currency"), []), False)
                    text = pick.row["value"] if pick.row is not None else None
                    stated.add(text.strip().upper() if isinstance(text, str) else None)
                currency = next(iter(stated)) if len(stated) == 1 else None
                per[raw] = {"ids": ids, "unique": bool(names) and _one_party(names),
                            "names": named,
                            "currency": currency if isinstance(currency, str)
                            and _CURRENCY_CODE.match(currency) else None}
            out[name] = per
        return out

    @staticmethod
    def _party_currency(group: Mapping[str, Any], parties: Mapping[str, Mapping[str, Any]]
                        ) -> str | None:
        """The one currency the readable Business Central records of the group's parties
        state (``_parties``), only when every holder of the code names one party; else
        None."""
        found = set()
        for name, per in parties.items():
            got = per.get(_plain_key((group.get("key") or {}).get(name)) or "")
            if got and got["unique"] and got["currency"]:
                found.add(got["currency"])
        return next(iter(found)) if len(found) == 1 else None

    @staticmethod
    def _counterparty_rows(rows: Sequence[Mapping[str, Any]], currency: str | None
                           ) -> list[dict[str, Any]] | None:
        """The group's amounts read in the counterparty's currency, when every one is an
        amount whose document states no currency (The document's currency
        first, else the counterparty's record); else None (nothing filled, the mixing
        rules decide as before)."""
        if not currency or not rows or any(
                r["value_type"] != "money" or r["currency"] is not None for r in rows):
            return None
        return [dict({k: r[k] for k in r.keys()}, currency=currency) for r in rows]

    def _party_names(self, groups: list[dict[str, Any]],
                     parties: Mapping[str, Mapping[str, Any]], limit: int,
                     systems: Mapping[int, set[str]]) -> None:
        """Each shown group whose key is a party's code gets ``party_name``: per such
        property, the party's name as this reader reads it on the readable records holding
        that code (enveloped and cited like any source value). The holders in the systems
        the group's own members come from (``systems``) are read first (a short name in
        BC, the legal name in Sage), any readable holder when none is; the most common
        spelling is shown. A code whose holders, readable or not, name different parties
        names nobody."""
        from operating.ontology_tools_aggregate import record_system
        for group in groups:
            for name, per in parties.items():
                got = per.get(_plain_key((group.get("key") or {}).get(name)) or "")
                if not got or not got["unique"] or not got["names"]:
                    continue
                near = systems.get(id(group)) or set()
                rows = [row for oid, row in sorted(got["names"].items())
                        if record_system(oid) in near] or \
                    [row for _oid, row in sorted(got["names"].items())]
                if not _one_party([str(r["value"]) for r in rows]):
                    continue
                row = _best_spelling(rows)
                shown = _envelope(" ".join(str(row["value"]).split()), row["l1_handle"],
                                  row["field_path"], limit)
                if isinstance(shown, dict) and shown.get("untrusted_text"):
                    group.setdefault("party_name", {})[name] = shown
