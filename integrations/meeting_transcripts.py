"""Meeting transcripts from every source as one shape, admitted only past the people
exclusion, and marked.

Sources (``parse``):

* ``teams_vtt``: a Teams meeting transcript as the meetings capture keeps it
  (``integrations.capture_meetings.transcript_row``: WebVTT cues, its meeting's subject,
  start, organizer); the capture reads it through the one GET-only Graph client and stays
  dormant until OnlineMeetingTranscript.Read.All is granted
  (``capture_meetings.TRANSCRIPTS_GRANTED``). A raw WebVTT body with its meeting's
  metadata (``vtt`` plus ``meeting``) is read the same way.
* ``export``: a Circleback or Granola export dropped in ``<state>/meeting-exports/`` (no
  API is called for either): JSON (``title``/``name``, ``date``/``start``/``createdAt``,
  ``attendees`` [{name, email}], ``organizer``, ``transcript`` [{speaker, timestamp|start,
  text}], optional ``notes``) or markdown (``# Title``, ``Date:``, ``Organizer:``,
  ``Attendees: Name <address>, ...`` and one line per utterance: ``[hh:mm:ss] Name: text``,
  ``**Name** (hh:mm:ss): text`` or ``Name: text``).

Anything else is refused (``TranscriptRefused("unknown_format")``): nothing is guessed.

Admission (``admit``, fail closed): the people exclusion must be usable; the organizer and
every attendee must be decided not excluded by address (an attendee with no address, or one
the guard cannot decide, withholds the whole transcript); every speaker must be the
organizer or an attendee by display name (an unlisted voice could be anyone, an excluded
person included). A withheld transcript is never stored or read further; only its reason
is counted.

Markings (``markings``): every transcript is ``meeting`` (visible to its participants and
the owner); one whose title, notes or any line speaks of pay, HR or personal matters
(``SENSITIVE``: a general vocabulary, never a person or a case) is also ``hr`` and ``pii``.
Over-marking only narrows who reads it.

Stdlib only; no network call.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "plx-meeting-transcript/v1"
SOURCES = ("teams_vtt", "circleback", "granola", "markdown", "json")
MAX_CUES = 20_000
MAX_TEXT = 2000
MEETING, HR, PII = "meeting", "hr", "pii"

# Pay, HR and personal matters, said any way a meeting says them (word bounded, any case,
# word forms included). Fail closed: a phrase that may be about a person's pay, job, health
# or leave marks the meeting hr; only business senses that are plainly not ("fire drill",
# "freight rate") stay unmarked.
_POSS = r"(?:\w+'s|his|her|their|your|my|our\s+staff'?s?)"
SENSITIVE = re.compile(
    r"\b(?:salar(?:y|ies)|payroll|pay\s?(?:cheque|check|stub|slip)s?|payslips?"
    r"|pay\s+(?:rate|cut|raise|rise|increase|bump|review|grade|band|scale|equity|gap)s?"
    r"|" + _POSS + r"\s+(?:pay|salary|wages?|(?:hourly\s+)?rate|raises?|bonus(?:es)?|"
    r"review|leave|hours|overtime|benefits|contract)"
    r"|pay\s+(?:is\s+|was\s+)?(?:going|goes|go|went|gone)\s+(?:up|down)"
    r"|pay\s+(?:him|her|them)\s+(?:more|less)"
    r"|(?:increase|increasing|raise|raising|bump|bumping|cut|cutting|lower|reduce|reducing)\s+"
    r"(?:" + _POSS + r"\s+)?(?:pay|salary|salaries|wages?|hourly\s+rate)"
    r"|(?:got|get|gets|getting|give|gives|giving|gave|asked\s+for|ask\s+for|deserves?)\s+"
    r"(?:(?:him|her|them|me|you)\s+)?an?\s+raise|raises?\s+for"
    r"|compensat\w*|bonus(?:es)?|wages?|hourly(?:\s+rate)?|overtime\s+(?:rate|pay)|per\s+hour"
    r"|day\s+rate|terminat\w*|fir(?:e|es|ed|ing)\b(?!\s+(?:drill|alarm|extinguisher|"
    r"marshal|safety|code|door|exit|inspection|suppression|hazard))"
    r"|let(?:s|ting)?\s+(?:\w+\s+){1,2}go\b|lay\s?-?offs?|laid\s+off|laying\s+off"
    r"|disciplin\w*|written\s+warning|misconduct|grievances?|harass\w*|resign\w*"
    r"|performance\s+(?:review|improvement|issue|problem|plan|evaluation|appraisal)s?|\bpip\b"
    r"|appraisals?"
    r"|(?:on|sick|medical|maternity|paternity|parental|bereavement|stress|unpaid|disability|"
    r"compassionate)\s+leave|leaves?\s+of\s+absence"
    r"|medical\w*|sick\w*|surger(?:y|ies)|surgical|hospital\w*|illness\w*|injur(?:y|ies|ed)"
    r"|diagnos\w+|disabilit\w+|doctor'?s?|therap(?:y|ist)|pregnan\w*|maternity|paternity"
    r"|accommodations?\b(?!\s+(?:for|of)\s+(?:the\s+)?(?:order|delivery|shipment|customer))"
    r"|benefits?\s+(?:enrol\w*|claim|plan)|\bsin\s+number|social\s+insurance|\bssn\b"
    r"|date\s+of\s+birth|home\s+address|immigration|work\s+permit|background\s+check"
    r"|personal\s+(?:matter|issue|reasons|situation)|family\s+emergency)",
    re.I)

_VTT_CUE = re.compile(r"^\s*((?:\d{1,2}:)?\d{2}:\d{2})[.,]\d{1,3}\s+-->")
_CLOCK = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})$")
_MD_LINE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\[(?P<t1>(?:\d{1,2}:)?\d{1,2}:\d{2})\]\s*)?(?:\*\*)?"
    r"(?P<who>[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3})(?:\*\*)?\s*"
    r"(?:\((?P<t2>(?:\d{1,2}:)?\d{1,2}:\d{2})\))?\s*(?:\*\*)?:(?:\*\*)?\s+(?P<text>\S.*)$")
_MD_FIELD = re.compile(r"^\s*(?:\*\*)?(title|date|start|organi[sz]er|attendees|participants)"
                       r"(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.+)$", re.I)
_PERSON = re.compile(r"^\s*([^<>,]+?)\s*(?:<\s*([^<>\s,]+@[^<>\s,]+)\s*>)?\s*$")
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")
_HEADER_WORDS = frozenset({"title", "date", "start", "organizer", "organiser", "attendees",
                           "participants", "notes", "summary", "action items", "transcript"})


class TranscriptRefused(ValueError):
    """A transcript that is not ingested; ``code`` says why (counted, never its content)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    out = " ".join(value.split())
    return out[:limit] or None


