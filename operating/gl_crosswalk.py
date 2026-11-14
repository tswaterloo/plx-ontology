"""The Sage -> BC G/L account crosswalk a person prepared (V-19, ask 19), read from its
workbook into one canonical reference file, and checked against the served ontology.

The workbook (an ``.xlsx`` the finance owner hands over; this module reads it with the
standard library: the package's XML, shared or inline strings) has three sheets this reads:

* ``Keeping_or_refactoring``: one row per active Sage account (``action``, ``sage_acctid``,
  ``sage_name``, ``bc_acctid``, ``bc_name``, ``change_type``, ``sage_active``,
  ``acctfmttd``, ``defcurncod``). A row with a ``bc_acctid`` maps the Sage account to that
  shared-chart account (kept, renumbered or renamed); one without is an account to be added
  to the shared chart, so it maps to nothing yet. Each currency version of a Sage account
  (``-01`` CAD, ``-02`` USD) is its own row and keeps its own currency: two Sage accounts may
  map to one shared-chart number.
* ``Adding_new``: shared-chart accounts with no Sage account.
* ``Dropping_not_ported``: inactive Sage accounts not ported.

``load`` writes the canonical file (``plx-gl-crosswalk/v1``) with the workbook's sha256, so
every mapping cites the exact file it came from. The file holds account numbers and names
only (the workbook has no balances) and lives in the Workbench state directory, never in
Git. Nothing here writes to a business system or to the served store; ``check`` only reads.

Refused (``CrosswalkError``): a sheet or a header column missing, a mapped row without a
Sage account number, one Sage account mapped twice, a currency other than an ISO 4217 code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

FORMAT = "plx-gl-crosswalk/v1"
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
SHEETS = {
    "mapped": ("Keeping_or_refactoring",
               ("action", "sage_acctid", "sage_name", "bc_acctid", "bc_name", "change_type",
                "sage_active", "acctfmttd", "defcurncod")),
    "new_on_chart": ("Adding_new", ("bc_acctid", "bc_name")),
    "not_ported": ("Dropping_not_ported", ("sage_acctid", "sage_name", "acctfmttd")),
}
MAX_BYTES = 20 << 20
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class CrosswalkError(ValueError):
    pass


def _cells(path: Path) -> dict[str, list[list[str]]]:
    """Every sheet's rows as lists of cell text (columns in order, gaps as "")."""
    if path.stat().st_size > MAX_BYTES:
        raise CrosswalkError("workbook_too_large")
    try:
        book = zipfile.ZipFile(path)
        workbook = ET.fromstring(book.read("xl/workbook.xml"))
        rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise CrosswalkError(f"not_an_xlsx_workbook:{type(exc).__name__}") from None
    targets = {rel.get("Id"): rel.get("Target") for rel in rels}
    shared: list[str] = []
    if "xl/sharedStrings.xml" in book.namelist():
        strings = ET.fromstring(book.read("xl/sharedStrings.xml"))
        # a shared string's own text runs only (``t``, ``r/t``), never phonetic ``rPh`` runs
        shared = ["".join(t.text or "" for t in [*item.findall("m:t", _NS),
                                                   *item.findall("m:r/m:t", _NS)])
                  for item in strings.findall("m:si", _NS)]
    out: dict[str, list[list[str]]] = {}
    sheets = workbook.find("m:sheets", _NS)
    for sheet in sheets if sheets is not None else ():
        target = (targets.get(sheet.get(_REL)) or "").lstrip("/")
        target = target if target.startswith("xl/") else f"xl/{target}"
        try:
            root = ET.fromstring(book.read(target))
        except (KeyError, ET.ParseError):
            raise CrosswalkError(f"sheet_unreadable:{sheet.get('name')}") from None
        rows = []
        for row in root.iter(f"{{{_NS['m']}}}row"):
            cells: dict[int, str] = {}
            for cell in row.findall("m:c", _NS):
                column = re.match(r"[A-Z]+", cell.get("r") or "")
                if not column:
                    continue
                index = 0
                for char in column.group():
                    index = index * 26 + ord(char) - 64
                kind = cell.get("t")
                if kind == "inlineStr":
                    text = "".join(t.text or "" for t in cell.iter(f"{{{_NS['m']}}}t"))
                else:
                    value = cell.find("m:v", _NS)
                    text = value.text if value is not None and value.text is not None else ""
                    if kind == "s" and text:
                        text = shared[int(text)]
                cells[index - 1] = text.strip()
            rows.append([cells.get(i, "") for i in range(max(cells, default=-1) + 1)])
        out[sheet.get("name") or ""] = rows
    return out


def _table(rows: Sequence[Sequence[str]], sheet: str, columns: Sequence[str]
           ) -> list[dict[str, str]]:
    """The rows under the first row naming every one of ``columns`` (title and note rows
    above it are skipped), as mappings; blank rows dropped."""
    for at, row in enumerate(rows):
        names = [cell.strip().lower() for cell in row]
        if all(column in names for column in columns):
            index = {name: i for i, name in enumerate(names) if name}
            out = []
            for body in rows[at + 1:]:
                record = {name: (body[i] if i < len(body) else "").strip()
                          for name, i in index.items()}
                if any(record.values()):
                    out.append(record)
            return out
    raise CrosswalkError(f"header_missing:{sheet}:{','.join(columns)}")


