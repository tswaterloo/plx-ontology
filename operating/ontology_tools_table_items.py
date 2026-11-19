"""The per-item read rules of mail and Teams chats inside ``plx.table``'s one statement
(Teams: ``TeamsRules``).

``plx.table`` counts a captured message (``Email``) only when the mail item gate
(``OntologyTools._item_allowed`` -> ``CaseRuntime.resolve_citation`` ->
``RuntimeCitations.mail_capture``) would serve it to the reader, decided with the gate's own
functions:

* the reader's parts once per call: the people guard usable (else nothing is served and the
  call is refused), the reader's mail audience, the mailboxes ``visible_mailboxes`` serves it
  (bound as the audience keys ``mail:<mailbox id>``), the capture's ``undecided_addresses``,
  and the people directory (``RuntimeCitations.people_directory``) the gate resolves every
  participant with;
* per message, in SQL over the projected sender (``row.from``) and To/Cc lists
  (``row.to``/``row.cc``): a message naming an undecided address, an excluded or undecided
  participant, or no sender identity is not there for this reader (as the gate: never
  counted, never withheld). Only what SQL provably decides as Python does is decided there:
  a plain ASCII address (``mail_address`` of it is itself) that a captured mailbox records
  (bound: each such address decided by ``PeopleDirectory.address``), or whose domain is
  outside the tenant (external: cleared). Every other string (a tenant address no mailbox
  holds, an object id, any non-ASCII or unusual form) is returned by a first pass, decided
  by the same ``PeopleDirectory`` call and bound for a second pass; past ``PENDING_MAX``
  distinct strings the rows naming an undecided one are ``withheld`` (the answer partial);
* Bcc is not projected: a message that may carry one (any sender but an external address;
  Graph keeps Bcc on the sender's copy only) is looked up in the capture by its handle (the
  gate's latest version), its Bcc decided as To/Cc, its whole record checked for Freightcom;
  past ``BCC_MAX`` such messages, or past the time a bounded call leaves its second
  pass, the rest are ``withheld``. A handle the capture does not
  hold is not there (the gate reads nothing for it either). A message
  any projected value of which names Freightcom (``image_ocr_gpu.FREIGHTCOM``, the OCR
  refusal's own pattern), or whose captured record does (``touches_freightcom``, read for
  the messages looked up above), is never in a group or row: it is counted ``withheld``.

Attachments, the types derived from mail, SharePoint, Teams channels and files stay
refused: their permissions (carried-mail verdicts, item ACLs) are not held in Postgres."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable, Mapping

from operating.ontology_tools_identity import ToolError

# Record types read through the mail item gate's rules here.
MAIL_TYPES = frozenset({"Email"})
MAIL_SCHEME = "graph-mail"
PENDING_MAX = 500
BCC_MAX = 60_000
_CHUNK = 500
# a plain address whose ``mail_address`` (ontology.identity_scoring) is itself, lower case:
# no whitespace, <>@,; or non-ASCII, no leading dot, a ``dns_domain`` domain
_STRICT = (r"^[a-z0-9!#$%&'*+/=?^_`{|}~-][a-z0-9.!#$%&'*+/=?^_`{|}~-]*"
           r"@[a-z0-9-]+(\.[a-z0-9-]+)+$")
_STRICT_RE = re.compile(_STRICT)
_GUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_PRINTABLE = r"^[ -~]*$"
OK, IGNORE, DENY = "ok", "ignore", "deny"


def _sqlite_fold(value: str) -> str:
    """SQLite's ``lower(trim(x))``: spaces trimmed, ASCII letters lowered only (the capture's
    ``undecided_free_sql`` compares so)."""
    return value.strip(" ").translate(_ASCII_LOWER)


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def freightcom_match(expr: str, q: Any) -> str:
    """SQL true when the text ``expr`` names Freightcom: the one rule
    (``integrations.freightcom_rule``), in SQL."""
    from integrations.freightcom_rule import sql_match
    return sql_match(expr, q.bind)


class MailRules:
    """One reader's mail parts for one ``plx.table`` call (module doc). Raises
    ``ToolError`` when the gate's parts cannot be computed (no capture, no usable people
    guard, no runtime): nothing is then counted from mail, and the caller is told."""

    def __init__(self, base: Any, actor: Any) -> None:
        runtime = getattr(getattr(base, "item_gate", None), "runtime", None)
        if runtime is None:
            raise ToolError("not_served", "this host decides mail per item through no "
                            "case runtime, so plx.table cannot apply its rules",
                            "use plx.find or plx.documents")
        holds = getattr(runtime, "_holds_mail_capture", None)
        if not callable(holds) or not holds():
            raise ToolError("not_served", "this host holds no mail capture plx.table can "
                            "decide mail with", "use plx.find or plx.documents")
        refusal = runtime.exclusion_refusal()
        if refusal:
            raise ToolError("unavailable", "the people exclusion cannot be read now, so no "
                            "mail is served", None)
        from operating.ontology_tools_evidence import RuntimeCitations
        self.runtime = runtime
        self.reader = RuntimeCitations(runtime, mail_capture_dir=runtime.mail_capture_dir)
        self.decided: dict[str, str] = {}
        self.bcc_ok: set[str] = set()
        self.bcc_deny: set[str] = set()
        self.bcc_capped = False
        self.pending_capped = False
        try:
            audience = runtime.live.audience
            gated = runtime.employee_gate(actor)
            with self.reader._mail_store() as store:
                if not audience.may_read_any(gated, "mail:"):
                    boxes: frozenset[str] = frozenset()
                else:
                    boxes = self.reader.visible_mailboxes(store, gated, audience)
                self.directory = self.reader.people_directory(store, {})
                if self.directory is None or self.directory.unavailable:
                    raise ToolError("unavailable", "the people directory cannot be read "
                                    "now, so no mail is served")
                self.undecided = sorted(self._undecided(store))
                identities = self._mailbox_identities(store)
        except ToolError:
            raise
        except Exception:  # noqa: BLE001 - the gate would deny: refuse, never guess
            raise ToolError("unavailable", "the mail capture cannot be read now, so no "
                            "mail is served") from None
        self.keys = sorted(f"mail:{box}" for box in boxes)
        self.domains = sorted(self.directory.domains)
        # every plain address a captured mailbox records, decided as the gate decides it
        self.box_all = sorted(identities)
        self.box_ok = sorted(a for a in identities if self._resolve(a) == OK)

    # ------------------------------------------------------------------ deciding

    @staticmethod
    def _undecided(store: Any) -> set[str]:
        try:
            return {a for (a,) in store.db.execute("SELECT address FROM undecided_addresses")}
        except Exception as exc:  # noqa: BLE001
            if "no such table" in str(exc):
                return set()
            raise

    @staticmethod
    def _mailbox_identities(store: Any) -> set[str]:
        out = set()
        for box, upn, text in store.db.execute(
                "SELECT mailbox_id, upn, identities FROM mailboxes"):
            try:
                listed = json.loads(text) if isinstance(text, str) else []
            except ValueError:
                listed = []
            for value in [box, upn, *(listed if isinstance(listed, list) else [])]:
                if isinstance(value, str):
                    v = value.strip().lower()
                    if _STRICT_RE.match(v):
                        out.add(v)
        return out

    def _resolve(self, value: str) -> str:
        """``ok`` / ``deny`` for one identity ``value`` (stripped, lowered), as
        ``excluded_people.participants_withheld`` resolves it."""
        from operating.excluded_people import EXTERNAL, PERSON, _GUID as GUID
        found = (self.directory.object_id(value) if GUID.match(value)
                 else self.directory.address(value))
        return OK if getattr(found, "state", None) in (PERSON, EXTERNAL) else DENY

    def category(self, raw: str) -> str:
        """``ok`` (a cleared identity), ``ignore`` (no identity: a display name) or
        ``deny`` (an undecided address, an excluded or undecidable identity) for one
        captured participant string, as the gate decides it."""
        from operating.excluded_people import _GUID as GUID
        if _sqlite_fold(raw) in self._undecided_set:
            return DENY
        value = raw.strip().lower()
        if not value or not (GUID.match(value) or "@" in value):
            return IGNORE
        return self._resolve(value)

    @property
    def _undecided_set(self) -> frozenset[str]:
        got = getattr(self, "_undecided_frozen", None)
        if got is None:
            got = self._undecided_frozen = frozenset(self.undecided)
        return got

    def learn(self, probe: Mapping[str, Any], until: float | None = None) -> None:
        """Decide what a first pass returned (``probe``: ``pending`` strings, ``bcc``
        handles), so the second pass binds it. ``until`` (``time.monotonic``): the Bcc
        lookups stop there, so the second pass keeps its time; the handles not looked up
        are withheld (``bcc_capped``), never counted undecided."""
        pending = [s for s in probe.get("pending") or () if isinstance(s, str)]
        if len(pending) > PENDING_MAX:
            self.pending_capped = True
            pending = pending[:PENDING_MAX]
        for raw in pending:
            if raw not in self.decided:
                self.decided[raw] = self.category(raw)
        handles = [h for h in probe.get("bcc") or () if isinstance(h, str)]
        if len(handles) > BCC_MAX:
            self.bcc_capped = True
            handles = handles[:BCC_MAX]
        self._decide_bcc(handles, until)

    def _decide_bcc(self, handles: list[str], until: float | None = None) -> None:
        from integrations.image_ocr_gpu import touches_freightcom
        from operating.excluded_people import strings
        wanted = [h for h in handles if h not in self.bcc_ok and h not in self.bcc_deny]
        if not wanted:
            return
        found: dict[str, Any] = {}
        with self.reader._mail_store() as store:
            for i in range(0, len(wanted), _CHUNK):
                if until is not None and time.monotonic() >= until:
                    self.bcc_capped = True
                    wanted = wanted[:i]
                    break
                chunk = wanted[i:i + _CHUNK]
                marks = ",".join("?" * len(chunk))
                # the gate's version of each handle: its latest (``MailCaptureStore._latest``)
                for handle, record in store.db.execute(
                        "SELECT l1_handle, record FROM messages WHERE version_id IN ("
                        "SELECT max(version_id) FROM messages WHERE l1_handle IN "
                        f"({marks}) GROUP BY l1_handle)", chunk):
                    found[handle] = record
        for handle in wanted:
            record = found.get(handle)
            try:
                rec = json.loads(record) if isinstance(record, str) else None
            except ValueError:
                rec = None
            if not isinstance(rec, Mapping):
                self.bcc_deny.add(handle)   # not held (or unreadable): the gate serves nothing
                continue
            if touches_freightcom(rec):
                self.freightcom.add(handle)
            bcc = [s for s in strings(rec.get("bcc")) if isinstance(s, str)]
            if any(self.category(s) == DENY for s in bcc):
                self.bcc_deny.add(handle)
            else:
                self.bcc_ok.add(handle)

    @property
    def freightcom(self) -> set[str]:
        got = getattr(self, "_freightcom", None)
        if got is None:
            got = self._freightcom = set()
        return got

    def by_category(self, category: str) -> list[str]:
        return sorted(raw for raw, cat in self.decided.items() if cat == category)

    # ------------------------------------------------------------------ SQL

    def join(self, q: Any, ver: str = "v.ver") -> str:
        """A lateral join ``ml`` over the record's projected sender and To/Cc: ``deny``
        (the gate serves it to nobody: an undecided address, a participant not cleared,
        no sender identity), ``pend`` (the strings not decided yet), ``bad`` (a list that
        is no JSON array of strings: never decided here), ``mb`` (it may carry a Bcc) and
        ``fc`` (a projected value names Freightcom)."""
        und = q.bind(self.undecided)
        box_all, box_ok = q.bind(self.box_all), q.bind(self.box_ok)
        doms = q.bind(self.domains)
        ok, ign, deny = (q.bind(self.by_category(OK)), q.bind(self.by_category(IGNORE)),
                         q.bind(self.by_category(DENY)))
        v = "lower(btrim(p.s, ' '))"
        strict = f"{v} ~ {q.bind(_STRICT)}"
        domain = f"split_part({v}, '@', 2)"
        # one participant string's category: ok / ignore / deny / pend
        cat = (f"CASE WHEN p.s = ANY({ok}::text[]) THEN 'ok' "
               f"WHEN p.s = ANY({ign}::text[]) THEN 'ignore' "
               f"WHEN p.s = ANY({deny}::text[]) THEN 'deny' "
               f"WHEN p.s !~ {q.bind(_PRINTABLE)} THEN 'pend' "
               f"WHEN {v} = ANY({und}::text[]) THEN 'deny' "
               f"WHEN {v} ~ {q.bind(_GUID)} THEN 'pend' "
               f"WHEN position('@' in {v}) = 0 THEN 'ignore' "
               f"WHEN NOT ({strict}) THEN 'pend' "
               f"WHEN {v} = ANY({box_all}::text[]) THEN CASE WHEN {v} = ANY({box_ok}::text[]) "
               "THEN 'ok' ELSE 'deny' END "
               f"WHEN NOT ({domain} = ANY({doms}::text[])) THEN 'ok' "
               "ELSE 'pend' END")
        # the sender is external (so no Bcc can be on this copy): a plain address no
        # mailbox records, outside the tenant
        external = (f"(p.s ~ {q.bind(_PRINTABLE)} AND {strict} AND "
                    f"NOT ({v} = ANY({box_all}::text[])) AND NOT ({domain} = ANY({doms}::text[])))")
        # the participant fields' ids, read once per statement (an uncorrelated InitPlan):
        # matched per message through the cell key, never a field-path scan per message
        src = "FROM value_sources s JOIN value_cells c ON c.src = s.src"
        own = f"s.ver = {ver} AND s.superseded_at IS NULL"
        sender = ("c.field = ANY(ARRAY(SELECT field FROM value_fields "
                  "WHERE field_path = 'row.from'))")
        rcpts = ("c.field = ANY(ARRAY(SELECT field FROM value_fields "
                 "WHERE field_path IN ('row.to', 'row.cc')))")
        array = ("(pg_input_is_valid(c.value, 'json') AND "
                 "json_typeof(c.value::json) = 'array')")
        lists = (f"SELECT 'rcpt'::text AS role, CASE WHEN json_typeof(e.x) = 'string' THEN "
                 "e.x #>> '{}' END AS s, json_typeof(e.x) <> 'string' AS bad "
                 f"{src} CROSS JOIN LATERAL json_array_elements(CASE WHEN {array} "
                 "THEN c.value::json ELSE '[]'::json END) e(x) "
                 f"WHERE {own} AND {rcpts} "
                 f"UNION ALL SELECT 'rcpt', NULL, true {src} WHERE {own} AND {rcpts} "
                 f"AND NOT {array}")
        parts = (f"SELECT 'from'::text AS role, c.value AS s, c.value IS NULL AS bad {src} "
                 f"WHERE {own} AND {sender} UNION ALL {lists}")
        cells = f"{src} WHERE {own}"
        return (
            "LEFT JOIN LATERAL (SELECT "
            "COALESCE(bool_or(t.cat = 'deny'), false) "
            "OR NOT COALESCE(bool_or(t.role = 'from' AND t.cat IN ('ok', 'pend')), false) "
            "OR COALESCE(count(*) FILTER (WHERE t.role = 'from'), 0) <> 1 AS deny, "
            "COALESCE(array_agg(DISTINCT t.s) FILTER (WHERE t.cat = 'pend'), '{}') AS pend, "
            "COALESCE(bool_or(t.bad), false) AS bad, "
            "NOT COALESCE(bool_or(t.role = 'from' AND t.ext), false) AS mb, "
            f"EXISTS (SELECT 1 {cells} AND {freightcom_match('c.value', q)}) AS fc "
            f"FROM (SELECT p.role, p.s, p.bad, CASE WHEN p.s IS NULL THEN 'ignore' ELSE {cat} "
            f"END AS cat, COALESCE({external}, false) AS ext FROM ({parts}) p) t) ml ON true")

    def deny_sql(self, q: Any, handle: str = "v.l1_handle") -> str:
        """True for a record the gate serves to nobody (never counted, never withheld)."""
        return (f"(ml.deny OR (ml.mb AND {handle} = ANY({q.bind(sorted(self.bcc_deny))}"
                "::text[])))")

    def ok_sql(self, q: Any, handle: str = "r._h") -> str:
        """True for a kept record every rule decided served and that names no Freightcom:
        any other is counted ``withheld``."""
        return (f"(cardinality(r._mpend) = 0 AND NOT r._mbad AND NOT r._mfc AND NOT ("
                f"{handle} = ANY({q.bind(sorted(self.freightcom))}::text[])) AND "
                f"(NOT r._mmb OR {handle} = ANY({q.bind(sorted(self.bcc_ok))}::text[])))")

    def gaps(self) -> list[str]:
        out = []
        if self.pending_capped:
            out.append(f"more than {PENDING_MAX} distinct participants needed deciding one "
                       "by one: the messages naming the rest are withheld")
        if self.bcc_capped:
            out.append(f"more messages may carry a Bcc than this call could look up (at "
                       f"most {BCC_MAX}, within its time): the rest are withheld (narrow the "
                       "request)")
        return out


    # ------------------------------------------------------------------ the table's hooks

    scheme = MAIL_SCHEME
    columns = ", ml.pend AS _mpend, ml.bad AS _mbad, ml.mb AS _mmb, ml.fc AS _mfc"
    withheld_note = ("one whose data the owner's egress rule keeps from any model, or one "
                     "whose participants or Bcc could not be decided in this call")

    def audience_sql(self, alias: str, q: Any) -> str:
        """The record's audience key one of the mailboxes the gate serves this reader."""
        return f"{alias}.audience_key = ANY({q.bind(self.keys)}::text[])"

    def extras_sql(self, matched: str, q: Any) -> str:
        """What the rules left for the gate's own functions (``learn``): the distinct
        participant strings not decided, and the messages that may carry a Bcc not looked
        up yet, of the matching records; and how many records the rules withheld."""
        return (", 'pending', (SELECT json_agg(x.s) FROM (SELECT DISTINCT unnest(r._mpend) "
                f"AS s FROM kept r WHERE r._fok AND ({matched}) ORDER BY 1 LIMIT "
                f"{PENDING_MAX + 1}) x), "
                "'bcc', (SELECT json_agg(x.h) FROM (SELECT DISTINCT r._h AS h FROM kept r "
                f"WHERE r._fok AND r._mmb AND ({matched}) AND NOT (r._h = ANY("
                f"{q.bind(sorted(self.bcc_ok))}::text[])) ORDER BY 1 LIMIT {BCC_MAX + 1}) x), "
                "'withheld_items', (SELECT count(*) FROM kept r WHERE r._fok AND NOT r._ok)")

    @staticmethod
    def needs_second(got: Mapping[str, Any]) -> bool:
        return bool(got.get("pending") or got.get("bcc"))