def _clock(seconds: Any) -> str | None:
    """hh:mm:ss from seconds or a clock string; None when it is neither."""
    if isinstance(seconds, bool):
        return None
    if isinstance(seconds, (int, float)) and seconds >= 0:
        s = int(seconds)
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
    if isinstance(seconds, str):
        m = _CLOCK.match(seconds.strip().split(".")[0])
        if m:
            return f"{int(m.group(1) or 0):02d}:{int(m.group(2)):02d}:{int(m.group(3)):02d}"
    return None


def _person(raw: Any) -> dict[str, str | None] | None:
    if isinstance(raw, Mapping):
        name = _text(raw.get("name") or raw.get("displayName"), 120)
        address = raw.get("email") or raw.get("address") or (
            (raw.get("emailAddress") or {}).get("address") if isinstance(
                raw.get("emailAddress"), Mapping) else None)
    elif isinstance(raw, str):
        m = _PERSON.match(raw)
        if not m:
            return None
        name, address = _text(m.group(1), 120), m.group(2)
    else:
        return None
    address = address.strip().lower() if isinstance(address, str) and _ADDRESS.match(
        address.strip()) else None
    if not name and not address:
        return None
    return {"name": name, "address": address}


def _people(raw: Any) -> list[dict[str, str | None]]:
    items = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, list) else []
    out = []
    for item in items:
        p = _person(item)
        if p and p not in out:
            out.append(p)
    return out


