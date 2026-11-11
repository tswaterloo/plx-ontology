"""Assist mode measured on real self-started mail (dev days only): for each role, mail its
people started themselves (no reply or forward, no earlier message of its conversation in
their mailbox, not automated, not a template or scheduled send); the system is given only
a one-line intent derived from the subject and the first recipient's name ("email <name>:
<subject>"), never the body; ``operating.assist`` drafts it with the sender's own sent mail
before that day as recipient history and the Ask for facts, read as the sender (their own
identity with their role's grant, ``ROLE_GRANTS``; the operator's own mail on the
operator's owner host) and as of the send instant (a record card is read as the store knew
it then, ``fast_record``; other fast answers read now); a record question the fast path
does not settle goes to the Ask's agent, at most ``AGENT_PER_ROLE`` per role (model calls on
the operator's login; ``--no-agent``: none). ``run`` drafts the first ``PER_ROLE_RUN`` of
each role's sample. The draft is then scored against the real mail:

* **recipients**: the real first recipient is on the draft (``primary``); the draft's To
  set equals the real one (``exact``);
* **facts**: the record ids, numbers, dates and status words of the real body that the
  intent did not already say (``real``) against those the draft added (``drafted``):
  ``found`` = drafted that the real body states, ``unconfirmed`` = drafted that it does
  not, ``conflict`` = a drafted status whose opposite the real body states;
* **usable** = primary recipient right and no conflict;
* **facts present**: of drafts naming a record, those carrying a gated fact (``_fast``:
  carrying one from the fast path, whatever the agent added); **facts wrong**:
  conflicting drafted facts of all drafted facts.

Stages (each resumable, each one foreground call): ``sample`` (capture only), ``run``
(drafts, appended to ``drafts.jsonl``; ``--seconds`` bounds one call), ``score``. The
sample and drafts hold message text and stay in ``--out`` (scratch, mode 0600), never in
the repository; ``score`` prints counts and shares only. People pass the exclusion first
(an excluded or undecidable sender or internal recipient: the mail is not sampled).

Run::

    python3 -m evals.assist_eval sample --state STATE --store STORE --roles ROLES --out DIR
    python3 -m evals.assist_eval run --state STATE --out DIR --reader-env FILE [--seconds 200]
    python3 -m evals.assist_eval score --out DIR
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

SINCE, UNTIL = "2026-10-06T07:00:00Z", "2026-10-26T07:00:00Z"   # dev days only
PER_ROLE = 60
MAIL_MINUTES, REVIEW_MINUTES = 6, 2
_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|tr|sv|antw)\s*:\s*)+", re.I)
_DATE_NUM = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_WORD = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|"
                        r"oct|nov|dec)[a-z]*\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|"
                        r"dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_ID = re.compile(r"\b(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]{3,}\b")
_NUM = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w])|(?<![\w.])\d+\.\d+(?![\w])")
STATUS = {"hold": "released", "held": "released", "released": "hold", "shipped": "not shipped",
          "received": "not received", "late": "on time", "on time": "late",
          "approved": "rejected", "rejected": "approved", "paid": "unpaid", "unpaid": "paid",
          "confirmed": "unconfirmed", "closed": "open", "cancelled": "open", "open": "closed"}


def facts_of(text: str) -> set[str]:
    """Record ids, numbers, dates (as m-d) and status words a text states."""
    text = text or ""
    out: set[str] = set()
    for y, mo, d in _DATE_ISO.findall(text):
        out.add(f"date:{int(mo)}-{int(d)}")
    for m in _DATE_WORD.finditer(text):
        day, mon = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        out.add(f"date:{_MONTHS.index(mon[:3].lower()) + 1}-{int(day)}")
    cleaned = _DATE_ISO.sub(" ", text)
    for a, b, _y in _DATE_NUM.findall(cleaned):
        if 1 <= int(a) <= 12 and 1 <= int(b) <= 31:
            out.add(f"date:{int(a)}-{int(b)}")
    cleaned = _DATE_NUM.sub(" ", cleaned)
    for token in _ID.findall(cleaned):
        if sum(c.isdigit() for c in token) >= 3:
            out.add("id:" + token.upper().lstrip("0"))
    for n in _NUM.findall(cleaned):
        out.add("num:" + n.replace(",", "").rstrip("0").rstrip("."))
    low = " " + re.sub(r"\s+", " ", text.casefold()) + " "
    for word in STATUS:
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", low) and \
                not re.search(rf"\bnot {re.escape(word)}\b", low):
            out.add("status:" + word)
    return out


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------------------- sample

def _people(state: Path) -> Any:
    from integrations.people_exclusion import environ_with_exclusion
    from operating.chunking.permissions import load_exclusion
    return load_exclusion(state, environ_with_exclusion())


def _body(state: Path, digest: Any) -> str:
    from integrations.captured_text import read_blob
    from integrations.document_understanding import html_text
    for d in (state / "mail-capture" / "blobs" / "sha256",
              state / "mail-capture" / "blobs.nvme" / "sha256"):
        raw = read_blob(d, digest)
        if raw is not None:
            text = raw.decode("utf-8", "replace")
            return html_text(text) if "<" in text and ">" in text else text
    return ""


def _top(body: str) -> str:
    """The message's own text (a quoted earlier message cut off)."""
    cut = re.search(r"\n\s*(?:From:|-----Original Message|On .{5,80} wrote:|_{5,})", body)
    return body[:cut.start()] if cut else body


