"""The plan call's precedent search tool: a stdio MCP server over ONE matter's fenced precedents (a JSON
file the plan runner wrote for that call). The plan prompt shows the person's block and the cases
nearest to the matter; this tool reaches every other precedent of that matter, so no case is out of the
model's reach (owner 11-23: no cut of what Assist can read). It serves what the runner gave it and
nothing else: no database, no network, no credential.

``precedents`` takes ``find`` (words that must all appear in a case, any case), ``act`` (one act kind)
and ``offset`` (where to continue); it answers the matching cases nearest first, whole, up to one page
of ``PAGE_CHARS`` characters, with ``next_offset`` (None when the last matching case is in the page), the
number matching and the number held. A page is a display unit: ``next_offset`` reaches all of it. A single case longer than a page comes in
slices: its ``next_text_offset`` and ``action_key`` go back as ``text_offset`` and ``action_key`` for the
next slice (the model's window is the one bound; every character is reachable).

  python3 -m operating.assist_precedent_tool --file F"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, BinaryIO, Mapping, Optional, Sequence

PAGE_CHARS = 40000          # one reply (a display page; ``next_offset`` continues to the end)
NAME = "precedents"
PROTOCOL = "2024-11-05"
FIELDS = ("action_key", "at", "why", "counterpart", "situation", "act", "act_to", "act_text", "new_facts")
SCHEMA = {"type": "object", "properties": {
    "find": {"type": "string", "description": "words that must all appear in the case (the other side, "
             "what was asked, what was done, the words); empty: every case"},
    "act": {"type": "string", "description": "only cases where the person did this: reply, forward, "
            "new_message, attach, system_write or nothing"},
    "offset": {"type": "integer", "minimum": 0, "description": "the next_offset of the previous page"},
    "action_key": {"type": "string", "description": "one case, to read the rest of a case that was cut "
                   "at the end of a page"},
    "text_offset": {"type": "integer", "minimum": 0, "description": "with action_key: the case's "
                    "next_text_offset"}},
    "additionalProperties": False}


def record_of(p: Mapping[str, Any]) -> dict[str, Any]:
    """One precedent as the file holds it: only what the model reads, times as text."""
    out = {k: p.get(k) for k in FIELDS}
    at = out.get("at")
    out["at"] = at.isoformat() if hasattr(at, "isoformat") else str(at or "")
    out["act_to"] = [str(t) for t in out.get("act_to") or ()]
    return out


def text_of(p: Mapping[str, Any]) -> str:
    from operating.assist_plan import precedent_text
    return precedent_text(p)


def _bad(arguments: Mapping[str, Any]) -> Optional[str]:
    unknown = sorted(set(arguments) - set(SCHEMA["properties"]))
    for key in ("find", "act", "action_key"):
        if not isinstance(arguments.get(key, ""), str):
            return f"{key} is text"
    for key in ("offset", "text_offset"):
        v = arguments.get(key, 0)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return f"{key} is a whole number >= 0"
    if "text_offset" in arguments and not arguments.get("action_key"):
        return "text_offset goes with action_key"
    return f"unknown: {', '.join(unknown)}" if unknown else None


def search(cases: Sequence[Mapping[str, Any]], arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The page of ``cases`` (nearest first) that match ``arguments``, or the next slice of one case; an
    unknown argument or a value of the wrong type is an error the model reads, never ignored."""
    bad = _bad(arguments)
    if bad:
        return {"error": "invalid_input", "hint": bad}
    key = arguments.get("action_key", "")
    if key:
        one = next((p for p in cases if p.get("action_key") == key), None)
        if one is None:
            return {"error": "not_found", "hint": "no case with that action_key"}
        text, at = text_of(one), arguments.get("text_offset", 0)
        return {"action_key": key, "text_offset": at, "text": text[at:at + PAGE_CHARS],
                "next_text_offset": at + PAGE_CHARS if at + PAGE_CHARS < len(text) else None}
    find, act, offset = arguments.get("find", ""), arguments.get("act", ""), arguments.get("offset", 0)
    words = [w.lower() for w in find.split()]
    hits = [p for p in cases
            if (not act or p.get("act") == act) and all(w in text_of(p).lower() for w in words)]
    page, used, nxt = [], 0, None
    for i, p in enumerate(hits):
        if i < offset:
            continue
        text = text_of(p)
        if page and used + len(text) > PAGE_CHARS:
            nxt = i
            break
        cut = len(text) > PAGE_CHARS
        page.append({"action_key": p.get("action_key"), "text": text[:PAGE_CHARS],
                     "next_text_offset": PAGE_CHARS if cut else None})
        used += min(len(text), PAGE_CHARS)
    return {"held": len(cases), "matching": len(hits), "offset": offset, "returned": len(page),
            "next_offset": nxt, "cases": page}


def handle(cases: Sequence[Mapping[str, Any]], message: Any) -> Optional[dict[str, Any]]:
    """The JSON-RPC answer to one message (None for a notification)."""
    if not isinstance(message, Mapping):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    ident, method = message.get("id"), message.get("method")
    if "id" not in message:
        return None
    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion")
        return {"jsonrpc": "2.0", "id": ident, "result": {
            "protocolVersion": asked if isinstance(asked, str) else PROTOCOL,
            "capabilities": {"tools": {}}, "serverInfo": {"name": "plan", "version": "1"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": ident, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": ident, "result": {"tools": [{
            "name": NAME, "description": "Search the other past cases of this person for this matter: "
            "every case not shown in the prompt, nearest first, whole.", "inputSchema": SCHEMA}]}}
    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != NAME:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "unknown tool"}}
        args = params.get("arguments")
        out = search(cases, args if isinstance(args, Mapping) else {})
        from operating.card_text import mask_served
        return {"jsonrpc": "2.0", "id": ident, "result": {
            "content": [{"type": "text", "text": mask_served(json.dumps(out, ensure_ascii=False))}],
            "isError": "error" in out}}
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "method not found"}}


def serve(cases: Sequence[Mapping[str, Any]], reader: BinaryIO, writer: BinaryIO) -> None:
    while True:
        line = reader.readline()
        if not line:
            return
        try:
            answer = handle(cases, json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, ValueError):
            answer = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if answer is not None:
            writer.write(json.dumps(answer, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
            writer.flush()


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m operating.assist_precedent_tool")
    p.add_argument("--file", required=True)
    a = p.parse_args(argv)
    with open(a.file, encoding="utf-8") as fh:
        cases = json.load(fh)
    if not isinstance(cases, list):
        print("the file holds no list of cases", file=sys.stderr)
        return 2
    serve(cases, sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
