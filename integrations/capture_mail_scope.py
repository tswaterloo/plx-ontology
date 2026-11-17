"""Mail capture configuration: the capture scope file, the state directory guards, the
capture error and time stamps (split from ``integrations.capture_mail``, which re-exports
every name)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from operating.case_mail_scope import MailScope
from . import live_source_grammar as grammar


SCOPE_SCHEMA = "plx-mail-capture-scope/v1"
ALL_FOLDERS = "all"   # a scope's folders: every folder of the mailbox
HISTORIES = ("window", "all")
# The mail every first-tier mailbox gets before any older mail (``priority`` recent_days),
# and how recently a mailbox received mail to count as active (``active_days``).
RECENT_DAYS = 730
ACTIVE_DAYS = 30
OCR_MODES = ("inline", "deferred")   # a scope's ``ocr`` (CaptureScope)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCOPE = REPO_ROOT / "integrations" / "mail_capture_scope.json"


_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_UPN = re.compile(r"^[a-z0-9._%+'-]{1,64}@[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+$")
_DOMAIN = re.compile(r"^[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+$")


class CaptureError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    """Fixed-width UTC text, so recorded/superseded times compare as strings."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _normal_time(value: Any) -> str:
    if not isinstance(value, str) or _ISO_Z.fullmatch(value) is None:
        raise CaptureError("as_known_at_invalid")
    head, _, frac = value[:-1].partition(".")
    return f"{head}.{(frac or '').ljust(6, '0')}Z"


WORKBENCH_STATE_ENV = "PLX_WORKBENCH_STATE_DIR"   # the persistent host's state directory
CHECK_STATE = "mail-capture-check"   # its leaf below the host state base (the live check's)


def check_state_dir(env: Mapping[str, str], check: str, *, injected: bool
                    ) -> tuple[Path, Path, str]:
    """(the host state directory of a live capture check kept across its runs, the base
    it lies under, which base): ``$PLX_WORKBENCH_STATE_DIR/<check>`` (``
    workbench_state_dir``, the credentialed host's persistent store, kept between daily
    runs), else ``$XDG_STATE_HOME/plx-workbench/<check>`` (``xdg_state_home``), else the
    same under ``$HOME/.local/state`` (``home``); then ``live``, or
    ``injected-transport`` so a fixture run never shares the live store. A set variable
    that is not an absolute path, or none set: refused (``ValueError``), never a store
    in a place nobody chose. Never in Git: the caller checks it
    (:func:`assert_outside_git`)."""
    leaf = "injected-transport" if injected else "live"
    workbench = env.get(WORKBENCH_STATE_ENV)
    if workbench:
        if not Path(workbench).is_absolute():
            raise ValueError(f"state_dir_invalid: {WORKBENCH_STATE_ENV} is not absolute")
        return Path(workbench) / check / leaf, Path(workbench), "workbench_state_dir"
    base, kind = env.get("XDG_STATE_HOME"), "xdg_state_home"
    if not base and env.get("HOME"):
        base, kind = str(Path(env["HOME"]) / ".local" / "state"), "home"
    if not base or not Path(base).is_absolute():
        raise ValueError(f"state_dir_unset: give --state-dir or set {WORKBENCH_STATE_ENV}, "
                         "XDG_STATE_HOME or HOME")
    return Path(base) / "plx-workbench" / check / leaf, Path(base), kind


def assert_outside_git(path: str | Path) -> Path:
    """The resolved state directory; ``CaptureError`` when it, or any parent,
    is a git work tree (or lies inside this repository). Checked before any
    directory is created, so captured mail can never land in a checkout."""
    if not isinstance(path, (str, Path)) or not str(path):
        raise CaptureError("state_dir_required")
    resolved = Path(path).expanduser().resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise CaptureError("state_dir_inside_git_tree")
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            raise CaptureError("state_dir_inside_git_tree")
    return resolved


# ------------------------------------------------------------------ scope

