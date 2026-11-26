"""Promises and waits (ASSIST specs 2.9 steps 2-3).

A reading of a message the person SENT (``sender_person``) that holds a ``promise`` (what they said
they would do) opens a ``promise`` matter for them unless an open ``conversation`` matter already
tracks the same message, thread, or same-moment commitment (in which case the conversation matter is
enriched with the commitment due date, ``next_act``, and promise metadata instead of spawning a
duplicate ``promise`` row). One that holds ``waiting_on`` (what they wait for from whom) opens a
``wait`` matter with a clear plain-English title naming what is being waited on and a populated
``counterpart``.

There is no fixed default: when the message states no date, the matter is due after the person's
OWN measured lag (the 75th percentile, over the answered waits or kept promises in their mail), and
a person with no such history gets the matter shown at once asking when to look for it
(``next_act = 'ask_person'``, due NULL). A wait stays out of the list until it is due
(``assist_matters.as_items``). A wait closes when someone else writes in the thread after it; a
promise when the person writes in the thread after it (``reverify``). Automated IT/system monitoring
readings are never turned into wait or promise matters. One statement per bulk step; no per-matter
loop.
Evidence: isolated-fixture (``operating/tests/test_assist_waits.py``); live-unverified."""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

SCHEMA = "assist"
SAME_EVENT_WINDOW_SECONDS = 120.0

