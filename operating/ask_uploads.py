"""Files a person attaches to an Ask, streamed to disk as they arrive (owner 11-23: no size
limit, nothing held whole in a request body).

``POST /api/ask/files`` writes the body to ``<state>/ask-uploads/<owner>/<id>.bin`` chunk by
chunk (no size limit: only the disk bounds it) and answers its id; ``/api/ask`` then names the
file by id. A file is the uploader's alone (the owner's directory is the hash of their
principals): another reader's id is not found. It is held until the person removes it or until
``KEEP_SECONDS`` after it arrived (a week; the sweep runs on every upload, and an id that has
gone is answered "attach it again", never read as empty); the Ask's own read of it never
deletes it, so a follow-up turn sends the same ids again."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from operating.disk_guard import Headroom

KEEP_SECONDS = 7 * 24 * 3600
ID = re.compile(r"[0-9a-f]{32}")


class Uploads:
    def __init__(self, root: "str | Path | None") -> None:
        self.root = Path(root) if root else Path(tempfile.gettempdir()) / f"plx-ask-uploads-{os.getuid()}"

    @classmethod
    def for_state_dir(cls, state_dir: Any) -> "Uploads":
        return cls(Path(state_dir) / "ask-uploads" if state_dir else None)

    def _owner(self, owner: str) -> Path:
        return self.root / hashlib.sha256(owner.encode("utf-8")).hexdigest()[:24]

    def put(self, owner: str, name: str, chunks: Iterable[bytes]) -> dict[str, Any]:
        """Stream ``chunks`` to disk; the new upload's ``{id, name, size, digest}``."""
        mine = self._owner(owner)
        mine.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sweep()
        uid = uuid.uuid4().hex
        part, final = mine / f"{uid}.part", mine / f"{uid}.bin"
        digest, size = hashlib.sha256(), 0
        room = Headroom(f"ask-upload-{uid}", mine)
        try:
            with open(part, "wb") as out:
                for chunk in chunks:
                    room.room(size)   # DiskRefused (said) before the disk is taken below its floor
                    out.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            meta = {"id": uid, "name": name, "size": size, "digest": digest.hexdigest()[:16],
                    "at": time.time()}
            (mine / f"{uid}.json").write_text(json.dumps(meta), "utf-8")
            os.replace(part, final)
        except BaseException:
            for leftover in (part, final, mine / f"{uid}.json"):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            raise
        finally:
            room.release()
        return {k: meta[k] for k in ("id", "name", "size", "digest")}

    def get(self, owner: str, uid: Any) -> Optional[tuple[Path, dict[str, Any]]]:
        """The file and its meta, or None (not this person's, never uploaded, removed, kept
        past ``KEEP_SECONDS``)."""
        if not isinstance(uid, str) or not ID.fullmatch(uid):
            return None
        mine = self._owner(owner)
        try:
            meta = json.loads((mine / f"{uid}.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None
        path = mine / f"{uid}.bin"
        return (path, meta) if path.is_file() else None

    def forget(self, owner: str, uid: Any) -> bool:
        """Remove one of this person's uploads; whether it was held."""
        if not isinstance(uid, str) or not ID.fullmatch(uid):
            return False
        mine, held = self._owner(owner), False
        for suffix in (".bin", ".json", ".part"):
            try:
                (mine / f"{uid}{suffix}").unlink()
                held = held or suffix == ".bin"
            except OSError:
                pass
        return held

    def sweep(self, now: Optional[float] = None) -> list[str]:
        """Remove every upload (and any half-written part) older than ``KEEP_SECONDS``."""
        gone: list[str] = []
        cut = (now or time.time()) - KEEP_SECONDS
        try:
            owners = [d for d in self.root.iterdir() if d.is_dir()]
        except OSError:
            return gone
        for mine in owners:
            for f in mine.iterdir():
                try:
                    if f.stat().st_mtime < cut:
                        f.unlink()
                        gone.append(f.name)
                except OSError:
                    pass
        return gone