class CaptureScope:
    """Which mailboxes and folders are captured, and how far back.

    ``plx-mail-capture-scope/v1``: ``mailboxes`` (``{address, folders}``),
    ``allowlisted_mailboxes`` (``{"folders": [...]}``: every mailbox of the
    allowlist, explicit ``mailboxes`` entries keeping their own folders),
    ``window_days`` (first-round window), ``internal_domains`` (sender class
    of the untrusted-text envelope; default: the allowlist's) and
    ``all_mailboxes`` (``{"enabled": bool, "folders": [...], "exclude":
    [{"address", "reason"}]}``: every tenant directory user with a mail address
    except the excluded ones, listed through the guarded ``/users`` GET at run
    time; the allowlist does not narrow it). ``allowlist`` (default: the built-in
    ``MailScope``; ``MailScope.unrestricted()`` only for isolated tests over
    synthetic mailboxes): a named mailbox outside it is set aside
    (``outside_allowlist``) and never captured. Configuration, never a grant:
    the capture actor still needs the live-source audience.

    ``folders`` may be ``"all"`` (:data:`ALL_FOLDERS`): every folder of the mailbox, listed
    when the run opens it (``GraphMailReader.list_folders``). ``history`` ``"all"``: after a
    folder's windowed delta round, the mail older than its window is read too, newest
    first, resuming by its own cursor (``"window"``, the default: the window only)."""

    def __init__(self, mailboxes: Sequence[Mapping[str, Any]], *, window_days: int = 90,
                 internal_domains: Iterable[str] | None = None, all_mailboxes: bool = False,
                 all_mailbox_folders: Sequence[str] | str = ("inbox",),
                 allowlisted_folders: Sequence[str] | None = None,
                 allowlist: MailScope | None = None, exclude: Iterable[str] = (),
                 history: str = "window", ocr: str = "inline",
                 priority: Mapping[str, Any] | None = None) -> None:
        if history not in HISTORIES:
            raise CaptureError("scope_history_invalid")
        if ocr not in OCR_MODES:
            raise CaptureError("scope_ocr_invalid")
        self.history = history
        # ``deferred``: a scan's OCR is put off and its bytes dropped (``ocr_deferred``,
        # read later by ``ocr-deferred``); ``inline``: kept until OCR has read it.
        self.ocr = ocr
        self.allowlist = MailScope.builtin() if allowlist is None else allowlist
        if not isinstance(self.allowlist, MailScope):
            raise CaptureError("scope_allowlist_invalid")
        if isinstance(window_days, bool) or not isinstance(window_days, int) \
                or not 1 <= window_days <= 3650:
            raise CaptureError("scope_window_invalid")
        self.window_days = window_days
        entries: list[tuple[str, tuple[str, ...]]] = []
        outside: list[str] = []
        seen: set[str] = set()
        for entry in mailboxes:
            if not isinstance(entry, Mapping) or set(entry) - {"address", "folders", "note"}:
                raise CaptureError("scope_mailbox_invalid")
            address = entry.get("address")
            if not isinstance(address, str) or not (
                    _UPN.fullmatch(address) or grammar.TYPES["user_id"].valid(address)):
                raise CaptureError("scope_mailbox_invalid")
            if address in seen:
                raise CaptureError("scope_mailbox_duplicate")
            seen.add(address)
            folders = self._folders(entry.get("folders", ["inbox"]))
            if self.allowlist.allows([address]):
                entries.append((address, folders))
            else:
                outside.append(address)
        if allowlisted_folders is not None:
            if self.allowlist.is_unrestricted:
                raise CaptureError("scope_allowlist_unrestricted")
            folders = self._folders(allowlisted_folders)
            entries += [(address, folders) for address in sorted(self.allowlist.addresses)
                        if address not in seen]
        self.mailboxes = tuple(entries)
        self.outside_allowlist = tuple(outside)
        domains = list(self.allowlist.internal_domains if internal_domains is None
                       else internal_domains)
        if any(not isinstance(d, str) or _DOMAIN.fullmatch(d) is None for d in domains):
            raise CaptureError("scope_domain_invalid")
        self.internal_domains = frozenset(domains)
        if not isinstance(all_mailboxes, bool):
            raise CaptureError("scope_all_mailboxes_invalid")
        self.all_mailboxes = all_mailboxes
        self.all_mailbox_folders = self._folders(all_mailbox_folders)
        excluded = set()
        for address in exclude:
            if not isinstance(address, str) or not (
                    _UPN.fullmatch(address.lower()) or grammar.TYPES["user_id"].valid(address)):
                raise CaptureError("scope_exclude_invalid")
            excluded.add(address.lower())
        self.excluded = frozenset(excluded)
        # Without a ``priority`` section a run takes its folders in schedule order only.
        self.prioritized = priority is not None
        self._priority(priority or {})
        if not self.mailboxes and not self.all_mailboxes and not self.outside_allowlist:
            raise CaptureError("scope_empty")

    def _priority(self, priority: Any) -> None:
        """``priority`` (:class:`MailPriority`): ``mailboxes`` (addresses or object ids read
        first, in this order), ``recent_days`` (the history every first-tier mailbox gets
        before any older mail, default :data:`RECENT_DAYS`) and ``active_days`` (a mailbox
        with mail received this recently, or none captured yet, is active)."""
        if not isinstance(priority, Mapping) or set(priority) - {
                "mailboxes", "recent_days", "active_days", "note"}:
            raise CaptureError("scope_priority_invalid")
        first = priority.get("mailboxes", [])
        if isinstance(first, (str, bytes)) or not isinstance(first, Sequence) or any(
                not isinstance(a, str) or not (_UPN.fullmatch(a.lower())
                                               or grammar.TYPES["user_id"].valid(a.lower()))
                for a in first):
            raise CaptureError("scope_priority_invalid")
        days = {}
        for key, default in (("recent_days", RECENT_DAYS), ("active_days", ACTIVE_DAYS)):
            value = priority.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 36500:
                raise CaptureError("scope_priority_invalid")
            days[key] = value
        self.priority_mailboxes = tuple(dict.fromkeys(a.lower() for a in first))
        self.recent_days, self.active_days = days["recent_days"], days["active_days"]

    def excludes(self, identities: Iterable[str]) -> bool:
        """Whether any of a mailbox's identities is on the exclusion list."""
        return any(str(i).lower() in self.excluded for i in identities)

    def directory_allows(self, identities: Sequence[str]) -> bool:
        """The ``directory_mailboxes`` filter: every listed mailbox not excluded."""
        return not self.excludes(identities)

    @staticmethod
    def _folders(folders: Any) -> tuple[str, ...]:
        if folders == ALL_FOLDERS:
            return (ALL_FOLDERS,)
        if isinstance(folders, (str, bytes)) or not isinstance(folders, Sequence) \
                or not folders or len(folders) > 50:
            raise CaptureError("scope_folders_invalid")
        values = []
        for folder in folders:
            if not grammar.TYPES["mail_folder"].valid(folder) or folder in values:
                raise CaptureError("scope_folders_invalid")
            values.append(folder.lower() if folder.lower()
                          in grammar.WELL_KNOWN_MAIL_FOLDERS else folder)
        return tuple(values)

    @classmethod
    def from_json(cls, document: Any, *, allowlist: MailScope | None = None
                  ) -> "CaptureScope":
        allowed = {"schema", "note", "window_days", "internal_domains", "mailboxes",
                   "all_mailboxes", "allowlisted_mailboxes", "history", "ocr", "priority"}
        if (not isinstance(document, dict) or document.get("schema") != SCOPE_SCHEMA
                or set(document) - allowed or not isinstance(document.get("mailboxes", []),
                                                             list)):
            raise CaptureError("scope_document_invalid")
        every = document.get("all_mailboxes", {"enabled": False})
        if not isinstance(every, dict) or set(every) - {"enabled", "folders", "exclude",
                                                         "note"}:
            raise CaptureError("scope_all_mailboxes_invalid")
        exclude = every.get("exclude", [])
        if not isinstance(exclude, list) or any(
                not isinstance(e, dict) or set(e) != {"address", "reason"}
                or not isinstance(e["reason"], str) or not e["reason"].strip()
                for e in exclude):
            raise CaptureError("scope_exclude_invalid")
        listed = document.get("allowlisted_mailboxes")
        if listed is not None and (not isinstance(listed, dict)
                                   or set(listed) - {"folders", "note"}):
            raise CaptureError("scope_allowlisted_mailboxes_invalid")
        return cls(document.get("mailboxes", []), window_days=document.get("window_days", 90),
                   internal_domains=document.get("internal_domains"),
                   all_mailboxes=every.get("enabled", False),
                   all_mailbox_folders=every.get("folders", ["inbox"]),
                   allowlisted_folders=None if listed is None
                   else listed.get("folders", ["inbox", "sentitems"]), allowlist=allowlist,
                   exclude=[e["address"] for e in exclude],
                   history=document.get("history", "window"),
                   ocr=document.get("ocr", "inline"), priority=document.get("priority"))

    @classmethod
    def from_file(cls, path: str | Path, *, allowlist: MailScope | None = None
                  ) -> "CaptureScope":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CaptureError("scope_unreadable") from None
        return cls.from_json(document, allowlist=allowlist)
