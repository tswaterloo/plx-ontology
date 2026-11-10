"""Assist mode's route (``POST /api/autopilot/assist``) over ``operating.assist``: a line
the reader typed drafted into a message as this reader. Recipients come from the tenant
directory (cleared by the people exclusion), the reader's own sent mail and the supplier or
customer records the reader can read; facts from the reader's own Ask (``ask``: the fast
path, else the agent, through the answer gate). Nothing is sent or written."""

from __future__ import annotations

from typing import Any, Optional

from operating import assist

from . import models as m
from .auth import Reader
from .errors import UserFacing

REFUSED = {
    "empty": "Type what to send and to whom.",
    "too_long": "Keep it to one line.",
    "no_words": "Type what to send and to whom.",
    "people_exclusion_unavailable": "Drafting is paused: the people list it checks "
                                    "recipients against is not loaded.",
    "not_an_employee": "Assist drafts for a signed-in employee.",
}


class AssistMixin:
    """Needs ``ask`` and ``state_dir``; tests may set ``assist_people`` (the people
    exclusion with its directory), ``assist_sent`` (``oid -> sent rows``) and
    ``assist_party`` (``(name, side) -> addresses``)."""

    assist_people: Any = None
    assist_sent: Any = None
    assist_party: Any = None

    def _assist_people(self) -> Any:
        if self.assist_people is not None:
            return self.assist_people
        state = getattr(self, "state_dir", None)
        if state is None:
            return None
        from integrations.people_exclusion import environ_with_exclusion
        from operating.chunking.permissions import load_exclusion
        try:
            return load_exclusion(state, environ_with_exclusion())
        except Exception:  # noqa: BLE001 - no directory: refused, never guessed
            return None

    def _assist_party(self, reader: Reader) -> Any:
        if self.assist_party is not None:
            return self.assist_party

        def party(name: str, side: Optional[str]) -> set[str]:
            from operating import fast_answer

            def job(view: Any) -> set[str]:
                access = view.access
                if not all(hasattr(access, n) for n in ("host", "runtime", "agent_server")):
                    return set()
                reads = fast_answer.Reads(access.host, access.runtime, access.actor,
                                          access.agent_server())
                return assist.party_addresses(reads, name, side)
            try:
                return self.reads.run(reader, job, timeout=30)  # type: ignore[attr-defined]
            except (TimeoutError, PermissionError):
                return set()
        return party

    def _assist_answers(self, reader: Reader, questions: list[str]) -> list[dict[str, Any]]:
        out = []
        for question in questions:
            found: Optional[m.AnswerPayload] = None
            try:
                for event, payload in self.ask(  # type: ignore[attr-defined]
                        reader, m.AskRequest(question=question)):
                    if event == "answer":
                        found = payload
            except (UserFacing, TimeoutError):
                found = None
            out.append({"question": question,
                        "outcome": found.outcome if found else "not_answered",
                        "facts": [f.model_dump(mode="json") for f in found.facts]
                        if found else []})
        return out

    def autopilot_assist(self, reader: Reader, body: m.AssistRequest) -> m.AssistDraft:
        try:
            intent = assist.parse(body.intent)
            oid = getattr(reader.session, "subject_oid", None)
            if not oid:
                raise assist.AssistRefused("not_an_employee")
            people = self._assist_people()
            if people is None:
                raise assist.AssistRefused("people_exclusion_unavailable")
            oid = str(oid).lower()
            own = {a for a, o in (getattr(people, "directory", None) or {}).items()
                   if str(o).lower() == oid}
            sent = self.assist_sent(oid) if self.assist_sent is not None else \
                assist.capture_history(getattr(self, "state_dir", None) or "", oid, own)
            roles = self._apa_roles() if hasattr(self, "_apa_roles") else {}
            recipients = assist.resolve(intent, me=oid, people=people, sent=sent,
                                        roles=roles, party=self._assist_party(reader))
        except assist.AssistRefused as refused:
            status = 403 if refused.code == "not_an_employee" else \
                503 if refused.code == "people_exclusion_unavailable" else 422
            raise UserFacing("assist_refused", REFUSED[refused.code], status)
        answers = self._assist_answers(reader, assist.questions(intent))
        made = assist.draft(intent, recipients, answers, outside=recipients.outside)
        names = made["names"]
        return m.AssistDraft(
            intent=intent.text, channel=made["channel"],
            to=[m.AssistRecipient(address=a, name=names.get(a)) for a in made["to"]],
            choices=[m.AssistChoice(mention=k, options=[m.AssistRecipient(address=a,
                                                                          name=names.get(a))
                                                        for a in v])
                     for k, v in made["choices"].items()],
            withheld=made["withheld"], subject=made["subject"], body=made["body"],
            facts=[m.Fact(**f) for f in made["facts"]], gaps=made["gaps"],
            questions=[q for q in made["questions"] if q])
