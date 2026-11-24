"""Precedents (ASSIST specs 2.5 steps 3-6): what a person did, paired with what they did it to,
so the plan tier can say what they would do now. Built from ``assist.actions`` (``assist_actions``)
and the comms views, set-based, no window on how old the trigger or the matter is (owner 11-23).

* ``candidates``: every send of a person not yet a precedent, with the newest earlier message of
  its thread from someone else (the trigger). The act is data, not words: ``reply`` when the
  recipients include the trigger's sender; ``forward`` when they do not and include an address not
  on the trigger; ``new_message`` (trigger ``none``) when nothing earlier was sent by anyone else.
  A chat has no recipient list in the data (MODEL2's gap), so a chat send is a ``reply`` or a
  ``new_message``. A system act (Sage, SharePoint, Planner, FileMaker, an MC decision) is a
  ``system_write`` to ``{system:kind}``. A matter the person set aside is a ``nothing`` precedent
  (their own word, ``done_reason = person_dropped``): no 7-day rule guesses it.
* ``build``: the precedents' own words (``act_text``: the send's whole own text, ``assist_readings.
  own_text``, one definition) and their ``situation`` (what is embedded), inserted once
  (``ON CONFLICT DO NOTHING``); ``embed`` stores one vector per situation hash in
  ``assist.precedent_vectors`` (the embedder is injected: the Spark GPU's in production).
* ``nearest``: EVERY precedent of the person dated before the matter's basis (the fence), ranked by
  similarity, same-party cases first; no LIMIT (the plan prompt fills the model window nearest
  first; the draft session pages the rest by search). A thin history adds colleagues' cases of the
  same role, marked ``colleague``. ``total`` is the person's own count.
Evidence: isolated-fixture (``operating/tests/test_assist_precedents.py``); live-unverified."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping, Optional, Sequence

SCHEMA = "assist"
THIN = 30                  # the person's own precedents below which colleagues' are added (spec 2.5)
SYSTEMS = ("sage", "sharepoint", "planner", "filemaker")

_SENDS = f"""
WITH sends AS (
  SELECT a.action_key, a.person, a.at, a.kind, a.system, a.source, a.message_key, a.thread_key,
         a.recipients, a.target_handle,
         CASE a.system WHEN 'mail' THEN 'email' ELSE 'chat' END AS channel
  FROM {SCHEMA}.actions a
  WHERE a.kind IN ('mail_sent', 'teams_sent') AND a.not_human = '' AND a.thread_key IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.precedents p WHERE p.action_key = a.action_key)),
mail AS (
  SELECT s.*, t.message_id AS t_key, t.sender AS t_sender, t.received_at AS t_at,
         t.to_addresses || t.cc_addresses || ARRAY[t.sender] AS t_on, t.subject AS t_subject,
         t.object_ids[1] AS t_oid, t.handle AS t_handle
  FROM sends s LEFT JOIN LATERAL (
    SELECT e.* FROM biz.emails e
    WHERE s.system = 'mail' AND e.thread = s.thread_key AND e.received_at < s.at
      AND NOT (lower(e.sender) = ANY (SELECT lower(x) FROM biz.mailbox_owners o,
                                      unnest(o.addrs) x WHERE o.mailbox = s.person))
    ORDER BY e.received_at DESC LIMIT 1) t ON true WHERE s.system = 'mail'),
chat AS (
  SELECT s.*, t.handle AS t_key, t.sender AS t_sender, t.created_at AS t_at,
         NULL::text[] AS t_on, t.subject AS t_subject, t.object_ids[1] AS t_oid, t.handle AS t_handle
  FROM sends s LEFT JOIN LATERAL (
    SELECT m.* FROM biz.teams_messages m
    WHERE s.system = 'teams' AND m.chat = s.thread_key AND m.created_at < s.at
      AND m.sender IS DISTINCT FROM (SELECT x.sender FROM biz.teams_messages x
                                     WHERE x.handle = s.target_handle)
    ORDER BY m.created_at DESC LIMIT 1) t ON true WHERE s.system = 'teams')
