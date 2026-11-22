"""The older-mail leg of search: mail the box's search tables do not hold, searched in the
old-mail store on sb4ec (``operating.embed.oldmail``) and admitted only by the box.

Off unless ``PLX_SEARCH_OLDMAIL=1`` and ``PLX_OLDMAIL_DSN`` (the store through the box's
tunnel, ``deploy/spark/oldmail/plx-oldmail-tunnel.service``) are both set.

One leg per search, beside the box's own retrievers (``operating.search.core``):

1. In the store: the meaning list (``retrievers.dense``) and the words list
   (``retrievers.bm25``) under the reader's scope (the box's allowed audience keys and held
   markings: the same predicate as on the box), then each candidate's object, kind,
   audience key and markings. No text yet.
2. On the box, before ranking or counting: a candidate is kept only when the box admits its
   object now (``admitted``): its mail (or attachment, and that attachment's message) is in
   the box's comms tables in the same mailbox, the people exclusion does not hold it (the
   owner excepted), Freightcom is decided not named (``ai_ok``), and every marking is held.
   Mail deleted or purged since the store was built, a moved mailbox, a newly excluded
   person: each falls out here, whatever the store says.
3. In the store again: the shown fields of the kept candidates only (``retrievers.details``).

The whole leg has ``BUDGET_S``: past it, or on any error, the search answers without it and
names it failed (``older_mail``), which the tools state as "older mail wasn't searched"
(``plug.fallback_gap``). A store that failed to answer is not asked again for
``RETRY_AFTER_S``, so each question does not wait out the budget. Store connections are
read-only and kept for reuse (at most ``POOL``)."""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Mapping, Sequence

ENV_ON = "PLX_SEARCH_OLDMAIL"
ENV_DSN = "PLX_OLDMAIL_DSN"
BUDGET_S = 1.0
STATEMENT_MS = 900
RETRY_AFTER_S = 60.0
POOL = 4
# the box's re-check needs at least this much of the budget left, else the leg stops
RECHECK_MIN_MS = 50
KINDS = ("mail", "attachment")
ATTACHMENT_MARK = "#attachment="


class NotSearched(RuntimeError):
    """The leg did not run or did not finish in time (its reason is the message)."""


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ENV_ON, "").strip() == "1" and bool(env.get(ENV_DSN, "").strip())


_lock = threading.Lock()
_idle: list[Any] = []
_down_until = 0.0


def _reset() -> None:
    """Forget the pool and the back-off (tests)."""
    global _down_until
    with _lock:
        for conn in _idle:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        _idle.clear()
        _down_until = 0.0


def _connect(dsn: str) -> Any:
    import psycopg
    return psycopg.connect(dsn, autocommit=True, connect_timeout=2,
                           options=f"-c statement_timeout={STATEMENT_MS} "
                                   "-c default_transaction_read_only=on")


def _borrow(dsn: str, connect: Callable[[str], Any]) -> Any:
    with _lock:
        while _idle:
            conn = _idle.pop()
            if not getattr(conn, "closed", False):
                return conn
    return connect(dsn)


def _give_back(conn: Any) -> None:
    with _lock:
        if len(_idle) < POOL and not getattr(conn, "closed", False):
            _idle.append(conn)
            return
    conn.close()


def _store_lists(sc: Any, scope: dict[str, Any], vectors: Sequence[Sequence[float]],
                 model: str, texts: Sequence[str], n: int, ef_search: int
                 ) -> tuple[list[tuple[str, float]], list[tuple[str, float]], dict[str, tuple]]:
    from operating.search import retrievers as rt
    own = dict(scope, kinds=[k for k in (scope.get("kinds") or KINDS) if k in KINDS])
    if not own["kinds"]:
        return [], [], {}
    dense = rt.dense(sc, own, vectors, model, n, ef_search=ef_search,
                     timeout_ms=STATEMENT_MS) if vectors else []
    words = rt.bm25(sc, own, list(texts), n) if texts else []
    ids = sorted({c for c, _ in dense} | {c for c, _ in words})
    meta = {r[0]: tuple(r[1:]) for r in sc.execute(
        "SELECT chunk_id, object_id, kind, audience_key, markings, source_handle "
        "FROM search.chunks WHERE chunk_id = ANY(%s::text[])", (ids,)).fetchall()} if ids else {}
    return dense, words, meta


