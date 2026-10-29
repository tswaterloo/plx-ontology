"""Refusals a person may read as they are: a stable code and a plain sentence."""

from __future__ import annotations


class UserFacing(Exception):
    def __init__(self, code: str, message: str, status: int = 503) -> None:
        super().__init__(code)
        self.code, self.message, self.status = code, message, status


ASK_OFF = UserFacing("ask_not_served", "Ask isn't switched on on this server.", 503)
ASK_BUSY = UserFacing("busy", "The Workbench is answering several questions right now. "
                              "Try again in a minute.", 503)
ASK_TIMEOUT = UserFacing("timeout", "That question took too long to answer. Try a narrower "
                                    "question, or name the record.", 504)
BUSY = UserFacing("busy", "The Workbench is busy right now. Try again in a moment.", 503)
