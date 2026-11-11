"""The unit migration layer with its captured documents parsed on every core.

Measured 2026-11-09 on the live host: ``python3 -m ontology.schema_migrate build`` for a
770-binding migration projected its bindings on one core, more than two hours of it
parsing the attachments of the first graph_mail ``documentFields`` binding
(``document_fields._fields`` over ``captured_text.read``), twelve mail and several
SharePoint ``documentFields`` bindings to go, with ``PLX_BUILD_WORKERS=16`` set and
unused.

``python3 -m ontology.unit_layer_workers build ...`` runs that same command (its arguments
are ``ontology.schema_migrate``'s) while a build's document workers
(``ontology.project_parallel.DocumentPool``) parse the documents, as a generation build's
do: ``document_fields.captured`` hands a relation's records to the workers in tasks and
yields their records back in the records' order, and the build's own process stays the
one writer of the kept fields, readings and member readings (``KeptFields.absorb``), so
the layer equals a serial one row for row and what is kept equals what a serial build
keeps. A worker that raises or dies leaves its documents to the build's own process.

Why a module of its own: ``ontology.schema_migrate`` is part of the derivation code
(``ontology.project.derivation_code``, the key of every kept document field), so a
change to it would make a restarted build parse every document again. Nothing the
derivation code covers imports this module.

Its ``build`` also runs fitted (``ontology.unit_layer_fit``): the Portal mirror's
re-inserted copies the projection's collapse would drop are left unread, and the link
phase runs on the same number of worker processes; the layer is the plain build's row
for row.

Before anything is read, each link job is sized from the captures
(``ontology.build_budget``): a key join over ``PLX_LINK_CANDIDATE_BUDGET`` refuses the
build at its start (exit 3; ``PLX_UNIT_LAYER_PREFLIGHT=off`` skips it). The units are
projected on worker processes, a task each, longest first (``unit_layer_fit.parallel_units``;
``PLX_UNIT_LAYER_UNITS=serial``: one projector, its link phase on every core), and the
build keeps checkpoints (``--resume-dir``, default ``OUT.resume``): a rerun of the same
command resumes where the last one stopped; ``--fresh`` discards them, ``--no-resume``
keeps none.

Workers: ``PLX_BUILD_WORKERS``, else the CPUs less two (``project_parallel.workers``);
1 runs serially; a count that is not a positive integer is refused before anything is
read. Each worker holds the bounded memos of a serial parse.

Stdlib only.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Any, Iterator, Sequence


@contextlib.contextmanager
def document_workers(workers: int | None = None) -> Iterator[Any]:
    """While open, captured documents are parsed by ``workers`` engaged document workers
    (None: ``PLX_BUILD_WORKERS``, else the CPUs less two); yields the pool, or None when
    the count is one or a pool is already held (the caller's, left as it is)."""
    from ontology import project_parallel as par

    count = par.workers(workers)
    if count < 2 or par.DOCUMENTS is not None:
        yield None
        return
    with par.document_pool(count) as pool:
        pool.engaged = True
        yield pool


PREFLIGHT_ENV = "PLX_UNIT_LAYER_PREFLIGHT"   # "off": no pre-flight budget


RESUME_HELP = """--resume-dir DIR (default: OUT.resume) keeps the build's checkpoints there and
a rerun of the same command resumes from the last one; --fresh discards them first;
--no-resume keeps none."""


def _resume_args(args: list[str]) -> tuple[list[str], dict[str, Any]]:
    """``schema_migrate``'s arguments, and this command's own resume options."""
    rest, options = [], {"dir": None, "fresh": False, "off": False}
    items = iter(args)
    for item in items:
        if item == "--resume-dir":
            options["dir"] = next(items, None)
            if not options["dir"]:
                raise SystemExit("--resume-dir needs a directory")
        elif item.startswith("--resume-dir="):
            options["dir"] = item.split("=", 1)[1]
        elif item == "--fresh":
            options["fresh"] = True
        elif item == "--no-resume":
            options["off"] = True
        else:
            rest.append(item)
    return rest, options


def identity(plan: Any, new: Any, state: Any, out: Any) -> dict[str, Any]:
    """What a unit layer build's checkpoints depend on: the derivation code (the kept
    fields' key), this command's load code, the plan, both schemas, the captures'
    directory and the output."""
    from pathlib import Path
    from ontology import project, unit_layer_fit
    return {"derivation_code": project.derivation_code(),
            "load_code": unit_layer_fit.code_digest(
                unit_layer_fit.CollapsingMirror, unit_layer_fit._kept_copies,
                unit_layer_fit.collapsible, unit_layer_fit.mirror_opener,
                unit_layer_fit._VIEW, sys.modules[__name__]),
            "plan": plan["id"], "from": plan["from"], "to": plan["to"],
            "schema": getattr(new, "digest", None),
            "state": str(Path(state).resolve()), "out": str(Path(out).resolve())}


def main(argv: Sequence[str] | None = None) -> int:
    from ontology import schema_migrate

    args, options = _resume_args(list(sys.argv[1:] if argv is None else argv))
    if args[:1] == ["build"] and os.environ.get(PREFLIGHT_ENV) != "off":
        # every link job sized from the captures first: one over budget refuses the
        # build before anything is read (``ontology.build_budget``)
        from pathlib import Path
        from ontology import build_budget, step_migrate
        from ontology.schema import load_schema
        unavailable = build_budget.people_guard()
        if unavailable:
            print(f"refused: people exclusion unavailable ({unavailable}): the people "
                  "directory would build no Person and no link to one", file=sys.stderr)
            return 4
        new = load_schema()
        the_plan = step_migrate.plan(_arg(args, "--from-ref"), new)
        budget = build_budget.preflight(the_plan, new, Path(_arg(args, "--state-dir")))
        print(f"preflight: {json.dumps({k: v for k, v in budget.items() if k != 'largest'})}",
              file=sys.stderr, flush=True)
        if budget["over"]:
            print("refused: link joins over the candidate budget: "
                  + ", ".join(budget["over"]), file=sys.stderr)
            return 3
    with contextlib.ExitStack() as stack:
        pool = stack.enter_context(document_workers())
        fit = None
        resume_box: dict[str, Any] = {}
        if args[:1] == ["build"]:
            # Re-inserted copies left unread, the link phase on every core within the
            # candidate budget, checkpoints to resume from (``ontology.unit_layer_fit``).
            from ontology.schema import load_schema
            from ontology import unit_layer_fit
            from ontology.unit_layer_fit import Resume, fitted
            out = _arg(args, "--out")
            resume = None
            if not options["off"] and out is not None:
                resume = _Deferred(options["dir"] or f"{out}.resume", options["fresh"],
                                   resume_box)
            fit = stack.enter_context(fitted(load_schema(), log=sys.stderr,
                                             resume=resume))
            if resume is not None:
                stack.enter_context(resume.wired(schema_migrate))
        code = schema_migrate.main(args)
        held = resume_box.get("resume")
        if held is not None:
            if code == 0:
                held.finish()
            else:
                held.close()
    if fit is not None:
        print(f"copies left unread: {json.dumps(fit['unread'], sort_keys=True)}",
              file=sys.stderr)
    if pool is not None:
        print(f"document workers: {pool.count}, tasks {pool.tasks}, parsed in the build "
              f"{pool.fallback}, broken {pool.broken}", file=sys.stderr)
    return code


def _arg(args: Sequence[str], name: str) -> str | None:
    for i, item in enumerate(args):
        if item == name and i + 1 < len(args):
            return args[i + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None


class _Deferred:
    """The build's :class:`ontology.unit_layer_fit.Resume`, opened once the plan is
    known (``schema_migrate.build_from_state`` is given it): until then its phase is
    None. It also hands the build the delta mark and sequence it first read."""

    def __init__(self, directory: str, fresh: bool, box: dict[str, Any]) -> None:
        self.directory, self.fresh, self.box = directory, fresh, box
        self.inner: Any = None

    def __getattr__(self, name: str) -> Any:
        inner = self.__dict__.get("inner")
        if inner is None:
            if name in ("phase",):
                return None
            if name == "resuming":
                return False
            raise AttributeError(name)
        return getattr(inner, name)

    @contextlib.contextmanager
    def wired(self, schema_migrate: Any) -> Iterator[None]:
        from operating.pg import ontology_migrate
        from ontology.unit_layer_fit import Resume
        build, mark, sequence = (schema_migrate.build_from_state, ontology_migrate.delta_mark,
                                 schema_migrate.next_sequence)

        def build_from_state(plan: Any, new: Any, state: Any, out: Any, **kwargs: Any) -> Any:
            self.inner = Resume(self.directory, identity(plan, new, state, out),
                                fresh=self.fresh)
            self.box["resume"] = self.inner
            print(f"checkpoints: {self.directory} ({self.inner.phase or 'none yet'})",
                  file=sys.stderr, flush=True)
            return build(plan, new, state, out, **kwargs)

        def delta_mark(dsn: str) -> int:
            return self.inner.value("delta_mark", lambda: mark(dsn)) \
                if self.inner is not None else mark(dsn)

        def next_sequence(dsn: str) -> int:
            return self.inner.value("sequence", lambda: sequence(dsn)) \
                if self.inner is not None else sequence(dsn)

        schema_migrate.build_from_state = build_from_state
        ontology_migrate.delta_mark = delta_mark
        schema_migrate.next_sequence = next_sequence
        try:
            yield
        finally:
            schema_migrate.build_from_state = build
            ontology_migrate.delta_mark = mark
            schema_migrate.next_sequence = sequence


if __name__ == "__main__":
    sys.exit(main())
