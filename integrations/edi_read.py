"""EDI reader: ANSI X12 850 (purchase order), 855 (purchase order acknowledgment) and 856
(advance ship notice) files from a drop folder, read-only and off until configured.

Customers who order over EDI have their documents land as files; this reader parses what is
in ``PLX_EDI_DROP_DIR`` (set it to turn the source on; unset: disabled, nothing is read).
Nothing is ever written, moved or deleted in that folder. A remote source (SFTP) is the
pluggable ``RemoteFiles`` protocol (``list_names``/``read``) a deployment supplies; this
repository ships none and imports no FTP or SSH library (the egress gate bans them), so the
partner's SFTP mirror is an operator task that lands files in the drop folder.

Parsing (``parse_interchange``) is strict and fails closed: the separators come from the
fixed-width ``ISA`` segment (element = byte 4, sub-element = byte 105, terminator = byte
106); ISA/IEA, GS/GE and ST/SE control numbers and counts must agree; an unknown
transaction set is counted, never guessed. A file that fails is refused whole with its reason
(``refused``: reason -> count); no partial interchange is returned.

Ontology (``BINDINGS``): 850 -> ``SalesOrder`` (customer PO number, order date, lines ->
``SalesOrderLine``), 856 -> ``Shipment`` (shipment id, carrier, tracking/PRO, customer PO),
855 -> an acknowledgment that joins its order by customer PO (no ontology type of its own).
Quantities are Decimal; a price is kept with the set's ``CUR`` currency, and no ``CUR`` means
no currency, never defaulted. The sender and receiver ids are the trading partner's identity
and the only customer join key (never a name match).

Processed files are tracked by SHA-256 of their bytes in the sweep state, so a re-dropped
identical file is not read twice. Fixtures follow the public X12 layouts; live-unverified
until a real partner file has passed ``edi.drop_folder``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Protocol, Sequence

from . import capture_vendor as cv
from . import vendor_sweep as vs
from .http import SourceUnavailable

SOURCE = "edi"
SETTING_DROP_DIR = "PLX_EDI_DROP_DIR"
REQUIRED_SETTINGS = (SETTING_DROP_DIR,)
AUDIENCE_UNIT = "edi:drop"
MAX_FILE_BYTES = 5_000_000
MAX_FILES_PER_SWEEP = 500
MAX_REMEMBERED = 20_000
SUFFIXES = (".edi", ".x12", ".txt", ".850", ".855", ".856")
SETS = ("850", "855", "856")
_DECIMAL = re.compile(r"-?[0-9]{1,15}(?:\.[0-9]{1,6})?")
_CURRENCY = re.compile(r"[A-Z]{3}")

BINDINGS: dict[str, dict[str, Any]] = {
    "EdiPurchaseOrder": {"ontology_type": "SalesOrder", "join_key": "customer_po",
                         "properties": {"customer_po": "customer_po",
                                        "order_date": "order_date"}},
    "EdiPurchaseOrderLine": {"ontology_type": "SalesOrderLine", "join_key": "customer_po",
                             "properties": {"ordered_quantity": "ordered_quantity",
                                            "unit_price": "unit_price", "uom": "uom"}},
    "EdiOrderAcknowledgment": {"ontology_type": None, "properties": {},
                               "join_key": "customer_po",
                               "why_unbound": "our acknowledgment of a customer PO; joins "
                                              "the order by customer PO"},
    "EdiShipNotice": {"ontology_type": "Shipment", "join_key": "customer_po",
                      "properties": {"shipment_number": "shipment_number",
                                     "ship_date": "ship_date", "carrier": "carrier",
                                     "tracking_number": "tracking_number",
                                     "customer_po": "customer_po"}},
}


class EdiRefused(ValueError):
    """The file is not a well-formed interchange; ``str(exc)`` is the reason code."""


class RemoteFiles(Protocol):
    """A deployment-supplied remote source (SFTP): names and bytes, read only."""

    def list_names(self) -> Sequence[str]: ...

    def read(self, name: str, max_bytes: int) -> bytes: ...


def _dec(value: Any) -> str | None:
    if not (isinstance(value, str) and _DECIMAL.fullmatch(value.strip())):
        return None
    try:
        return format(Decimal(value.strip()), "f")
    except InvalidOperation:
        return None


def _date(value: str | None) -> str | None:
    if value and re.fullmatch(r"(?:[0-9]{2})?[0-9]{6}", value):
        v = value if len(value) == 8 else "20" + value
        try:
            return datetime.strptime(v, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    return None


def segments(raw: str) -> tuple[list[list[str]], str]:
    """``(segments as element lists, sub-element separator)`` from the fixed-width ISA."""
    if not raw.startswith("ISA") or len(raw) < 106:
        raise EdiRefused("no_isa")
    element, sub, term = raw[3], raw[104], raw[105]
    if len({element, sub, term}) != 3 or any(c.isalnum() for c in (element, sub, term)):
        raise EdiRefused("separators_invalid")
    parts = [s.strip("\r\n ") for s in raw.split(term)]
    out = [p.split(element) for p in parts if p]
    if not out or out[0][0] != "ISA" or len(out[0]) != 17:
        raise EdiRefused("isa_invalid")
    return out, sub


def _el(seg: Sequence[str], n: int) -> str | None:
    return seg[n].strip() or None if len(seg) > n else None


def _parse_850(body: Sequence[list[str]], base: dict[str, Any]) -> list[dict[str, Any]]:
    beg = next((s for s in body if s[0] == "BEG"), None)
    po = _el(beg, 3) if beg else None
    if po is None:
        raise EdiRefused("850_no_po_number")
    cur = next((_el(s, 2) for s in body if s[0] == "CUR"), None)
    currency = cur if cur and _CURRENCY.fullmatch(cur) else None
    ship_to = next(({"name": _el(s, 2), "id": _el(s, 4)} for s in body
                    if s[0] == "N1" and _el(s, 1) == "ST"), None)
    order = {**base, "record_type": "EdiPurchaseOrder", "ontology_type": "SalesOrder",
             "citation": base["citation"] + f"/po/{po}",
             "join_keys": {**base["join_keys"], "customer_po": po},
             "properties": {"customer_po": po, "order_date": _date(_el(beg, 5)),
                            "purpose": _el(beg, 1), "currency": currency},
             "ship_to": ship_to}
    out = [order]
    for s in body:
        if s[0] != "PO1":
            continue
        ids = {q: _el(s, i + 1) for i in range(6, len(s) - 1, 2)
               if (q := _el(s, i)) in ("VP", "BP", "UP", "UK", "SK")}
        price = _dec(_el(s, 4))
        out.append({**base, "record_type": "EdiPurchaseOrderLine",
                    "ontology_type": "SalesOrderLine",
                    "citation": f"{order['citation']}/line/{_el(s, 1) or len(out)}",
                    "join_keys": {**base["join_keys"], "customer_po": po},
                    "properties": {"ordered_quantity": _dec(_el(s, 2)), "uom": _el(s, 3),
                                   "unit_price": ({"amount": price, "currency": currency}
                                                  if price is not None and currency else None)},
                    "product_ids": ids})
    if len(out) == 1:
        raise EdiRefused("850_no_lines")
    return out


def _parse_855(body: Sequence[list[str]], base: dict[str, Any]) -> list[dict[str, Any]]:
    bak = next((s for s in body if s[0] == "BAK"), None)
    po = _el(bak, 3) if bak else None
    if po is None:
        raise EdiRefused("855_no_po_number")
    acks = [{"status": _el(s, 1), "quantity": _dec(_el(s, 2)),
             "uom": _el(s, 3)} for s in body if s[0] == "ACK"]
    return [{**base, "record_type": "EdiOrderAcknowledgment", "ontology_type": None,
             "citation": base["citation"] + f"/ack/{po}",
             "join_keys": {**base["join_keys"], "customer_po": po},
             "properties": {"customer_po": po, "purpose": _el(bak, 1),
                            "ack_type": _el(bak, 2), "ack_date": _date(_el(bak, 4))},
             "lines": acks}]


def _parse_856(body: Sequence[list[str]], base: dict[str, Any]) -> list[dict[str, Any]]:
    bsn = next((s for s in body if s[0] == "BSN"), None)
    shipment = _el(bsn, 2) if bsn else None
    if shipment is None:
        raise EdiRefused("856_no_shipment_id")
    pos = sorted({v for s in body if s[0] == "PRF" and (v := _el(s, 1))})
    refs = {q: v for s in body if s[0] == "REF" and (q := _el(s, 1)) and (v := _el(s, 2))}
    td5 = next((s for s in body if s[0] == "TD5"), None)
    lines = [{"quantity": _dec(_el(s, 2)), "uom": _el(s, 3)} for s in body if s[0] == "SN1"]
    return [{**base, "record_type": "EdiShipNotice", "ontology_type": "Shipment",
             "citation": base["citation"] + f"/asn/{shipment}",
             # one PO joins; several POs in one notice are ambiguous and never join
             "join_keys": {**base["join_keys"], "shipment_number": shipment,
                           "customer_po": pos[0] if len(pos) == 1 else None,
                           "ambiguous_customer_pos": len(pos) > 1},
             "properties": {"shipment_number": shipment, "ship_date": _date(_el(bsn, 3)),
                            "carrier": (_el(td5, 5) or _el(td5, 3)) if td5 else None,
                            "tracking_number": refs.get("CN") or refs.get("BM"),
                            "customer_po": pos[0] if len(pos) == 1 else None},
             "purchase_orders": pos, "lines": lines}]


_PARSERS = {"850": _parse_850, "855": _parse_855, "856": _parse_856}


def parse_interchange(data: bytes, *, name: str = "file") -> tuple[list[dict[str, Any]],
                                                                  dict[str, int]]:
    """``(records, skipped sets)`` of one interchange; ``EdiRefused`` for any malformed one."""
    if len(data) > MAX_FILE_BYTES:
        raise EdiRefused("too_large")
    try:
        raw = data.decode("ascii")
    except UnicodeDecodeError:
        raise EdiRefused("not_ascii") from None
    segs, _sub = segments(raw)
    isa, iea = segs[0], segs[-1]
    if iea[0] != "IEA" or _el(iea, 2) != _el(isa, 13):
        raise EdiRefused("iea_missing_or_control_mismatch")
    sender, receiver = (_el(isa, 6) or "", _el(isa, 8) or "")
    digest = hashlib.sha256(data).hexdigest()
    records: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    groups = 0
    i = 1
    while i < len(segs) - 1:
        gs = segs[i]
        if gs[0] != "GS":
            raise EdiRefused("expected_gs")
        j = next((k for k in range(i + 1, len(segs)) if segs[k][0] == "GE"), None)
        if j is None or _el(segs[j], 2) != _el(gs, 6):
            raise EdiRefused("ge_missing_or_control_mismatch")
        sets, k = 0, i + 1
        while k < j:
            st = segs[k]
            if st[0] != "ST":
                raise EdiRefused("expected_st")
            e = next((m for m in range(k + 1, j + 1) if segs[m][0] == "SE"), None)
            if e is None or _el(segs[e], 2) != _el(st, 2):
                raise EdiRefused("se_missing_or_control_mismatch")
            if _el(segs[e], 1) != str(e - k + 1):
                raise EdiRefused("se_segment_count_wrong")
            kind = _el(st, 1) or ""
            base = {"source": SOURCE, "audience_unit": AUDIENCE_UNIT,
                    "citation": f"edi://{digest[:16]}/{_el(isa, 13)}/{_el(st, 2)}",
                    "join_keys": {"trading_partner_sender": sender.strip(),
                                  "trading_partner_receiver": receiver.strip(),
                                  "edi_file_sha256": digest},
                    "set": kind, "file": name}
            if kind in _PARSERS:
                records.extend(_PARSERS[kind](segs[k + 1:e], base))
            else:
                skipped[kind or "?"] = skipped.get(kind or "?", 0) + 1
            sets += 1
            k = e + 1
        if _el(segs[j], 1) != str(sets):
            raise EdiRefused("ge_set_count_wrong")
        groups += 1
        i = j + 1
    if _el(iea, 1) != str(groups):
        raise EdiRefused("iea_group_count_wrong")
    return records, skipped


class DropFolder:
    """The configured folder as a read-only ``RemoteFiles``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def list_names(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_file() and not p.is_symlink() and p.suffix.lower() in SUFFIXES)

    def read(self, name: str, max_bytes: int) -> bytes:
        path = self.root / name
        if "/" in name or "\\" in name or path.is_symlink() or not path.is_file():
            raise SourceUnavailable("edi file name refused")
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
        return data


