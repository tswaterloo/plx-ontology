"""Draft on request (ASSIST specs 2.6 step 8; L1, the worker's first piece).

A person's click on "Draft it" appends a ``requested`` row to ``assist.matter_log`` (the API's
only write). This worker serves those requests: for each open matter whose newest request is
later than its last plan, it takes the matter's basis message (the newest reading of the
thread), finds the message's record, and calls the one Assistant session on it
(``reply_drafts.line_draft``: the event is built from the record by ``autopilot_engine.discover``,
which reads the registry's *sources* only, never a trigger; the material, the screens, the
person's memory and the session as the person are the drafts path's own). Then it writes:

* ``plan_status = 'drafted'`` and ``plan.prepared.draft`` for a reply;
* ``needs_you`` with ``plan.prepared.your_call`` for a question only the person can answer;
* ``failed`` with a typed ``plan_reason`` (a screen, no draft, a session that raised, a matter
  with no message to draft from): never an empty "nothing to prepare"; the list says "ask
  again";
* one ``drafted`` or ``plan_failed`` row in the matter's log and one ``worker_runs`` row.

No deadline ends a draft: a request is served until the session answers; only a process that
hangs is ended (by the unit's own stop). Nothing here sends or writes to a business system.
The plan tier (what the person would do, from their precedents) and unasked drafts for shown
people come with AS5/AS6's later steps.
Evidence: isolated-fixture (``operating/tests/test_assist_worker.py``); live-unverified."""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

LOG = logging.getLogger(__name__)
SCHEMA = "assist"
LOCK = 41050
PARALLEL = 3
FEATURE = "assist_worker"

_REQUESTED = f"""
SELECT m.matter_id, m.owner_person, m.kind, m.sources->'readings'->-1->>0 AS source,
       r.handles, rq.at
FROM {SCHEMA}.matters m
JOIN LATERAL (SELECT max(l.at) AS at FROM {SCHEMA}.matter_log l
              WHERE l.matter_id = m.matter_id AND l.what = 'requested') rq ON rq.at IS NOT NULL
LEFT JOIN {SCHEMA}.readings r ON jsonb_typeof(m.sources->'readings') = 'array'
     AND r.source = m.sources->'readings'->-1->>0 AND r.message_key = m.sources->'readings'->-1->>1
WHERE m.state = 'open' AND rq.at > coalesce(m.plan_at, '-infinity'::timestamptz)
ORDER BY rq.at"""

_OIDS = ("SELECT handle, object_ids[1] FROM biz.emails WHERE handle = ANY(%s) UNION ALL "
         "SELECT handle, object_ids[1] FROM biz.teams_messages WHERE handle = ANY(%s)")

# the channel of a reply by the basis message's source; a record matter (a task, an approval) has none
_CHANNELS = {"emails": "mail", "teams_messages": "teams"}

_DONE = f"""
WITH v AS (SELECT * FROM jsonb_to_recordset(%(v)s::jsonb) AS x(matter text, status text,
    reason text, plan jsonb, seconds double precision, usd text)),
upd AS (UPDATE {SCHEMA}.matters m SET
          plan = CASE WHEN v.status = 'failed' THEN m.plan ELSE v.plan END,
          plan_status = CASE WHEN v.status = 'failed' AND m.plan_status IN ('drafted', 'needs_you')
                             THEN m.plan_status ELSE v.status END,
          plan_reason = v.reason, plan_ver = 1, plan_at = now(),
          ready_at = CASE WHEN v.status IN ('drafted', 'needs_you') THEN now() ELSE m.ready_at END
        FROM v WHERE m.matter_id = v.matter RETURNING m.matter_id, v.status, v.reason, m.owner_person,
          v.seconds, v.usd),
lg AS (INSERT INTO {SCHEMA}.matter_log (matter_id, what, detail)
       SELECT matter_id, CASE WHEN status = 'failed' THEN 'plan_failed' ELSE 'drafted' END,
              jsonb_build_object('status', status, 'reason', reason) FROM upd RETURNING 1),
wr AS (INSERT INTO {SCHEMA}.worker_runs (matter_id, person, tier, shadow, host, model, usd,
          usd_basis, seconds, status, reason)
       SELECT matter_id, owner_person, 'draft', false, %(host)s, %(model)s, usd::numeric,
              CASE WHEN usd IS NULL THEN 'estimate' ELSE 'reported' END,
              seconds, CASE WHEN status = 'failed' THEN 'failed' ELSE 'ok' END, reason
       FROM upd RETURNING 1)
SELECT count(*) FROM upd"""