def party_names(store: str) -> dict[str, str]:
    """address or @domain -> the one customer or supplier name stating it (else absent)."""
    from operating import autopilot as ap
    st = ap._store(store)
    try:
        found = ap._party_addresses(st)
        ids = sorted({o for v in found.values() for _s, o in v})
        names = {}
        for start in range(0, len(ids), 5000):
            for oid, row in ap._read(st, ids[start:start + 5000], ("name",)).items():
                if ap._v(row, "name"):
                    names[oid] = ap._v(row, "name")
        out = {}
        for key, parties in found.items():
            said = {names.get(o) for _s, o in parties} - {None}
            if len(said) == 1:
                out[key] = said.pop()
        return out
    finally:
        st.close()


def sample(state: Path, store: str, roles: Mapping[str, list[str]], out: Path) -> dict[str, Any]:
    from operating import assist
    from operating import proactive_threads as pt
    people = _people(state)
    if people is None or people.unavailable:
        raise SystemExit("people exclusion unavailable: nothing sampled")
    directory = {a.lower(): o.lower() for a, o in people.directory.items()}
    internal = tuple(people.internal)
    cache = out / "parties.json"
    if cache.exists():
        parties = json.loads(cache.read_text())
    else:
        parties = party_names(store)
        out.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(parties))
    conn = sqlite3.connect(f"file:{state / 'mail-capture' / 'capture.db'}?mode=ro", uri=True)
    rows_out, counts = [], {}
    for role, members in sorted(roles.items()):
        part = out / f"sample-{role.replace(':', '_')}.json"
        if part.exists():
            done = json.loads(part.read_text())
            counts[role], rows_out = done["counts"], rows_out + done["rows"]
            continue
        pool, seen = [], Counter()
        for oid in members:
            if not people.identity_clear(oid):
                continue
            own = {a for a, o in directory.items() if o == oid}
            mine = [(h, json.loads(r), b) for h, r, b in conn.execute(
                "SELECT l1_handle, record, body_sha256 FROM messages WHERE mailbox_id = ? AND "
                "superseded_at IS NULL AND removed_reason IS NULL AND "
                "json_extract(record, '$.sent_at') >= ? AND json_extract(record, '$.sent_at') < ?",
                (oid, SINCE, UNTIL))]
            mine = [(h, r, b) for h, r, b in mine if str(r.get("from") or "").lower() in own]
            convs = sorted({r.get("conversation_id") for _h, r, _b in mine
                            if r.get("conversation_id")})
            first = dict(conn.execute(
                "SELECT json_extract(record, '$.conversation_id') AS c, MIN(coalesce("
                "json_extract(record, '$.sent_at'), json_extract(record, '$.received_at'))) "
                "FROM messages WHERE mailbox_id = ? AND superseded_at IS NULL AND "
                "c IN (SELECT value FROM json_each(?)) GROUP BY c", (oid, json.dumps(convs))))
            subjects = Counter(_PREFIX.sub("", str(r.get("subject") or "")).strip().casefold()
                               for _h, r, _b in mine)
            for h, r, b in mine:
                subject = str(r.get("subject") or "").strip()
                to = assist._addresses(r.get("to"))
                at = str(r.get("sent_at"))
                why = None
                if not to or not subject:
                    why = "no_recipient_or_subject"
                elif _PREFIX.match(subject):
                    why = "reply_or_forward"
                elif (first.get(r.get("conversation_id") or "") or at) < at:
                    why = "earlier_message_in_conversation"
                elif subjects[subject.casefold()] >= 3:
                    why = "repeated_subject"
                elif any(_internal(a, internal) and not people.identity_clear(directory.get(a, a))
                         for a in to + assist._addresses(r.get("cc"))):
                    why = "recipient_not_cleared"
                body = "" if why else _top(_body(state, b))
                if not why and (pt.automated(str(r.get("from")), body, subject)
                                or len(body.split()) < 3):
                    why = "automated_or_empty"
                seen[why or "self_started"] += 1
                if why:
                    continue
                first_to = to[0]
                if _internal(first_to, internal):
                    name = assist._label(first_to) or first_to.split("@")[0]
                else:
                    name = parties.get(first_to) or parties.get("@" + first_to.split("@")[1]) \
                        or first_to.split("@")[1].split(".")[0]
                pool.append({"role": role, "sender": oid, "handle": h, "at": at,
                             "to": to, "cc": assist._addresses(r.get("cc")),
                             "subject": subject, "body": body[:6000],
                             "intent": f"email {name}: {_PREFIX.sub('', subject)}"[:300],
                             "outside": not _internal(first_to, internal)})
        pool.sort(key=lambda x: _digest(x["handle"]))
        counts[role] = {"members": len(members), "window_days": 20, **dict(seen),
                        "sampled": min(PER_ROLE, len(pool))}
        rows_out += pool[:PER_ROLE]
        out.mkdir(parents=True, exist_ok=True)
        part.write_text(json.dumps({"counts": counts[role], "rows": pool[:PER_ROLE]}))
    conn.close()
    out.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    with open(out / "sample.jsonl", "w", encoding="utf-8") as f:
        for row in rows_out:
            f.write(json.dumps(row) + "\n")
    (out / "sample-counts.json").write_text(json.dumps(counts, indent=1, sort_keys=True))
    return counts