SELECT u.*, r.summary AS r_summary, r.act AS r_act, r.counterpart AS r_counterpart,
       r.counterpart_party AS r_party,
       (SELECT coalesce(e.object_ids[1], tm.object_ids[1]) FROM (SELECT 1) one
        LEFT JOIN biz.emails e ON u.system = 'mail' AND e.handle = u.target_handle
        LEFT JOIN biz.teams_messages tm ON u.system = 'teams' AND tm.handle = u.target_handle) AS oid
FROM (SELECT * FROM mail UNION ALL SELECT * FROM chat) u
LEFT JOIN {SCHEMA}.readings r ON r.source = CASE u.system WHEN 'mail' THEN 'emails'
     ELSE 'teams_messages' END AND r.message_key = u.t_key"""

_SYSTEM = f"""
SELECT a.action_key, a.person, a.at, a.kind, a.system, a.fields
FROM {SCHEMA}.actions a
WHERE (a.system = ANY(%(systems)s) OR a.kind = 'approval') AND a.not_human = ''
  AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.precedents p WHERE p.action_key = a.action_key)"""

_DROPPED = f"""
SELECT m.matter_id, m.owner_person, m.done_at, m.sources, m.title, m.counterpart,
       m.counterpart_party, m.subject_key
FROM {SCHEMA}.matters m
WHERE m.state = 'dropped' AND m.done_reason = 'person_dropped' AND m.kind = 'conversation'
  AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.precedents p WHERE p.action_key = 'none:' || m.matter_id)"""

_PUT = f"""
INSERT INTO {SCHEMA}.precedents (action_key, person, role, at, trigger_kind, trigger_source,
    trigger_message_key, trigger_at, thread_key, channel, counterpart, counterpart_party, act,
    act_to, latency_s, situation, situation_sha, act_text, act_fields)
SELECT x.action_key, x.person, coalesce(x.role, ''), x.at, x.trigger_kind, x.trigger_source,
       x.trigger_message_key, x.trigger_at, x.thread_key, x.channel, coalesce(x.counterpart, ''),
       x.counterpart_party, x.act, coalesce(x.act_to, '{{}}'), x.latency_s, x.situation,
       x.situation_sha, coalesce(x.act_text, ''), coalesce(x.act_fields, '{{}}')
FROM jsonb_to_recordset(%(v)s::jsonb) AS x(action_key text, person text, role text, at timestamptz,
    trigger_kind text, trigger_source text, trigger_message_key text, trigger_at timestamptz,
    thread_key text, channel text, counterpart text, counterpart_party text, act text,
    act_to text[], latency_s integer, situation text, situation_sha text, act_text text,
    act_fields jsonb)
