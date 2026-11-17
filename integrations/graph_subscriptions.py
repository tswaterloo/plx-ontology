"""Keeps the Graph change-notification subscriptions alive (AUD-05).

Mail and Teams subscriptions expire (mail at most 10,080 minutes, Teams chat and channel
messages at most 4,320). ``integrations.graph_notifications`` only receives; this renewer,
a separate short run (``plx-graph-subscriptions.timer``, every 10 minutes), keeps each
configured subscription registered:

* **renews** one whose remaining life is at most half of its configured lifetime (PATCH
  ``expirationDateTime`` to now + lifetime), well before expiry;
* **recreates** one that is missing or already expired (POST), matched by resource and
  notification URL in the app's own subscription list;
* **verifies** each write: the reply must carry an ``expirationDateTime`` later than the one
  held, else the entry is failed (``renewal_not_confirmed``), never assumed renewed;
* writes ``<state>/host/graph-subscriptions.json`` (names, states, counts, error codes and
  times only: no id, resource path, URL, client state or key) and
  ``operating.ops_alarms`` raises ``graph_subscription:<name>`` when an entry failed twice
  running or is within a quarter of its lifetime of expiry, and ``graph_subscriptions`` when
  the status is stale.

**Off unless configured**: without ``PLX_GRAPH_SUBS_CONFIG`` (a JSON file of the wanted
subscriptions, no secret in it) it prints ``{"status": "not_configured"}``, exits 0 and writes
nothing. The client state is the receiver's (``PLX_GRAPH_NOTIFY_CLIENT_STATE_SECRET``) and
the app credentials are ``AZURE_TENANT_ID`` / ``AZURE_CLIENT_ID`` / ``AZURE_CLIENT_SECRET``
from this unit's own environment, never a capture run's (the ingest strips them from its
children). Requests go only through ``integrations.graph_subscription_sender`` (pinned by the
HTTP-egress gate to the token URL and ``/subscriptions``); this module never deletes.

Config file::

    {"subscriptions": [{"name": "mail-ops", "source": "mail",
      "resource": "users/<id>/mailFolders('inbox')/messages",
      "notification_url": "EventHub:https://<vault>/keyVaults/<v>/secrets/<s>?tenantId=<t>",
      "lifecycle_url": "(required for Teams, same form)", "change_type": "created,updated",
      "lifetime_minutes": 4200}]}

``lifetime_minutes`` defaults to 4200 (mail) or 4000 (Teams) and may not exceed Graph's
maximum for the source.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote

from integrations import graph_subscription_sender as sender
from integrations.graph_notifications import ENV_CLIENT_STATE, ENV_TENANT, _write_json

ENV_CONFIG = "PLX_GRAPH_SUBS_CONFIG"
ENV_CLIENT_ID, ENV_CLIENT_SECRET = "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"
STATUS_FILE = Path("host") / "graph-subscriptions.json"
STATUS_SCHEMA = "plx-graph-subscriptions/v1"
MAX_MINUTES = {"mail": 10080, "teams": 4320}
DEFAULT_MINUTES = {"mail": 4200, "teams": 4000}
MIN_MINUTES = 60
RENEW_AT = 0.5                    # renew when this share of the lifetime or less remains
ALARM_AT = 0.25                   # ops alarm when this share or less remains
REQUEST_SECONDS = 15
Request = Callable[[str, str, Mapping[str, str], "bytes | None", float], "tuple[int, bytes]"]


_ID = r"(?:\('[^'/]+'\)|/[^/()]+)"
_RESOURCES = {          # a subscription's resource (no message id), by source
    "mail": re.compile(rf"^/?users{_ID}(?:/mailfolders{_ID})?/messages$", re.I),
    "teams": re.compile(
        rf"^/?(?:chats{_ID}/messages|chats/getallmessages|users{_ID}/chats/getallmessages|"
        rf"teams{_ID}/channels{_ID}/messages|teams/getallmessages|"
        rf"teams{_ID}/channels/getallmessages)$", re.I)}


def resource_ok(source: Any, resource: Any) -> bool:
    return source in _RESOURCES and isinstance(resource, str) and len(resource) <= 512 \
        and _RESOURCES[source].match(resource) is not None


class NotConfigured(Exception):
    """``PLX_GRAPH_SUBS_CONFIG`` is not set: the renewer does nothing."""


class SettingsInvalid(ValueError):
    """The config or credentials are malformed or incomplete (a code only)."""


@dataclass(frozen=True)
class Wanted:
    name: str
    source: str
    resource: str
    notification_url: str
    lifecycle_url: str | None
    change_type: str
    lifetime_minutes: int


@dataclass(frozen=True)
class Settings:
    wanted: tuple[Wanted, ...]
    client_state: str = field(repr=False)
    tenant: str = field(repr=False)
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Settings":
        path = (environ.get(ENV_CONFIG) or "").strip()
        if not path:
            raise NotConfigured("not_configured")
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
            rows = document["subscriptions"]
        except (OSError, ValueError, KeyError, TypeError):
            raise SettingsInvalid("config_unreadable") from None
        if not isinstance(rows, list) or not rows or len(rows) > 64:
            raise SettingsInvalid("config_subscriptions_invalid")
        wanted: list[Wanted] = []
        for row in rows:
            if not isinstance(row, dict):
                raise SettingsInvalid("config_entry_invalid")
            name, source = row.get("name"), row.get("source")
            resource, url = row.get("resource"), row.get("notification_url")
            life_url, kind = row.get("lifecycle_url"), row.get("change_type", "created,updated")
            minutes = row.get("lifetime_minutes", DEFAULT_MINUTES.get(source))
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name) \
                    or any(w.name == name for w in wanted):
                raise SettingsInvalid("config_name_invalid")
            if not resource_ok(source, resource):
                raise SettingsInvalid(f"config_resource_invalid:{name}")
            for value in (url, life_url):
                if value is not None and (not isinstance(value, str)
                                          or not value.startswith("EventHub:https://")
                                          or len(value) > 2048):
                    raise SettingsInvalid(f"config_url_invalid:{name}")
            if url is None or not isinstance(kind, str) \
                    or not re.fullmatch(r"[a-z]+(,[a-z]+){0,2}", kind):
                raise SettingsInvalid(f"config_entry_invalid:{name}")
            if type(minutes) is not int or not MIN_MINUTES <= minutes <= MAX_MINUTES[source]:
                raise SettingsInvalid(f"config_lifetime_invalid:{name}")
            if source == "teams" and minutes > MIN_MINUTES and not life_url:
                raise SettingsInvalid(f"config_lifecycle_url_required:{name}")
            if any(_norm(w.resource) == _norm(resource)
                   and _norm_url(w.notification_url) == _norm_url(url) for w in wanted):
                raise SettingsInvalid(f"config_duplicate_subscription:{name}")
            wanted.append(Wanted(name, source, resource, url, life_url, kind, minutes))
        state = (environ.get(ENV_CLIENT_STATE) or "").strip()
        if not 16 <= len(state) <= 128:
            raise SettingsInvalid("client_state_length")
        creds = [(environ.get(n) or "").strip() for n in
                 (ENV_TENANT, ENV_CLIENT_ID, ENV_CLIENT_SECRET)]
        if not all(creds):
            raise SettingsInvalid("credentials_incomplete")
        return cls(tuple(wanted), state, *creds)


def _parse(stamp: Any) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", stamp.strip().replace("Z", "+00:00"))
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _norm(resource: Any) -> str:
    """A resource path in one form: decoded, lower case, no slashes at the ends, no
    ``https://graph.microsoft.com`` host and no ``v1.0``/``beta`` prefix."""
    text = unquote(unquote(str(resource).strip())).strip().strip("/").lower()
    text = re.sub(r"^https://graph\.microsoft\.com/", "", text).strip("/")
    return re.sub(r"^(?:v1\.0|beta)/", "", text).strip("/")


