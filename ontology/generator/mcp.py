"""MCP tools as ontology sources and actions: Mission Control (``plx-mc``), the brain
(``plx-brain``) and VMC context (``vmc-context``).

Every ``mcp_tool`` entry of these systems in ``ontology/source_registry.json`` gets exactly
one role (``roles``):

* ``bound``: a curated binding whose records are the tool's output records lists the entry
  in its ``registry_entries`` (``ontology/schema/domains/mission_control.json`` and
  ``knowledge.json``: MC tasks, approval gates, task events, buckets, knowledge hits and
  brain items). Only read tools: for ``plx-mc`` a read of ``integrations/mc_tool_policy.json``.
* ``action``: an approvable MC write (the policy's ``approvable_writes``, every one of them)
  is an action type of ``mission_control.json`` that this module generates from the pin
  (``action_types``). It is a typed description only: the one send path stays
  ``integrations.mc.MissionControlMcpClient.call_write_tool`` with a per-call human
  ``McWriteAuthorization`` (the MC write door); the read door refuses it before I/O.
* ``excluded``: ``ontology/source_scope.json`` names the entry id with the decided record
  ``DECISION``, which states the tool's reason. Every never-sent MC tool is excluded.

An entry with no role or with two, a policy class that contradicts its role, or a decision
record that does not name an excluded tool fails ``check``.

The pin (``ontology/schema/generated/meta/mcp_tools.json``, ``PIN_FORMAT``) is what the
upstream code declares at the registry's upstream commits, read with local git only: per
tool its registration (``path:line``, the sha256 of the registration call), its input
parameters (top-level keys of the zod shape, the zod base type, required unless the
top-level chain is ``.optional()``/``.nullish()``/``.default()``; a shape that is no object
literal or ``const`` reference resolvable in that repository is ``input: null`` with
``input_unresolved``) and ``output_schema`` (``not_declared`` unless the registration has an
``outputSchema``). It is source-derived, not a ``tools/list`` capture:
``scripts.live_read_probes mc-tools`` compares each live ``tools/list`` ``inputSchema``
(property names, ``required``) with it, and the live check
``mc.initialize_tools_list`` fails on any mismatch or unpinned tool.

Commands (repository root; local git only, no network)::

    python3 -m ontology.generator.mcp pin [--checkout REPO=PATH ...]   # rewrite the pin
    python3 -m ontology.generator.mcp repin-digest --previous-registry FILE
        # no checkout: carry the pin to a registry whose MCP tool entries and upstream
        # commits are those of FILE, the registry the pin was built from
        # (git show <pin's commit>:ontology/source_registry.json > FILE); else fails
    python3 -m ontology.generator.mcp generate [--check]               # action types
    python3 -m ontology.generator.mcp status                           # roles as JSON
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
PIN_FORMAT = "plx-mcp-tools-pin/v1"
PIN_PATH = ROOT / "ontology" / "schema" / "generated" / "meta" / "mcp_tools.json"
REGISTRY_PATH = ROOT / "ontology" / "source_registry.json"
POLICY_PATH = ROOT / "integrations" / "mc_tool_policy.json"
DOMAIN_PATH = ROOT / "ontology" / "schema" / "domains" / "mission_control.json"
DECISION = "devloop/DECISIONS/2026-10-07-mcp-tool-roles.md"
# Registry system -> the upstream repository that serves it.
SYSTEMS = {"plx-mc": "PLX_MC", "plx-brain": "agentic-swarm", "vmc-context": "agentic-swarm"}
KIND = "mcp_tool"
ACTION_TARGET = "MCTask"   # every approvable MC write acts on one MC task
# zod base -> action parameter type; any other base (array, object, a schema reference)
# is not typed and is named in the action description instead.
PARAM_TYPES = {"string": "text", "enum": "text", "boolean": "boolean", "number": "decimal"}
# Inputs that name a record (a task or checkout id, a commit, a pull request, a person) or a
# scope (a repository, a bucket): typed identifier and cited only, so the loader's
# owner-write rule (ontology/schema.py OWNER_REFERENCE_PARAMS) holds for them.
_NAMES_RECORD_OR_SCOPE = re.compile(r"Id$|^(?:repo|bucket|commitSha|prUrl|assignee|reporter)$")
_OPTIONAL = re.compile(r"\.\s*(?:optional|nullish|default)\s*\(")
_IDENT = re.compile(r"^([A-Za-z_$][A-Za-z0-9_$]*)(?:\s*\.\s*shape)?\s*$")


class McpGenError(Exception):
    pass


# --------------------------------------------------------------------- TypeScript reading

def _skip_string(text: str, i: int) -> int:
    """Index after the string literal (or template, with nested ``${}``) starting at i."""
    quote = text[i]
    i += 1
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == quote:
            return i + 1
        if quote == "`" and text.startswith("${", i):
            i = _close(text, i + 1) + 1
            continue
        i += 1
    raise McpGenError("unterminated string literal")


def _close(text: str, i: int) -> int:
    """Index of the bracket closing the one at ``text[i]``, skipping strings and comments."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[text[i]]]
    i += 1
    while i < len(text):
        c = text[i]
        if c in "'\"`":
            i = _skip_string(text, i)
            continue
        if text.startswith("//", i):
            i = text.find("\n", i)
            i = len(text) if i < 0 else i
            continue
        if text.startswith("/*", i):
            i = text.index("*/", i) + 2
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif c in ")]}":
            if c != stack.pop():
                raise McpGenError("unbalanced brackets")
            if not stack:
                return i
        i += 1
    raise McpGenError("unclosed bracket")