ON CONFLICT (action_key) DO NOTHING"""


def act_of(recipients: Sequence[str], t_sender: Optional[str], t_on: Optional[Sequence[str]],
           *, chat: bool) -> tuple[str, str]:
    """(act, trigger kind) from the data: who the send went to against who the trigger was from."""
    if t_sender is None:
        return "new_message", "none"
    if chat:
        return "reply", "message"
    rec = {str(r).lower() for r in recipients or ()}
    if t_sender.lower() in rec:
        return "reply", "message"
    on = {str(x).lower() for x in t_on or ()}
    return ("forward", "message") if rec - on else ("reply", "message")


def sha_of(situation: str) -> str:
    return hashlib.sha256(situation.encode("utf-8")).hexdigest()


def situation_of(subject: str, own: str, summary: str = "", act: str = "", counterpart: str = ""
                 ) -> str:
    """What is embedded: the trigger's reading when there is one (what it was and what it asked),
    else its subject and own words whole."""
    if summary:
        return f"{summary} | asks: {act} | {counterpart}"
    return f"{subject} | {own}"


def candidates(pg: Any) -> dict[str, list[dict[str, Any]]]:
    """Sends, system acts and set-aside matters not yet precedents (module text)."""
    def rows(sql: str, params: Any = None) -> list[dict[str, Any]]:
        cur = pg.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"sends": rows(_SENDS), "system": rows(_SYSTEM, {"systems": list(SYSTEMS)}),
            "dropped": rows(_DROPPED)}


def build(pg: Any, texts: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Mapping[str, str]]],
          *, roles: Optional[Mapping[str, str]] = None) -> dict[str, int]:
    """Insert every candidate as a precedent. ``texts(sends)`` -> {action_key: the send's own
    words, whole} (``assist_readings.own_text`` over the stored text: the caller reads it, the
    store being the box's). ``roles``: person -> role (the roles file). Returns the counts."""
    found = candidates(pg)
    own = dict(texts(found["sends"]))     # action_key -> {"own": the send's words, "trigger": its trigger's}
    out: list[dict[str, Any]] = []
    for s in found["sends"]:
        act, kind = act_of(s["recipients"], s["t_sender"], s["t_on"], chat=s["system"] == "teams")
        words = (own.get(s["action_key"]) or {}).get("own", "")
        asked = (own.get(s["action_key"]) or {}).get("trigger", "")
        out.append({"action_key": s["action_key"], "person": s["person"],
                    "role": (roles or {}).get(s["person"], ""), "at": s["at"],
                    "trigger_kind": kind, "trigger_source": None if kind == "none" else (
                        "emails" if s["system"] == "mail" else "teams_messages"),
                    "trigger_message_key": s["t_key"], "trigger_at": s["t_at"],
                    "thread_key": s["thread_key"], "channel": s["channel"], "act": act,
                    "act_to": [str(r) for r in s["recipients"] or ()],
                    "latency_s": int((s["at"] - s["t_at"]).total_seconds()) if s["t_at"] else None,
                    "counterpart": s["r_counterpart"] or "", "counterpart_party": s["r_party"],
                    "situation": situation_of(s["t_subject"] or "", asked, s["r_summary"] or "",
                                              s["r_act"] or "", s["r_counterpart"] or "")
                    if kind != "none" else situation_of("", words),
                    "act_text": words})
    for s in found["system"]:
        kind = "approval" if s["kind"] == "approval" else s["kind"]
        to = "mc:approval_decide" if s["kind"] == "approval" else f"{s['system']}:{kind}"
        out.append({"action_key": s["action_key"], "person": s["person"],
                    "role": (roles or {}).get(s["person"], ""), "at": s["at"],
                    "trigger_kind": "none", "channel": "mission_control" if s["kind"] == "approval"
                    else "system", "act": "system_write", "act_to": [to],
                    "situation": f"{kind} | {s['system']}", "act_fields": s["fields"] or {}})
    for d in found["dropped"]:
        out.append({"action_key": "none:" + d["matter_id"], "person": d["owner_person"],
                    "role": (roles or {}).get(d["owner_person"], ""), "at": d["done_at"],
                    "trigger_kind": "none", "thread_key": d["subject_key"], "channel": "email",
                    "counterpart": d["counterpart"], "counterpart_party": d["counterpart_party"],
                    "act": "nothing", "situation": d["title"]})
    for o in out:
        o["situation_sha"] = sha_of(o["situation"])
    for i in range(0, len(out), 2000):
        pg.execute(_PUT, {"v": json.dumps(out[i:i + 2000], default=str)})
    counts: dict[str, int] = {}
    for o in out:
        counts[o["act"]] = counts.get(o["act"], 0) + 1
    return {"candidates": len(out), **counts}


def _encode(embedder: Any, texts: Sequence[str]) -> Any:
    """The vectors of ``texts`` by whichever the embedder offers (``encode``: the search
    embedders, a numpy array; ``embed``: lists)."""
    return embedder.encode(texts) if hasattr(embedder, "encode") else embedder.embed(texts)


def stored_texts(pg: Any) -> Callable[[Sequence[Mapping[str, Any]]], dict[str, dict[str, str]]]:
    """``build``'s ``texts``: each send's own words and its trigger's, whole, from the stored
    message text (``assist_readings.message_texts`` + ``own_text``: the one definition of a
    message's own part; a marked, missing or unreadable text is an empty string, so the
    precedent says nothing rather than something made up)."""
    from operating import assist_readings as ar

    def read(sends: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
        pairs = [(s["target_handle"], s["oid"]) for s in sends if s.get("oid")] + [
            (s["t_handle"], s["t_oid"]) for s in sends if s.get("t_oid")]
        got = ar.message_texts(pg, pairs)
        out = {}
        for s in sends:
            def own(handle: Any, oid: Any) -> str:
                text = got.get((handle, oid), (None, ""))[0]
                return ar.own_text(text, mail=s["system"] == "mail")[0] if text else ""
            out[s["action_key"]] = {"own": own(s["target_handle"], s.get("oid")),
                                    "trigger": own(s["t_handle"], s.get("t_oid"))}
        return out
    return read


_EMBED = f"""
SELECT DISTINCT p.situation_sha, p.situation FROM {SCHEMA}.precedents p
WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.precedent_vectors v
                  WHERE v.situation_sha = p.situation_sha AND v.embed_model = %s)"""


def embed(pg: Any, embedder: Any, model: str, *, batch: int = 64) -> int:
    """One vector per situation hash without one (``embedder.embed(texts)``), all of them."""
    rows = pg.execute(_EMBED, (model,)).fetchall()
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        vectors = _encode(embedder, [t for _s, t in part])
        pg.execute(
            f"INSERT INTO {SCHEMA}.precedent_vectors (situation_sha, embed_model, embedding) "
            "SELECT u.sha, %s, u.vec::halfvec FROM unnest(%s::text[], %s::text[]) AS u(sha, vec) "
            "ON CONFLICT DO NOTHING",
            (model, [s for s, _t in part],
             ["[" + ",".join(f"{x:.6f}" for x in v) + "]" for v in vectors]))
    return len(rows)


_NEAREST = f"""
WITH req AS (
  SELECT * FROM unnest(%(rid)s::text[], %(person)s::text[], %(role)s::text[], %(basis)s::timestamptz[],
                       %(q)s::text[], %(party)s::text[], %(cp)s::text[])
       AS r(rid, person, role, basis, q, party, cp))
SELECT r.rid, u.* FROM req r CROSS JOIN LATERAL (
  WITH mine AS (
    SELECT p.*, pv.embedding <=> r.q::halfvec AS d
    FROM {SCHEMA}.precedents p
    JOIN {SCHEMA}.precedent_vectors pv ON pv.situation_sha = p.situation_sha AND pv.embed_model = %(m)s
    WHERE p.person = r.person AND p.at < r.basis),
  ranked AS (
    SELECT mine.*, 'own' AS why,
           (r.party IS NOT NULL AND counterpart_party = r.party)
           OR (r.party IS NULL AND counterpart <> '' AND counterpart = r.cp) AS same
    FROM mine),
  pool AS (
    SELECT p.*, pv.embedding <=> r.q::halfvec AS d, 'colleague' AS why, false AS same
    FROM {SCHEMA}.precedents p
    JOIN {SCHEMA}.precedent_vectors pv ON pv.situation_sha = p.situation_sha AND pv.embed_model = %(m)s
    WHERE p.role = r.role AND r.role <> '' AND p.person <> r.person AND p.at < r.basis
      AND (SELECT count(*) FROM mine) < {THIN})
  SELECT x.*, row_number() OVER (ORDER BY (x.why = 'own') DESC, x.same DESC, x.d, x.at DESC) AS rn
  FROM (SELECT * FROM ranked UNION ALL SELECT * FROM pool) x) u
ORDER BY r.rid, u.rn"""


def _vec(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


def nearest_many(pg: Any, asks: Sequence[Mapping[str, Any]], *, model: str
                 ) -> dict[str, tuple[list[dict[str, Any]], int]]:
    """For a batch of matters, ONE statement (a LATERAL per ask): ``asks`` are ``{id, person, role,
    basis_at, vector, party?, counterpart?}``; returns ``{id: (every precedent of that person
    dated before that basis, same-party cases first then by similarity, colleagues' after when
    the person's own are thin; the person's own total)}``. An ask with nothing to show maps to
    ([], total): every id asked is answered. No limit (the plan prompt cuts by the model window)."""
    if not asks:
        return {}
    ids = [str(a["id"]) for a in asks]
    if len(set(ids)) != len(ids):
        raise ValueError("nearest_many: an id is asked twice")
    cur = pg.execute(_NEAREST, {
        "rid": ids, "person": [a["person"] for a in asks], "role": [a.get("role") or "" for a in asks],
        "basis": [a["basis_at"] for a in asks], "q": [_vec(a["vector"]) for a in asks],
        "party": [a.get("party") for a in asks], "cp": [a.get("counterpart") or "" for a in asks],
        "m": model})
    cols = [d.name for d in cur.description]
    out: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
    for r in cur.fetchall():
        row = dict(zip(cols, r))
        row.pop("rn")
        out[row.pop("rid")].append(row)
    people = sorted({a["person"] for a in asks})
    totals = dict(pg.execute(f"SELECT person, count(*) FROM {SCHEMA}.precedents WHERE person = ANY(%s) "
                             "GROUP BY person", (people,)).fetchall())
    return {str(a["id"]): (out[str(a["id"])], int(totals.get(a["person"], 0))) for a in asks}


def nearest(pg: Any, person: str, role: str, basis_at: Any, vector: Sequence[float], *,
            model: str, party: Optional[str] = None, counterpart: str = "", offset: int = 0,
            limit: Optional[int] = None) -> tuple[list[dict[str, Any]], int]:
    """(every precedent of ``person`` dated before ``basis_at``, ...): ``nearest_many`` for one
    ask (one definition). ``limit`` / ``offset`` page it for the search tool: the plan prompt takes
    it all and cuts by the window."""
    rows, total = nearest_many(pg, [{"id": "one", "person": person, "role": role,
                                     "basis_at": basis_at, "vector": vector, "party": party,
                                     "counterpart": counterpart}], model=model)["one"]
    rows = rows[offset:]
    return (rows[:limit] if limit is not None else rows), total


def roles_of(roles_file: Any) -> dict[str, str]:
    """person -> role from the roles file (``{"roles": {role: [ids]}}``): the first role that
    names them; {} when the file is not there (precedents then carry no role: no colleague pool)."""
    from pathlib import Path
    path = Path(roles_file)
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for role, members in sorted((json.loads(path.read_text()).get("roles") or {}).items()):
        for who in members or ():
            out.setdefault(str(who), role)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python3 -m operating.assist_precedents run --state-dir D``: pair what is new, then
    embed every situation without a vector on a Spark GPU host (``operating.embed.spark_embed``:
    the search follower's own path). No fit GPU host: nothing is embedded, said, and the next run
    does it; there is no CPU fallback on the box (owner 11-22: background work stays off it)."""
    import argparse
    from pathlib import Path
    p = argparse.ArgumentParser(prog="python3 -m operating.assist_precedents")
    p.add_argument("command", choices=("run",))
    p.add_argument("--state-dir", required=True)
    a = p.parse_args(argv)
    import psycopg
    from operating.embed import spark_embed
    from operating.pg import config
    state = Path(a.state_dir)
    with psycopg.connect(config.dsn(), autocommit=True) as pg:
        built = build(pg, stored_texts(pg), roles=roles_of(state / "work-graph" / "roles-derived.json"))
        model = config.embed_model()
        hosts = spark_embed.fit_hosts(state)
        with spark_embed.embedder(state, model, config.embed_dim(model), hosts) as emb:
            if emb is None:
                print(json.dumps({"status": "waiting", "reason": "no_gpu_host", "built": built}))
                return 0
            embedded = embed(pg, emb, model)
    print(json.dumps({"status": "ok", "built": built, "embedded": embedded}, sort_keys=True))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