_WAIT_LEAD_RE = re.compile(
    r"^(?:(?:still\s+)?waiting\s+(?:for|on)\s+|awaiting\s+|no\s+answer\s+yet\s+from\s+[^:]+:\s*)",
    re.IGNORECASE,
)
_WAIT_TRAILING_GENERIC_FROM_RE = re.compile(
    r"(?:[,;\s]+)\bfrom\s+(?:them|our\s+side|their\s+side|us|the\s+team|someone)\s*$",
    re.IGNORECASE,
)
_AGENT_SELF_MSG_RE = re.compile(
    r"^\s*(?:agent\s+reports\b|workbench\s+can\s+now\b|"
    r"instant\s+updates\s+can'?t\s+be\s+set\s+up\b)",
    re.IGNORECASE,
)
_SELF_NOTIFY_TEAMS_RE = re.compile(
    r"^\s*(?:agent\s+reports\b|two\s+things\s+need\s+your\s+attention\b|"
    r"instant\s+updates\s+can'?t\s+be\s+set\s+up\b|workbench\s+can\s+now\b)",
    re.IGNORECASE,
)
_WAIT_FROM_TRAILING_RE = re.compile(
    r"[,;\s]+\bfrom\s+([A-Z][A-Za-z0-9.'&-]+(?:\s+[A-Z][A-Za-z0-9.'&-]+){0,3})\s*$",
)
_WAIT_FROM_ANY_RE = re.compile(
    r"(?:\bfrom\s+([A-Z][A-Za-z0-9.'&-]+(?:\s+[A-Z][A-Za-z0-9.'&-]+){0,3})\b|"
    r"^([A-Z][A-Za-z0-9.'&-]+(?:\s+[A-Z][A-Za-z0-9.'&-]+){0,2})\s*:)"
)
_PROMISE_LEAD_RE = re.compile(r"^you\s+said\s*:\s*", re.IGNORECASE)
_POSSESSIVE_RE = re.compile(r"(?:'s|\u2019s)\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CODE_TOKEN_RE = re.compile(r"^[a-z]*\d+[a-z0-9]*$")
_GENERIC_COUNTERPARTS = frozenset({
    "them", "the", "a", "an", "someone", "whoever", "source", "unknown", "none", "n/a",
})
# guard-ok: English grammatical stopwords stripped only when checking same-moment promise/conversation topic overlap.
_TOPIC_STOPWORDS = frozenset({
    "the", "a", "an", "to", "for", "of", "on", "in", "is", "are", "was", "were",
    "and", "or", "with", "from", "by", "at", "it", "this", "that", "be", "has", "have",
    "had", "he", "she", "they", "him", "her", "his", "their", "them", "you", "your",
    "we", "our", "will", "would", "can", "could", "should", "may", "might", "been",
    "being", "says", "said", "trying", "try", "going", "about", "into", "still",
    "also", "just", "please", "thanks", "thank", "need", "needs", "want", "wants",
    "as", "up", "out", "so", "if", "not", "no", "yes", "do", "does", "did",
})


def _clean_text(text: Any) -> str:
    from operating.assist_matters import clean_user_text
    return clean_user_text(text or "")


def _clean_counterpart(raw: Any) -> str:
    cleaned = _clean_text(raw).strip(" .,;:-")
    if not cleaned or cleaned.lower() in _GENERIC_COUNTERPARTS:
        return ""
    return cleaned


def _infer_counterpart_from_said(said: str) -> str:
    cleaned = _clean_text(said)
    for pat in (_WAIT_FROM_TRAILING_RE, _WAIT_FROM_ANY_RE):
        m = pat.search(cleaned)
        if not m:
            continue
        candidate = next((g for g in m.groups() if g), "")
        got = _clean_counterpart(candidate)
        if got:
            return got
    return ""


def _infer_counterpart_from_recipients(
    to_addresses: Sequence[str],
    mine_addrs: Iterable[str],
    staff_names: Mapping[str, str],
) -> str:
    from operating.assist_matters import _name_from_address
    mine = {str(a or "").strip().lower() for a in mine_addrs if a}
    for addr in to_addresses or ():
        low = str(addr or "").strip().lower()
        if not low or low in mine:
            continue
        if low in staff_names and staff_names[low]:
            return staff_names[low]
        named = _name_from_address(low)
        if named:
            return named
        if "@" in low:
            return low
    return ""


def resolve_wait_counterpart(
    counterpart: Any,
    said: Any,
    to_addresses: Sequence[str] = (),
    mine_addrs: Iterable[str] = (),
    staff_names: Optional[Mapping[str, str]] = None,
) -> str:
    """Resolve a concrete human or company counterpart for a wait matter: stored
    counterpart first, then inferred from ``waiting_on``, then first non-owner email
    recipient."""
    direct = _clean_counterpart(counterpart)
    if direct:
        return direct
    inferred = _infer_counterpart_from_said(str(said or ""))
    if inferred:
        return inferred
    return _infer_counterpart_from_recipients(to_addresses, mine_addrs, staff_names or {})


def is_agent_notification(*texts: Any, self_assigned: bool = False) -> bool:
    """True when any text starts with an agent/Workbench self-notification header that
    must never become a wait or an action item. When ``self_assigned`` is True (reading
    had ``actor_person == sender_person``), also matches Workbench self-notice banners."""
    pat = _SELF_NOTIFY_TEAMS_RE if self_assigned else _AGENT_SELF_MSG_RE
    for raw in texts:
        cleaned = _clean_text(raw)
        if cleaned and pat.match(cleaned):
            return True
    return False


def _name_tokens(text: Any) -> set[str]:
    cleaned = _clean_text(text).lower()
    return {t for t in _TOKEN_RE.findall(cleaned) if len(t) >= 2 and t not in _TOPIC_STOPWORDS}


def is_self_wait(
    owner_person: Any,
    counterpart: Any,
    *,
    staff_names: Mapping[str, str] = (),  # type: ignore[assignment]
    mine_addrs: Iterable[str] = (),
) -> bool:
    """True when the waited-on counterpart IS the owner themselves (by person id, email
    address, full display name, or first name)."""
    owner_low = str(owner_person or "").strip().lower()
    cp_clean = _clean_counterpart(counterpart)
    if not cp_clean:
        return False
    cp_low = cp_clean.lower()
    if owner_low and cp_low == owner_low:
        return True
    mine = {str(a or "").strip().lower() for a in (mine_addrs or ()) if a}
    if cp_low in mine:
        return True
    owner_names: set[str] = set()
    if isinstance(staff_names, Mapping):
        if owner_low and staff_names.get(owner_low):
            owner_names.add(str(staff_names[owner_low]).strip().lower())
        for addr in mine:
            if staff_names.get(addr):
                owner_names.add(str(staff_names[addr]).strip().lower())
    for addr in mine:
        local = addr.split("@", 1)[0]
        if local and len(local) >= 2:
            owner_names.add(local)
    for full in owner_names:
        if cp_low == full:
            return True
        first = full.split()[0] if full.split() else ""
        if len(first) >= 2 and cp_low == first:
            return True
        # "
        cp_words = [w for w in _TOKEN_RE.findall(cp_low) if len(w) >= 2]
        full_words = [w for w in _TOKEN_RE.findall(full) if len(w) >= 2]
        if cp_words and full_words:
            if len(cp_words) == 1 and cp_words[0] == full_words[0]:
                return True
            if len(full_words) >= 2 and set(full_words) <= set(cp_words):
                return True
    return False


def build_wait_title(said: Any, counterpart: Any) -> str:
    """Plain-English title for a wait matter naming WHAT is waited for (and from whom
    when known), never the generic 'No answer yet from them' or doubled 'from X from Y'."""
    topic = _clean_text(said)
    topic = _WAIT_LEAD_RE.sub("", topic).strip(" .,;:-")
    # Strip trailing generic source phrases like ", from them" / ", from our side"
    while True:
        stripped = _WAIT_TRAILING_GENERIC_FROM_RE.sub("", topic).strip(" .,;:-")
        if stripped == topic:
            break
        topic = stripped
    cp = _clean_counterpart(counterpart) or _infer_counterpart_from_said(topic)
    if not topic:
        return f"Waiting on {cp}" if cp else "Waiting for a reply"
    if cp:
        cp_low = cp.lower()
        # Normalize ", from <counterpart>" into " from <counterpart>" so the title reads naturally
        topic = re.sub(
            r"\s*[,;]\s+from\s+" + re.escape(cp) + r"\b",
            f" from {cp}",
            topic,
            flags=re.IGNORECASE,
        ).strip(" .,;:-")
        # Also normalize generic ", from <entity>" trailing comma before "from"
        topic = re.sub(r"\s*,\s+(from\s+[A-Z])", r" \1", topic).strip(" .,;:-")
        topic_low = topic.lower()
        already_named = cp_low in topic_low
        has_from_clause = False
        if not already_named:
            # Avoid double "from X from Y" when topic already carries a "from ..." clause.
            # If the clause shares any meaningful token with cp (e.g. "from Josh at KBM Resorts"
            # with cp "KBM Resorts (Josh)"), cp is already named; otherwise attach cp cleanly
            # in parentheses ("from Gia (Ningbo Horizon...)") instead of "from Gia from Ningbo".
            m_from = re.search(r"\bfrom\s+(.+)$", topic, re.IGNORECASE)
            if m_from:
                has_from_clause = True
                from_tokens = _name_tokens(m_from.group(1))
                cp_tokens = _name_tokens(cp)
                if from_tokens and cp_tokens and (from_tokens & cp_tokens):
                    already_named = True
        if already_named:
            title = f"Waiting for {topic}"
        elif has_from_clause:
            title = f"Waiting for {topic} ({cp})"
        else:
            title = f"Waiting for {topic} from {cp}"
    else:
        title = f"Waiting for {topic}"
    if len(title) > 180:
        title = title[:177].rstrip(" ,;:-") + "..."
    return title


def _normalize_token(tok: str) -> str:
    if len(tok) >= 5 and tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
        return tok[:-1]
    return tok


def _topic_tokens(text: Any) -> set[str]:
    cleaned = _PROMISE_LEAD_RE.sub("", _clean_text(text))
    cleaned = _POSSESSIVE_RE.sub("", cleaned).lower()
    out: set[str] = set()
    for raw in _TOKEN_RE.findall(cleaned):
        if len(raw) < 2:
            continue
        tok = _normalize_token(raw)
        if tok in _TOPIC_STOPWORDS or raw in _TOPIC_STOPWORDS:
            continue
        out.add(tok)
    return out


def topic_tokens_overlap(left: Any, right: Any) -> bool:
    """Whether two short work descriptions refer to the same topic (used only to merge a
    promise with a same-owner conversation matter within ``SAME_EVENT_WINDOW_SECONDS``)."""
    ta = _topic_tokens(left)
    tb = _topic_tokens(right)
    if not ta or not tb:
        return False
    shared = ta & tb
    if not shared:
        return False
    small = min(len(ta), len(tb))
    if len(shared) >= 2 and (len(shared) / small) >= 0.5:
        return True
    if len(shared) >= 3 and (len(shared) / small) >= 0.4:
        return True
    has_code = any(_CODE_TOKEN_RE.match(t) for t in shared)
    if has_code and len(shared) >= 2:
        return True
    return False


def _reading_pairs(sources: Any) -> set[tuple[str, str]]:
    if not isinstance(sources, Mapping):
        return set()
    out: set[tuple[str, str]] = set()
    for entry in sources.get("readings") or ():
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            out.add((str(entry[0]), str(entry[1])))
    pr = sources.get("promise_reading")
    if isinstance(pr, (list, tuple)) and len(pr) >= 2:
        out.add((str(pr[0]), str(pr[1])))
    return out


def _within_window(a: Any, b: Any, window_s: float = SAME_EVENT_WINDOW_SECONDS) -> bool:
    if not isinstance(a, dt.datetime) or not isinstance(b, dt.datetime):
        return False
    return abs((a - b).total_seconds()) <= window_s


def _matches_conversation(
    *,
    owner: str,
    subject_key: str,
    basis_at: Any,
    readings: set[tuple[str, str]],
    topic_text: str,
    conv: Mapping[str, Any],
) -> bool:
    if conv.get("owner_person") != owner:
        return False
    if subject_key and conv.get("subject_key") == subject_key:
        return True
    conv_readings = _reading_pairs(conv.get("sources"))
    if readings and conv_readings and (readings & conv_readings):
        return True
    if _within_window(basis_at, conv.get("basis_at")):
        conv_text = f"{conv.get('title') or ''} {conv.get('detail') or ''}"
        if topic_tokens_overlap(topic_text, conv_text):
            return True
    return False


def _build_conv_enrichment(
    conv: Mapping[str, Any],
    *,
    promise_text: str,
    promise_reading: Optional[tuple[str, str]],
    due_at: Any,
    has_explicit_due: bool,
    next_act: str,
    counterpart: str,
    counterpart_party: Any,
) -> Optional[dict[str, Any]]:
    """Merge a commitment's metadata, due date, and next_act into its matched conversation
    matter; returns the update payload or None when nothing changes."""
    old_sources = dict(conv.get("sources") or {}) if isinstance(conv.get("sources"), Mapping) else {}
    new_sources = json.loads(json.dumps(old_sources, default=str))
    changed = False
    clean_promise = _clean_text(promise_text)
    if clean_promise and new_sources.get("promise") != clean_promise:
        new_sources["promise"] = clean_promise
        changed = True
    if promise_reading is not None:
        pair = [promise_reading[0], promise_reading[1]]
        if new_sources.get("promise_reading") != pair:
            new_sources["promise_reading"] = pair
            changed = True
        readings = list(new_sources.get("readings") or [])
        if pair not in readings:
            readings.append(pair)
            new_sources["readings"] = readings
            changed = True

    old_due = conv.get("due_at")
    new_due = old_due
    if due_at is not None:
        if old_due is None or has_explicit_due or (isinstance(old_due, dt.datetime) and due_at < old_due):
            if new_due != due_at:
                new_due = due_at
                changed = True

    old_act = str(conv.get("next_act") or "")
    new_act = old_act
    if (not old_act or old_act == "nothing") and next_act and next_act != "nothing":
        new_act = next_act
        changed = True

    old_cp = str(conv.get("counterpart") or "")
    new_cp = old_cp
    if not old_cp and counterpart:
        new_cp = counterpart
        changed = True

    old_party = conv.get("counterpart_party")
    new_party = old_party
    if not old_party and counterpart_party:
        new_party = counterpart_party
        changed = True

    old_detail = str(conv.get("detail") or "")
    new_detail = old_detail
    if not old_detail and clean_promise:
        new_detail = clean_promise
        changed = True

    if not changed:
        return None
    return {
        "matter_id": conv["matter_id"],
        "sources": new_sources,
        "due_at": new_due,
        "next_act": new_act or old_act or "ask_person",
        "counterpart": new_cp,
        "counterpart_party": new_party,
        "detail": new_detail,
    }


_CANDIDATES = f"""
WITH k AS (
  SELECT r.*, v.kind, v.said, coalesce(r.thread_key, r.message_key) AS tk
  FROM {SCHEMA}.readings r
  CROSS JOIN LATERAL (VALUES ('wait', r.waiting_on), ('promise', r.promise)) v(kind, said)
  WHERE r.status = 'ok' AND r.sender_person IS NOT NULL AND v.said <> ''),
newest AS (SELECT DISTINCT ON (sender_person, kind, tk) * FROM k
           ORDER BY sender_person, kind, tk, said_at DESC, message_key DESC),
mine AS (SELECT o.mailbox, o.addrs FROM biz.mailbox_owners o),
-- the person's measured lag: for each earlier wait the seconds to the next message of someone
-- else in the thread, for each earlier promise the seconds to the person's next message
pairs AS (
  SELECT k.sender_person AS person, k.kind,
         extract(epoch FROM (SELECT min(e.received_at) FROM biz.emails e
              WHERE k.source = 'emails' AND e.thread = k.thread_key AND e.received_at > k.said_at
                AND ((k.kind = 'wait' AND NOT lower(e.sender) = ANY (SELECT lower(a) FROM mine m,
                          unnest(m.addrs) a WHERE m.mailbox = k.sender_person))
                  OR (k.kind = 'promise' AND lower(e.sender) = ANY (SELECT lower(a) FROM mine m,
                          unnest(m.addrs) a WHERE m.mailbox = k.sender_person)))) - k.said_at)
         AS secs
  FROM k),
lag AS (SELECT person, kind, percentile_cont(0.75) WITHIN GROUP (ORDER BY secs) AS p75
        FROM pairs WHERE secs IS NOT NULL GROUP BY 1, 2)
SELECT n.sender_person, n.kind, n.tk, n.source, n.message_key, n.said_at, n.said,
       n.due, due.at AS due_at, coalesce(n.counterpart, ''), n.counterpart_party,
       n.summary, n.act, n.quote,
       coalesce((SELECT jsonb_agg(DISTINCT a) FROM (
           SELECT unnest(e.audience_keys) AS a FROM biz.emails e WHERE e.handle = ANY(n.handles)
           UNION ALL SELECT unnest(t.audience_keys) FROM biz.teams_messages t
           WHERE t.handle = ANY(n.handles)) z), '[]'::jsonb) AS audiences,
       coalesce((SELECT jsonb_agg(DISTINCT mk) FROM (
           SELECT jsonb_array_elements_text(rm::jsonb) AS mk FROM (
             SELECT unnest(e.rec_marks) AS rm FROM biz.emails e WHERE e.handle = ANY(n.handles)
             UNION ALL SELECT unnest(t.rec_marks) FROM biz.teams_messages t
             WHERE t.handle = ANY(n.handles)) y) z), '[]'::jsonb) AS markings,
       e_info.sender, e_info.subject, coalesce(e_info.to_addresses, '{{}}'::text[]),
       t_info.sender, t_info.subject,
       coalesce((SELECT m.addrs FROM mine m WHERE m.mailbox = n.sender_person LIMIT 1), '{{}}'::text[])
FROM newest n
LEFT JOIN lag l ON l.person = n.sender_person AND l.kind = n.kind
CROSS JOIN LATERAL (SELECT coalesce(((n.due + 1)::timestamp AT TIME ZONE %(tz)s),
                                    n.said_at + l.p75 * interval '1 second') AS at) due
LEFT JOIN LATERAL (
    SELECT e.sender, e.subject, e.to_addresses FROM biz.emails e
    WHERE n.source = 'emails' AND e.handle = ANY(n.handles) LIMIT 1
) e_info ON true
LEFT JOIN LATERAL (
    SELECT t.sender, t.subject FROM biz.teams_messages t
    WHERE n.source = 'teams_messages' AND t.handle = ANY(n.handles) LIMIT 1
) t_info ON true"""

_OPEN_CONVERSATIONS = f"""
SELECT matter_id, owner_person, subject_key, sources, basis_at, title, detail,
       next_act, due_at, counterpart, counterpart_party
FROM {SCHEMA}.matters
WHERE kind = 'conversation' AND state IN ('open', 'unverified')"""

_OPEN_PROMISES = f"""
SELECT matter_id, owner_person, subject_key, sources, basis_at, title, detail,
       next_act, due_at, counterpart, counterpart_party
FROM {SCHEMA}.matters
WHERE kind = 'promise' AND state IN ('open', 'unverified')"""

_ENRICH_CONVERSATIONS = f"""
WITH x AS (SELECT * FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(
    matter_id text, sources jsonb, due_at timestamptz, next_act text,
    counterpart text, counterpart_party text, detail text)),
upd AS (
  UPDATE {SCHEMA}.matters m SET
      sources = x.sources,
      due_at = x.due_at,
      next_act = x.next_act,
      counterpart = x.counterpart,
      counterpart_party = x.counterpart_party,
      detail = x.detail
  FROM x
  WHERE m.matter_id = x.matter_id
    AND (m.sources IS DISTINCT FROM x.sources
      OR m.due_at IS DISTINCT FROM x.due_at
      OR m.next_act IS DISTINCT FROM x.next_act
      OR m.counterpart IS DISTINCT FROM x.counterpart
      OR m.counterpart_party IS DISTINCT FROM x.counterpart_party
      OR m.detail IS DISTINCT FROM x.detail)
  RETURNING m.matter_id)
SELECT count(*) FROM upd"""

_UPSERT_WAITS = f"""
WITH x AS (SELECT * FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(
    owner_person text, kind text, subject_key text, sources jsonb, basis_at timestamptz,
    title text, detail text, next_act text, due_at timestamptz, counterpart text,
    counterpart_party text, audiences jsonb, markings jsonb)),
old AS (SELECT matter_id, basis_at FROM {SCHEMA}.matters WHERE kind IN ('wait', 'promise')),
up AS (
  INSERT INTO {SCHEMA}.matters AS m (matter_id, owner_person, kind, subject_key, sources, basis_at,
      title, detail, next_act, due_at, counterpart, counterpart_party, audiences, markings)
  SELECT left(encode(sha256(convert_to(x.owner_person || '|' || x.kind || '|' || x.subject_key, 'UTF8')),
              'hex'), 24),
         x.owner_person, x.kind, x.subject_key, x.sources, x.basis_at, x.title, x.detail,
         x.next_act, x.due_at, x.counterpart, x.counterpart_party, x.audiences, x.markings
  FROM x
  ON CONFLICT (owner_person, kind, subject_key) DO UPDATE SET
      sources = CASE WHEN m.basis_at < EXCLUDED.basis_at THEN EXCLUDED.sources ELSE m.sources END,
      basis_at = greatest(m.basis_at, EXCLUDED.basis_at),
      title = EXCLUDED.title,
      detail = CASE WHEN m.basis_at < EXCLUDED.basis_at THEN EXCLUDED.detail ELSE m.detail END,
      next_act = CASE WHEN m.basis_at < EXCLUDED.basis_at THEN EXCLUDED.next_act ELSE m.next_act END,
      due_at = CASE WHEN m.basis_at < EXCLUDED.basis_at THEN EXCLUDED.due_at ELSE m.due_at END,
      counterpart = CASE WHEN m.basis_at < EXCLUDED.basis_at OR m.counterpart = ''
                         THEN EXCLUDED.counterpart ELSE m.counterpart END,
      counterpart_party = CASE WHEN m.basis_at < EXCLUDED.basis_at OR m.counterpart_party IS NULL
                               THEN EXCLUDED.counterpart_party ELSE m.counterpart_party END,
      audiences = CASE WHEN m.basis_at < EXCLUDED.basis_at THEN EXCLUDED.audiences ELSE m.audiences END,
      markings = CASE WHEN m.basis_at < EXCLUDED.basis_at THEN EXCLUDED.markings ELSE m.markings END,
      state = CASE WHEN m.basis_at < EXCLUDED.basis_at THEN 'open' ELSE m.state END,
      done_reason = CASE WHEN m.basis_at < EXCLUDED.basis_at THEN NULL ELSE m.done_reason END,
      done_at = CASE WHEN m.basis_at < EXCLUDED.basis_at THEN NULL ELSE m.done_at END,
      plan = CASE WHEN m.basis_at < EXCLUDED.basis_at THEN NULL ELSE m.plan END,
      plan_status = CASE WHEN m.basis_at < EXCLUDED.basis_at THEN 'none' ELSE m.plan_status END
  WHERE m.basis_at < EXCLUDED.basis_at
     OR (m.state = 'open' AND m.kind = 'wait'
         AND (m.title ILIKE 'No answer yet from %%'
              OR m.title IS DISTINCT FROM EXCLUDED.title
              OR (m.counterpart = '' AND EXCLUDED.counterpart <> '')))
  RETURNING m.matter_id, m.basis_at, (xmax = 0) AS fresh),
logged AS (INSERT INTO {SCHEMA}.matter_log (matter_id, what, detail)
           SELECT u.matter_id, CASE WHEN u.fresh THEN 'opened' ELSE 'basis_moved' END, '{{}}'::jsonb
           FROM up u LEFT JOIN old o USING (matter_id)
           WHERE u.fresh OR u.basis_at > o.basis_at RETURNING what)
SELECT (SELECT count(*) FROM up), (SELECT count(*) FROM logged WHERE what = 'opened')"""

_CLOSE = f"""
WITH m AS (
  SELECT m.matter_id, m.kind, m.owner_person, m.subject_key, m.basis_at,
         m.sources->'readings'->-1->>0 AS src,
         coalesce((SELECT o.addrs FROM biz.mailbox_owners o WHERE o.mailbox = m.owner_person),
                  '{{}}') AS mine
  FROM {SCHEMA}.matters m WHERE m.kind IN ('wait', 'promise') AND m.state = 'open'),
v AS (
  SELECT m.matter_id, CASE
    WHEN m.src = 'emails' AND m.kind = 'wait' AND EXISTS (SELECT 1 FROM biz.emails e
         WHERE e.thread = m.subject_key AND e.received_at > m.basis_at
           AND NOT lower(e.sender) = ANY (SELECT lower(a) FROM unnest(m.mine) a)) THEN 'answered'
    WHEN m.src = 'emails' AND m.kind = 'promise' AND EXISTS (SELECT 1 FROM biz.emails e
         WHERE e.thread = m.subject_key AND e.received_at > m.basis_at
           AND lower(e.sender) = ANY (SELECT lower(a) FROM unnest(m.mine) a)) THEN 'kept'
    WHEN m.src = 'teams_messages' AND m.kind = 'wait' AND EXISTS (SELECT 1
         FROM biz.teams_messages t WHERE t.chat = m.subject_key AND t.created_at > m.basis_at
           AND t.sender LIKE '{{%%' AND t.sender::jsonb->>'id' <> m.owner_person) THEN 'answered'
    WHEN m.src = 'teams_messages' AND m.kind = 'promise' AND EXISTS (SELECT 1
         FROM biz.teams_messages t WHERE t.chat = m.subject_key AND t.created_at > m.basis_at
           AND t.sender LIKE '{{%%' AND t.sender::jsonb->>'id' = m.owner_person) THEN 'kept'
    END AS why FROM m),
upd AS (UPDATE {SCHEMA}.matters mm SET state = 'done', done_reason = v.why, done_at = now()
        FROM v WHERE mm.matter_id = v.matter_id AND v.why IS NOT NULL RETURNING mm.matter_id, v.why),
lg AS (INSERT INTO {SCHEMA}.matter_log (matter_id, what, detail)
       SELECT matter_id, 'closed', jsonb_build_object('reason', why) FROM upd RETURNING 1)
SELECT count(*) FROM upd"""


def _load_open_conversations(pg: Any) -> dict[str, list[dict[str, Any]]]:
    cols = ("matter_id", "owner_person", "subject_key", "sources", "basis_at", "title",
            "detail", "next_act", "due_at", "counterpart", "counterpart_party")
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for row in pg.execute(_OPEN_CONVERSATIONS).fetchall():
        d = dict(zip(cols, row))
        by_owner.setdefault(d["owner_person"], []).append(d)
    return by_owner


def open_from_readings(pg: Any, tz: str) -> dict[str, int]:
    """Promise and wait matters from the readings of messages the person sent.

    - Skips automated IT/system monitoring noise readings.
    - When a promise matches an open conversation matter for the same owner (same
      thread, overlapping source reading, or basis within 120s with overlapping topic
      tokens), enriches that conversation matter instead of opening a duplicate
      ``promise`` row.
    - Builds plain-English ``wait`` titles naming what is waited for and populates
      ``counterpart`` cleanly at creation time.
    """
    from operating import assist_matters as am
    from operating.assist_readings import is_automated_system_alert

    rows = pg.execute(_CANDIDATES, {"tz": tz}).fetchall()
    if not rows:
        return {"changed": 0, "opened": 0, "merged_into_conversation": 0, "system_noise_skipped": 0}

    staff_names = am._staff_names(pg)
    has_promises = any(r[1] == "promise" for r in rows)
    open_convs = _load_open_conversations(pg) if has_promises else {}

    upserts: list[dict[str, Any]] = []
    enrichments: dict[str, dict[str, Any]] = {}
    noise_skipped = 0
    self_skipped = 0
    merged = 0

    for (sender_person, kind, tk, source, message_key, said_at, said, raw_due, due_at,
         counterpart, counterpart_party, summary, act, quote, audiences, markings,
         e_sender, e_subject, to_addrs, t_sender, t_subject, mine_addrs) in rows:
        msg_sender = e_sender if source == "emails" else t_sender
        msg_subject = e_subject if source == "emails" else t_subject
        if is_automated_system_alert(
            sender=msg_sender,
            subject=msg_subject,
            summary=summary or said,
            note=act,
            message=quote,
        ) or is_agent_notification(summary, said, quote, msg_subject):
            noise_skipped += 1
            continue

        clean_said = _clean_text(said)
        resolved_cp = resolve_wait_counterpart(
            counterpart, clean_said, to_addrs or (), mine_addrs or (), staff_names,
        )
        if kind == "wait" and (
            is_self_wait(sender_person, resolved_cp, staff_names=staff_names, mine_addrs=mine_addrs or ())
            or is_self_wait(sender_person, counterpart, staff_names=staff_names, mine_addrs=mine_addrs or ())
        ):
            self_skipped += 1
            continue
        next_act = "ask_person" if due_at is None else ("new_message" if kind == "wait" else "nothing")

        if kind == "promise":
            matched = next(
                (c for c in open_convs.get(sender_person, ())
                 if _matches_conversation(
                     owner=sender_person,
                     subject_key=tk,
                     basis_at=said_at,
                     readings={(source, message_key)},
                     topic_text=f"{clean_said} {summary or ''}",
                     conv=c,
                 )),
                None,
            )
            if matched is not None:
                merged += 1
                enr = _build_conv_enrichment(
                    matched,
                    promise_text=clean_said,
                    promise_reading=(source, message_key),
                    due_at=due_at,
                    has_explicit_due=raw_due is not None,
                    next_act=next_act,
                    counterpart=resolved_cp,
                    counterpart_party=counterpart_party,
                )
                if enr is not None:
                    enrichments[matched["matter_id"]] = enr
                    matched.update(enr)
                continue

        if kind == "wait":
            title = build_wait_title(clean_said, resolved_cp)
        else:
            title = f"You said: {clean_said}" if clean_said else "You made a commitment"

        detail = clean_said + (
            " (nothing in your mail says how long this usually takes: when should it be looked for?)"
            if due_at is None else ""
        )
        upserts.append({
            "owner_person": sender_person,
            "kind": kind,
            "subject_key": tk,
            "sources": {"readings": [[source, message_key]]},
            "basis_at": said_at,
            "title": title,
            "detail": detail,
            "next_act": next_act,
            "due_at": due_at,
            "counterpart": resolved_cp,
            "counterpart_party": counterpart_party,
            "audiences": list(audiences or []),
            "markings": list(markings or []),
        })

    enriched_count = 0
    if enrichments:
        enriched_count = int(
            pg.execute(_ENRICH_CONVERSATIONS, {"rows": json.dumps(list(enrichments.values()), default=str)}).fetchone()[0]
        )

    changed, opened = 0, 0
    if upserts:
        row = pg.execute(_UPSERT_WAITS, {"rows": json.dumps(upserts, default=str)}).fetchone()
        changed, opened = int(row[0]), int(row[1])

    return {
        "changed": changed + enriched_count,
        "opened": opened,
        "merged_into_conversation": merged,
        "system_noise_skipped": noise_skipped,
        "self_wait_skipped": self_skipped,
    }


def reconcile_duplicate_promises(pg: Any) -> dict[str, int]:
    """Idempotently merge and close any already-open ``promise`` matters that duplicate an
    open ``conversation`` matter for the same owner (same thread, overlapping reading, or
    basis within 120s with overlapping topic tokens)."""
    from operating import assist_matters as am

    cols = ("matter_id", "owner_person", "subject_key", "sources", "basis_at", "title",
            "detail", "next_act", "due_at", "counterpart", "counterpart_party")
    promises = [dict(zip(cols, r)) for r in pg.execute(_OPEN_PROMISES).fetchall()]
    if not promises:
        return {"merged": 0}
    open_convs = _load_open_conversations(pg)
    if not open_convs:
        return {"merged": 0}

    enrichments: dict[str, dict[str, Any]] = {}
    verdicts: list[dict[str, str]] = []
    for p in promises:
        owner = p["owner_person"]
        p_readings = _reading_pairs(p.get("sources"))
        p_text = f"{p.get('title') or ''} {p.get('detail') or ''}"
        matched = next(
            (c for c in open_convs.get(owner, ())
             if _matches_conversation(
                 owner=owner,
                 subject_key=str(p.get("subject_key") or ""),
                 basis_at=p.get("basis_at"),
                 readings=p_readings,
                 topic_text=p_text,
                 conv=c,
             )),
            None,
        )
        if matched is None:
            continue
        promise_phrase = _PROMISE_LEAD_RE.sub("", _clean_text(p.get("title") or p.get("detail") or "")).strip()
        first_reading = next(iter(p_readings), None)
        enr = _build_conv_enrichment(
            matched,
            promise_text=promise_phrase,
            promise_reading=first_reading,
            due_at=p.get("due_at"),
            has_explicit_due=False,
            next_act=str(p.get("next_act") or ""),
            counterpart=str(p.get("counterpart") or ""),
            counterpart_party=p.get("counterpart_party"),
        )
        if enr is not None:
            enrichments[matched["matter_id"]] = enr
            matched.update(enr)
        verdicts.append({
            "matter": p["matter_id"],
            "state": "done",
            "reason": "merged_into_conversation",
        })

    if enrichments:
        pg.execute(_ENRICH_CONVERSATIONS, {"rows": json.dumps(list(enrichments.values()), default=str)})
    if verdicts:
        am.set_states(pg, verdicts)
    return {"merged": len(verdicts)}


def reverify(pg: Any) -> dict[str, int]:
    """Close a wait someone answered and a promise the person kept (module text)."""
    return {"closed": int(pg.execute(_CLOSE).fetchone()[0])}