def requested(pg: Any) -> list[dict[str, Any]]:
    """The open matters asked for, oldest request first, each with its basis message's record
    id (``oid``; None when there is no message record: said when it is served)."""
    cur = pg.execute(_REQUESTED)
    cols = [d.name for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    handles = sorted({h for r in rows for h in (r["handles"] or ())})
    found = dict(pg.execute(_OIDS, (handles, handles)).fetchall()) if handles else {}
    for r in rows:
        r["oid"] = next((found[h] for h in (r["handles"] or ()) if found.get(h)), None)
    return rows


def outcome(rows: Optional[Sequence[Mapping[str, Any]]], matter: Mapping[str, Any],
            meta: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """The matter's new plan from what the Assistant gave (``reply_drafts.line_draft`` rows);
    ``meta`` (a prepared session's reads, unread sources, and dry-run write intents) is kept
    on the plan's ``prepared``."""
    res = _outcome(rows, matter)
    if res["status"] == "failed" and res["reason"] == "nothing_to_do" and (meta or {}).get("nothing"):
        # a prepared session found the matter done and named the record that shows it: kept as the
        # plan tier keeps "nothing" (an act of that kind and its why), never a bare failure
        none = {"act": "nothing", "to": [], "about": "", "attach": "",
                "system": {"system": "", "operation": "", "record": ""}, "question": "", "choices": []}
        res = {"status": "planned", "reason": "", "plan": {
            "ver": 1, "acts": [none], "why": str(meta["nothing"]),
            "prepared": {k: v for k, v in meta.items() if k != "usd"}}, }
        return res
    if res["status"] == "failed" and res["reason"] == "changes_prepared" and (meta or {}).get("write_intents"):
        # a prepared session produced validated dry-run system write intents with no reply needed
        res = {"status": "drafted", "reason": "", "plan": {
            "prepared": {k: v for k, v in (meta or {}).items() if k != "usd"},
            "drafted_by": "model"}}
        return res
    if meta and isinstance(res.get("plan"), dict) and isinstance(res["plan"].get("prepared"), dict):
        res["plan"]["prepared"].update(meta)
    return res


def _outcome(rows: Optional[Sequence[Mapping[str, Any]]], matter: Mapping[str, Any]
             ) -> dict[str, Any]:
    from operating import reply_drafts as rd
    if not matter.get("oid"):
        return {"status": "failed", "reason": f"no_basis:{matter.get('kind')}", "plan": None}
    got = rd.result_of(rows or [])
    if got["state"] == "ready" and got["kind"] == "reply":
        prepared: dict[str, Any] = {"draft": {"channel": _CHANNELS.get(str(matter.get("source") or ""), ""),
                                              "to": [], "body": got["body"]},
                                    "note": got.get("ask") or "", "warning": got.get("why") or ""}
        return {"status": "drafted", "reason": "", "plan": {"prepared": prepared,
                                                            "drafted_by": "model"}}
    if got["state"] == "ready":
        return {"status": "needs_you", "reason": "", "plan": {"prepared": {"your_call": {
            "ask": got["ask"], "options": got.get("options") or []}}, "drafted_by": "model"}}
    return {"status": "failed", "reason": str(got.get("why") or "no_draft"), "plan": None}


def run_queue(pg: Any, draft: Callable[[str, str], Sequence[Mapping[str, Any]]], *,
              parallel: int = PARALLEL, model: Any = "", host: str = "",
              select: Optional[Callable[[Any], list[dict[str, Any]]]] = None,
              prepare: Optional[Callable[[Mapping[str, Any]], tuple[Sequence[Mapping[str, Any]],
                                                                    Mapping[str, Any]]]] = None
              ) -> dict[str, Any]:
    """Serve every request: ``draft(owner, oid)`` for each (``parallel`` at once). No count or
    time bound: a request is served until the session answers; only a hung process ends the run
    (the unit's stop), and each matter's result is written the moment it is known, so a hang
    loses nothing finished and never leaves "Drafting" for a matter already answered. A session
    that raises is that matter's typed failure, never the run's; a failed re-draft keeps the
    matter's earlier plan (and its draft) and says why."""
    got = pg.execute("SELECT pg_try_advisory_lock(%s)", (LOCK,)).fetchone()[0]
    if not got:
        return {"status": "skipped", "reason": "another worker holds the lock"}
    try:
        todo = (select or requested)(pg)
        write = threading.Lock()        # one connection: results go in one at a time
        count: dict[str, int] = {}

        def one(matter: dict[str, Any]) -> None:
            t0 = time.monotonic()
            rows, meta, usd = None, None, None
            if matter.get("oid"):
                try:
                    if prepare is not None:       # a prepared session: its rows and what it read
                        rows, meta = prepare(matter)
                        usd = meta.get("usd")      # the model's reported cost: the run row's, not the plan's
                        meta = {k: v for k, v in meta.items() if k != "usd"}
                    else:
                        rows = draft(matter["owner_person"], matter["oid"])
                except Exception as exc:  # noqa: BLE001 - typed on the matter, logged
                    LOG.warning("draft failed for %s", matter["matter_id"], exc_info=True)
                    rows = [{"why": f"draft_failed:{type(exc).__name__}"}]
            res = outcome(rows, matter, meta)
            result = {"matter": matter["matter_id"], "status": res["status"],
                      "reason": res["reason"], "plan": res["plan"], "usd": usd,
                      "seconds": round(time.monotonic() - t0, 1)}
            with write:
                pg.execute(_DONE, {"v": json.dumps([result]), "host": host or platform.node(),
                                   "model": model() if callable(model) else model})
                count[res["status"]] = count.get(res["status"], 0) + 1

        with ThreadPoolExecutor(max(1, parallel), thread_name_prefix="assist-draft") as pool:
            list(pool.map(one, todo))
        return {"status": "ok", "requested": len(todo), "served": sum(count.values()), **count}
    finally:
        pg.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))


