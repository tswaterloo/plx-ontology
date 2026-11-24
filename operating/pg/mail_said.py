"""What a mail itself says (``biz.emails.said``), as one SQL expression over the message's
body preview: written by the emails build and every refresh (``business_comms.EMAIL``).
Its own module so the comms module stays within its size; a change here rebuilds the
emails table (``business_views_code.EXTERNAL``)."""
from __future__ import annotations

# What a message newly says: the mail service's own preview of its body (Graph
# ``bodyPreview``, about the first 255 characters of the newest text) with the parts that
# are not the sender's words taken out, so a list of a thread's messages shows each reply
# ("Yes all received!") without opening one (11-22: Ask read a reply's quoted history and
# never its answer). Taken out, in order: the mail service's first-contact safety tip; the
# quoted history from its first marker to the end (the markers ``ontology.mentions``'
# ``quoted_ranges`` reads: a separator line, a forward or "On ... wrote:" line, an Outlook
# From/Sent header block, a '>' line; a From line the preview's cut ends in); links; runs
# of blank and invisible characters. Nothing left (a bare forward): NULL. POSIX regular
# expressions (repetition counts at most 255), case-insensitive, newline-sensitive for the
# line anchors; no lazy quantifier (one would make the whole match lazy).
_SAID_TIP = (r"(Some people who received this message don.t|You don.t) often get email "
             r"from [^\n]*Learn why this is important")
_SAID_FROM = r"(From|Von|De|Da|Van|Fra|Od)"
_SAID_SENT = (r"(Sent|Date|Gesendet|Datum|Envoy[ée]|Inviato|Enviado|Fecha|Data|Verzonden|"
              r"Skickat|Sendt)")
_SAID_HISTORY = (
    r"(^[ \t]*(-{2,}[ \t]*[^\s-][^\n]{0,60}[ \t]*-{2,}|_{8,}|(Begin forwarded message|"
    r"Forwarded message)[ \t]*:|On [^\n]{1,250}wrote[ \t]*:)"
    rf"|^[ \t]*{_SAID_FROM}[ \t]*:[^\n]*\n([^\n]*\n){{0,3}}[ \t]*{_SAID_SENT}[ \t]*:"
    rf"|{_SAID_FROM}[ \t]*:[^\n]{{1,250}}{_SAID_SENT}[ \t]*:"
    rf"|^[ \t]*{_SAID_FROM}[ \t]*:[^\n]*(\n[^\n]*){{0,3}}\Z"
    r"|^[ \t]*>)[\s\S]*\Z")
_SAID_LINK = r"<?(https?|mailto):[^\s<>]*>?"
_SAID_BLANK = r"[\s ­͏ ​‌‍⁠﻿]+"


def said_sql(e: str) -> str:
    """SQL: the message's own new words from its body preview ``e`` (``_SAID_*``)."""
    def lit(text: str) -> str:
        return "'" + text.replace("'", "''") + "'"
    return (f"nullif(btrim(regexp_replace(regexp_replace(regexp_replace(regexp_replace({e}, "
            f"{lit(_SAID_TIP)}, '', 'i'), {lit(_SAID_HISTORY)}, '', 'in'), {lit(_SAID_LINK)}, "
            f"'', 'gi'), {lit(_SAID_BLANK)}, ' ', 'g')), '')")


# the emails column (``business_comms.EMAIL_COLS``)
SAID_COL = ("said", "text", "what this message itself says: the first ~255 characters of its "
            "own new text (the quoted history of a reply or forward, the mail service's "
            "safety notice and links taken out; untrusted text). Read it to see a reply's "
            "answer (a thread's rows by thread, newest first); null: the message adds no text "
            "of its own (a bare forward). Open the handle for the whole body")
