"""The prepared writes of the app (``operating.prepared_writes``): the person's own write
tools for the Ask (``prepared_tools``), their prepared changes and the two-step confirm
(``/api/prepared...``), one desk over the autopilot's write state and executor.

"Could the person do this themselves" (``_pw_may``) reuses each system's own permission
source, never a new one:

* mail and calendar: their own mailbox and calendar (the executor forces both);
* BC: the write grants file (``write_intents.WriteGrants``, ``bc:<entity set>``);
* SharePoint files (upload, replace): the write or owner role the capture recorded on the
  folder or the file (``instruct_files.folder_writable``, as the upload desk reads it);
  SharePoint lists: the write grants file (``sharepoint:list:<list id>``);
* Mission Control: approving a gate needs an MC admin/owner role in the approver roster
  (``operating/approver_roster.json``, ``mc_role``) and never one the person requested;
  a task note needs the person on that roster;
* Planner, Teams and Portal: the write grants file (``<system>:<plan, chat or entity>``):
  no membership is read yet, so nobody holds one unless the owner granted it.

Anything else, or a source that cannot be read, refuses (fail closed). A full reader
(the owner, the CEO) skips this check only.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional

from operating import prepared_writes as pw

from . import models as m
from .auth import Reader
from .errors import UserFacing
from .teams_connect import TeamsConnectMixin


# the spec is the desk's (an autopilot job proposes edits with the same one)
DOC_TOOL = pw.DOC_TOOL_SPEC


class DocumentEditMixin:
    """``plx.edit_document``: the general document edit (``operating.doc_edit``) over a
    file the reader may read. The bytes are the capture's of exactly the cited version,
    read only after the reader's own host opened that citation (their source gate); the
    write is a ``file_replace`` over that version's eTag, so a file changed since is
    refused by SharePoint (412), never overwritten."""

    prepared_doc_read: Any = None   # tests: (reader, citation) -> (name, bytes, etag) | None

    def _pw_doc_bytes(self, reader: Reader, citation: str) -> Optional[tuple[str, bytes, str]]:
        if self.prepared_doc_read is not None:
            return self.prepared_doc_read(reader, citation)
        from integrations import capture_sharepoint as cs
        from integrations.live_source_guard import parse_handle
        (drive, item), version, _x = parse_handle(citation.split("#", 1)[0], "sharepoint", 2)
        folder = getattr(self, "sharepoint_capture_dir", None)
        if not folder or not cs.store_exists(Path(folder)) \
                or not self._host_opens(reader, citation):  # type: ignore[attr-defined]
            return None
        store = cs.SharePointCaptureStore.open_read_only(Path(folder))
        try:
            from urllib.parse import unquote
            row = store.current(unquote(drive), unquote(item))
            version = unquote(version)
            if row is None or row.get("etag") != version or not row.get("content_sha256") \
                    or store.original_dropped(row["content_sha256"]):
                return None
            record = json.loads(row.get("record") or "{}")
            name = str(record.get("name") or str(row.get("path") or "").rsplit("/", 1)[-1])
            return name, store.blob(row["content_sha256"]), version
        finally:
            close = getattr(store, "close", None)
            if close is not None:
                close()

    @staticmethod
    def _pw_withheld(text: str, guard: Any) -> Optional[str]:
        """Why a document's text may not reach the model (None: it may): Freightcom named
        anywhere in it, an excluded (or undecidable) person's address or id in it, or the
        exclusion list not loaded. The same detection every read uses; the reason never
        names what was found."""
        from integrations.image_ocr_gpu import touches_freightcom
        from .feedback import _ADDRESS, _UUID, _excluded_address, _excluded_id
        if touches_freightcom(text):
            return ("That document holds material the assistant may not read, so it is not "
                    "edited here: edit it in SharePoint.")
        if guard is None or getattr(guard, "unavailable", "absent"):
            return ("The list of people whose data stays out is not loaded, so no document "
                    "is edited now.")
        if any(_excluded_id(guard, oid.lower()) for oid in _UUID.findall(text)) or any(
                _excluded_address(guard, a.lower()) for a in _ADDRESS.findall(text)):
            return ("That document names a person whose data the assistant may not read, "
                    "so it is not edited here: edit it in SharePoint.")
        return None

    def _pw_doc_tool(self, reader: Reader, desk: pw.Desk, person: pw.Person) -> Any:
        import base64
        from operating import doc_edit
        from operating.agent_session import LocalTool
        from integrations.live_source_guard import parse_handle

        def call(arguments: dict[str, Any]) -> Any:
            if not isinstance(arguments, Mapping) or set(arguments) - set(
                    DOC_TOOL["inputSchema"]["properties"]):  # type: ignore[index]
                return {"refused": ["Unknown fields."]}
            citation = str(arguments.get("citation") or "")
            try:
                (drive, item), _v, _x = parse_handle(citation.split("#", 1)[0], "sharepoint", 2)
            except ValueError:
                return {"refused": ["Give the document's sharepoint:// citation as read."]}
            from urllib.parse import unquote
            drive, item = unquote(drive), unquote(item)
            read = self._pw_doc_bytes(reader, citation)
            if read is None:
                return {"refused": ["That document is not one you can read here as that "
                                    "version (or it changed since): read it again."]}
            name, data, etag = read
            change = {k: arguments[k] for k in ("find", "replace", "sheet", "cell", "value")
                      if k in arguments}
            try:
                _ext, kind = doc_edit.kind_of(name)
                # the whole text first, filtered as every read is, before any of it (the
                # diff, a refusal quoting the find) can reach the model
                text = doc_edit.text_of(name, data)
            except doc_edit.Refused as refused:
                return {"refused": [str(refused)]}
            try:   # the people exclusion the writes are cleared by
                guard = desk.executor().guard
            except Exception:  # noqa: BLE001 - undecidable: refused
                guard = None
            withheld = self._pw_withheld(" \n".join(
                [name, text, *(str(v) for v in change.values()),
                 str(arguments.get("reason") or "")]), guard)
            if withheld:
                return {"refused": [withheld]}
            try:
                new, diff = doc_edit.edit(name, data, change)
            except doc_edit.Refused as refused:
                return {"refused": [str(refused)]}
            request = {"system": "sharepoint", "operation": "file_replace",
                       # the file's name is the card's record (as read), not a target key
                       "target": {"drive_id": drive, "item_id": item},
                       "change": {"content_b64": base64.b64encode(new).decode("ascii"),
                                  "content_type": kind},
                       "precondition": {"etag": etag, "prior": {
                           "content_b64": base64.b64encode(data).decode("ascii")}},
                       "title": f"Edit {name}"[:200],
                       "reason": str(arguments.get("reason") or "")[:600]}
            try:
                card = desk.prepare(person, request, citations=[citation], diff=diff,
                                    read={"name": name})
            except pw.Refused as refused:
                return {"refused": refused.problems}
            return {"prepared_id": card["prepared_id"], "diff": card["diff"],
                    "note": "Prepared, not done: the person confirms it in the app."}

        return LocalTool(spec=DOC_TOOL, call=call)


class PreparedWritesMixin(DocumentEditMixin, TeamsConnectMixin):
    """Needs ``AutopilotAreaMixin`` (``_apa_state``, ``_apa_executor``, ``_ap_env``) and
    ``InstructMixin`` (``_captured_acls``)."""

    prepared_roster: Any = None   # tests: the approver roster's approvers mapping
    prepared_read: Any = None     # (reader, person, action) -> {name, values, requested_by}

    # ------------------------------------------------------------ plumbing

    def _pw_person(self, reader: Reader) -> pw.Person:
        session = reader.session
        employee = getattr(session, "employee", None)
        ids = {str(i).strip().lower() for i in (
            reader.email, getattr(session, "email", None),
            getattr(employee, "object_id", None), getattr(session, "subject_oid", None),
            *(getattr(session, "identities", ()) or ())) if isinstance(i, str) and i.strip()}
        # the owner-open reader (``feedback.owner_open``, the autopilot's owner test)
        full = bool(self._apa_is_owner(reader))  # type: ignore[attr-defined]
        # the email first: the item's owner is the person's mailbox
        email = (reader.email or getattr(session, "email", None) or "").strip().lower()
        ordered = tuple([email] if email else []) + tuple(sorted(ids - {email}))
        return pw.Person(ids=ordered, full=full)

    def _pw_desk(self, reader: Reader) -> pw.Desk:
        state = self._apa_state()  # type: ignore[attr-defined]
        if state is None:
            raise UserFacing("action_refused", "Prepared changes are not configured on this "
                             "server.", 409)
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        return pw.Desk(state, lambda: self._apa_executor(state),  # type: ignore[attr-defined]
                       may=self._pw_may(reader), read=self._pw_read(reader))

    def _pw_read(self, reader: Reader) -> Optional[pw.Read]:
        """The system's current record for an action (``prepared_writes.Read``): the
        record's name, the current values of the fields a write sets and an MC gate's
        requester, read with the reader's own access. ``prepared_read`` (tests and the
        per-system readers) answers it; by default ``operating.prepared_read.SystemReader``
        (BC, Planner, calendar, SharePoint list items, MC gates; a system it cannot read
        leaves edits refused, never shown against the model's word)."""
        hook = self.prepared_read
        if hook is not None:
            return lambda person, action: hook(reader, person, action)
        state = self._apa_state()  # type: ignore[attr-defined]
        held: dict[str, Any] = {}

        def read(person: pw.Person, action: Mapping[str, Any]) -> Any:
            # the per-system readers (``operating.prepared_read``): built on first use with
            # the executor's adapters, so BC is read in the environment and company the
            # write goes to
            if "reader" not in held:
                from operating.prepared_read import SystemReader
                held["reader"] = SystemReader(dict(self._ap_env()),  # type: ignore[attr-defined]
                                              self._apa_executor(state).adapters)  # type: ignore[attr-defined]
            return held["reader"](person, action)
        return read

    def _pw_roster(self) -> Mapping[str, Any]:
        if self.prepared_roster is not None:
            return self.prepared_roster
        try:
            raw = json.loads((Path(__file__).resolve().parents[1] / "operating"
                              / "approver_roster.json").read_text(encoding="utf-8"))
            approvers = raw.get("approvers") if raw.get("schema") == \
                "plx-approver-roster/v1" else None
        except (OSError, ValueError, AttributeError):
            approvers = None
        return {str(k).lower(): v for k, v in approvers.items()} \
            if isinstance(approvers, Mapping) else {}

    def _pw_may(self, reader: Reader) -> pw.Check:
        from operating import write_intents as wi
        from operating.autopilot_write_adapters import BCAdapter
        env = dict(self._ap_env())  # type: ignore[attr-defined]
        grants = wi.WriteGrants.from_env(env)

        def granted(ids: tuple[str, ...], system: str, scope: str) -> bool:
            return grants.holds(ids, system, scope) or grants.holds(ids, system, "*")

        def file_role(ids: tuple[str, ...], drive: str, node: str) -> Optional[bool]:
            """A write or owner role of the person's own on the folder (an upload) or the
            file (a replace), as the capture recorded its whole permission list."""
            from operating import instruct_files as files
            acls = getattr(self, "folder_acls", None) or self._captured_acls()  # type: ignore[attr-defined]
            if acls is None:
                return None
            try:
                return files.folder_writable(acls(drive, node), None, ids) if node else None
            except Exception:  # noqa: BLE001 - permissions that cannot be read: not known
                return None

        def may(person: pw.Person, action: Mapping[str, Any]) -> Optional[str]:
            kind = str(action.get("kind") or "")
            operation = str(action.get("operation") or "")
            target = action.get("target") if isinstance(action.get("target"), Mapping) else {}
            ids = person.ids
            if kind == "bc_write":
                line = BCAdapter.LINE_OPERATIONS.get(operation)
                entity = (line or {}).get("lines") if line else (
                    wi.OPERATIONS.get(operation) or {}).get("entity_set")
                return None if entity and granted(ids, "bc", str(entity)) else (
                    f"you hold no BC write grant for {entity or operation}")
            if kind == "sharepoint_write":
                if operation in ("file_upload", "file_replace"):
                    drive = str(target.get("drive_id") or "")
                    node = str(target.get("parent_id") if operation == "file_upload"
                               else target.get("item_id") or "")
                    held = file_role(ids, drive, node)
                    if held is True:
                        return None
                    return ("SharePoint does not record a write or owner role of yours there"
                            if held is False else "its SharePoint permissions are not known")
                lst = str(target.get("list_id") or "")
                return None if lst and granted(ids, "sharepoint", f"list:{lst}") else (
                    "you hold no write grant for that SharePoint list")
            if kind in ("mc_approval", "mc_task"):
                entry = next((v for k, v in self._pw_roster().items() if k in ids), None)
                if not isinstance(entry, Mapping):
                    return "you are not on the Mission Control approver roster"
                if kind == "mc_approval":
                    if str(entry.get("mc_role") or "").lower() not in ("admin", "owner"):
                        return "Mission Control lets only its admins and owners decide gates"
                    # the requester as Mission Control holds it (Desk.prepare reads it)
                    asked = [str(x).lower() for x in action.get("people") or () if x]
                    if not asked or asked[0] in ids:
                        return "you requested it, and nobody approves their own request"
                return None
            payload = action.get("payload") if isinstance(action.get("payload"), Mapping) else {}
            # the plan a task is posted to (a new task's planId); a target's plan_id is
            # never the write's (an update's task may sit in any plan: no grant decides it)
            scope = {"planner_write": ("planner", payload.get("planId")),
                     "send_teams": ("teams", target.get("chat_id") or target.get("team_id")),
                     "portal_write": ("portal", target.get("entity") or operation)}.get(kind)
            if scope and scope[1] and granted(ids, scope[0], str(scope[1])):
                return None
            if scope:
                return f"the Workbench cannot read your {scope[0]} membership, and you hold " \
                       "no write grant there"
            return "the Workbench cannot check your own access there"
        return may

    def prepared_tools(self, reader: Reader) -> list[Any]:
        """The Ask's write tools bound to this reader (``simple_ask.write_tools``); none
        for a session that is not the person's own (viewing as someone, the Ask canary):
        nothing is prepared in another person's name."""
        session = getattr(reader, "session", None)
        if getattr(session, "view_as", False) or getattr(session, "canary", False):
            return []
        desk = self._pw_desk(reader)
        person = self._pw_person(reader)
        tools = [self._pw_with_files(reader, pw.tool(desk, person))]
        opener = getattr(self, "_pw_doc_tool", None)
        if callable(opener):
            tools.append(opener(reader, desk, person))
        return tools

    def _pw_file(self, reader: Reader, file_id: Any) -> Optional[dict[str, Any]]:
        """One file the reader's Ask made for them (``assistant_tools``, kept by the
        instruction engine's file store): its name, type, bytes (base64) and digest; None
        for any other id (another reader's file, or none)."""
        import base64
        from operating import instruct_files as files
        uploads = getattr(self, "_uploads", None)
        if not isinstance(file_id, str) or not callable(uploads):
            return None
        desk = uploads()
        data = desk.file(reader.key, file_id)
        rows = [r for r in desk._rows() if r.get("event") == "prepared"
                and r.get("reader") == reader.key and r.get("file_id") == file_id]
        if data is None or not rows:
            return None
        # a person the file names, excluded since it was made (or no list to decide
        # with): never sent anywhere, as it is never downloaded (``instruct_file``)
        from .feedback import _excluded_address
        named = next((r["people"] for r in rows if r.get("people")), ())
        guard = self._instruct_guard() if named else None  # type: ignore[attr-defined]
        if named and (guard is None or any(_excluded_address(guard, str(a)) for a in named)):
            return None
        digest = hashlib.sha256(data).hexdigest()
        return {"file_id": file_id, "name": rows[-1]["name"],
                "content_type": files.MEDIA[rows[-1]["kind"]], "size": len(data),
                "sha256": digest, "content_b64": base64.b64encode(data).decode("ascii"),
                "people": sorted({str(a).lower() for a in named})}

    def _pw_beside(self, reader: Reader, handle: Any) -> Optional[dict[str, str]]:
        """``{drive_id, parent_id, folder}`` of the folder holding a SharePoint file the
        reader may open now (``folder``: its path in words); None when they may not, or its
        folder is not known."""
        from operating import instruct_files as files
        if not isinstance(handle, str) or not handle.startswith("sharepoint://"):
            return None
        try:
            opened = self.source(reader, handle, 0)  # type: ignore[attr-defined]
        except (KeyError, LookupError, ValueError, PermissionError):
            return None
        where = files.target_of(handle, opened.fields or {}, opened.label)
        if where is None:
            return None
        from urllib.parse import unquote
        parent = where.parent or self._pw_parent(handle)
        # a path field may name the file itself: its folder is the path above it
        folder = where.folder
        if opened.label and folder.endswith("/" + opened.label):
            folder = folder[:-len(opened.label) - 1] or "/"
        return {"drive_id": unquote(where.drive), "parent_id": parent,
                "folder": folder} if parent else None

    def _pw_parent(self, handle: str) -> Optional[str]:
        """The id of the folder holding a file, as the SharePoint capture walked it: the
        folder at the path above the file's current version (``walked_folders``; the drive's
        ``root`` for a file at its top), read-only; None when it was not walked."""
        from integrations import capture_sharepoint as cs
        from integrations.live_source_guard import parse_handle
        from urllib.parse import unquote
        folder = getattr(self, "sharepoint_capture_dir", None)
        try:
            (drive, item), _v, _x = parse_handle(handle.split("#", 1)[0], "sharepoint", 2)
        except (ValueError, TypeError):
            return None
        if not folder or not cs.store_exists(Path(folder)):
            return None
        store = cs.SharePointCaptureStore.open_read_only(Path(folder))
        try:
            row = store.current(unquote(drive), unquote(item)) or {}
            path = str(row.get("path") or "")
            if "/" not in path:
                return None
            above = path.rsplit("/", 1)[0]
            if not above:
                return "root"
            got = store.db.execute("SELECT folder_id FROM walked_folders WHERE drive_id = ? "
                                   "AND path = ? LIMIT 1", (unquote(drive), above)).fetchone()
            return str(got[0]) if got and got[0] else None
        finally:
            close = getattr(store, "close", None)
            if close is not None:
                close()

    def _pw_resolve(self, reader: Reader, arguments: Any) -> tuple[Any, Optional[str]]:
        """(the prepare request with the reader's own made files and a ``beside`` folder
        resolved, None) or (None, why it is refused): ``_pw_with_files``. Every path that
        prepares a change from a model's request (the Ask, an autopilot job) goes through
        it, so a file reaches a card only from the reader's own file store, and the people
        the file names go with it (the executor clears them again as the change runs)."""
        if not isinstance(arguments, Mapping) or not isinstance(arguments.get("change"),
                                                                Mapping):
            return arguments, None
        system = str(arguments.get("system") or "").strip().lower()
        change = dict(arguments["change"])
        if system == "mail" and "attachments" in change:
            ids = change["attachments"]
            got = [self._pw_file(reader, i) for i in ids] if isinstance(ids, list) else [None]
            if any(g is None for g in got):
                return None, ("An attachment must be a file you made here "
                              "(plx.make_document), named by its file_id.")
            change["attachments"] = got
        elif system == "sharepoint" and "file_id" in change:
            got = self._pw_file(reader, change.pop("file_id"))
            if got is None or str(arguments.get("operation")) != "file_upload":
                return None, ("A file to save to SharePoint must be one you made here "
                              "(plx.make_document), uploaded with file_upload.")
            change["content_b64"] = got["content_b64"]
            change["content_type"] = got["content_type"]
            change.setdefault("name", got["name"])
            # whom the file names: the write reaches them (cleared again as it runs)
            arguments = {**arguments, "people": [
                *[p for p in arguments.get("people") or () if isinstance(p, str)],
                *got["people"]]}
        target = arguments.get("target")
        if system == "sharepoint" and isinstance(target, Mapping) and "beside" in target:
            # the folder of a file the reader opens now (their own read), as an upload
            # draft names it (``instruct_files.target_of``)
            where = self._pw_beside(reader, target.get("beside"))
            if where is None:
                return None, ("The file to save beside is not one you can open, or its "
                              "folder is not known: name another file there.")
            folder = where.pop("folder")
            arguments = {**arguments, "target": {
                **{k: v for k, v in target.items() if k != "beside"}, **where}}
            # the card says where it goes in words, not only by the folder's id
            arguments.setdefault("title", f"Save {change.get('name') or 'the file'} "
                                          f"in {folder}"[:200])
        return {**arguments, "change": change}, None

    def _pw_with_files(self, reader: Reader, tool: Any) -> Any:
        """``plx.prepare_write`` taking the reader's own made files by id: an email's
        ``change.attachments`` (file ids) and a SharePoint ``file_upload``'s
        ``change.file_id`` become the files' bytes, read here from the reader's own files
        (any other id, or an attachment given any other way, is refused: a card's attachment
        is always a file the person can open from it); a SharePoint ``target.beside`` (a
        file there) names its folder (``_pw_beside``)."""
        from operating.agent_session import LocalTool

        def call(arguments: dict[str, Any]) -> Any:
            resolved, refused = self._pw_resolve(reader, arguments)
            return {"refused": [refused]} if refused else tool.call(resolved)

        return LocalTool(spec=tool.spec, call=call)

    @staticmethod
    def _pw_card(card: Mapping[str, Any]) -> m.PreparedWrite:
        return m.PreparedWrite(**{k: v for k, v in card.items()
                                  if k in m.PreparedWrite.model_fields})

    def _pw_do(self, step: Any) -> Any:
        try:
            return step()
        except pw.Refused as refused:
            code = {404: "not_found", 403: "forbidden", 429: "busy"}.get(
                refused.status, "action_refused")
            raise UserFacing(code, " ".join(refused.problems)[:600], refused.status) from None

    # ------------------------------------------------------------ the routes

    def prepared_list(self, reader: Reader) -> m.PreparedWrites:
        desk, person = self._pw_desk(reader), self._pw_person(reader)
        return m.PreparedWrites(items=[self._pw_card(c) for c in self._pw_do(
            lambda: desk.mine(person))])

    def prepared_preview(self, reader: Reader, pid: str) -> m.PreparedWrite:
        desk, person = self._pw_desk(reader), self._pw_person(reader)
        return self._pw_card(self._pw_do(lambda: desk.preview(person, pid)))

    def prepared_confirm(self, reader: Reader, pid: str,
                         body: m.PreparedConfirmRequest) -> m.PreparedWrite:
        desk, person = self._pw_desk(reader), self._pw_person(reader)
        card = self._pw_card(self._pw_do(lambda: desk.confirm(
            person, pid, preview_hash=body.preview_hash, confirm=body.confirm,
            now=datetime.now(timezone.utc))))
        self._apa_job_decided(reader, pid)  # type: ignore[attr-defined]
        return card

    def prepared_dismiss(self, reader: Reader, pid: str) -> m.PreparedWrite:
        desk, person = self._pw_desk(reader), self._pw_person(reader)
        card = self._pw_card(self._pw_do(lambda: desk.dismiss(person, pid)))
        self._apa_job_decided(reader, pid)  # type: ignore[attr-defined]
        return card

    def prepared_undo(self, reader: Reader, pid: str) -> m.PreparedWrite:
        desk, person = self._pw_desk(reader), self._pw_person(reader)
        card = self._pw_card(self._pw_do(lambda: desk.undo(person, pid)))
        self._apa_job_decided(reader, pid)  # type: ignore[attr-defined]
        return card

    def prepared_retry(self, reader: Reader, pid: str) -> m.PreparedWrite:
        """Prepare again a change that was dismissed, expired, failed or recorded as a
        dry run: a new item, every check run anew (``Desk.retry``)."""
        desk, person = self._pw_desk(reader), self._pw_person(reader)
        return self._pw_card(self._pw_do(lambda: desk.retry(person, pid)))