def admitted(box: Any, scope: Mapping[str, Any],
             candidates: Mapping[str, tuple]) -> set[str]:
    """The candidate chunk ids the box admits now (see the module doc). ``candidates``:
    chunk id -> (object_id, kind, audience_key, markings, source_handle) as the store has
    them. Anything the box cannot decide is left out (fail closed)."""
    from operating.pg.access_classes import OWNER_KEY
    allowed, held = set(scope.get("allowed") or ()), set(scope.get("held") or ())
    owner = OWNER_KEY in allowed
    objects = sorted({c[0] for c in candidates.values() if isinstance(c[0], str) and c[0]})
    if not objects:
        return set()
    rows: dict[str, list[tuple]] = {}
    for table in ("emails", "attachments"):
        for oid, handle, keys, marks, excluded, ai_ok in box.execute(
                f"SELECT t.object_ids[1], t.handle, t.audience_keys, t.rec_marks, t.excluded, "
                f"t.ai_ok FROM biz.{table} t WHERE t.object_ids[1] = ANY(%s::text[])",
                (objects,)).fetchall():
            rows.setdefault(oid, []).append((table, handle, set(keys or ()), set(marks or ()),
                                             excluded, ai_ok))
    people = {r[0] for r in box.execute(
        "SELECT object_id FROM ontology.people_excluded_objects WHERE object_id = "
        "ANY(%s::text[])", (objects,)).fetchall()}
    # an attachment is read only where its message is
    messages = sorted({h.split(ATTACHMENT_MARK, 1)[0] for got in rows.values()
                       for t, h, *_ in got if t == "attachments" and isinstance(h, str)
                       and ATTACHMENT_MARK in h})
    parents: dict[str, list[tuple]] = {}
    if messages:
        for handle, oid, keys, marks, excluded, ai_ok in box.execute(
                "SELECT e.handle, e.object_ids[1], e.audience_keys, e.rec_marks, e.excluded, "
                "e.ai_ok FROM biz.emails e WHERE e.handle = ANY(%s::text[])",
                (messages,)).fetchall():
            parents.setdefault(handle, []).append((oid, set(keys or ()), set(marks or ()),
                                                   excluded, ai_ok))
        more = sorted({p[0] for got in parents.values() for p in got if p[0]})
        if more:
            people |= {r[0] for r in box.execute(
                "SELECT object_id FROM ontology.people_excluded_objects WHERE object_id = "
                "ANY(%s::text[])", (more,)).fetchall()}

    def passes(oid: Any, key: str, keys: set, marks: set, excluded: Any, ai_ok: Any) -> bool:
        return (key in keys and key in allowed and marks <= held and ai_ok is True
                and (owner or (excluded is False and oid not in people)))

    keep: set[str] = set()
    for cid, (oid, kind, key, markings, _handle) in candidates.items():
        if kind not in KINDS or not isinstance(key, str) or not set(markings or ()) <= held:
            continue
        for table, handle, keys, marks, excluded, ai_ok in rows.get(oid, ()):
            if (table == "emails") != (kind == "mail"):
                continue
            if not passes(oid, key, keys, marks, excluded, ai_ok):
                continue
            if table == "attachments":
                parent = (handle or "").split(ATTACHMENT_MARK, 1)[0]
                if not any(passes(p[0], key, *p[1:]) for p in parents.get(parent, ())):
                    continue
            keep.add(cid)
            break
    return keep


def search(box: Any, scope: dict[str, Any], vectors: Sequence[Sequence[float]], model: str,
           texts: Sequence[str], n: int, *, ef_search: int = 1000,
           environ: Mapping[str, str] | None = None, budget_s: float = BUDGET_S,
           connect: Callable[[str], Any] = _connect,
           clock: Callable[[], float] = time.monotonic
           ) -> tuple[list[tuple[str, float]], list[tuple[str, float]], dict[str, dict]]:
    """(meaning list, words list, shown fields by chunk id) of the store's candidates the
    box admits (``box``: a connection to the box's database). Raises ``NotSearched``."""
    global _down_until
    env = os.environ if environ is None else environ
    if not enabled(env):
        raise NotSearched("off")
    if clock() < _down_until:
        raise NotSearched("store_unreachable_recently")
    dsn = env[ENV_DSN].strip()
    deadline = clock() + budget_s
    out: dict[str, Any] = {}

    def in_store(work: Callable[[Any], Any]) -> Any:
        """``work`` on a store connection, at most until the deadline."""
        global _down_until
        done, late = threading.Event(), threading.Event()

        def run() -> None:
            conn = None
            try:
                conn = _borrow(dsn, connect)
                out["value"] = work(conn)
                out.pop("error", None)
            except Exception as exc:  # noqa: BLE001 - named below
                out["error"] = exc
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    conn = None
            finally:
                if conn is not None:
                    if late.is_set():
                        conn.close()   # its answer came too late: never reused
                    else:
                        _give_back(conn)
                done.set()
        out.pop("value", None)
        t = threading.Thread(target=run, name="plx-older-mail", daemon=True)
        t.start()
        left = deadline - clock()
        if left <= 0 or not done.wait(left):
            late.set()   # the thread closes its connection when the statement ends
            with _lock:
                _down_until = clock() + RETRY_AFTER_S
            raise NotSearched("timeout")
        if "error" in out:
            with _lock:
                _down_until = clock() + RETRY_AFTER_S
            raise NotSearched(type(out["error"]).__name__)
        return out["value"]

    dense, words, meta = in_store(
        lambda sc: _store_lists(sc, scope, vectors, model, texts, n, ef_search))
    # the box's re-check inside the same budget (its own statement timeout: what is left)
    left_ms = int((deadline - clock()) * 1000)
    if left_ms < RECHECK_MIN_MS:
        raise NotSearched("timeout")
    try:
        with box.transaction():
            box.execute(f"SET LOCAL statement_timeout = {left_ms}")
            keep = admitted(box, scope, meta)
    except Exception as exc:  # noqa: BLE001 - a re-check that did not finish admits nothing
        raise NotSearched("recheck_" + type(exc).__name__) from exc
    if not keep:
        return [], [], {}
    from operating.search import retrievers as rt
    info = in_store(lambda sc: rt.details(sc, scope, sorted(keep)))
    keep &= set(info)
    return ([(c, s) for c, s in dense if c in keep], [(c, s) for c, s in words if c in keep],
            {c: info[c] for c in keep})
