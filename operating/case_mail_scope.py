"""The one mail allowlist: which mailboxes are captured and whose mail may enter a
case (ING-13, WS-08).

``operating/case_mail_scope.json`` is canonical for both. The mail capture
(``integrations.capture_mail.CaptureScope``) refuses a scope naming any other
mailbox before anything is requested; its scope file only says which folders,
how far back and how.

A ``MailScope`` is a positive list of mailbox addresses
(``plx-case-mail-scope/v1``; built in: ``operating/case_mail_scope.json``, the
reported Graph mail allowlist at petrasoap.com). A mailbox is in scope when
any of its directory identities (object id, UPN, mail, SMTP proxy addresses,
as ``GraphMailReader`` resolves them) equals a listed address whose status is
``reported_allowlisted``; an Exchange address names exactly one mailbox, so
one equal address identifies it. Anything else, including a listed address
still ``pending_confirmation``, is out of scope (fail closed).

The scope never grants a read: the live-source audience, the mailbox owner
ACL and the employee gate still decide what a reader sees. It only removes:
``CaseRuntime`` applies the built-in scope by default and refuses every mail
read outside it before any message is requested, so mail of an unlisted
mailbox never enters a case. ``MailScope.unrestricted()`` is the one,
deliberately named opt-out (isolated tests over synthetic mailboxes); it
allows every mailbox the audience grants and ``operating.case_mail`` refuses it.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from integrations.people_exclusion import is_object_id

SCHEMA = "plx-case-mail-scope/v1"
BUILTIN_FILE = Path(__file__).with_name("case_mail_scope.json")
IN_SCOPE_STATUS = "reported_allowlisted"
STATUSES = (IN_SCOPE_STATUS, "pending_confirmation", "removed")
OUTSIDE_SCOPE = "mail_outside_case_scope"
_ADDRESS = re.compile(r"^[a-z0-9._%+'-]{1,64}@[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+$")
_DOMAIN = re.compile(r"^[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+$")


class MailScopeError(ValueError):
    """An invalid scope document (code only)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MailScope:
    """The in-scope mailbox addresses plus the internal domains of the business."""

    def __init__(self, addresses: Iterable[str], *, internal_domains: Iterable[str] = (),
                 excluded: Iterable[str] = (), source: str | None = None,
                 all_directory: bool = False) -> None:
        allowed = set()
        for address in addresses:
            if not isinstance(address, str) or _ADDRESS.fullmatch(address.strip().lower()) is None:
                raise MailScopeError("mail_scope_address_invalid")
            allowed.add(address.strip().lower())
        if not allowed:
            raise MailScopeError("mail_scope_empty")
        domains = set()
        for domain in internal_domains:
            if not isinstance(domain, str) or _DOMAIN.fullmatch(domain.strip().lower()) is None:
                raise MailScopeError("mail_scope_domain_invalid")
            domains.add(domain.strip().lower())
        self.is_unrestricted = False
        # Every directory user but the excluded people.
        self.all_directory = bool(all_directory)
        self.people: Any = None
        self.addresses = frozenset(allowed)
        self.excluded = frozenset(a.strip().lower() for a in excluded if isinstance(a, str))
        self.internal_domains = frozenset(domains)
        self.source = source

    def __repr__(self) -> str:
        if self.is_unrestricted:
            return "MailScope(unrestricted)"
        return f"MailScope({len(self.addresses)} mailboxes)"

    @classmethod
    def unrestricted(cls) -> "MailScope":
        """No mailbox restriction (the audience alone decides). Only for isolated
        tests and fixtures over synthetic mailboxes; never a host default."""
        scope = cls.__new__(cls)
        scope.is_unrestricted = True
        scope.all_directory = False
        scope.people = None
        scope.addresses = frozenset()
        scope.excluded = frozenset()
        scope.internal_domains = frozenset()
        scope.source = "unrestricted (explicit opt-out)"
        return scope

    @classmethod
    def from_json(cls, document: Any) -> "MailScope":
        if (not isinstance(document, Mapping) or document.get("schema") != SCHEMA
                or set(document) - {"schema", "note", "source", "mailboxes",
                                    "internal_domains", "all_directory"}):
            raise MailScopeError("mail_scope_document_invalid")
        entries = document.get("mailboxes")
        if not isinstance(entries, list) or not entries:
            raise MailScopeError("mail_scope_mailboxes_invalid")
        allowed, excluded = [], []
        for entry in entries:
            if (not isinstance(entry, Mapping) or set(entry) - {"address", "status", "kind", "note"}
                    or entry.get("status") not in STATUSES
                    or not isinstance(entry.get("address"), str)):
                raise MailScopeError("mail_scope_entry_invalid")
            (allowed if entry["status"] == IN_SCOPE_STATUS else excluded).append(entry["address"])
        domains = document.get("internal_domains", [])
        if not isinstance(domains, list):
            raise MailScopeError("mail_scope_domain_invalid")
        every = document.get("all_directory", {"enabled": False})
        if (not isinstance(every, Mapping) or set(every) - {"enabled", "note"}
                or not isinstance(every.get("enabled"), bool)):
            raise MailScopeError("mail_scope_all_directory_invalid")
        return cls(allowed, internal_domains=domains, excluded=excluded,
                   all_directory=every["enabled"], source=document.get("source") if isinstance(document.get("source"), str)
                   else None)

    @classmethod
    def from_file(cls, path: str | Path) -> "MailScope":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise MailScopeError("mail_scope_file_unreadable") from None
        return cls.from_json(document)

    @classmethod
    def builtin(cls) -> "MailScope":
        return cls.from_file(BUILTIN_FILE)

    def attach_people(self, people: Any) -> None:
        """The people-exclusion guard (``integrations.people_exclusion``) that decides
        who an ``all_directory`` scope leaves out. Without a usable one nobody is in
        scope through ``all_directory`` (fail closed)."""
        self.people = people

    def _directory_allows(self, found: set[str]) -> bool:
        """``all_directory``: a directory user (an object id among the identities) the
        people guard does not exclude. Addresses are checked once the guard is bound;
        the object id alone decides before that."""
        people = self.people
        if people is None or getattr(people, "unavailable", "no guard"):
            return False
        ids = {i for i in found if is_object_id(i)}
        if not ids:
            return False
        try:
            return not people.excluded(sorted(found) if people.bound else sorted(ids))
        except Exception:  # noqa: BLE001 - a guard that cannot decide excludes
            return False

    def allows(self, identities: Any) -> bool:
        """Whether a mailbox with these directory identities is in scope."""
        if self.is_unrestricted:
            return True
        if isinstance(identities, str) or not isinstance(identities, Iterable):
            return False
        found = {str(item).strip().lower() for item in identities if isinstance(item, str)}
        # The named mailboxes stay in scope; all_directory adds every other directory user
        # the people guard leaves in.
        return bool(found & self.addresses) or (
            self.all_directory and self._directory_allows(found))

    def allows_record(self, record: Any) -> bool:
        """A ``GraphMailReader`` record: its mailbox owner identities (the full
        directory set the reader resolved) or, without them, its UPN."""
        if not isinstance(record, Mapping):
            return False
        acl = record.get("source_acl")
        identities = list(acl.get("identities") or []) if isinstance(acl, Mapping) else []
        identities.append(record.get("mailbox_upn"))
        return self.allows([item for item in identities if isinstance(item, str)])

    def describe(self) -> dict[str, Any]:
        """Counts and domains only (the addresses are configuration, not secrets,
        but they are not needed to read a report)."""
        return {"schema": SCHEMA, "unrestricted": self.is_unrestricted,
                "all_directory": self.all_directory,
                "mailboxes_in_scope": len(self.addresses),
                "mailboxes_excluded": len(self.excluded),
                "internal_domains": sorted(self.internal_domains)}


__all__ = ["BUILTIN_FILE", "IN_SCOPE_STATUS", "MailScope", "MailScopeError", "OUTSIDE_SCOPE",
           "SCHEMA"]