def _internal(address: str, domains: Sequence[str]) -> bool:
    domain = address.rsplit("@", 1)[-1].lower()
    return any(domain == d or domain.endswith("." + d) for d in domains)


# --------------------------------------------------------------------------- run

def _reader(path: str) -> dict[str, Any]:
    for line in Path(path).read_text().splitlines():
        if "=" in line:
            return json.loads(line.split("=", 1)[1].strip().strip("'\""))
    raise SystemExit("no reader identity")


# The grant each sampled role reads with: the eval host role (``scripts.eval_owner_host``
# ``ROLES``, an ordinary department member, no admin role, no full read) nearest the role's
# work, never more than it (finance and receivables hold no finance marking here: an amount
# they could see is withheld, never one they could not shown). ``None``: the owner
# principals (SUPER_ADMIN, no full read); ``person:vince``: the operator's owner host.
ROLE_GRANTS = {"purchasing": "purchasing", "quality": "quality", "rnd": "quality",
               "customer_service": "sales", "receivables": "sales", "finance": "sales",
               "payables": "purchasing", "production_planning": "sales",
               "person:stephen": None}
PER_ROLE_RUN = 15
AGENT_PER_ROLE = 10
AGENT_SECONDS = 75.0


def sender_host(out: Path, t: Mapping[str, Any], people: Any, owner: Mapping[str, str],
                owner_config: str) -> tuple[Path, dict[str, str]]:
    """The host config and claims the item is read with: the sending person, with their
    role's grant (``ROLE_GRANTS``), their own Entra object id and address; the operator's
    owner host for the operator's own mail. A role with no declared grant: SystemExit."""
    from scripts import eval_owner_host as oh
    sender = str(t["sender"]).lower()
    if sender == str(owner.get("entraObjectId", "")).lower():
        return Path(owner_config), dict(owner)
    if t["role"] not in ROLE_GRANTS:
        raise SystemExit(f"no grant declared for role {t['role']}: nothing read")
    own = sorted(a for a, o in people.directory.items() if str(o).lower() == sender)
    if not own:
        raise SystemExit("sender not in the directory: nothing read")
    claims = {"uid": "assist-eval-" + _digest(sender)[:16], "email": own[0],
              "entraTenantId": owner["entraTenantId"], "entraObjectId": sender}
    role = ROLE_GRANTS[t["role"]]
    where = out / "hosts" / _digest(sender + str(role))[:16]
    if not (where / "host.json").exists():
        files = oh.build(claims, role=role, department_member=role is not None)
        files["host.json"]["entra_directory"] = False   # offline: ACL groups unread, fewer
        oh.write(where, files)
    return where / "host.json", claims