def _norm_url(url: Any) -> str:
    """A notification URL in one form (decoded, lower case, no trailing slash), so Graph
    echoing it differently never makes the renewer create another subscription."""
    return unquote(unquote(str(url).strip())).strip().rstrip("/").lower()


class Renewer:
    """One pass over the wanted subscriptions. ``request`` is injected in tests."""

    def __init__(self, settings: Settings, state: Path, request: Request | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.settings, self.state, self.now = settings, Path(state), now
        self.request = request or sender.send_subscription
        self._token: str | None = None

    # -- transport ---------------------------------------------------------------------
    def _call(self, method: str, url: str, document: Mapping[str, Any] | None = None
              ) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {self._bearer()}", "Accept": "application/json"}
        body = None
        if document is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(document).encode()
        status, raw = self._send(method, url, headers, body)
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, {}

    def _send(self, method, url, headers, body) -> tuple[int, bytes]:
        try:
            return self.request(method, url, headers, body, REQUEST_SECONDS)
        except sender.SenderRefused:
            raise _Failed("request_refused") from None
        except Exception:  # noqa: BLE001 - no connection, timeout: a code, never a message
            raise _Failed("graph_unreachable") from None

    def _bearer(self) -> str:
        if self._token:
            return self._token
        from urllib.parse import quote, urlencode
        s = self.settings
        status, raw = self._send(
            "POST", f"{sender.TOKEN_PREFIX}{quote(s.tenant, safe='')}/oauth2/v2.0/token",
            {"Content-Type": "application/x-www-form-urlencoded"},
            urlencode({"grant_type": "client_credentials", "client_id": s.client_id,
                       "client_secret": s.client_secret,
                       "scope": "https://graph.microsoft.com/.default"}).encode())
        try:
            token = json.loads(raw or b"{}").get("access_token") if status == 200 else None
        except (ValueError, AttributeError):
            token = None
        if not isinstance(token, str) or not token:
            raise _Failed("token_refused")
        self._token = token
        return token

    # -- one pass ----------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        now = self.now()
        previous = self._previous()
        entries: dict[str, dict[str, Any]] = {}
        listed: list[Mapping[str, Any]] | None = None
        truncated = False
        error: str | None = None
        try:
            status, body = self._call("GET", sender.SUBSCRIPTIONS_URL)
            value = body.get("value") if isinstance(body, dict) else None
            if status != 200 or not isinstance(value, list):
                raise _Failed(f"list_refused_http_{status}")
            listed = [v for v in value if isinstance(v, dict)]
            truncated = bool(body.get("@odata.nextLink"))
        except _Failed as failed:
            error = failed.code
        for want in self.settings.wanted:
            held = previous.get(want.name) or {}
            entry: dict[str, Any] = {
                "source": want.source, "lifetime_minutes": want.lifetime_minutes,
                "last_ok_at": held.get("last_ok_at"), "expires_at": held.get("expires_at"),
                "failures": int(held.get("failures") or 0)}
            try:
                if listed is None:
                    raise _Failed(error or "list_unavailable")
                entry.update(self._one(want, listed, truncated, now))
                entry["failures"], entry["last_ok_at"] = 0, _iso(now)
                entry.pop("error", None)
            except _Failed as failed:
                entry.update(state="failed", error=failed.code,
                             failures=entry["failures"] + 1)
            entries[want.name] = entry
        status_doc = {"schema": STATUS_SCHEMA, "checked_at": _iso(now), "entries": entries}
        _write_json(self.state / STATUS_FILE, status_doc)
        return status_doc

    def _previous(self) -> dict[str, Any]:
        try:
            held = json.loads((self.state / STATUS_FILE).read_text(encoding="utf-8"))
            entries = held["entries"]
            return entries if isinstance(entries, dict) else {}
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def _one(self, want: Wanted, listed: Sequence[Mapping[str, Any]], truncated: bool,
             now: datetime) -> dict[str, Any]:
        mine = [s for s in listed if _norm(s.get("resource")) == _norm(want.resource)
                and _norm_url(s.get("notificationUrl")) == _norm_url(want.notification_url)
                and isinstance(s.get("id"), str)
                and _parse(s.get("expirationDateTime")) is not None]
        live = [s for s in mine if _parse(s["expirationDateTime"]) > now]
        if not live:
            if truncated:            # the list was cut short: never create a duplicate
                raise _Failed("list_truncated")
            return self._create(want, now)
        best = max(live, key=lambda s: _parse(s["expirationDateTime"]))
        expires = _parse(best["expirationDateTime"])
        if expires - now > timedelta(minutes=want.lifetime_minutes * RENEW_AT):
            return {"state": "ok", "expires_at": _iso(expires)}
        target = now + timedelta(minutes=want.lifetime_minutes)
        status, body = self._call("PATCH", f"{sender.SUBSCRIPTIONS_URL}/{best['id']}",
                                  {"expirationDateTime": _iso(target)})
        if status == 404:
            return self._create(want, now)
        if status != 200:
            raise _Failed(f"renew_refused_http_{status}")
        got = _parse(body.get("expirationDateTime") if isinstance(body, dict) else None)
        if got is None or got <= expires:
            raise _Failed("renewal_not_confirmed")
        return {"state": "renewed", "expires_at": _iso(got)}

    def _create(self, want: Wanted, now: datetime) -> dict[str, Any]:
        document = {"changeType": want.change_type, "notificationUrl": want.notification_url,
                    "resource": want.resource, "clientState": self.settings.client_state,
                    "expirationDateTime": _iso(now + timedelta(minutes=want.lifetime_minutes))}
        if want.lifecycle_url:
            document["lifecycleNotificationUrl"] = want.lifecycle_url
        status, body = self._call("POST", sender.SUBSCRIPTIONS_URL, document)
        got = _parse(body.get("expirationDateTime") if isinstance(body, dict) else None)
        if status not in (200, 201):
            raise _Failed(f"create_refused_http_{status}")
        if got is None or got <= now or not isinstance(body.get("id"), str):
            raise _Failed("creation_not_confirmed")
        return {"state": "created", "expires_at": _iso(got)}


class _Failed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _record_config_error(state: Path, code: str, now: datetime) -> None:
    """An invalid config on any run, the very first included, is written to the status file
    (``config_error``: a code only) so ``operating.ops_alarms`` raises
    ``graph_subscriptions_config``; the next valid pass clears it. Entries held from earlier
    passes are kept, so their expiry alarms go on."""
    held = Renewer._previous(types.SimpleNamespace(state=state))
    try:
        _write_json(state / STATUS_FILE, {"schema": STATUS_SCHEMA, "checked_at": _iso(now),
                                          "entries": held, "config_error": code[:128]})
    except OSError:
        pass


def alarm_entries(state: Path, now: datetime) -> list[dict[str, Any]] | None:
    """What ``operating.ops_alarms`` reads: None when the renewer never ran (inert), else
    ``[{"name", "failures", "minutes_left", "lifetime_minutes", "error"}]`` plus a first row
    ``{"name": None, "age_minutes": ...}`` for the status file's age."""
    try:
        held = json.loads((state / STATUS_FILE).read_text(encoding="utf-8"))
        entries = held["entries"]
        made = _parse(held.get("checked_at"))
        if not isinstance(entries, dict) or made is None:
            raise TypeError("entries")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    out: list[dict[str, Any]] = [{"name": None,
                                  "age_minutes": (now - made).total_seconds() / 60,
                                  "config_error": str(held.get("config_error") or "")[:128]}]
    for name, row in sorted(entries.items()):
        if not isinstance(row, dict):
            continue
        expires = _parse(row.get("expires_at"))
        life = row.get("lifetime_minutes")
        out.append({"name": str(name), "failures": row.get("failures") if type(
            row.get("failures")) is int else 0,
            "minutes_left": None if expires is None else (expires - now).total_seconds() / 60,
            "lifetime_minutes": life if type(life) is int and life > 0 else None,
            "error": str(row.get("error") or "")[:64]})
    return out


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None,
         request: Request | None = None,
         now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> int:
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="python3 -m integrations.graph_subscriptions")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("once", "status"):
        sub.add_parser(name).add_argument("--state", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "status":
        try:
            print((args.state / STATUS_FILE).read_text(encoding="utf-8").strip())
        except OSError:
            print(json.dumps({"status": "no_status"}))
        return 0
    try:
        settings = Settings.from_env(environ)
    except NotConfigured:
        print(json.dumps({"status": "not_configured"}))
        return 0
    except SettingsInvalid as error:
        _record_config_error(args.state, str(error), now())
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 2
    done = Renewer(settings, args.state, request, now).run()
    failed = sorted(n for n, e in done["entries"].items() if e["state"] == "failed")
    print(json.dumps({"status": "failed" if failed else "ok", "failed": failed,
                      "states": {n: e["state"] for n, e in sorted(done["entries"].items())}},
                     sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
