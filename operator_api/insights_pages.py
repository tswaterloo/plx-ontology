"""Saved Insights answer pages (PLAN-P2-A7): persist a whole Ask answer — its title,
summary, charts, and governed queries or measures — as one cohesive collection of live
tiles strictly scoped to the authenticated reader's own ``reader.key``.

Every tile is stored as a normal row in ``insights.insight`` (migration 0046) with
collection metadata under the ``_page`` key in its ``chart`` JSONB column:

* ``insights._chart_spec`` ignores ``_page`` when parsing the chart spec, so every saved
  tile renders, refreshes, schedules and deletes through the exact same door as any other
  saved insight.
* No business row, cell value or raw query result is stored: opening a saved page reads
  each tile again through ``plx.sql`` as its owner, through their own door.
* Every item (raw SQL or catalogue measure) is checked and read once as ``reader`` before
  ANY row is written (atomic all-or-nothing validation).
* Strictly scoped to ``reader.key``: ``view_as`` and excluded actors are refused; another
  reader can never list, read or delete someone else's saved answer page."""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from typing import Any, Mapping, Optional, Sequence

from . import models as m

PAGE_ID = re.compile(r"^p[a-f0-9]{12}$")


def page_meta(chart_raw: Any) -> dict[str, Any]:
    """Extract ``_page`` metadata from a stored ``insights.insight.chart`` value, else {}."""
    if isinstance(chart_raw, str):
        try:
            chart_raw = json.loads(chart_raw or "{}")
        except ValueError:
            return {}
    if not isinstance(chart_raw, Mapping):
        return {}
    raw = chart_raw.get("_page")
    if not isinstance(raw, Mapping):
        return {}
    pid = str(raw.get("id") or "")
    if not PAGE_ID.match(pid):
        return {}
    return {
        "id": pid,
        "title": str(raw.get("title") or ""),
        "summary": str(raw.get("summary") or ""),
        "question": str(raw.get("question") or ""),
        "created_at": str(raw.get("created_at") or ""),
    }


def resolve_item_sql(item: m.InsightAnswerItem) -> str:
    """Resolve one Ask answer item into a checked read-only SQL statement: either its
    explicit ``sql`` (checked by ``plx.sql``) or its catalogue ``measure`` (built by
    ``operating.measures.statement`` and checked by ``plx.sql``)."""
    from operating import measures as ms
    from operating.ontology_tools_identity import ToolError
    from operating.ontology_tools_sql import check
    from .insights import Refused

    raw_sql = (item.sql or "").strip()
    measure_id = (item.measure or "").strip()
    if not raw_sql and not measure_id:
        raise Refused("insight_invalid_sql", "each item needs either sql or a measure id")
    try:
        if raw_sql:
            return check(raw_sql)
        sql, _cite = ms.statement(
            measure_id,
            by=item.by,
            days=item.days,
            compare=item.compare,
            period=item.period,
            against=item.against,
            filter=item.filter,
            parameters=item.parameters,
        )
        return check(sql)
    except ToolError as exc:
        raise Refused("insight_invalid_sql", exc.message) from None


def _build_page(meta: Mapping[str, Any], specs: Sequence[Any]) -> m.InsightAnswerPage:
    from .insights import info
    return m.InsightAnswerPage(
        id=str(meta["id"]),
        title=str(meta["title"]),
        summary=str(meta.get("summary") or ""),
        question=str(meta.get("question") or ""),
        created_at=str(meta.get("created_at") or ""),
        items=[info(sp) for sp in specs],
    )


