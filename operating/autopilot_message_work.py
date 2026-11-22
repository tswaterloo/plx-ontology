"""The work a message leaves its reader beyond a reply, as the reader's item.

Fresh days 11-19..20 (five roles, 952 sends labelled by a small model, never tuned): about a
quarter of the time people spent on sends no item anticipated went on acting on something a
message told them (a colleague's "BOL attached for today's pickup", a carrier's discrepancy
to pass on, documents to file, a customer's go-ahead), not on answering it. Every message to
a person is already read by the triage model (``chat_ask_model``), which names that work in
a few words ("work": "process BOL for today's pickup") and nothing read it.

Here that reading is the item, for one kind of reading only: a person wrote the message to
the reader, expects no reply, and the model named work for them (the registry's
``triage_work_in`` ``fyi``; an automated mail, a system notice or a message read as owed a
reply never makes one here). No rule names a kind of work: the words are the model's. The
item carries the message, the records it names with their current state (an outside
sender's own records only, ``autopilot_handlers.senders_own``: a number shared by another
customer's order is never named), and the link to each. It is a ``standing`` job
(``autopilot_jobs``): when the job model is on for the person, the session proposes the
system change or the message to whoever must act (each through the write path's checks and
the person's confirm) and its proposals are the item; otherwise, or when it proposes
nothing, this to-do stands. Nothing is written or sent here.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import autopilot_engine
from . import autopilot_handlers
from . import autopilot_records

WORK_CHARS = 120     # the model's words, as ``chat_ask_model.WORK_CHARS``
RECORDS = 6


def h_message_work(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    """The to-do the triage reading named for the message's reader (module doc)."""
    from .autopilot_work import _current, _status_lines, body_text
    work = " ".join(str(event.get("triage_work") or "").split())[:WORK_CHARS]
    if not work:
        return {"skip": "no_work_named"}
    subject = str(event.get("subject") or "")
    own = body_text(ctx, event).split("\nFrom:")[0][:4000]
    named = autopilot_handlers._records_named(ctx, {"subject": subject, "text": own,
                                                    "records": event.get("records") or []})
    pos = [o for o in [*(event.get("records") or ()),
                       *autopilot_handlers.identified(ctx, subject, own)]
           if o.startswith("PurchaseOrder:")]
    records = list(dict.fromkeys([*named, *pos]))[:RECORDS]
    orders = _current(ctx, [o for o in records if o.startswith("SalesOrder:")])
    # a sender outside the company: only their own customer's orders are stated (an order
    # number is no key across customers); unresolved: the records stay, no state is said
    mine = autopilot_handlers.senders_own(ctx, {**event, "text": own}, orders) if orders \
        else []
    lines, _facts = _status_lines(ctx, mine) if mine else ([], {})
    who = autopilot_engine.sender_label(event.get("sender"), event.get("sender_name"))
    # a to-do (``todos``): the app shows it as a to-do card (done or not needed; nothing
    # written); not ``kind`` job, so the job session's proposals replace it when it is on
    # and it stands when the session proposes nothing (``autopilot.STANDING_WHEN``)
    return {"kind": "todo", "summary": " ".join([f"{work[:1].upper()}{work[1:]} (from {who}).",
                                                *lines])[:300],
            "todos": [{"system": "", "record": "", "what": work}],
            "records": [{"object_id": o} for o in records],
            "links": [{"label": "Open the message",
                       "href": autopilot_records.object_href(event["object_id"])}]
            + [{"label": "Open the record", "href": autopilot_records.object_href(o)}
               for o in records[:3]],
            "gaps": [] if records else ["no_record_named"]}