# --------------------------------------------------------------------------- Teams

TEAMS_TYPES = frozenset({"TeamsChatMessage"})
TEAMS_SCHEME = "teams-chat"
TEAMS_MAX = 60_000


class TeamsRules:
    """One reader's Teams chat rules for one ``plx.table`` call. A captured chat message
    counts only when the item gate would serve it to the reader, decided by the gate's
    own function over the host's Teams capture (``CaseRuntime.capture_resolve_many``: the
    live audience, the employee's own item ACL check over the message's and the chat's
    latest captured membership with its visible-history start and capture-age bound, the
    people rule over the sender, the participants and the membership, object ids only),
    read in batches:

    * the first pass returns the matching messages' handles (at most ``TEAMS_MAX``); each
      is decided by that function and bound for the second pass. A message the gate
      refuses is not there (never counted, never withheld); one not decided (past the cap,
      or past the time the call leaves its second pass) is ``withheld``;
    * Freightcom: a message any projected value of which names Freightcom
      (``image_ocr_gpu.FREIGHTCOM``), or whose captured record does
      (``touches_freightcom``), is never in a group or row: it is counted ``withheld``.

    Raises ``ToolError`` when the gate's parts are not there (no runtime, no live audience,
    no Teams capture) or the people guard is not usable: nothing is counted from Teams."""

    scheme = TEAMS_SCHEME
    columns = ", ml.fc AS _mfc"
    withheld_note = ("one whose data the owner's egress rule keeps from any model, or one "
                     "this call could not decide")

    def __init__(self, base: Any, actor: Any) -> None:
        runtime = getattr(getattr(base, "item_gate", None), "runtime", None)
        if runtime is None or not callable(getattr(runtime, "capture_resolve_many", None)) \
                or not callable(getattr(runtime, "teams_chat_units", None)):
            raise ToolError("not_served", "this host decides Teams messages per item "
                            "through no case runtime, so plx.table cannot apply its rules",
                            "use plx.find or plx.documents")
        from integrations.capture_teams import DB_FILE, STORE_DIR
        if getattr(runtime, "live", None) is None or not (
                runtime.state_dir / STORE_DIR / DB_FILE).is_file():
            raise ToolError("not_served", "this host holds no Teams capture plx.table can "
                            "decide messages with", "use plx.find or plx.documents")
        if runtime.exclusion_refusal():
            raise ToolError("unavailable", "the people exclusion cannot be read now, so no "
                            "Teams message is served", None)
        self.runtime, self.actor = runtime, actor
        # the chats whose membership admits the reader at all: no message of another chat
        # is read, counted or withheld (``CaseRuntime.teams_chat_units``)
        self.keys = sorted(runtime.teams_chat_units(actor))
        self.ok: set[str] = set()
        self.deny: set[str] = set()
        self.freightcom: set[str] = set()
        self.capped = False

    def learn(self, probe: Mapping[str, Any], until: float | None = None) -> None:
        """Decide the handles a first pass returned (``probe['teams']``) with the gate's
        function; those not decided by ``until`` stay undecided (withheld)."""
        from integrations.image_ocr_gpu import touches_freightcom
        handles = [h for h in probe.get("teams") or () if isinstance(h, str)
                   and h not in self.ok and h not in self.deny]
        if len(handles) > TEAMS_MAX:
            self.capped = True
            handles = handles[:TEAMS_MAX]
        try:
            got = self.runtime.capture_resolve_many(handles, self.actor, until=until)
        except Exception:  # noqa: BLE001 - undecided: withheld, never counted
            got = {}
        if len(got) < len(handles):
            self.capped = True
        for handle, result in got.items():
            if result is None:
                self.deny.add(handle)
                continue
            self.ok.add(handle)
            if touches_freightcom(result):
                self.freightcom.add(handle)

    @staticmethod
    def needs_second(got: Mapping[str, Any]) -> bool:
        return bool(got.get("teams"))

    def audience_sql(self, alias: str, q: Any) -> str:
        return f"{alias}.audience_key = ANY({q.bind(self.keys)}::text[])"

    def join(self, q: Any, ver: str = "v.ver") -> str:
        """A lateral join ``ml``: ``fc``, whether a projected value names Freightcom."""
        return ("LEFT JOIN LATERAL (SELECT EXISTS (SELECT 1 FROM value_sources s JOIN "
                f"value_cells c ON c.src = s.src WHERE s.ver = {ver} AND s.superseded_at "
                f"IS NULL AND {freightcom_match('c.value', q)}) AS fc) ml ON true")

    def deny_sql(self, q: Any, handle: str = "v.l1_handle") -> str:
        """True for a message the gate refuses this reader (not there)."""
        return f"({handle} = ANY({q.bind(sorted(self.deny))}::text[]))"

    def ok_sql(self, q: Any, handle: str = "r._h") -> str:
        """True for a kept message the gate serves and that names no Freightcom."""
        return (f"(NOT r._mfc AND {handle} = ANY({q.bind(sorted(self.ok - self.freightcom))}"
                "::text[]))")

    def extras_sql(self, matched: str, q: Any) -> str:
        """The matching messages not decided yet, and how many the rules withheld."""
        decided = q.bind(sorted(self.ok | self.deny))
        return (", 'teams', (SELECT json_agg(x.h) FROM (SELECT DISTINCT r._h AS h FROM kept r "
                f"WHERE r._fok AND ({matched}) AND NOT (r._h = ANY({decided}::text[])) "
                f"ORDER BY 1 LIMIT {TEAMS_MAX + 1}) x), "
                "'withheld_items', (SELECT count(*) FROM kept r WHERE r._fok AND NOT r._ok)")

    def gaps(self) -> list[str]:
        if not self.capped:
            return []
        return [f"more Teams messages matched than this call could decide (at most "
                f"{TEAMS_MAX}, within its time): the rest are withheld (narrow the request)"]


def item_family(otype: str) -> str | None:
    if otype in MAIL_TYPES:
        return "mail"
    return "teams" if otype in TEAMS_TYPES else None


def item_rules(family: str, base: Any, actor: Any) -> Any:
    """The rules of ``family`` (``item_family``) for one reader and call."""
    return (MailRules if family == "mail" else TeamsRules)(base, actor)


__all__ = ["MAIL_TYPES", "TEAMS_TYPES", "MailRules", "TeamsRules", "item_family",
           "item_rules", "freightcom_match"]