def _facts(text: str) -> list[dict[str, Any]]:
    return [{"text": s.strip(), "sources": ["gated"]}
            for s in re.split(r"(?<=[.?!])\s+", text or "") if s.strip()]


def _agent_answer(state: Path, config: Path, claims: Mapping[str, str], secret: str,
                  question: str) -> tuple[str, str]:
    """The Ask's agent path for one question as the sender (``operator_api.agent_ask``:
    one Claude Code session on the operator's login, every claim through the answer
    gate): (its gated prose, "") or ("", why it settled nothing: a failure code or the
    answerability, never text)."""
    from operator_api.agent_ask import HOST_HELPERS_ENV, AgentAsk, reply_prose
    from scripts import eval_real_agent as real
    # one host per session: helper hosts beside it outgrow the eval's memory cap and the
    # session's reads stall (measured 2026-11-10 under a 4 GB scope)
    os.environ[HOST_HELPERS_ENV] = "0"

    def host_for(_session: Any) -> Any:
        return real.LiveHost(state, config, dict(claims), secret, tenant="plx",
                             agent="plx.agent.investigate")
    ask = AgentAsk(state_dir=state, host_config=config, secret=secret,
                   seconds=AGENT_SECONDS, host_for=host_for)
    try:
        ran = ask.run(None, question, route="lookup")
    except Exception as error:  # noqa: BLE001 - a failed session settles nothing
        return "", "error:" + type(error).__name__
    if ran.failure or not ran.answer or ran.answer.get("answerability") not in (
            "answerable", "partial"):
        return "", ran.failure or ("timeout" if (ran.usage or {}).get("subtype") == "timeout"
                                   else "") \
            or str((ran.answer or {}).get("answerability") or "no_answer")
    return reply_prose(ran.final_text, ran.answer), ""