def save_answer_page(backend: Any, store: Any, reader: Any,
                     body: m.InsightAnswerSave) -> m.InsightAnswerPage:
    """Validate every item in ``body`` as ``reader`` and persist them atomically as one
    cohesive saved Insights page owned exclusively by ``reader.key``."""
    from .charts import NotDrawn, build, columns_of
    from .insights import Refused, Spec, _chart_spec, read_sql, watch_of

    if store is None:
        raise Refused("unavailable")
    store._writable(reader)

    page_title = body.title.strip()
    if not page_title:
        raise Refused("insight_invalid_sql", "title must not be blank")
    page_summary = (body.summary or "").strip()
    page_question = (body.question or "").strip()
    page_id = "p" + uuid.uuid4().hex[:12]
    created_at = dt.datetime.now(dt.timezone.utc).isoformat()
    meta = {
        "id": page_id,
        "title": page_title,
        "summary": page_summary,
        "question": page_question,
        "created_at": created_at,
    }

    # Phase 1: validate every item completely before writing anything (all-or-nothing)
    prepared: list[tuple[Spec, dict[str, Any]]] = []
    for item in body.items:
        item_title = item.title.strip()
        if not item_title:
            raise Refused("insight_invalid_sql", "each item needs a non-empty title")
        sql = resolve_item_sql(item)
        chart = _chart_spec(item.chart.model_dump() if item.chart else None)
        got = read_sql(backend, reader, sql)
        rows = [r.get("cells", []) for r in got.get("rows") or () if isinstance(r, Mapping)]
        if chart and rows:
            try:
                build(chart, got["columns"], rows, truncated=bool(got.get("truncated")))
            except NotDrawn as why:
                raise Refused("insight_chart", str(why)) from None
        missing = [c for c in columns_of(chart) if c not in got["columns"]] if chart else []
        if missing:
            raise Refused("insight_chart", "it has no column " + ", ".join(missing))
        item_question = (item.question or page_question).strip()
        spec = Spec(
            id="",
            title=item_title,
            about=item_question,
            sql=sql,
            chart=chart,
            group=f"Page: {page_title}",
            question=item_question,
            schedule=item.schedule,
            notify=item.notify,
            saved=True,
            page_id=page_id,
            page_title=page_title,
        )
        stored_chart = {**dict(chart), "_page": meta}
        prepared.append((spec, stored_chart))

    # Phase 2: persist all tiles in one Postgres transaction scoped strictly to reader.key
    saved_specs: list[Spec] = []
    with store.connect() as conn:
        (base_pos,) = conn.execute(
            "SELECT count(*) FROM insights.insight WHERE owner = %s AND deleted_at IS NULL",
            (reader.key,),
        ).fetchone()
        for offset, (spec, stored_chart) in enumerate(prepared):
            (rid,) = conn.execute(
                "INSERT INTO insights.insight (owner, title, question, sql, chart, schedule, "
                "notify, watch, position) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (
                    reader.key,
                    spec.title,
                    spec.question,
                    spec.sql,
                    json.dumps(stored_chart),
                    spec.schedule,
                    spec.notify,
                    json.dumps(watch_of(spec.watch) if spec.notify == "crosses" else {}),
                    base_pos + offset,
                ),
            ).fetchone()
            saved_specs.append(Spec(**{**spec.__dict__, "id": f"s{rid}", "saved": True}))
        conn.commit()

    return _build_page(meta, saved_specs)


def list_answer_pages(store: Any, reader: Any) -> m.InsightAnswerPageList:
    """List all saved Ask answer pages owned by ``reader.key`` (empty during ``view_as``
    or when the store is not configured)."""
    from .insights import _viewing_as

    if store is None or _viewing_as(reader):
        return m.InsightAnswerPageList(pages=[])
    with store.connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join('i.' + c for c in store.COLS)} FROM insights.insight i "
            "WHERE i.owner = %s AND i.deleted_at IS NULL AND i.chart ? '_page' "
            "ORDER BY i.position, i.id",
            (reader.key,),
        ).fetchall()
    order: list[str] = []
    metas: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        meta = page_meta(row[4])
        if not meta:
            continue
        pid = meta["id"]
        if pid not in grouped:
            order.append(pid)
            metas[pid] = meta
            grouped[pid] = []
        grouped[pid].append(store._spec(row))
    return m.InsightAnswerPageList(
        pages=[_build_page(metas[pid], grouped[pid]) for pid in order]
    )


def get_answer_page(store: Any, reader: Any,
                    page_id: str) -> Optional[m.InsightAnswerPage]:
    """Read one saved Ask answer page owned by ``reader.key``, or None if not theirs."""
    from .insights import _viewing_as

    if store is None or _viewing_as(reader) or not PAGE_ID.match(page_id or ""):
        return None
    with store.connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join('i.' + c for c in store.COLS)} FROM insights.insight i "
            "WHERE i.owner = %s AND i.deleted_at IS NULL "
            "AND i.chart -> '_page' ->> 'id' = %s "
            "ORDER BY i.position, i.id",
            (reader.key, page_id),
        ).fetchall()
    if not rows:
        return None
    meta = page_meta(rows[0][4])
    if not meta:
        return None
    return _build_page(meta, [store._spec(r) for r in rows])


def delete_answer_page(store: Any, reader: Any, page_id: str) -> bool:
    """Soft-delete all tiles belonging to one of ``reader.key``'s saved answer pages."""
    from .insights import Refused

    if store is None:
        raise Refused("unavailable")
    store._writable(reader)
    if not PAGE_ID.match(page_id or ""):
        return False
    with store.connect() as conn:
        rows = conn.execute(
            "UPDATE insights.insight SET deleted_at = now(), updated_at = now() "
            "WHERE owner = %s AND deleted_at IS NULL "
            "AND chart -> '_page' ->> 'id' = %s "
            "RETURNING id",
            (reader.key, page_id),
        ).fetchall()
        conn.commit()
    return bool(rows)
