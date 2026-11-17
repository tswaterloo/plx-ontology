"""The plan steps of ``InvestigateTools``: the types and paths a step starts from and
how each step runs over the collected objects.

Split from ``operating/ontology_tools_investigate.py``; moved unchanged.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence
from ontology.json_contract import validate
from . import answerers
from .ontology_tools import default_grammar
from .ontology_tools_traverse import with_leads
from .ontology_tools_investigate_gaps import _binding_of, _gap, _type_of, _value_text
from .ontology_tools_investigate_plans import (
    DEFAULT_MAX_EACH, _Collection, _Run, _SET_PLACEHOLDER, _canonical, _froms,
    _join_empty_hops, _path_types, ask_again, link_hops, undeclared_hops,
)


class _StepsMixin:
    """Methods of ``InvestigateTools`` (``ontology_tools_investigate_steps``); mixed in there, never used alone."""

    # ------------------------------------------------------------------ steps

    def _step_types(self, step: Mapping[str, Any]) -> set[str] | None:
        """Types a step can start from: its declared object_types, narrowed to a
        plx.around path's start types (from the schema)."""
        allowed = set(step.get("object_types") or ()) or None
        path = (step.get("args") or {}).get("path")
        if step["tool"] == "plx.around" and isinstance(path, list) and path:
            key = _canonical(path)
            if key not in self._starts:
                ends, problem = _path_types(path, self.schema)
                self._starts[key] = None if problem else frozenset(ends)
            starts = self._starts[key]
            if starts is not None:
                allowed = set(starts) if allowed is None else allowed & starts
        return allowed

    def _paths(self, step: Mapping[str, Any], type_id: str) -> list[list[dict[str, str]] | None]:
        """The paths a step asks from an object of ``type_id``: one per link its ``links``
        selector derives from the schema (none: the object is no input), else the step's
        own arguments (None)."""
        links = step.get("links")
        if links is None:
            return [None]
        key = (type_id, _canonical(links))
        if key not in self._hops:
            self._hops[key] = [[hop] for hop in link_hops(self.schema, type_id, links)]
        return self._hops[key]

    def _step(self, run: _Run, step: Mapping[str, Any]) -> None:
        sid, tool = step["id"], step["tool"]
        froms = [run.collections.get(name) or _Collection() for name in _froms(step)]
        filled = [each for each in froms if each.ids]
        # the union with empty collections is the one filled collection: its set handle
        # still holds exactly its objects
        source = filled[0] if len(filled) == 1 else froms[0] if len(froms) == 1 else \
            _Collection(list(dict.fromkeys(oid for each in froms for oid in each.ids)))
        if step.get("if_declared"):
            missing = undeclared_hops((step.get("args") or {}).get("path") or (), self.schema)
            if missing:
                run.collections[sid] = _Collection()
                run.trace.append({"step": sid, "tool": tool, "status": "skipped", "calls": 0,
                                  "objects": 0, "reason": "link_not_declared"})
                run.add_gap(dict(_gap("unknown", "link_not_served", sid,
                                      "the served schema does not declare "
                                      + ", ".join(sorted(set(missing)))
                                      + " yet: this step was not run, so what it would "
                                      "reach is not in the answer"),
                                 # a schema the Workbench serves, not a source's record
                                 who_can_answer=[answerers.typed("Workbench operator")]))
                return
        allowed = self._step_types(step)
        targets = [oid for oid in source.ids if (allowed is None or _type_of(oid) in allowed)
                   and self._paths(step, _type_of(oid))]
        if not targets:
            run.collections[sid] = _Collection()
            run.trace.append({"step": sid, "tool": tool, "status": "skipped", "calls": 0,
                              "objects": 0, "reason": "no_input"})
            return
        if not self._available(tool):
            run.collections[sid] = _Collection()
            run.trace.append({"step": sid, "tool": tool, "status": "step_unavailable",
                              "calls": 0, "objects": 0})
            run.add_gap(_gap("unknown", "step_unavailable", f"{tool} ({sid})",
                             "the host does not serve this primitive; the plan's step was "
                             "not run"))
            run.reason("step_unavailable")
            if step.get("required"):
                run.reason("required_step_incomplete")
            return
        if run.stopped or run.spent():
            self._stop(run, sid, tool, step)
            return
        bind = step["bind"]
        accepts_set = not validate(_SET_PLACEHOLDER, self._input(tool).get(bind, {}),
                                   root=self.contracts, limit=1)
        named = any("link" in hop for hop in (step.get("args") or {}).get("path") or ())
        # the union of the from collections' exact parts, when every filled one has them
        parts = list(dict.fromkeys(h for each in filled for h in each.parts)) \
            if filled and all(each.parts for each in filled) else []

        def per_object() -> list[str]:
            limit = int(run.plan.doc.get("max_each") or DEFAULT_MAX_EACH)
            limit = min(limit, int(step.get("max_each") or limit))
            if len(targets) > limit:
                run.truncated = run.truncated or "page_cap"
                run.reason("step_truncated")
                run.add_gap(_gap("unknown", "page_cap", f"{sid}",
                                 f"{len(targets) - limit} of {len(targets)} starting objects "
                                 f"were not read (max_each {limit})"))
            return targets[:limit]

        reused = True   # the step asks earlier steps' set handles, not each object
        if accepts_set and source.set_handle and source.exact and len(targets) == len(source.ids) \
                and "links" not in step and not named:
            batches: list[str] = [source.set_handle]
        elif accepts_set and parts and len(parts) < len(targets) \
                and len(targets) == len(source.ids) and "links" not in step and not named:
            batches = parts   # one call per part: fewer than one per object, none left out
        else:
            batches, reused = per_object(), False
        prior_calls = 0
        first_result = len(run.results.get(sid, []))
        first_gap = len(run.gaps)
        while True:
            reached: list[str] = []
            handles: list[str] = []
            whole: list[str] = []   # per call that reached something: its answer's set handle
            unioned = step.get("retry") is None \
                and (step.get("args") or {}).get("tier_floor") != "lead"
            calls, status, error_code = prior_calls, "ok", None
            results = run.results.setdefault(sid, [])
            cut = False  # a last page read of some target is itself truncated
            retry = step.get("retry")
            known = set(run.objects)  # reached before this step: a party check never unreaches
            expired = False
            asks = [(target, path) for target in batches for path in (
                self._paths(step, _type_of(target)) if "links" in step else [None])]
            for target, path in asks:
                if run.spent():
                    status = "budget_exhausted"
                    run.stopped = True
                    break
                args = dict(step.get("args") or {})
                args[bind] = target
                if path is not None:
                    args["path"] = path
                before = run.truncated
                first, result = ask_again(
                    lambda asked: self._dispatch(run, tool, asked, harvest=retry is None), args,
                    retry, lambda _first: not run.spent())
                calls += 1
                if reused and "error" in result \
                        and result["error"].get("code") == "handle_expired":
                    expired = True
                    break   # a reused set expired: ask each object instead
                if result is not first:
                    # The primitive's own gap asked for this; its first answer is replaced.
                    calls += 1
                    args = dict(args, **retry["set_args"])
                    run.add_gap(_gap("unknown", retry["note_gap"], f"{sid}",
                                     "asked again with " + ", ".join(
                                         f"{k}={v}" for k, v in
                                         sorted(retry["set_args"].items())) +
                                     ": what this step reached rests on weaker links"))
                    # A note, not a reason: each entry resting on a lead says so (lead_basis).
                if tool == "plx.around" and args.get("tier_floor") == "lead" and \
                        result is first and isinstance(result.get("possibly_related"), Mapping):
                    # a step that asks the lead floor itself labels what it reaches (each entry
                    # resting on a lead says lead_basis): it follows the lead set plx.around
                    # sets apart, as a step asked again at that floor does (ask_again)
                    apart = result["possibly_related"]
                    merged = dict(with_leads(result))
                    if not result.get("objects"):
                        merged["set"] = apart.get("set")
                    merged.pop("possibly_related", None)
                    result = merged
                    if retry is None:   # the solid part was harvested at dispatch
                        self._harvest(run, {"objects": apart.get("objects") or [],
                                            "edges": apart.get("edges") or []})
                if retry is not None:
                    self._harvest(run, result)
                # A paged answer is read to its last page within the budget, with the same
                # arguments; only an answer read whole undoes the truncation its pages reported.
                pages = [result]
                while "error" not in result and (result.get("completeness") or {}).get(
                        "next_cursor") and "cursor" in self._input(tool):
                    if run.spent():
                        status = "budget_exhausted"
                        run.stopped = True
                        break
                    result = self._dispatch(run, tool, dict(
                        args, cursor=result["completeness"]["next_cursor"]))
                    calls += 1
                    pages.append(result)
                last = (result.get("completeness") or {}) if "error" not in result else {}
                if len(pages) > 1 and "error" not in result and not last.get("truncated"):
                    run.truncated = before
                cut = cut or "error" in result or bool(last.get("truncated"))
                for result in pages:
                    results.append(result)
                    if "error" in result:
                        status, error_code = "error", result["error"].get("code")
                        run.add_gap(_gap("unknown", "step_failed", f"{tool} ({sid})",
                                         f"the primitive answered {error_code}"))
                        continue
                    for ref in result.get("objects") or ():
                        if ref["object_id"] not in reached:
                            reached.append(ref["object_id"])
                    owner = result.get("object")
                    if isinstance(owner, Mapping) and owner.get("object_id") not in reached:
                        reached.append(owner["object_id"])
                    if isinstance(result.get("set"), str):
                        handles.append(result["set"])
                if any(page.get("objects") or isinstance(page.get("object"), Mapping)
                       for page in pages):
                    sets = {page.get("set") for page in pages}
                    whole.append(sets.pop() if len(sets) == 1 and None not in sets else "")
            if not expired:
                break
            # Every call above went to a set handle, and one had expired (evicted under
            # load or past its ttl): the step is asked again per object, as though no
            # set had been kept, so a reused set never fails a step.
            del run.results.setdefault(sid, [])[first_result:]
            batches, reused = per_object(), False
            prior_calls = calls
        if "links" in step:
            _join_empty_hops(run, sid, first_gap, {
                f"{hop['link_type']} {hop['direction']}" for target in batches
                for path in self._paths(step, _type_of(target)) if path for hop in path})
        if status != "ok" and step.get("required"):
            run.reason("required_step_incomplete")
        if status == "error":
            # A failed step read nothing for some start object: never answerable or complete.
            run.reason("step_failed")
            run.truncated = run.truncated or "step_failed"
        elif cut:
            # A last page still truncated (capped, or a page the tool cannot be asked for).
            run.reason("step_truncated")
            run.truncated = run.truncated or "step_truncated"
        if status == "budget_exhausted":
            run.reason("step_budget_exhausted")
            run.truncated = run.truncated or "step_budget"
        kept = self._party_check(run, step, reached, known) if "party_check" in step \
            else reached
        exact = len(set(handles)) == 1 and len(batches) == 1 and not cut \
            and len(kept) == len(reached)
        # Every call answered whole with its own set: their union is exactly what was
        # reached (the calls that reached nothing add nothing), so a later step asks each
        # set once instead of each object.
        union = unioned and not cut and status == "ok" and len(kept) == len(reached) \
            and "" not in whole
        parts = tuple(dict.fromkeys(whole)) if union else ()
        handle = handles[0] if handles else None
        if len(parts) == 1 and not exact:
            handle, exact = parts[0], True
        reached = kept
        run.collections[sid] = _Collection(reached, handle, exact, parts)
        entry = {"step": sid, "tool": tool, "status": status, "calls": calls,
                 "objects": len(reached)}
        if error_code:
            entry["reason"] = str(error_code)[:64]
        run.trace.append(entry)

    def _party_check(self, run: _Run, step: Mapping[str, Any], reached: Sequence[str],
                     known: set[str]) -> list[str]:
        """``reached`` without the records of another party that merely share the number:
        an object whose reference identifiers (``ontology.resolve.corroborations``, the
        properties the schema declares ``identifies: reference``) conflict with some
        object of the ``against`` collections and agree with none. It leaves this step's
        collection, and the run when this step first reached it, and every one is
        declared in a ``party_differs`` gap per source relation and identifier (never a
        silent pick). The values compared are those the primitives show this reader
        (``_party_values``); an unreadable side proves nothing and the object stays."""
        from ontology import resolve
        names = step["party_check"].get("against") or _froms(step)
        peers = list(dict.fromkeys(oid for name in names
                                   for oid in (run.collections.get(name) or _Collection()).ids))
        normalize = getattr(self._grammar(), "normalize", None)
        differs: dict[str, set[str]] = {}
        undecided = 0
        for oid in reached:
            verdicts: set[str] = set()
            features: set[str] = set()
            for peer in peers:
                if peer == oid:
                    continue
                checks = [resolve.compare_linked(check, own, linked, normalize)
                          for check, own, linked in self._party_pairs(run, oid, peer)]
                verdicts.add(resolve.verdict(checks))
                features |= {c.feature for c in checks if c.verdict == "conflict"}
            if "conflict" in verdicts and "agree" not in verdicts:
                differs[oid] = features
            elif "agree" not in verdicts and run.spent():
                undecided += 1
        if undecided:
            # Kept, as the primitive returned them, but never silently taken as checked:
            # the run is then short of its step budget like a step that could not run.
            run.reason("step_budget_exhausted")
            run.truncated = run.truncated or "step_budget"
            run.add_gap(_gap("unknown", "step_budget", f"{step['id']}: party check",
                             f"{undecided} record(s) were not checked for another party "
                             "within the step budget; each is kept as reached, unchecked"))
        if not differs:
            return list(reached)
        # Which same-number record is the root's own rests on a corroboration a person may
        # overturn: the answer is short of that judgement, never whole.
        run.reason("party_differs")
        for oid in differs:
            if oid not in known:
                run.objects.pop(oid, None)
                run.edges = {lid: edge for lid, edge in run.edges.items()
                             if oid not in (edge.get("from"), edge.get("to"))}
        groups: dict[tuple[str, str], list[str]] = {}
        for oid, features in differs.items():
            for feature in features:
                groups.setdefault((_binding_of(oid), feature), []).append(oid)
        for (binding, feature), oids in sorted(groups.items()):
            spec = resolve.binding_spec(self.schema, binding)
            relation = f"{spec[1].get('source')}:{spec[1].get('relation')}" if spec else binding
            party = " ".join(feature.split("_")[:-1]) or feature
            run.add_gap(_gap("unknown", "party_differs",
                             f"{len(oids)} {relation} records share the number but belong to "
                             f"another {party}",
                             f"{step['id']}: each one's {feature} conflicts with a record of "
                             f"{', '.join(names)} and agrees with none; left out: "
                             + ", ".join(sorted(oids))))
        return [oid for oid in reached if oid not in differs]

    def _party_pairs(self, run: _Run, oid: str, peer: str
                     ) -> list[tuple[Any, list[Any], list[tuple[str, Any]] | None]]:
        """(corroboration, the cited values of one side's reference identifier, the
        identifiers of the record the other side's fk names) for each reference check the
        schema declares between two objects, in both directions."""
        from ontology import resolve
        out = []
        for own, other in ((oid, peer), (peer, oid)):
            declared = {p["name"]: p for p in (self.schema.object_types.get(_type_of(own))
                                               or {}).get("properties", ())}
            for check in resolve.corroborations(self.schema, _binding_of(own),
                                                _binding_of(other)):
                if (declared.get(check.root_property) or {}).get("identifies") != "reference":
                    continue  # the record's own number, not a party it names
                values = [value for path, prop, value in self._party_values(run, own)
                          if path == check.root_path and prop == check.root_property]
                if not values:
                    continue  # nothing to compare: no read of the other side
                linked = self._party_linked(run, other, check)
                out.append((check, values, linked))
        return out

    def _party_linked(self, run: _Run, oid: str, check: Any
                      ) -> list[tuple[str, Any]] | None:
        """``(path, cited value)`` of the identifiers on the one record ``oid``'s fk link
        ``check.link`` reaches for this reader (``plx.around``), else None."""
        key = f"{oid}\n{check.link}"
        if key not in run.party_reads:
            linked = None
            if self._available("plx.around") and not run.spent():
                result = self._dispatch(run, "plx.around", {
                    "from": oid, "path": [{"link_type": check.link, "direction": "forward"}]},
                    harvest=False)
                found = {ref.get("object_id") for ref in result.get("objects") or ()
                         if isinstance(ref, Mapping)} if "error" not in result else set()
                if len(found) == 1:
                    linked = found.pop()
            run.party_reads[key] = linked
        linked = run.party_reads[key]
        if not isinstance(linked, str) or _binding_of(linked) != check.linked_binding:
            return None
        return [(path, value) for path, _prop, value in self._party_values(run, linked)
                if path in check.linked_paths]

    def _party_values(self, run: _Run, oid: str) -> list[tuple[str, str, Any]]:
        """``(column path, property, cited value)`` of each identifier ``plx.object`` shows
        this reader on ``oid`` (the run's read, else one read within the budget that adds
        nothing to the evidence)."""
        from ontology.resolve import CitedValue
        if oid not in run.party_reads:
            values: Sequence[Any] = run.values.get(oid, ())
            if oid not in run.read and self._available("plx.object") and not run.spent():
                result = self._dispatch(run, "plx.object", {"object_id": oid}, harvest=False)
                values = result.get("values") or () if "error" not in result else ()
            out = []
            for value in values:
                cite = value.get("citation") if isinstance(value, Mapping) else None
                text = _value_text(value.get("value")) if isinstance(cite, Mapping) else None
                if text is None or value.get("value_type") != "identifier" \
                        or not isinstance(cite.get("field_path"), str):
                    continue
                path = cite["field_path"].removeprefix("row.")
                out.append((path, str(value.get("property")), CitedValue(
                    text, str(cite.get("l1_handle") or ""), cite["field_path"],
                    str(cite.get("digest") or ""), str(cite.get("lineage") or ""))))
            run.party_reads[oid] = out
        return run.party_reads[oid]

    def _grammar(self) -> Any:
        if not hasattr(self, "_identifier_grammar"):
            self._identifier_grammar = default_grammar()
        return self._identifier_grammar

    def _stop(self, run: _Run, sid: str, tool: str, step: Mapping[str, Any]) -> None:
        run.stopped = True
        run.collections[sid] = _Collection()
        run.trace.append({"step": sid, "tool": tool, "status": "budget_exhausted",
                          "calls": 0, "objects": 0})
        run.reason("step_budget_exhausted")
        run.truncated = run.truncated or "step_budget"
        if step.get("required"):
            run.reason("required_step_incomplete")

    @staticmethod
    def _trace(step: str, tool: str, result: Mapping[str, Any], *, calls: int) -> dict[str, Any]:
        entry = {"step": step, "tool": tool, "status": "error" if "error" in result else "ok",
                 "calls": calls, "objects": len(result.get("candidates") or ())
                 if "candidates" in result else int("object" in result)}
        if "error" in result:
            entry["reason"] = str(result["error"].get("code"))[:64]
        return entry
