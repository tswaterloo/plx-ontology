"""Write or check the raw money profile (``ontology.money_profile``): see its module doc.

    python3 -m scripts.gen_raw_money_profile --mirror <source-mirror.sqlite>
    python3 -m scripts.gen_raw_money_profile --state-dir <STATE> [--check]
"""

from ontology.money_profile import main

if __name__ == "__main__":
    raise SystemExit(main())
