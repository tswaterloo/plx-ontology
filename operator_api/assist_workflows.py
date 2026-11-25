"""Assist's workflows routes (PLAN-1124 "Assist = always-on agent" (b); ``operating/assist_workflows.py``).

* ``GET  /api/assist/workflows``: the reader's own workflows (with each run folded from ``assist.job_log``),
  their mined routines (``assist.routines``), and starter templates. Nobody reads another person's workflow.
* ``POST /api/assist/workflows/parse``: preview how plain language parses into a trigger and instruction.
* ``POST /api/assist/workflows``: create a workflow from plain language, a mined routine, a template, or
  explicit trigger + instruction. No per-person cap.
* ``PATCH /api/assist/workflows/{id}``: edit name, instruction or trigger.
* ``POST /api/assist/workflows/{id}/pause`` / ``resume`` / ``stop``: pause, resume (clears failure backoff),
  or permanently stop (also stops any live job of that workflow).
* ``POST /api/assist/workflows/{id}/run``: run on demand now (appends one ``assist.job_log`` job).

The API role appends only what the person does (``created``, ``updated``, ``paused``, ``resumed``, ``stopped``,
``run_now`` on ``assist.workflow_log``; ``requested`` / ``stop`` on ``assist.job_log``). Nothing sends or writes
to a business system from here."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Mapping, Sequence

from fastapi import Depends

from . import assist_jobs as aj_api
from . import models as m
from .auth import Reader
from .errors import UserFacing

LOG = logging.getLogger(__name__)
_ERR = {401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse},
        409: {"model": m.ErrorResponse}, 422: {"model": m.ErrorResponse}}


def _pool(backend: Any) -> Any:
    return getattr(getattr(backend, "memory", None), "pool", None)


def _person_ids(backend: Any, reader: Reader) -> list[str]:
    ids = sorted(set(backend._apa_me(reader)) - {"unknown"})
    if not ids:
        raise UserFacing("not_found", "Assist does not know who you are here.", 404)
    return ids


def _iso(v: Any) -> str:
    return v.isoformat() if isinstance(v, dt.datetime) else str(v or "")


def _templates() -> list[m.AssistWorkflowTemplate]:
    from operating import assist_workflows as aw
    return [m.AssistWorkflowTemplate(
        template_id=t["template_id"], name=t["name"], description=t["description"],
        trigger_kind=t["trigger_kind"], trigger=dict(t["trigger"]),
        trigger_summary=aw.describe_trigger(t["trigger_kind"], t["trigger"]),
        instruction=t["instruction"],
    ) for t in aw.TEMPLATES]


def _load_jobs(pg: Any, job_ids: Sequence[str]) -> dict[str, m.AssistJob]:
    from operating import assist_job as aj
    from operating import assist_workflows as aw
    ids = sorted({j for j in job_ids if j})
    if not ids:
        return {}
    try:
        cur = pg.execute(
            f"SELECT seq, job_id, person, at, what, detail FROM {aj.TABLE} WHERE job_id = ANY(%s) ORDER BY seq",
            (ids,),
        )
    except Exception as exc:  # noqa: BLE001 - missing 0082 table surfaced cleanly without 500
        if aw.is_missing_table(exc):
            LOG.info("assist workflows: assist.job_log unavailable (%s)", exc)
            return {}
        raise
    by: dict[str, list[dict[str, Any]]] = {}
    for r in cur.fetchall():
        row = dict(zip(("seq", "job_id", "person", "at", "what", "detail"), r))
        by.setdefault(row["job_id"], []).append(row)
    out: dict[str, m.AssistJob] = {}
    for jid, rows in by.items():
        if any(x["what"] == "requested" for x in rows):
            out[jid] = aj_api.shown(aj.fold(rows))
    return out


def shown(wf: Mapping[str, Any], jobs: Mapping[str, m.AssistJob],
          now: dt.datetime | None = None) -> m.AssistWorkflow:
    from operating import assist_workflows as aw
    now_utc = now or dt.datetime.now(dt.timezone.utc)
    nxt = (aw.next_due(wf["trigger_kind"], wf["trigger"], now_utc)
           if wf["status"] == "active" and wf["trigger_kind"] == "schedule" else None)
    runs = [m.AssistWorkflowRun(
        job_id=r["job_id"], triggered_at=_iso(r["at"]), kind=r["kind"],
        trigger_note=r.get("trigger_note", ""), job=jobs.get(r["job_id"]),
    ) for r in wf.get("runs") or ()]
    return m.AssistWorkflow(
        workflow_id=wf["workflow_id"], name=wf["name"], instruction=wf["instruction"],
        trigger_kind=wf["trigger_kind"], trigger=dict(wf["trigger"]),
        trigger_summary=wf["trigger_summary"], source_kind=wf["source_kind"],
        source_ref=wf["source_ref"], status=wf["status"], pause_reason=wf["pause_reason"],
        created_at=_iso(wf["created_at"]), updated_at=_iso(wf["updated_at"]),
        next_run_at=_iso(nxt) if nxt else None,
        last_fired_at=_iso(wf["last_fired_at"]) if wf.get("last_fired_at") else None,
        consecutive_failures=int(wf.get("consecutive_failures") or 0),
        backoff_until=_iso(wf["backoff_until"]) if wf.get("backoff_until") else None,
        last_error=str(wf.get("last_error") or ""),
        runs=runs,
    )


def _mine(backend: Any, reader: Reader, workflow_id: str) -> tuple[dict[str, Any], list[str]]:
    from operating import assist_workflows as aw
    pool = _pool(backend)
    if pool is None:
        raise UserFacing("action_refused", "Workflows are not available here.", 409)
    ids = _person_ids(backend, reader)
    try:
        with pool.connection() as pg:
            rows = aw.rows_of(pg, workflow_id)
    except Exception as exc:  # noqa: BLE001 - missing migration surfaced cleanly
        if aw.is_missing_table(exc):
            raise UserFacing("action_refused", "Workflows are not ready on this server yet (migration 0083 pending).", 409) from None
        raise
    if not rows or rows[0]["person"] not in ids:
        raise UserFacing("not_found", "That workflow is not on your list.", 404)
    wf = aw.fold(rows)
    if wf.get("deleted"):
        raise UserFacing("not_found", "That workflow is not on your list.", 404)
    return wf, ids


def _one_shown(pool: Any, workflow_id: str) -> m.AssistWorkflow:
    from operating import assist_workflows as aw
    with pool.connection() as pg:
        wf = aw.fold(aw.rows_of(pg, workflow_id))
        jobs = _load_jobs(pg, [r["job_id"] for r in wf["runs"]])
    return shown(wf, jobs)


def install(app: Any, backend: Any, reader: Callable[..., Reader]) -> None:

    @app.get("/api/assist/workflows", response_model=m.AssistWorkflows, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def workflows_list(who: Reader = Depends(reader)) -> m.AssistWorkflows:
        """Your workflows (with their runs), your mined routines, and starter templates."""
        from operating import assist_workflows as aw
        templates = _templates()
        pool = _pool(backend)
        if pool is None:
            return m.AssistWorkflows(templates=templates, status="not_run",
                                     reason="the Assist store is not connected here")
        ids = _person_ids(backend, who)
        try:
            with pool.connection() as pg:
                wfs = aw.workflows_of(pg, ids)
                jobs = _load_jobs(pg, [r["job_id"] for w in wfs for r in w["runs"]])
                raw_routines = aw.routines_of(pg, ids)
        except Exception as exc:  # noqa: BLE001 - missing 0083 migration returns not_run instead of 500
            if aw.is_missing_table(exc):
                LOG.info("assist workflows: table missing (%s)", exc)
                return m.AssistWorkflows(templates=templates, status="not_run",
                                         reason="Workflows are not ready on this server yet (migration 0083 pending).")
            raise
        routines = []
        for r in raw_routines:
            built = aw.from_routine(r)
            routines.append(m.AssistMinedRoutine(
                routine_id=r["routine_id"], kind=r["kind"], description=r.get("description") or "",
                trigger=dict(r.get("trigger") or {}), mined=dict(r.get("mined") or {}),
                validated=dict(r.get("validated") or {}), status=r.get("status") or "candidate",
                suggested_name=built["name"],
                suggested_trigger_summary=aw.describe_trigger(built["trigger_kind"], built["trigger"]),
            ))
        return m.AssistWorkflows(
            workflows=[shown(w, jobs) for w in wfs], routines=routines, templates=templates)

    @app.post("/api/assist/workflows/parse", response_model=m.AssistWorkflowParsed, tags=["actions"],
              responses=_ERR)
    def workflow_parse(body: m.AssistWorkflowParseRequest, who: Reader = Depends(reader)) -> m.AssistWorkflowParsed:
        """Preview how plain language turns into a workflow trigger and instruction."""
        from operating import assist_workflows as aw
        _ = who
        try:
            got = aw.parse_plain_language(body.text)
        except ValueError as exc:
            raise UserFacing("invalid_request", str(exc), 422) from None
        return m.AssistWorkflowParsed(**got)

    @app.post("/api/assist/workflows", response_model=m.AssistWorkflow, tags=["actions"], responses=_ERR)
    def workflow_create(body: m.AssistWorkflowCreate, who: Reader = Depends(reader)) -> m.AssistWorkflow:
        """Create a workflow from plain language, a mined routine, a template, or explicit fields."""
        from operating import assist_workflows as aw
        pool = _pool(backend)
        if pool is None:
            raise UserFacing("action_refused", "Workflows are not available here.", 409)
        ids = _person_ids(backend, who)
        person = ids[0]
        source_kind, source_ref = "plain_language", ""
        name = body.name or ""
        instruction = body.instruction or ""
        trigger_kind = body.trigger_kind or ""
        trigger: dict[str, Any] = dict(body.trigger or {})

        if body.template_id:
            tpl = aw.template_by_id(body.template_id)
            if tpl is None:
                raise UserFacing("not_found", "That workflow template does not exist.", 404)
            source_kind, source_ref = "template", tpl["template_id"]
            name = name or tpl["name"]
            instruction = instruction or tpl["instruction"]
            trigger_kind = trigger_kind or tpl["trigger_kind"]
            merged = dict(tpl["trigger"])
            merged.update(trigger)
            trigger = merged
        elif body.routine_id:
            with pool.connection() as pg:
                mine = aw.routines_of(pg, ids)
            found = next((r for r in mine if r["routine_id"] == body.routine_id), None)
            if found is None:
                raise UserFacing("not_found", "That routine is not on your list.", 404)
            built = aw.from_routine(found)
            source_kind, source_ref = "routine", found["routine_id"]
            name = name or built["name"]
            instruction = instruction or built["instruction"]
            trigger_kind = trigger_kind or built["trigger_kind"]
            merged = dict(built["trigger"])
            merged.update(trigger)
            trigger = merged
        elif body.plain_text:
            try:
                parsed = aw.parse_plain_language(body.plain_text)
            except ValueError as exc:
                raise UserFacing("invalid_request", str(exc), 422) from None
            name = name or parsed["name"]
            instruction = instruction or parsed["instruction"]
            if not trigger_kind:
                trigger_kind = parsed["trigger_kind"]
                merged = dict(parsed["trigger"])
                merged.update(trigger)
                trigger = merged
        else:
            trigger_kind = trigger_kind or "manual"

        try:
            with pool.connection() as pg:
                wid = aw.create(
                    pg, person, name=name, instruction=instruction,
                    trigger_kind=trigger_kind, trigger=trigger,
                    source_kind=source_kind, source_ref=source_ref,
                )
        except ValueError as exc:
            raise UserFacing("invalid_request", str(exc), 422) from None
        except Exception as exc:  # noqa: BLE001 - missing migration surfaced cleanly
            if aw.is_missing_table(exc):
                raise UserFacing("action_refused", "Workflows are not ready on this server yet (migration 0083 pending).", 409) from None
            raise
        return _one_shown(pool, wid)

    @app.patch("/api/assist/workflows/{workflow_id}", response_model=m.AssistWorkflow, tags=["actions"],
               responses=_ERR)
    def workflow_update(workflow_id: str, body: m.AssistWorkflowUpdate, who: Reader = Depends(reader)) -> m.AssistWorkflow:
        """Edit a workflow's name, instruction or trigger."""
        from operating import assist_workflows as aw
        wf, _ids = _mine(backend, who, workflow_id)
        if wf["status"] == "stopped":
            raise UserFacing("action_refused", "A stopped workflow cannot be edited.", 409)
        try:
            with _pool(backend).connection() as pg:
                aw.update_workflow(
                    pg, wf,
                    name=body.name,
                    instruction=body.instruction,
                    trigger_kind=body.trigger_kind,
                    trigger=body.trigger,
                )
        except ValueError as exc:
            msg = str(exc)
            status = 409 if "stopped" in msg.lower() else 422
            code = "action_refused" if status == 409 else "invalid_request"
            raise UserFacing(code, msg, status) from None
        return _one_shown(_pool(backend), workflow_id)

    @app.post("/api/assist/workflows/{workflow_id}/pause", response_model=m.AssistWorkflow, tags=["actions"],
              responses=_ERR)
    def workflow_pause(workflow_id: str, who: Reader = Depends(reader)) -> m.AssistWorkflow:
        """Pause a workflow: it stops firing on schedule or events until you resume it."""
        from operating import assist_workflows as aw
        wf, _ids = _mine(backend, who, workflow_id)
        if wf["status"] == "stopped":
            raise UserFacing("action_refused", "This workflow is already stopped.", 409)
        if wf["status"] != "paused":
            with _pool(backend).connection() as pg:
                aw.append(pg, workflow_id, wf["person"], "paused", {"by": "person", "reason": "paused_by_person"})
        return _one_shown(_pool(backend), workflow_id)

    @app.post("/api/assist/workflows/{workflow_id}/resume", response_model=m.AssistWorkflow, tags=["actions"],
              responses=_ERR)
    def workflow_resume(workflow_id: str, who: Reader = Depends(reader)) -> m.AssistWorkflow:
        """Resume a paused workflow (clears any failure pause/backoff and starts watching from now)."""
        from operating import assist_workflows as aw
        wf, _ids = _mine(backend, who, workflow_id)
        if wf["status"] == "stopped":
            raise UserFacing("action_refused", "A stopped workflow cannot be resumed. Create a new one instead.", 409)
        if wf["status"] != "active":
            with _pool(backend).connection() as pg:
                aw.append(pg, workflow_id, wf["person"], "resumed", {"by": "person"})
        return _one_shown(_pool(backend), workflow_id)

    @app.post("/api/assist/workflows/{workflow_id}/stop", response_model=m.AssistWorkflow, tags=["actions"],
              responses=_ERR)
    def workflow_stop(workflow_id: str, who: Reader = Depends(reader)) -> m.AssistWorkflow:
        """Stop a workflow permanently and stop any run currently in progress."""
        from operating import assist_workflows as aw
        wf, _ids = _mine(backend, who, workflow_id)
        with _pool(backend).connection() as pg:
            aw.stop_workflow(pg, wf)
        return _one_shown(_pool(backend), workflow_id)

    @app.delete("/api/assist/workflows/{workflow_id}", response_model=m.AssistWorkflow, tags=["actions"],
                responses=_ERR)
    def workflow_delete(workflow_id: str, who: Reader = Depends(reader)) -> m.AssistWorkflow:
        """Delete a workflow from your list and stop any run currently in progress."""
        from operating import assist_workflows as aw
        wf, _ids = _mine(backend, who, workflow_id)
        with _pool(backend).connection() as pg:
            aw.delete_workflow(pg, wf)
        return _one_shown(_pool(backend), workflow_id)

    @app.post("/api/assist/workflows/{workflow_id}/run", response_model=m.AssistWorkflowRunResult, tags=["actions"],
              responses=_ERR)
    def workflow_run_now(workflow_id: str, who: Reader = Depends(reader)) -> m.AssistWorkflowRunResult:
        """Run a workflow on demand right now: creates an Assist job whose pieces you review and confirm."""
        from operating import assist_job as aj
        from operating import assist_workflows as aw
        wf, _ids = _mine(backend, who, workflow_id)
        if wf["status"] == "stopped":
            raise UserFacing("action_refused", "A stopped workflow cannot be run.", 409)
        with _pool(backend).connection() as pg:
            jid = aw.trigger_run_now(pg, wf)
            job = aj_api.shown(aj.fold(aj.rows_of(pg, jid)))
        return m.AssistWorkflowRunResult(workflow=_one_shown(_pool(backend), workflow_id), job=job)
