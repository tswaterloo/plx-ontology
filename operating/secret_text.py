"""Credential-shaped text out of what the Workbench indexes and serves (SEC-S-09).

Captured mail and documents can carry a live secret (an API key mailed to a colleague, a
connection string in a spreadsheet). The capture keeps the source as it is; what is indexed
(``operating.chunking``) and every API response and Ask stream (``operator_api.app``) pass
through ``mask_secrets``, so no reader is shown one and no index row holds one.

Masked, value replaced by ``[secret]`` (a label before it is kept, so the text still reads):
- a labelled value: ``password: ...``, ``API key = ...``, ``client_secret="..."``,
  ``MC_MCP_API_KEY=...`` (an env-style name ending in KEY/SECRET/TOKEN/PASSWORD/PWD), when
  the value looks like a credential (8+ characters with a digit or symbol, or 16+);
- a connection-string or URL secret: ``AccountKey=``, ``SharedAccessKey=``, ``Password=``,
  ``sig=``/``access_token=`` query values, ``scheme://user:password@host``;
- a self-identifying token: OpenAI/Anthropic ``sk-``, GitHub, Slack, AWS ``AKIA``, Google
  ``AIza``, Stripe live keys, a JWT, an Azure client secret (``xxx8Q~...``), a PEM private key.

Not masked: GUIDs and object IDs, hashes, order and part numbers, ordinary URLs, the words
password or token in a sentence. The rule is idempotent: masked text is left as it is.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[secret]"

# the key body is base64 (no "-"), so each match stops at the first "-": linear on any input
_PEM = re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[^-]{0,16384}"
                  r"(?:-----END [A-Z ]{0,40}PRIVATE KEY-----)?")
_TOKENS = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-ant-[A-Za-z0-9_-]{20,}"
    r"|sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"
    r"|[sr]k_live_[A-Za-z0-9]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|[A-Za-z0-9_.-]{3}[0-9]Q~[A-Za-z0-9_.~-]{31,34}"
    r")(?![A-Za-z0-9_-])")
# a label, then ":", "=" or "is", then the value (quoted or a run without spaces)
_LABEL = (r"(?:client[ _-]?secret|secret(?:[ _-]?key)?|password|passwd"
          r"|pwd|passcode|pass[ _-]?phrase|api[ _-]?key|apikey|access[ _-]?key|account[ _-]?key"
          r"|private[ _-]?key|auth[ _-]?key|signing[ _-]?key|storage[ _-]?key|(?:access|auth|"
          r"bearer|refresh|api|session)[ _-]?token|token)")
_SEP = r"(?P<sep>[\"']?\s*(?::|=|\bis\b:?)\s*)"
_LABELLED = re.compile(r"(?P<label>\b" + _LABEL + r")" + _SEP
                       + r"(?P<value>[^\s\"'<>,;)\]}]{8,})", re.IGNORECASE)
# a quoted value may hold spaces: everything up to the closing quote
_LABELLED_QUOTED = re.compile(r"(?P<label>\b" + _LABEL + r")" + _SEP
                              + r"(?P<q>[\"'])(?P<value>[^\"'\n]{6,200})(?P=q)", re.IGNORECASE)
_ENV = re.compile(r"(?P<label>\b[A-Z][A-Z0-9_]*_(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD)\s*=\s*"
                  r"[\"']?)(?P<value>[^\s\"'<>,;]{8,}|(?<=[\"'])[^\"'\n]{6,200}(?=[\"']))")
# a connection string's key (after the start or ";"), or a URL query value (after "?"/"&")
_CONN = re.compile(r"(?P<label>(?:(?<![^\s;\"'])(?:AccountKey|SharedAccessKey|SharedAccessSignature"
                   r"|Password|Pwd)|(?<=[?&])(?:sig|signature|access_token|token|client_secret"
                   r"|api_key|apikey|key|code_verifier|password))=)"
                   r"(?P<value>[^;&\s\"'<>]{6,})", re.IGNORECASE)
_USERINFO = re.compile(r"(?P<label>\b[a-z][a-z0-9+.-]*://[^/\s:@\"'<>]+:)"
                       r"(?P<value>[^@\s/\"'<>]{3,})(?=@)", re.IGNORECASE)
_TRIGGER = re.compile(r"secret|passw|pwd|passcode|phrase|key|token|sk-|[sr]k_live|gh[pousr]_|"
                      r"github_pat|xox|AKIA|AIza|eyJ|Q~|PRIVATE|://|sig=|signature",
                      re.IGNORECASE)


def _credential(value: str) -> bool:
    if value.startswith("[") or set(value) <= set("*xX.-_•#"):
        return False                      # already masked, or a placeholder
    if value.isdigit() and len(value) < 16:
        return False                      # a count, a tracking or order number
    if len(value) >= 16:
        return True
    return any(c.isdigit() for c in value) or any(not c.isalnum() for c in value)


def _keep_label(match: re.Match[str]) -> str:
    value = match.group("value")
    if not _credential(value):
        return match.group(0)
    whole, start = match.group(0), match.start(0)
    return whole[: match.start("value") - start] + MASK + whole[match.end("value") - start:]


def mask_secrets(text: str) -> str:
    """``text`` with every credential-shaped value replaced by ``[secret]`` (module doc)."""
    if not text or not isinstance(text, str) or not _TRIGGER.search(text):
        return text
    out = _PEM.sub(MASK, text)
    out = _TOKENS.sub(MASK, out)
    out = _USERINFO.sub(_keep_label, out)
    out = _CONN.sub(_keep_label, out)
    out = _ENV.sub(_keep_label, out)
    out = _LABELLED_QUOTED.sub(_keep_label, out)
    return _LABELLED.sub(_keep_label, out)


def mask_secrets_in(value: Any) -> Any:
    """Every string inside a JSON-shaped value masked (mapping keys are kept)."""
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, dict):
        return {k: mask_secrets_in(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_secrets_in(v) for v in value]
    return value


__all__ = ["MASK", "mask_secrets", "mask_secrets_in"]