def run(state: Path, out: Path, reader_env: str, host_config: str, seconds: float,
        agent: bool = True, agent_per_role: int = AGENT_PER_ROLE) -> int:
    """Drafts for the first ``PER_ROLE_RUN`` sampled mails of each role, each read as its
    sender (``sender_host``) and as of its send instant (``Reads.as_of``: a record card is
    read as the store knew it then); a record question the fast path does not settle goes
    to the Ask's agent, at most ``AGENT_PER_ROLE`` per role (model calls on the operator's
    login), only when ``agent``. Resumable: ``drafts.jsonl`` is appended; a mail drafted
    with a record left unsettled and no agent is drafted again with it (the last line of a
    mail is its draft)."""
    import secrets
    from operating import assist, case_tools_host, fast_answer
    from scripts import eval_real_agent as real
    # a batch reader, as the proactive engine's (``warm=False``): no background SharePoint
    # warm-up per session host; a read that needs the file index builds it when asked
    # (measured 2026-11-10: the warm-up thread held each call's first two minutes)
    case_tools_host.warm_sharepoint = lambda *_a, **_k: None
    done: dict[str, dict[str, Any]] = {}
    path = out / "drafts.jsonl"
    if path.exists():
        done = {json.loads(line)["handle"]: json.loads(line) for line in path.open()}
    sample_rows = [json.loads(line) for line in (out / "sample.jsonl").open()]
    taken: Counter = Counter()
    todo, seen = [], set()
    agents_used = Counter(d.get("role") for d in done.values() if d.get("agent_calls"))
    queued: Counter = Counter()
    for t in sample_rows:
        # a mail sampled for two roles (its sender holds both) is drafted once, for the
        # first: one draft per mail
        if taken[t["role"]] < PER_ROLE_RUN and t["handle"] not in seen:
            taken[t["role"]] += 1
            seen.add(t["handle"])
            was = done.get(t["handle"])
            if was is None or (agent and "none" in (was.get("settled") or ())
                               and not was.get("agent_calls")
                               and agents_used[t["role"]] + queued[t["role"]]
                               < agent_per_role):
                queued[t["role"]] += was is not None
                todo.append(t)     # drafted again: a record the fast path left, agent on
    if not todo:
        return 0
    people = _people(state)
    if people is None or people.unavailable:
        raise SystemExit("people exclusion unavailable: nothing drafted")
    owner = _reader(reader_env)
    secret = secrets.token_hex(32)
    started = time.monotonic()
    wrote = 0
    with open(path, "a", encoding="utf-8") as f:
        for t in todo:
            left = seconds - (time.monotonic() - started)
            if left <= 0 or (t["handle"] in done and left < AGENT_SECONDS + 30):
                break       # a draft again is for its agent session: none fits this call
            config, claims = sender_host(out, t, people, owner, host_config)
            host = real.LiveHost(state, config, claims, secret, tenant="plx",
                                 agent="plx.agent.investigate")
            row: dict[str, Any] = {"handle": t["handle"], "role": t["role"], "as_of": t["at"],
                                   "grant": ROLE_GRANTS.get(t["role"], "owner")
                                   if config != Path(host_config) else "owner"}
            try:
                _sh, server = host.agent_session()
            except Exception as error:  # noqa: BLE001 - the host refused the sender
                row["refused"] = "reader_refused:" + type(error).__name__
                f.write(json.dumps(row) + "\n")
                f.flush()
                wrote += 1
                continue
            try:
                reads = fast_answer.Reads.of_server(server)
                intent = assist.parse(t["intent"])
                own = {a for a, o in people.directory.items() if o.lower() == t["sender"]}
                sent = assist.capture_history(state, t["sender"], own, until=t["at"][:10])
                recipients = assist.resolve(
                    intent, me=t["sender"], people=people, sent=sent, roles={},
                    party=lambda name, side: assist.party_addresses(reads, name, side))
                answers, settled, agent_calls, as_of_reads, agent_why = [], [], 0, [], []
                for q in assist.questions(intent):
                    asked = fast_answer.Reads.of_server(server)
                    asked.as_of = t["at"]
                    ran = fast_answer.answer(q, asked)
                    as_of_reads += asked.as_of_reads
                    text = (ran.final_text.split("```")[0].strip() if ran else "")
                    how = "fast" if text else "none"
                    if not text and agent and agents_used[t["role"]] < agent_per_role \
                            and time.monotonic() - started < seconds - AGENT_SECONDS - 10:
                        agent_calls += 1
                        text, why = _agent_answer(state, config, claims, secret, q)
                        how = "agent" if text else "none"
                        agent_why.append(why)
                    settled.append(how)
                    answers.append({"question": q, "outcome": "answered" if text else "none",
                                    "facts": _facts(text)})
                if agent_calls:
                    agents_used[t["role"]] += 1
                made = assist.draft(intent, recipients, answers, outside=recipients.outside)
                row.update({"settled": settled, "agent_calls": agent_calls,
                            "agent_why": agent_why,
                            "as_of_reads": as_of_reads,
                            "draft": {k: made[k] for k in (
                                "channel", "to", "subject", "body", "facts", "gaps",
                                "questions")}})
            except assist.AssistRefused as refused:
                row["refused"] = refused.code
            finally:
                server.close()
            f.write(json.dumps(row) + "\n")
            f.flush()
            wrote += 1
    return wrote