def read_workbook(path: str | Path) -> dict[str, Any]:
    """The canonical crosswalk of the workbook at ``path``."""
    path = Path(path)
    sheets = _cells(path)
    tables = {}
    for key, (sheet, columns) in SHEETS.items():
        if sheet not in sheets:
            raise CrosswalkError(f"sheet_missing:{sheet}")
        tables[key] = _table(sheets[sheet], sheet, columns)
    mapped, added, seen = [], [], set()
    for row in tables["mapped"]:
        sage = row["sage_acctid"]
        if not sage:
            raise CrosswalkError("mapped_row_without_sage_account")
        if sage in seen:
            raise CrosswalkError(f"sage_account_listed_twice:{sage}")
        seen.add(sage)
        currency = row["defcurncod"]
        if not _CURRENCY.match(currency):
            raise CrosswalkError(f"currency_not_iso_4217:{sage}")
        item = {"sage_acctid": sage, "sage_formatted": row["acctfmttd"],
                "sage_name": row["sage_name"], "currency": currency,
                "sage_active": row["sage_active"], "action": row["action"],
                "change_type": row["change_type"]}
        if row["bc_acctid"]:
            mapped.append({**item, "bc_acctid": row["bc_acctid"], "bc_name": row["bc_name"]})
        else:
            added.append(item)
    return {"format": FORMAT,
            "source": {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
            "mapped": mapped, "added_to_chart": added,
            "new_on_chart": [{"bc_acctid": r["bc_acctid"], "bc_name": r["bc_name"]}
                             for r in tables["new_on_chart"] if r["bc_acctid"]],
            "not_ported": [{"sage_acctid": r["sage_acctid"], "sage_name": r["sage_name"],
                            "sage_formatted": r["acctfmttd"]}
                           for r in tables["not_ported"] if r["sage_acctid"]]}


def links(crosswalk: Mapping[str, Any], sage_accounts: Mapping[str, str],
          bc_accounts: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """The Sage-to-BC account links the crosswalk states, over the served accounts:
    ``sage_accounts`` (Sage account number -> object id) and ``bc_accounts`` (BC account
    number -> object ids, one per BC company). A mapping whose Sage or BC account is not
    served is listed (``unmatched``), never guessed; an account the crosswalk adds to the
    shared chart has no BC account yet and no link."""
    out, unmatched = [], []
    for item in crosswalk["mapped"]:
        sage = sage_accounts.get(item["sage_acctid"])
        targets = list(bc_accounts.get(item["bc_acctid"], ()))
        if sage is None or not targets:
            unmatched.append({"sage_acctid": item["sage_acctid"], "bc_acctid": item["bc_acctid"],
                              "sage_served": sage is not None, "bc_served": bool(targets)})
            continue
        for target in targets:
            out.append({"from": sage, "to": target, "sage_acctid": item["sage_acctid"],
                        "bc_acctid": item["bc_acctid"], "currency": item["currency"],
                        "change_type": item["change_type"],
                        "evidence": {"crosswalk_sha256": crosswalk["source"]["sha256"],
                                     "file": crosswalk["source"]["file"]}})
    return {"links": out, "unmatched": unmatched,
            "mapped": len(crosswalk["mapped"]), "added_to_chart": len(crosswalk["added_to_chart"])}


def _served(dsn: str | None) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Sage and BC G/L accounts of the served ontology (Postgres), read only."""
    from operating.pg.pool import connect
    sql = ("SELECT o.object_id, btrim(v.value, '\" ') FROM ontology.objects o "
           "JOIN ontology.object_values v ON v.object_id = o.object_id "
           "AND v.superseded_at IS NULL WHERE o.object_type = %s AND v.property = %s "
           "AND o.missing_from_source_since IS NULL")
    sage: dict[str, str] = {}
    bc: dict[str, list[str]] = {}
    with connect(dsn) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        for oid, number in conn.execute(sql, ("RelSageGlAccount", "acctid")):
            sage[number] = oid
        for oid, number in conn.execute(sql, ("GlAccount", "number")):
            bc.setdefault(number, []).append(oid)
    return sage, bc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.gl_crosswalk")
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load", help="workbook -> canonical crosswalk file")
    load.add_argument("workbook")
    load.add_argument("--out", required=True)
    check = sub.add_parser("check", help="the links the crosswalk states, over served PG")
    check.add_argument("crosswalk")
    check.add_argument("--dsn", default=None, help="default: PLX_PG_DSN (never printed)")
    check.add_argument("--sample", type=int, default=10)
    args = parser.parse_args(argv)
    if args.command == "load":
        data = read_workbook(args.workbook)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # owner-only from birth
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(out)
        print(json.dumps({"out": str(out), "sha256": data["source"]["sha256"],
                          "mapped": len(data["mapped"]),
                          "added_to_chart": len(data["added_to_chart"]),
                          "new_on_chart": len(data["new_on_chart"]),
                          "not_ported": len(data["not_ported"])}))
        return 0
    data = json.loads(Path(args.crosswalk).read_text(encoding="utf-8"))
    if data.get("format") != FORMAT:
        raise CrosswalkError("not_a_crosswalk_file")
    sage, bc = _served(args.dsn)
    found = links(data, sage, bc)
    currencies: dict[str, int] = {}
    for item in data["mapped"]:
        currencies[item["currency"]] = currencies.get(item["currency"], 0) + 1
    print(json.dumps({"links": len(found["links"]), "mapped": found["mapped"],
                      "unmatched": found["unmatched"], "added_to_chart": found["added_to_chart"],
                      "mapped_by_currency": currencies,
                      "sample": found["links"][::max(1, len(found["links"])
                                                     // max(1, args.sample))][:args.sample]},
                     indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