def _split(text: str, sep: str = ",") -> list[str]:
    """Top-level parts of ``text`` separated by ``sep`` (empty parts dropped)."""
    parts, start, i = [], 0, 0
    while i < len(text):
        c = text[i]
        if c in "'\"`":
            i = _skip_string(text, i)
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        if text.startswith("/*", i):
            i = text.index("*/", i) + 2
            continue
        if c in "([{":
            i = _close(text, i) + 1
            continue
        if c == sep:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return [p for p in (_strip_comments(p).strip() for p in parts) if p]


def _strip_comments(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if c in "'\"`":
            end = _skip_string(text, i)
            out.append(text[i:end])
            i = end
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif text.startswith("/*", i):
            i = text.index("*/", i) + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _collapse(expr: str) -> str:
    """``expr`` with every bracketed group emptied: ``z.array(z.x().optional()).min(1)`` ->
    ``z.array().min()``, so only the top-level chain is read."""
    out, i = [], 0
    while i < len(expr):
        c = expr[i]
        if c in "'\"`":
            i = _skip_string(expr, i)
            out.append('""')
            continue
        if c in "([{":
            end = _close(expr, i)
            out.append(c + expr[end])
            i = end + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


class _Source:
    """One repository's product code at one commit, for resolving ``const`` references."""

    def __init__(self, checkout: Path, commit: str) -> None:
        self.checkout, self.commit = checkout, commit
        self._files: dict[str, str] = {}

    def _git(self, *args: str) -> str:
        done = subprocess.run(["git", "-C", str(self.checkout), *args], capture_output=True,
                              text=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        if done.returncode not in (0, 1):
            raise McpGenError(f"git {args[0]} failed in {self.checkout}: {done.stderr[:200]}")
        return done.stdout

    def read(self, path: str) -> str:
        if path not in self._files:
            self._files[path] = self._git("show", f"{self.commit}:{path}")
        return self._files[path]

    def definition(self, name: str, near: str) -> tuple[str, str] | None:
        """(initializer, file) of ``const name = ...`` in ``near``, else of the one exported
        ``const name`` of the repository's product TypeScript (None: absent or not unique)."""
        found = _const_init(self.read(near), name)
        if found is not None:
            return found, near
        from ontology.upstream import is_product

        # Git's extended regex uses the platform's POSIX engine: \s and \b are
        # not portable. A TypeScript identifier can also contain or end with $.
        listed = self._git("grep", "-l", "-E",
                           rf"export[[:space:]]+const[[:space:]]+{re.escape(name)}([^[:alnum:]_$]|$)",
                           self.commit, "--", "*.ts").splitlines()
        paths = sorted({p for p in (line.split(":", 1)[-1] for line in listed) if is_product(p)})
        found = _const_init(self.read(paths[0]), name) if len(paths) == 1 else None
        return None if found is None else (found, paths[0])


def _const_init(text: str, name: str) -> str | None:
    """The initializer of ``const name = <expr>;`` in ``text`` (up to its top-level ``;``)."""
    m = re.search(rf"\bconst\s+{re.escape(name)}\s*(?::[^=]+)?=\s*", text)
    if m is None:
        return None
    i = m.end()
    while i < len(text) and text[i] != ";":
        if text[i] in "'\"`":
            i = _skip_string(text, i)
        elif text[i] in "([{":
            i = _close(text, i) + 1
        else:
            i += 1
    return text[m.end():i].strip()


def _object_body(expr: str, src: _Source, path: str, depth: int = 0
                 ) -> tuple[str, str] | None:
    """(``{...}`` body, file) of a zod shape: an object literal, ``z.object({...})...`` or a
    ``const`` reference (``name`` / ``name.shape``) to one."""
    expr = expr.strip()
    if expr.startswith("{"):
        return expr[1:_close(expr, 0)], path
    m = re.match(r"z\s*\.\s*object\s*\(", expr)
    if m:
        inner = expr[m.end():].lstrip()
        return (inner[1:_close(inner, 0)], path) if inner.startswith("{") else None
    ref = _IDENT.match(expr)
    found = src.definition(ref.group(1), path) if ref and depth < 3 else None
    return None if found is None else _object_body(found[0], src, found[1], depth + 1)


def _param(expr: str, src: _Source, path: str, depth: int = 0) -> dict[str, Any]:
    chain = _collapse(expr)
    m = re.match(r"\s*z\s*\.\s*([A-Za-z]+)", chain)
    if m:
        base = m.group(1)
        if base == "number" and re.search(r"\.\s*int\s*\(", chain):
            base = "integer"
        return {"type": base, "required": not _OPTIONAL.search(chain)}
    head = re.match(r"\s*([A-Za-z_$][A-Za-z0-9_$]*)", chain)
    found = src.definition(head.group(1), path) if head and depth < 3 else None
    if found is None:
        return {"type": "unresolved", "required": not _OPTIONAL.search(chain)}
    inner = _param(found[0], src, found[1], depth + 1)
    # The reference's own chain plus what this expression chains onto it.
    return {"type": inner["type"], "required": inner["required"] and not _OPTIONAL.search(chain)}


def read_registration(src: _Source, path: str, line: int, name: str) -> dict[str, Any]:
    """The pinned facts of tool ``name`` registered at ``path:line``."""
    from ontology.generator.tool_registration import tool_registration

    text = src.read(path)
    offset = sum(len(row) + 1 for row in text.split("\n")[:line - 1])
    m = tool_registration(re.escape(name)).search(text, max(0, offset - 200))
    if m is None:
        raise McpGenError(f"{name}: no registration at {path}:{line}")
    open_at = text.index("(", m.start())
    call = text[open_at:_close(text, open_at) + 1]
    args = _split(call[1:-1])
    out: dict[str, Any] = {"path": path, "line": text.count("\n", 0, m.start()) + 1,
                           "registration_sha256": hashlib.sha256(call.encode()).hexdigest()}
    shape: str | None = None
    output = "not_declared"
    if text[m.start():].startswith(".registerTool"):
        config = _split(args[1][1:-1]) if len(args) > 1 and args[1].startswith("{") else []
        fields = {p.split(":", 1)[0].strip(): p.split(":", 1)[1] for p in config if ":" in p}
        shape = fields.get("inputSchema")
        output = "declared" if "outputSchema" in fields else output
    else:
        # tool(name, description, shape, [annotations,] cb); tool(name, shape, cb) only with
        # an object literal; tool(name, description, cb) takes no input.
        rest = args[2:-1] if len(args) > 3 else args[1:2] if args[1:2] and args[1].startswith("{") else []
        shape = rest[0] if rest else None
    out["output_schema"] = output
    if shape is None:
        out["input"] = {}
        return out
    found = _object_body(shape, src, path)
    if found is None:
        out.update(input=None, input_unresolved=f"shape {_collapse(shape)[:80]!r} is not an "
                   "object literal or a resolvable const")
        return out
    body, home = found
    params: dict[str, Any] = {}
    for part in _split(body):
        if part.startswith("..."):
            out.update(input=None, input_unresolved="shape spreads another shape")
            return out
        key, sep, expr = part.partition(":")
        if not sep:
            out.update(input=None, input_unresolved=f"shorthand property {key.strip()[:40]!r}")
            return out
        params[key.strip().strip("'\"")] = _param(expr, src, home)
    out["input"] = dict(sorted(params.items()))
    return out


# --------------------------------------------------------------------- pin

def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(value: Any) -> str:
    return json.dumps(value, indent=1, ensure_ascii=False) + "\n"


def tool_entries(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    return sorted((e for e in registry["entries"]
                   if e["kind"] == KIND and e["system"] in SYSTEMS), key=lambda e: e["id"])


def _home_citation(entry: Mapping[str, Any]) -> tuple[str, int]:
    repo = SYSTEMS[entry["system"]]
    for ref in entry["evidence"]:
        m = re.fullmatch(r"([^:]+):(.+)@[0-9a-f]{12}:(\d+)", ref)
        if m and m.group(1) == repo:
            return m.group(2), int(m.group(3))
    raise McpGenError(f"{entry['id']}: no {repo} citation with a line")


def build_pin(registry: Mapping[str, Any], checkouts: Mapping[str, Path]) -> dict[str, Any]:
    """The pin document, read at the registry's upstream commits from ``checkouts``."""
    upstream = {repo: registry["upstream"][repo]["commit"] for repo in sorted(set(SYSTEMS.values()))}
    sources = {repo: _Source(checkouts[repo], commit) for repo, commit in upstream.items()}
    servers: dict[str, Any] = {}
    for entry in tool_entries(registry):
        repo = SYSTEMS[entry["system"]]
        path, line = _home_citation(entry)
        block = servers.setdefault(entry["system"], {"repo": repo, "server": entry.get("server"),
                                                     "tools": {}})
        block["tools"][entry["name"]] = read_registration(sources[repo], path, line, entry["name"])
    return {"format": PIN_FORMAT,
            "_usage": "Generated by python3 -m ontology.generator.mcp pin from the upstream "
                      "code at the source registry's commits (local git): per MCP tool its "
                      "registration and zod input shape. Source-derived, not a tools/list "
                      "capture; scripts.live_read_probes mc-tools checks it against the live "
                      "MC tools/list (devloop/live-checks/mc.initialize_tools_list.json). "
                      "Do not edit by hand.",
            "evidence": "source-derived",
            "upstream": upstream, "registry_entries_digest": registry["entries_digest"],
            "servers": servers}


def _entries_digest(entries: Sequence[Mapping[str, Any]]) -> str:
    """``scripts.source_registry.entries_digest`` (ontology never imports scripts; a test
    pins the two equal)."""
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def carry_pin_digest(pin: Mapping[str, Any], previous: Mapping[str, Any],
                     current: Mapping[str, Any]) -> dict[str, Any]:
    """The pin re-stamped with ``current``'s entries digest, without an upstream checkout.

    ``build_pin`` reads only the MCP tool entries and the upstream commits of the registry,
    at those fixed commits; so when ``previous`` is the registry the pin was built from (its
    recomputed digest is the pin's) and ``current`` has the same tool entries and commits,
    ``build_pin(current)`` equals the pin but for the digest. Anything else fails closed:
    run ``pin`` from the checkouts."""
    problems = []
    for name, registry in (("previous", previous), ("current", current)):
        if _entries_digest(registry.get("entries", [])) != registry.get("entries_digest"):
            problems.append(f"{name} registry entries differ from its entries_digest")
    if previous.get("entries_digest") != pin.get("registry_entries_digest"):
        problems.append("previous registry is not the one the pin was built from")
    if not problems:
        if tool_entries(previous) != tool_entries(current):
            problems.append("MCP tool entries changed since the pin")
        repos = sorted(set(SYSTEMS.values()))
        commits = [{repo: r["upstream"][repo]["commit"] for repo in repos}
                   for r in (previous, current)]
        if commits[0] != commits[1] or commits[1] != pin.get("upstream"):
            problems.append("upstream commits changed since the pin")
    if problems:
        raise McpGenError("cannot carry the pin digest: " + "; ".join(problems)
                          + " (python3 -m ontology.generator.mcp pin)")
    return {**pin, "registry_entries_digest": current["entries_digest"]}


def checkouts_for(repos: Iterable[str], overrides: Mapping[str, str]) -> dict[str, Path]:
    from ontology.upstream import checkout_for
    pins = _read_json(ROOT / "devloop" / "upstream-pins.json")
    return {repo: checkout_for(repo, dict(overrides), pins) for repo in repos}


# --------------------------------------------------------------------- action types

def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def action_id(tool: str) -> str:
    return "MC" + "".join(p.capitalize() for p in tool.removeprefix("mc_").split("_"))


def action_types(pin: Mapping[str, Any], policy: Mapping[str, Any],
                 target_properties: Iterable[str]) -> list[dict[str, Any]]:
    """One action type per approvable MC write, from the pinned input shape. An input the
    MC write door never sends (``plx_hub_guard.MC_WRITE_ARGUMENTS_NOT_SENT``) is no
    parameter; the description names it."""
    from integrations.plx_hub_guard import MC_WRITE_ARGUMENTS_NOT_SENT

    props = set(target_properties)
    tools = pin["servers"]["plx-mc"]["tools"]
    commit = pin["upstream"]["PLX_MC"]
    out = []
    for tool in sorted(policy["approvable_writes"]):
        spec = tools.get(tool)
        if spec is None or spec.get("input") is None:
            raise McpGenError(f"{tool}: approvable write without a pinned input shape")
        params: dict[str, Any] = {}
        untyped = []
        not_sent = MC_WRITE_ARGUMENTS_NOT_SENT.get(tool, frozenset())
        for name, param in spec["input"].items():
            if name in not_sent:
                continue
            kind = "integer" if param["type"] == "integer" else PARAM_TYPES.get(param["type"])
            if kind is None:
                untyped.append(name)
                continue
            key = snake(name)
            names = bool(_NAMES_RECORD_OR_SCOPE.search(name))
            typed: dict[str, Any] = {
                "type": "identifier" if names and kind == "text" else kind,
                "provenance": ["cited_value"] if names else ["cited_value", "user_objective"],
                "required": param["required"], "description": f"MC input {name}."}
            if key in props:
                typed["property_ref"] = f"{ACTION_TARGET}.{key}"
            params[key] = typed
        required = sorted(k for k, p in params.items() if p["required"])
        description = (f"The approvable Mission Control write {tool} (target: the MC task it "
                       "acts on or creates), sent only through the MC write door with a "
                       "per-call human approval and verified by reading the task back.")
        if untyped:
            description += " Inputs not typed here (arrays, objects or schema references): " \
                           + ", ".join(sorted(untyped)) + "."
        if not_sent:
            description += " Inputs the MC write door never sends: " \
                           + ", ".join(sorted(not_sent & set(spec["input"]))) + "."
        out.append({
            "id": action_id(tool), "description": description, "target": ACTION_TARGET,
            "params": params,
            "submission_criteria": [
                {"id": "per_call_approval", "description":
                 "A configured human write approver authorized exactly this call "
                 "(integrations/mc.py call_write_tool, McWriteAuthorization, single use in the "
                 "durable WriteAuthorizationLedger); the read door refuses the tool."},
                {"id": "approvable_write", "description":
                 f"{tool} is in integrations/mc_tool_policy.json approvable_writes and opted "
                 "in on the sending client."}],
            "owner_system": "mission_control", "executor": "human_in_owner_system",
            "approval": {"route": "mc_request_approval", "decider":
                         "a configured human write approver, never the requester or an agent"},
            "verification": "read_back",
            "idempotency": ["target"] + [f"params.{k}" for k in required][:7],
            "registry_entries": [f"plx-mc.{KIND}:{tool}"],
            "evidence": [{"kind": "upstream_code", "repo": "PLX_MC", "path": spec["path"],
                          "commit": commit, "lines": str(spec["line"]),
                          "note": "Registration and zod input shape (pinned in "
                                  "ontology/schema/generated/meta/mcp_tools.json)."},
                         {"kind": "ref", "id": "wb.mc_write_door"}],
        })
    return out


# --------------------------------------------------------------------- roles and check

def roles(registry: Mapping[str, Any], compiled: Any, scope: Mapping[str, str]
          ) -> dict[str, list[tuple[str, str]]]:
    """Entry id -> every (role, holder) claiming it: ``("bound", binding id)``,
    ``("action", action type id)``, ``("excluded", decision path)``."""
    out: dict[str, list[tuple[str, str]]] = {e["id"]: [] for e in tool_entries(registry)}
    claims: list[tuple[str, str, str]] = []
    for bid, (_, binding) in sorted(compiled.bindings.items()):
        claims.extend((i, "bound", bid) for i in binding.get("registry_entries", ()))
    for aid, action in sorted(compiled.action_types.items()):
        claims.extend((i, "action", aid) for i in action.get("registry_entries", ()))
    claims.extend((i, "excluded", d) for i, d in scope.items())
    for entry_id, role, holder in claims:
        if entry_id in out:
            out[entry_id].append((role, holder))
    for entry in tool_entries(registry):
        scoped = scope.get(f"{entry['system']}.{entry['kind']}")
        if scoped:
            out[entry["id"]].append(("excluded", scoped))
    return out


def check(registry: Mapping[str, Any], compiled: Any, scope: Mapping[str, str],
          policy: Mapping[str, Any], pin: Mapping[str, Any], domain: Mapping[str, Any],
          decision_text: str) -> list[str]:
    """Every problem with the MCP tool roles, the pin and the generated action types."""
    problems = []
    names = {e["id"]: e["name"] for e in tool_entries(registry)}
    found = roles(registry, compiled, scope)
    for entry_id, claimed in sorted(found.items()):
        kinds = sorted({role for role, _ in claimed})
        # Exactly one role; only ``bound`` may have several holders (one per binding).
        if len(kinds) != 1 or (kinds != ["bound"] and len(claimed) != 1):
            problems.append(f"{entry_id}: roles {sorted(claimed)} (exactly one role)")
            continue
        role, holder = claimed[0]
        tool = names[entry_id]
        if role == "excluded" and (holder != DECISION or not re.search(
                rf"`{re.escape(tool)}`", decision_text)):
            problems.append(f"{entry_id}: excluded without its reason in {DECISION}")
        if entry_id.startswith("plx-mc."):
            cls = next((c for c in ("reads", "approvable_writes", "never_sent")
                        if tool in policy[c]), None)
            allowed = {"bound": ("reads",), "action": ("approvable_writes",),
                       "excluded": ("reads", "never_sent")}[role]
            if cls not in allowed:
                problems.append(f"{entry_id}: role {role} but policy class {cls}")
    action_tools = {names[i] for i, c in found.items()
                    if i.startswith("plx-mc.") and any(role == "action" for role, _ in c)}
    if action_tools != set(policy["approvable_writes"]):
        problems.append(f"action-typed MC tools {sorted(action_tools)} != approvable_writes")
    pinned = {(s, t) for s, block in pin["servers"].items() for t in block["tools"]}
    listed = {(e["system"], e["name"]) for e in tool_entries(registry)}
    if pinned != listed or pin["registry_entries_digest"] != registry["entries_digest"]:
        problems.append(f"pin is stale: {len(pinned ^ listed)} tools differ from the registry "
                        "(python3 -m ontology.generator.mcp pin)")
    else:
        target = next(t for t in domain["object_types"] if t["id"] == ACTION_TARGET)
        want = action_types(pin, policy, (p["name"] for p in target["properties"]))
        if domain.get("action_types", []) != want:
            problems.append(f"{DOMAIN_PATH.relative_to(ROOT)} action_types differ from a "
                            "regeneration (python3 -m ontology.generator.mcp generate)")
    return problems


def _inputs() -> tuple[Any, ...]:
    from ontology import schema as ontology_schema
    from ontology.source_scope import load_scope
    registry = _read_json(REGISTRY_PATH)
    compiled = ontology_schema.load_schema(ontology_schema.SCHEMA_DIR, check_format=False)
    decision = ROOT / DECISION
    return (registry, compiled, load_scope(ROOT), _read_json(POLICY_PATH), _read_json(PIN_PATH),
            _read_json(DOMAIN_PATH), decision.read_text(encoding="utf-8") if decision.is_file() else "")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ontology.generator.mcp")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pin", help="rewrite the pin from the upstream checkouts")
    p.add_argument("--checkout", action="append", default=[], metavar="REPO=PATH")
    r = sub.add_parser("repin-digest", help="carry the pin to the current registry digest "
                                            "when no tool entry or upstream commit moved")
    r.add_argument("--previous-registry", required=True, type=Path, metavar="FILE")
    g = sub.add_parser("generate", help="write the MC action types from the pin")
    g.add_argument("--check", action="store_true", help="exit 1 on any role or pin problem")
    sub.add_parser("status", help="every MCP tool entry with its role")
    args = parser.parse_args(argv)
    if args.command == "pin":
        registry = _read_json(REGISTRY_PATH)
        overrides = dict(c.split("=", 1) for c in args.checkout)
        pin = build_pin(registry, checkouts_for(set(SYSTEMS.values()), overrides))
        PIN_PATH.write_text(dump(pin), encoding="utf-8")
        print(json.dumps({"ok": True, "tools": sum(len(s["tools"]) for s in pin["servers"].values())}))
        return 0
    if args.command == "repin-digest":
        try:
            pin = carry_pin_digest(_read_json(PIN_PATH), _read_json(args.previous_registry),
                                   _read_json(REGISTRY_PATH))
        except McpGenError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        PIN_PATH.write_text(dump(pin), encoding="utf-8")
        print(json.dumps({"ok": True, "registry_entries_digest": pin["registry_entries_digest"]}))
        return 0
    registry, compiled, scope, policy, pin, domain, decision = _inputs()
    if args.command == "status":
        print(dump({i: [list(c) for c in claims]
                    for i, claims in roles(registry, compiled, scope).items()}), end="")
        return 0
    if not args.check:
        target = next(t for t in domain["object_types"] if t["id"] == ACTION_TARGET)
        domain["action_types"] = action_types(pin, policy, (p["name"] for p in target["properties"]))
        DOMAIN_PATH.write_text(dump(domain), encoding="utf-8")
        registry, compiled, scope, policy, pin, domain, decision = _inputs()
    problems = check(registry, compiled, scope, policy, pin, domain, decision)
    print(json.dumps({"ok": not problems, "problems": problems}, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