# --------------------------------------------------------------------------- score

def score_one(t: Mapping[str, Any], d: Mapping[str, Any]) -> dict[str, Any]:
    draft = d.get("draft") or {}
    to = set(draft.get("to") or ())
    real_to = set(t["to"])
    given = facts_of(t["intent"])
    real = facts_of(t["body"]) - given
    text = " ".join([draft.get("body") or ""] + [f["text"] for f in draft.get("facts") or ()])
    drafted = facts_of(text) - given
    real_all = facts_of(t["body"] + " " + t["subject"])
    conflict = sorted(f for f in drafted if f.startswith("status:")
                      and "status:" + STATUS[f[7:]] in real_all and f not in real_all)
    primary = t["to"][0] in to
    cited = len(draft.get("facts") or ())
    return {"role": t["role"], "outside": t["outside"], "refused": d.get("refused"),
            "has_facts": cited > 0, "settled": list(d.get("settled") or ()),
            "agent_calls": int(d.get("agent_calls") or 0),
            "as_of_reads": list(d.get("as_of_reads") or ()),
            "primary": primary, "exact": to == real_to,
            "real": len(real), "drafted": len(drafted), "found": len(drafted & real_all),
            "found_real": len(drafted & real), "unconfirmed": len(drafted - real_all),
            "conflict": len(conflict), "has_record": bool(draft.get("questions")),
            "usable": primary and not conflict}


