"""Build the HS category table and the HS x origin tariff table from live read-only relations.

    python3 -m scripts.build_hs_tariff_tables OUT_DIR

Key and URL come from PLX_WORKBENCH_API_KEY / PLX_WORKBENCH_API_URL and are never printed.
Writes ``hs_category.json`` and ``hs_origin_tariff.json`` into OUT_DIR (a capture store,
never Git) and prints coverage counts only. GET only, through the Workbench read client.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from integrations.workbench_read_api import WorkbenchReadApiClient
from operating import hs_tariff as hs

COLUMNS = {
    "public.app_product": ["id", "category_id", "hs_code", "sage_category"],
    "sage.ic_item": ["itemno", "category", "tariffcode"],
    "public.raw_material_supplier": ["id", "rate_c", "tariff_c"],
    "public.packaging_supplier": ["id", "rate_c", "tariff_c"],
    "public.app_supplier_product": ["id", "product_id", "country_of_origin"],
}


def build(client: WorkbenchReadApiClient) -> tuple[dict, dict]:
    served = client.relations()
    rows = {}
    for name, wanted in COLUMNS.items():
        have = set(served.get(name, {}).get("columns") or ())
        # FileMaker hscode / countryoforigin are read when the API serves them.
        extra = [c for c in ("hscode", "countryoforigin") if c in have]
        use = [c for c in wanted + extra if c in have]
        rows[name] = client.read_relation(name, columns=use)["rows"] if use else []
    hs_obs, cats, rate_obs = hs.observations_from_rows(rows)
    return hs.build_hs_category_table(hs_obs, cats), hs.build_tariff_table(rate_obs)


def main(argv: list[str]) -> int:
    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    client = WorkbenchReadApiClient(os.environ["PLX_WORKBENCH_API_URL"],
                                    os.environ["PLX_WORKBENCH_API_KEY"])
    category, tariff = build(client)
    (out / "hs_category.json").write_text(json.dumps(category, indent=1), encoding="utf-8")
    (out / "hs_origin_tariff.json").write_text(json.dumps(tariff, indent=1), encoding="utf-8")
    print(json.dumps({"hs_category": category["coverage"], "tariff": tariff["coverage"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