def sweep(environ: Mapping[str, str] | None = None, *, state_path: str | Path,
          source: RemoteFiles | None = None, now: datetime | None = None,
          store: Any = None) -> dict[str, Any]:
    """One bounded read of the drop. With ``store`` the records are saved (unit
    ``edi:drop``) before any file is remembered as processed: a failed save remembers
    nothing, so the same files are read again next sweep."""
    env = os.environ if environ is None else environ
    state = vs.SweepState(state_path)
    injected = source is not None
    if source is None:
        missing = vs.missing_settings(REQUIRED_SETTINGS, env)
        if missing:
            return {"source": SOURCE, "blocked": "credentials_absent",
                    "missing_settings": missing, "requests_sent": 0, "evidence_label": "not-run"}
        folder = Path(env[SETTING_DROP_DIR].strip())
        if not folder.is_dir():
            return {"source": SOURCE, "blocked": "setting_invalid",
                    "invalid_settings": [SETTING_DROP_DIR], "requests_sent": 0,
                    "evidence_label": "not-run"}
        source = DropFolder(folder)
    from datetime import timezone
    now = now or datetime.now(timezone.utc)
    entry = state.get(SOURCE)
    seen = list(entry.get("processed") or [])
    done = set(seen)
    records: list[dict[str, Any]] = []
    refused: dict[str, int] = {}
    skipped: dict[str, int] = {}
    read_files = 0
    complete = True
    for name in source.list_names():
        if read_files >= MAX_FILES_PER_SWEEP:
            complete = False
            break
        data = source.read(name, MAX_FILE_BYTES)
        digest = hashlib.sha256(data).hexdigest()
        if digest in done:
            continue
        read_files += 1
        try:
            found, skip = parse_interchange(data, name=name)
        except EdiRefused as exc:
            refused[str(exc)] = refused.get(str(exc), 0) + 1
            # a refused file is remembered too: it is reported once, not every sweep
        else:
            records.extend(found)
            for k, v in skip.items():
                skipped[k] = skipped.get(k, 0) + v
        done.add(digest)
        seen.append(digest)
    capture = None
    if store is not None:
        try:
            capture = cv.save(store, cv.EDI_KIND, records, lambda _r: "drop",
                              binding=SOURCE + ":" + getattr(source, "binding", "drop"),
                              source_api="edi.x12")
        except Exception as exc:  # noqa: BLE001 - a failed save remembers no file
            state.put(SOURCE, {**entry, "last_run_at": vs._iso(now),
                               "last_error_at": vs._iso(now), "last_error": "capture_failed"})
            return {"source": SOURCE, "records": records, "complete": False,
                    "error": "capture_failed",
                    "capture_error": str(getattr(exc, "code", "") or type(exc).__name__)[:80],
                    "files_read": read_files, "requests_sent": 0,
                    "freshness": vs.freshness(state.get(SOURCE), now),
                    "evidence_label": "injected-transport" if injected else "live-read"}
    saved = {**entry, "processed": seen[-MAX_REMEMBERED:], "last_run_at": vs._iso(now)}
    if complete:
        saved["last_complete_at"] = vs._iso(now)
    state.put(SOURCE, saved)
    out_capture = {"capture": capture} if capture is not None else {}
    return {**out_capture, "source": SOURCE, "records": records, "complete": complete,
            "refused": refused,
            "skipped_sets": skipped, "files_read": read_files, "requests_sent": 0,
            "freshness": vs.freshness(state.get(SOURCE), now),
            "evidence_label": "injected-transport" if injected else "live-read"}


def probe(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        result = sweep(environ, state_path=Path(tmp) / "state.json")
    records = result.pop("records", [])
    for kind in ("EdiPurchaseOrder", "EdiOrderAcknowledgment", "EdiShipNotice"):
        result[kind] = sum(1 for r in records if r["record_type"] == kind)
    result["ok"] = "blocked" not in result
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["probe"]:
        print("usage: python3 -m integrations.edi_read probe", file=sys.stderr)
        return 2
    result = probe()
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") or result.get("blocked") else 1


if __name__ == "__main__":
    raise SystemExit(main())
