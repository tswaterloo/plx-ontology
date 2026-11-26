"""SharePoint folder discovery and artifact upload routes for Artifacts Studio.

* ``GET /api/sharepoint/folders``: returns known SharePoint document library folders
  from Postgres (``biz.sharepoint_files``) and the SharePoint capture store, merged with
  curated library defaults so users can pick a real folder or enter a new nested path.
* ``POST /api/artifacts/sharepoint-upload``: generates or locates real Office/CSV/PDF file
  bytes for an artifact, uploads directly (creating missing folders when requested) when
  live ``sharepoint_write`` is open and Graph credentials are ready, or queues a durable
  Assist approval draft with an immediate local download URL under the pilot write gate.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Literal, Optional

from fastapi import Depends, Query
from pydantic import BaseModel, Field

from operating import autopilot_write_adapters as wa
from operating import instruct_files as files

from . import models as m
from .auth import Reader
from .errors import UserFacing

_ERR = {
    401: {"model": m.ErrorResponse},
    404: {"model": m.ErrorResponse},
    422: {"model": m.ErrorResponse},
    502: {"model": m.ErrorResponse},
}

DEFAULT_FOLDERS: tuple[str, ...] = (
    "Shared Documents/Operations",
    "Shared Documents/Finance",
    "Shared Documents/Purchasing",
    "Shared Documents/Quality",
    "Shared Documents/Sales",
)

_FALLBACK_UPLOADS = files.UploadDesk(None)


class SharePointFolder(BaseModel):
    path: str
    label: str
    drive_id: str = ""
    parent_id: str = ""
    site: str = ""
    file_count: int = 0
    source: str = "default"


class SharePointFoldersResponse(BaseModel):
    folders: list[SharePointFolder] = Field(default_factory=list)


class SharePointUploadRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=240)
    format: str = Field(default="docx", max_length=32)
    folder_path: str = Field(..., min_length=1, max_length=400)
    create_folder_if_missing: bool = True
    content_markdown: str = Field(default="", max_length=60_000)
    artifact_id: Optional[str] = Field(default=None, max_length=160)
    source_url: Optional[str] = Field(default=None, max_length=500)


class SharePointUploadResponse(BaseModel):
    status: Literal["uploaded", "approval_queued"]
    folder_path: str
    filename: str
    file_id: str = ""
    size_bytes: int = 0
    download_url: str = ""
    web_url: Optional[str] = None
    draft_id: Optional[str] = None
    upload_id: Optional[str] = None
    assist_url: Optional[str] = None
    create_folder_if_missing: bool = True
    folders_created: list[str] = Field(default_factory=list)
    missing_for_live: list[str] = Field(default_factory=list)
    message: str = ""


def _fallback_folders(limit: int) -> list[dict[str, Any]]:
    return [
        {
            "path": path,
            "label": path.rsplit("/", 1)[-1],
            "drive_id": "",
            "parent_id": "",
            "site": "Company Library",
            "file_count": 0,
            "source": "default",
        }
        for path in DEFAULT_FOLDERS[:limit]
    ]


def _fallback_upload(reader: Reader, body: SharePointUploadRequest) -> dict[str, Any]:
    """Safe fallback when running against a minimal mock backend without InstructMixin."""
    try:
        segments = wa.SharePointAdapter.normalize_folder_path(body.folder_path)
    except wa.AdapterRefused as err:
        raise UserFacing("invalid_request", err.problems[0], 422) from None
    normalized_folder = "/".join(segments)

    fmt_map = {
        "xlsx": "xlsx", "excel": "xlsx", "sheet": "xlsx", "sheets": "xlsx", "chart": "xlsx", "charts": "xlsx",
        "docx": "docx", "doc": "docx", "docs": "docx", "word": "docx", "custom": "docx", "quote": "docx",
        "pptx": "pptx", "ppt": "pptx", "slide": "pptx", "slides": "pptx",
        "csv": "csv", "pdf": "pdf",
    }
    kind = fmt_map.get(body.format.strip().lower(), "docx")
    filename = files.file_name(body.title, kind)
    if wa.SharePointAdapter.file_name_refused(filename):
        filename = files.file_name(re.sub(r"[^A-Za-z0-9 ._()-]+", " ", body.title), kind)
    content = body.content_markdown.strip() or (
        f"# {body.title}\n\nTarget SharePoint folder: {normalized_folder}\n"
    )
    try:
        data = files.make(kind, content)
    except files.FileRefused as refused:
        if refused.code == "pdf_unavailable":
            kind = "docx"
            filename = files.file_name(body.title, kind)
            data = files.make("docx", content)
        else:
            raise UserFacing("invalid_request", f"Could not generate .{kind} file.", 422) from None
    target = files.Target(
        anchor=f"sharepoint://pending/{normalized_folder}",
        drive="",
        folder=normalized_folder,
        label=filename,
        parent="root",
    )
    row = _FALLBACK_UPLOADS.prepare(reader.key, name=filename, kind=kind, data=data, target=target)
    download_url = f"/api/instruct/files/{row['file_id']}"
    return {
        "status": "approval_queued",
        "draft_id": row["upload_id"],
        "upload_id": row["upload_id"],
        "file_id": row["file_id"],
        "folder_path": normalized_folder,
        "create_folder_if_missing": bool(body.create_folder_if_missing),
        "filename": filename,
        "size_bytes": len(data),
        "download_url": download_url,
        "assist_url": "/assist",
        "missing_for_live": ["SharePoint live write executor is not active on this server."],
        "message": (
            f"Prepared {filename} for SharePoint folder \"{normalized_folder}\". "
            "Download now or approve in Assist."
        ),
    }


def install(app: Any, backend: Any, reader: Callable[..., Reader]) -> None:
    """Register SharePoint folder lookup and artifact upload routes on ``app``."""

    @app.get(
        "/api/sharepoint/folders",
        response_model=SharePointFoldersResponse,
        tags=["actions"],
        responses={401: {"model": m.ErrorResponse}},
        include_in_schema=False,
    )
    def sharepoint_folders_list(
        limit: int = Query(80, ge=1, le=200),
        who: Reader = Depends(reader),
    ) -> SharePointFoldersResponse:
        """Known SharePoint document folders for this workspace."""
        fn = getattr(backend, "sharepoint_folders", None)
        rows = fn(who, limit=limit) if callable(fn) else _fallback_folders(limit)
        return SharePointFoldersResponse(
            folders=[SharePointFolder(**row) for row in rows],
        )

    @app.post(
        "/api/artifacts/sharepoint-upload",
        response_model=SharePointUploadResponse,
        tags=["actions"],
        responses=_ERR,
        include_in_schema=False,
    )
    def artifact_sharepoint_upload(
        body: SharePointUploadRequest,
        who: Reader = Depends(reader),
    ) -> SharePointUploadResponse:
        """Generate/locate an artifact file and upload to SharePoint (or queue an Assist approval draft)."""
        fn = getattr(backend, "sharepoint_upload_artifact", None)
        if callable(fn):
            result = fn(
                who,
                title=body.title,
                fmt=body.format,
                folder_path=body.folder_path,
                create_folder_if_missing=body.create_folder_if_missing,
                content_markdown=body.content_markdown,
                artifact_id=body.artifact_id,
                source_url=body.source_url,
            )
        else:
            result = _fallback_upload(who, body)
        return SharePointUploadResponse(**result)