def score(out: Path) -> dict[str, Any]:
    sample_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for line in (out / "sample.jsonl").open():
        t = json.loads(line)
        sample_rows[(t["role"], t["handle"])] = t
    first_role: dict[str, str] = {}
    for role, handle in sample_rows:
        first_role.setdefault(handle, role)
    last = {json.loads(l)["handle"]: json.loads(l) for l in (out / "drafts.jsonl").open()} \
        if (out / "drafts.jsonl").exists() else {}
    drafts = list(last.values())      # a mail drafted again: its last draft
    counts = json.loads((out / "sample-counts.json").read_text())
    per: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in drafts:
        t = sample_rows[(d.get("role") or first_role[d["handle"]], d["handle"])]
        per[t["role"]].append(score_one(t, d))
    result = {"assumption": f"mail {MAIL_MINUTES} min, review of a usable draft "
                            f"{REVIEW_MINUTES} min, an unusable draft saves nothing",
              "roles": {}}
    for role, rows in sorted(per.items()):
        n = len(rows)
        c = counts.get(role, {})
        weekly = c.get("self_started", 0) / (c.get("window_days", 20) / 7)
        usable = sum(r["usable"] for r in rows)
        drafted = sum(r["drafted"] for r in rows)
        result["roles"][role] = {
            "n": n, "sampled_of": c.get("self_started", 0),
            "self_started_per_week": round(weekly, 1),
            "refused": sum(bool(r["refused"]) for r in rows),
            "primary_right": _pct(sum(r["primary"] for r in rows), n),
            "to_exact": _pct(sum(r["exact"] for r in rows), n),
            "external": sum(r["outside"] for r in rows),
            "with_record": sum(r["has_record"] for r in rows),
            "facts_real": sum(r["real"] for r in rows),
            "facts_drafted": drafted,
            "facts_recall": _pct(sum(r["found_real"] for r in rows), sum(r["real"] for r in rows)),
            "facts_confirmed": _pct(sum(r["found"] for r in rows), drafted),
            "facts_unconfirmed": sum(r["unconfirmed"] for r in rows),
            "drafts_with_conflict": sum(bool(r["conflict"]) for r in rows),
            # recipients right: the real first recipient is on the draft; facts present: a
            # draft naming a record carries at least one gated fact; wrong facts: drafted
            # facts whose opposite status the real mail states, of all drafted facts
            "facts_present": _pct(sum(r["has_facts"] for r in rows if r["has_record"]),
                                  sum(r["has_record"] for r in rows)),
            # the same share from the fast path alone: a draft's agent answer does not
            # count, so a fast-only reading compares like for like whichever agent sessions
            # a resumed run had already held (2026-11-10: an r2 "fast-only" score counted
            # one agent answer and read as a later fast-path regression)
            "facts_present_fast": _pct(
                sum(r["has_facts"] and "fast" in r["settled"] for r in rows if r["has_record"]),
                sum(r["has_record"] for r in rows)),
            "facts_wrong": _pct(sum(r["conflict"] for r in rows), drafted),
            "questions_settled": dict(Counter(s for r in rows for s in r["settled"])),
            "agent_sessions": sum(r["agent_calls"] for r in rows),
            "read_as_of": dict(Counter(a for r in rows for a in r["as_of_reads"])),
            "usable": _pct(usable, n),
            "hours_per_week": round(weekly * (usable / n if n else 0)
                                    * (MAIL_MINUTES - REVIEW_MINUTES) / 60, 2)}
    (out / "score.json").write_text(json.dumps(result, indent=1, sort_keys=True))
    return result


def _pct(a: int, b: int) -> Any:
    return {"k": a, "n": b, "percent": round(100 * a / b, 1) if b else None}


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m evals.assist_eval")
    p.add_argument("stage", choices=("sample", "run", "score"))
    p.add_argument("--state", default="/data/plx-state")
    p.add_argument("--store", default=None)
    p.add_argument("--roles", default=None, help="roles file (role -> object ids); "
                   "person:<address> roles are added with --person")
    p.add_argument("--person", action="append", default=[])
    p.add_argument("--out", required=True)
    p.add_argument("--reader-env", default=None)
    p.add_argument("--host-config", default=None)
    p.add_argument("--seconds", type=float, default=200.0)
    p.add_argument("--agent-per-role", type=int, default=AGENT_PER_ROLE)
    p.add_argument("--no-agent", action="store_true",
                   help="never start an agent session (fast path only)")
    a = p.parse_args(argv)
    out, state = Path(a.out), Path(a.state)
    os.umask(0o077)
    if a.stage == "sample":
        roles = dict(json.loads(Path(a.roles).read_text())["roles"]) if a.roles else {}
        people = _people(state)
        for address in a.person:
            oid = people.directory.get(address.lower())
            if oid:
                roles["person:" + address.split("@")[0]] = [oid.lower()]
        print(json.dumps(sample(state, a.store, roles, out), indent=1, sort_keys=True))
    elif a.stage == "run":
        print(json.dumps({"wrote": run(state, out, a.reader_env, a.host_config, a.seconds,
                                       agent=not a.no_agent,
                                       agent_per_role=a.agent_per_role)}))
    else:
        print(json.dumps(score(out), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