def _cue(i: int, t: str | None, speaker: Any, text: Any) -> dict[str, Any] | None:
    words = _text(text)
    if not words:
        return None
    return {"i": i, "t": t, "speaker": _text(speaker, 120), "text": words}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def _finish(source: str, meeting_id: Any, title: Any, start: Any, end: Any,
            organizer: Any, attendees: Iterable[Any], cues: Sequence[Mapping[str, Any]],
            notes: Any = None) -> dict[str, Any]:
    if not cues:
        raise TranscriptRefused("no_utterances")
    if len(cues) > MAX_CUES:
        raise TranscriptRefused("too_long")
    start = start if isinstance(start, str) and _DAY.match(start) else None
    if start is None:
        raise TranscriptRefused("no_meeting_date")
    org = _person(organizer)
    people = [p for p in _people(list(attendees)) if p != org]
    body = {"source": source, "title": _text(title, 300) or "Meeting", "start": start,
            "end": end if isinstance(end, str) and _DAY.match(end) else None,
            "organizer": org, "attendees": people, "cues": list(cues),
            "notes": _text(notes, 20_000)}
    digest = _digest(body)
    mid = _text(meeting_id, 300) if isinstance(meeting_id, str) else None
    mid = re.sub(r"[^A-Za-z0-9_.:-]", "_", mid)[:120] if mid else digest[:24]
    body.update(schema=SCHEMA, meeting_id=mid, digest=digest,
                citation=f"transcript://{source}/{mid}@{digest[:12]}")
    body["markings"] = markings(body)
    return body


def parse_vtt(text: str) -> list[dict[str, Any]]:
    """WebVTT cues as (index, start clock, speaker, text)."""
    from .capture_meetings import parse_vtt as capture_vtt
    out = []
    for c in capture_vtt(text):
        cue = _cue(len(out), _clock(c.get("start")), c.get("speaker"), c.get("text"))
        if cue:
            out.append(cue)
    return out


def from_teams(row: Mapping[str, Any]) -> dict[str, Any]:
    """A captured Teams transcript (``capture_meetings.transcript_row``) or a raw WebVTT
    body with its meeting (``{"vtt": ..., "meeting": {...}}``)."""
    if isinstance(row.get("vtt"), str):
        meeting = row.get("meeting") if isinstance(row.get("meeting"), Mapping) else {}
        cues = parse_vtt(row["vtt"])
        return _finish("teams_vtt", meeting.get("id"), meeting.get("subject"),
                       meeting.get("start"), meeting.get("end"), meeting.get("organizer"),
                       meeting.get("attendees") or (), cues)
    cues = []
    for c in row.get("cues") or ():
        if isinstance(c, Mapping):
            cue = _cue(len(cues), _clock(c.get("start")), c.get("speaker"), c.get("text"))
            if cue:
                cues.append(cue)
    return _finish("teams_vtt", row.get("ical_uid") or row.get("meeting_id"),
                   row.get("subject"), row.get("meeting_start"), row.get("meeting_end"),
                   row.get("organizer_person") or row.get("organizer"),
                   row.get("attendees") or (), cues)


def from_json(doc: Mapping[str, Any], source: str = "json") -> dict[str, Any]:
    lines = doc.get("transcript") or doc.get("utterances") or doc.get("segments")
    if not isinstance(lines, list):
        raise TranscriptRefused("unknown_format")
    cues = []
    for line in lines:
        if not isinstance(line, Mapping):
            raise TranscriptRefused("unknown_format")
        speaker = line.get("speaker")
        if isinstance(speaker, Mapping):
            speaker = speaker.get("name")
        t = _clock(line.get("timestamp", line.get("start", line.get("time"))))
        cue = _cue(len(cues), t, speaker, line.get("text") or line.get("content"))
        if cue:
            cues.append(cue)
    start = doc.get("date") or doc.get("start") or doc.get("createdAt") or doc.get("start_time")
    return _finish(source, doc.get("id"), doc.get("title") or doc.get("name"), start,
                   doc.get("end"), doc.get("organizer"),
                   doc.get("attendees") or doc.get("participants") or (), cues,
                   doc.get("notes") or doc.get("summary"))


