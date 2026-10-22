"""Harness re-reads of BC, mail and SharePoint citations from the capture stores.

The scorer (``evals/score.py``) checks an answer value against the harness's re-read of
each handle it cites (``evals.runner.harness_evidence``, ``plx.source_citation`` as the
item's actor). The case host resolves ``graph-mail://``, ``sharepoint://`` and ``bc://``
citations live and returns record metadata only (no row the claim gate could check), so a
correct answer citing a captured mail, document or BC record failed closed. This module
resolves them offline in the capture stores the product projected, exactly the version the
citation names:

* ``bc://<env>/<company>/<set>/<id>@<generation>-<capture>`` through the BC capture store
  (``integrations.capture_store.CaptureStore.resolve``); its revision is the record's
  ``source_version`` (the BC etag);
* ``graph-mail://<mailbox>/<message>@<changeKey>[#attachment=...]`` through the mail
  capture store (``MailCaptureStore.resolve``); the revision is the changeKey it names;
* ``sharepoint://<drive>/<item>@<eTag>[#page=<n>]`` through the SharePoint capture store
  (``SharePointCaptureStore.resolve``); a ``#page`` citation reads as that page's text.
  A byte-pinned ``#sha256=`` citation is left to the harness's document reader
  (``evals.baseline.ToolHost`` ``documents``: the re-extracted fields).

A read is ``{"ok": true, "relation", "key"?, "row" | "page"+"text", "provenance":
{"source": "capture-store", "revision"}}``; the runner keeps ``provenance`` in the
transcript, and the scorer matches an expected citation's revision against it. A handle
the stores do not hold (or the reader may not see) is left to the wrapped host, never
answered from anywhere else. The resolvers are the stores' own, bound by the caller to the
item's actor (``bc_resolver``: the run's principals) and the stores' audience, as every
other harness re-read is; this module does no I/O of its own.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

SOURCE = "capture-store"
Resolver = Callable[[str], Any]


def reader(*, bc: Resolver | None = None, mail: Resolver | None = None,
           sharepoint: Resolver | None = None) -> Callable[[str], dict[str, Any] | None]:
    """``handle -> read`` over the given store resolvers (each ``handle -> the store's
    resolve result``, bound to an actor and audience); None when no store answers."""
    from evals.score import record_page, record_revision

    def read(handle: str) -> dict[str, Any] | None:
        scheme = handle.partition("://")[0] if isinstance(handle, str) else ""
        if scheme == "bc" and bc is not None:
            found = bc(handle)
            if not isinstance(found, Mapping) or found.get("ok") is not True or \
                    not isinstance(found.get("row"), Mapping):
                return None
            return {"ok": True, "relation": f"bc.{handle.split('/')[4]}",
                    "key": found.get("key"), "row": dict(found["row"]),
                    "provenance": {"source": SOURCE,
                                   "revision": found.get("source_version")}}
        resolve = {"graph-mail": mail, "sharepoint": sharepoint}.get(scheme)
        if resolve is None or (scheme == "sharepoint" and "sha256=" in handle):
            return None
        found = resolve(handle)
        if not isinstance(found, Mapping) or not isinstance(found.get("row"), Mapping):
            return None
        provenance = {"source": SOURCE, "revision": record_revision(handle)}
        if record_page(handle) is not None:
            text = found["row"].get("content_text")
            if not isinstance(found.get("page"), int) or not isinstance(text, str):
                return None
            return {"ok": True, "relation": found.get("relation"), "page": found["page"],
                    "text": text, "text_source": found["row"].get("text_status"),
                    "provenance": provenance}
        return {"ok": True, "relation": found.get("relation"), "row": dict(found["row"]),
                "provenance": provenance}
    return read


class CaptureEvidenceHost:
    """A run host whose harness reads of captured citations come from the capture stores
    (``reader``); every other call and read goes to the wrapped host unchanged."""

    def __init__(self, host: Any, read: Callable[[str], dict[str, Any] | None]) -> None:
        self._host = host
        self._read = read

    def __getattr__(self, name: str) -> Any:
        return getattr(self._host, name)

    def read(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "plx.source_citation" and set(arguments) == {"citation"}:
            found = self._read(str(arguments["citation"]))
            if found is not None:
                return found
        return self._host.read(name, arguments)


def bc_resolver(db: Any, environment: str, *, granted: str
                ) -> Callable[[Iterable[str]], Resolver]:
    """``principals -> (handle -> CaptureStore.resolve)`` over one BC capture file, read as
    an actor holding ``principals`` (the item's). The store's audience grants the
    environment's companies to ``granted`` alone (the eval product's own principal, as its
    Portal audience grants the mirror), so a reader without it is answered nothing."""
    import sqlite3

    from integrations.capture_store import CaptureStore
    from integrations.live_source_guard import LiveSourceAudience
    from ontology.types import Actor

    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        tenant = dict(conn.execute("SELECT * FROM meta").fetchall()).get("tenant")
    audience = LiveSourceAudience({f"bc:{environment.lower()}:*": [granted]},
                                  tenant=str(tenant))
    store = CaptureStore(db, audience)

    def resolver(principals: Iterable[str]) -> Resolver:
        actor = Actor(str(tenant), frozenset(principals), "restricted")
        return lambda handle: store.resolve(handle, actor)
    return resolver


__all__ = ["CaptureEvidenceHost", "SOURCE", "bc_resolver", "reader"]