def make_draft(store: str, state_dir: str) -> tuple[Callable[..., Sequence[Mapping[str, Any]]],
                                                   Callable[[], str], Callable[[], None]]:
    """(``draft(owner, oid, extra="", as_of="")``, ``model()``, ``close()``): the drafts path's own stores and
    Assistant, built on the first request (a run with nothing requested costs one query); one store
    per worker thread. ``extra`` goes to the session beside the message (``reply_drafts.line_draft``)."""
    from operating import autopilot as apm
    from operating import autopilot_agent as ag
    from operating import proactive_engine as pe
    from operating import reply_drafts as rd
    state = Path(state_dir)
    ns = types.SimpleNamespace(capture_state=None, state_dir=str(state))
    local = threading.local()
    stores: list[Any] = []
    shared: dict[str, Any] = {}
    lock = threading.Lock()

    def setup() -> dict[str, Any]:
        """The people, registry sources, Assistant and memory."""
        with lock:
            if not shared:
                people = pe._people(Path(store))
                shared.update(registry=apm.load_registry(), people=people,
                              maps=apm._people_maps(people), assistant=ag.from_environ(os.environ),
                              held={})
                apm.prime_memory(state, shared["held"])
                if shared["assistant"] is not None and getattr(shared["assistant"], "lookup", None) is None:
                    # no tool session registered or buildable (run through ``operator_api.autopilot_context
                    # assist-worker|assist-prepared``, with the readers file and session secret): every
                    # session ends ``agent_no_session``, typed on its matter
                    LOG.error("the Assistant has no tool session: nothing can be read for any matter")
        return shared

    def draft(owner: str, oid: str, extra: str = "", as_of: str = "",
              prepared: Any = None) -> Sequence[Mapping[str, Any]]:
        sh = setup()
        if getattr(local, "st", None) is None:
            local.st = apm._with_captures(apm._store(store), ns)
            stores.append(local.st)
            try:
                local.queue = apm.Queue(state / "autopilot" / apm.QUEUE_FILE)
                stores.append(local.queue)
            except OSError:
                local.queue = None
        return rd.line_draft(local.st, sh["registry"], sh["people"], sh["maps"], sh["assistant"],
                             owner, oid, state_dir=state, held=sh["held"], queue=local.queue,
                             extra=extra, as_of=as_of,
                             **({"prep": prepared} if prepared is not None else {}))

    def screen(text: str) -> Optional[str]:
        """Why ``text`` may not go to the model (the drafts' thread screen: pay or HR matter, a person
        the exclusion names; undecidable counts as a hit), or None."""
        from operating.autopilot_drafter import thread_screen
        return thread_screen(setup()["people"], text)

    def close() -> None:
        for st in stores:
            st.close()
    draft.screen = screen        # type: ignore[attr-defined]
    return draft, (lambda: getattr(shared.get("assistant"), "model", "") or ""), close


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python3 -m operating.assist_worker run --store S --state-dir D``: serve the requests
    (the drafts path's own stores and Assistant; one store per worker thread)."""
    p = argparse.ArgumentParser(prog="python3 -m operating.assist_worker")
    p.add_argument("command", choices=("run",))
    p.add_argument("--store", required=True)
    p.add_argument("--state-dir", required=True)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    import psycopg
    from operating.pg import config
    draft, model, close = make_draft(a.store, a.state_dir)
    try:
        with psycopg.connect(config.dsn(), autocommit=True) as pg:
            out = run_queue(pg, draft, model=model)
    finally:
        close()
    print(json.dumps(out, sort_keys=True))
    return 0 if out["status"] in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