def from_markdown(text: str, source: str = "markdown") -> dict[str, Any]:
    fields: dict[str, str] = {}
    title = None
    cues: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if title is None and line.startswith("# "):
            title = line[2:].strip()
            continue
        field = _MD_FIELD.match(line)
        if field and not cues:
            fields[field.group(1).lower().replace("organiser", "organizer")] = field.group(2)
            continue
        m = _MD_LINE.match(line)
        if m and m.group("who").lower() not in _HEADER_WORDS:
            cue = _cue(len(cues), _clock(m.group("t1") or m.group("t2") or ""),
                       m.group("who"), m.group("text"))
            if cue:
                cues.append(cue)
    if not cues:
        raise TranscriptRefused("unknown_format")
    return _finish(source, None, fields.get("title") or title,
                   (fields.get("date") or fields.get("start") or "").strip(), None,
                   fields.get("organizer"),
                   _people(fields.get("attendees") or fields.get("participants") or ""), cues)


def parse(raw: Any, *, source: str | None = None) -> dict[str, Any]:
    """One transcript in the common shape (module doc); ``TranscriptRefused`` otherwise."""
    if isinstance(raw, Mapping):
        if source == "teams_vtt" or raw.get("source") == "graph_meeting_transcript" \
                or isinstance(raw.get("vtt"), str):
            return from_teams(raw)
        return from_json(raw, source if source in SOURCES else "json")
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise TranscriptRefused("unknown_format") from None
    if isinstance(raw, str):
        stripped = raw.lstrip("﻿").lstrip()
        if stripped.startswith("WEBVTT"):
            raise TranscriptRefused("vtt_without_meeting")
        if stripped.startswith("{"):
            try:
                doc = json.loads(stripped)
            except ValueError:
                raise TranscriptRefused("unknown_format") from None
            if isinstance(doc, Mapping):
                return from_json(doc, source if source in SOURCES else "json")
            raise TranscriptRefused("unknown_format")
        return from_markdown(stripped, source if source in SOURCES else "markdown")
    raise TranscriptRefused("unknown_format")


def markings(transcript: Mapping[str, Any]) -> list[str]:
    """``meeting``, plus ``hr`` and ``pii`` when its title, notes or any line is sensitive."""
    texts = [transcript.get("title") or "", transcript.get("notes") or ""] + [
        c.get("text") or "" for c in transcript.get("cues") or ()]
    if any(SENSITIVE.search(t) for t in texts):
        return [HR, MEETING, PII]
    return [MEETING]


def _first(name: Any) -> str:
    return str(name or "").strip().split(" ")[0].lower()


def admit(transcript: Mapping[str, Any], people: Any) -> dict[str, Any]:
    """The transcript when every person in it is cleared (module doc), else
    ``TranscriptRefused`` (``people_exclusion_unavailable``, ``excluded_person``,
    ``undecided_person`` or ``unknown_speaker``). Never partial."""
    if people is None or getattr(people, "unavailable", "absent"):
        raise TranscriptRefused("people_exclusion_unavailable")
    everyone = ([transcript["organizer"]] if transcript.get("organizer") else []) + list(
        transcript.get("attendees") or ())
    if not everyone:
        raise TranscriptRefused("undecided_person")
    for person in everyone:
        address = person.get("address")
        if not address or not people.decides(address):
            raise TranscriptRefused("undecided_person")
        try:
            if people.is_excluded(address):
                raise TranscriptRefused("excluded_person")
        except TranscriptRefused:
            raise
        except Exception:  # noqa: BLE001 - a guard that cannot decide withholds
            raise TranscriptRefused("undecided_person") from None
    lookup = getattr(people, "object_id_of", None)
    if callable(lookup):
        for person in everyone:
            oid = lookup(person["address"])
            if isinstance(oid, str):
                person["oid"] = oid.lower()
    names = {str(p.get("name") or "").strip().lower() for p in everyone} - {""}
    firsts = {_first(n) for n in names}
    for cue in transcript.get("cues") or ():
        who = str(cue.get("speaker") or "").strip().lower()
        if not who or (who not in names and not (" " not in who and who in firsts)):
            raise TranscriptRefused("unknown_speaker")
    out = dict(transcript)
    if transcript.get("organizer"):
        out["organizer"] = everyone[0]
    out["attendees"] = everyone[1:] if transcript.get("organizer") else everyone
    return out


__all__ = ["HR", "MEETING", "PII", "SCHEMA", "SENSITIVE", "SOURCES", "TranscriptRefused",
           "admit", "from_json", "from_markdown", "from_teams", "markings", "parse",
           "parse_vtt"]
