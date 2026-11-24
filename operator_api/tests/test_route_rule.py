"""The route rule (speed specs 1.3): every GET route of the API is listed in
``operator_api.route_budgets`` with its family, budget and piece, and a route listed as answering
from the served store or the door reaches none of the compute-in-the-request machinery, in its
handler or any function it calls inside ``operator_api`` (four calls deep). ``PLX_ROUTE_RULE``:
``warn`` prints the offenders and passes (until SV9), ``fail`` fails on them."""
from __future__ import annotations

import ast
import importlib
import inspect
import os
import sys
import textwrap
import unittest

from operator_api import route_budgets as rb
from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.mock import MockBackend

# what a stored or door read never names (the kept page, first wait, workers, warm-up, plans)
FORBIDDEN = ("_serving", "ReaderCache", "first_partial", "FIRST_PARTIAL", "reads.run",
             "access_worker", "Worker", "warm(", "_brief_run", "simple_ask", "plan_")
DEPTH = 4


def get_routes() -> dict[str, object]:
    app = create_app(MockBackend(), DevAuth(fixture_identity("plx", ["staff:demo"]),
                                            mode="mock"))
    return {r.path: r.endpoint for r in app.routes
            if "GET" in (getattr(r, "methods", None) or ()) and r.path.startswith("/api/")}


def _named(fn: object) -> tuple[set[str], str]:
    try:
        src = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
        return set(), ""
    tree = ast.parse(src)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    return names, src


def reached(fn: object, depth: int = DEPTH) -> list[str]:
    """The forbidden names ``fn`` or a function it names inside ``operator_api`` reaches."""
    seen, found, todo = set(), set(), [(fn, 0)]
    while todo:
        f, d = todo.pop()
        if id(f) in seen:
            continue
        seen.add(id(f))
        names, src = _named(f)
        found |= {m for m in FORBIDDEN if (m.endswith("(") and m in src) or
                  (not m.endswith("(") and ("." in m and m in src or m in names
                                           or (m.endswith("_") and any(n.startswith(m)
                                                                       for n in names))))}
        if d >= depth:
            continue
        mod = importlib.import_module(getattr(f, "__module__", "") or "builtins")
        for name in names:
            got = getattr(mod, name, None)
            if callable(got) and (getattr(got, "__module__", "") or "").startswith(
                    "operator_api"):
                todo.append((got, d + 1))
    return sorted(found)


class RouteRule(unittest.TestCase):
    def test_every_get_route_is_listed_once_with_a_known_family_and_piece(self):
        routes = get_routes()
        self.assertEqual(sorted(set(routes) - set(rb.ROUTES)), [],
                         "a GET route without a family and budget (route_budgets.ROUTES)")
        self.assertEqual(sorted(set(rb.ROUTES) - set(routes)), [],
                         "a budget for a route the API no longer has")
        for path, b in rb.ROUTES.items():
            with self.subTest(path=path):
                self.assertIn(b.family, rb.FAMILIES)
                if b.family in ("file", "external"):
                    self.assertIsNone(b.budget_ms)
                else:
                    self.assertTrue(isinstance(b.budget_ms, int) and 0 < b.budget_ms <= 100)
                self.assertEqual(b.replaced_by is not None, b.family == "computed")
                self.assertEqual(b.sv is not None, b.family == "computed")

    def test_a_served_or_door_route_never_reaches_the_compute_machinery(self):
        routes = get_routes()
        offenders = {path: reached(routes[path]) for path, b in rb.ROUTES.items()
                     if b.family in ("served", "door")}
        offenders = {p: f for p, f in offenders.items() if f}
        self.assertIn("/api/page/{kind}/{key:path}", rb.ROUTES)
        self.assertEqual(rb.ROUTES["/api/page/{kind}/{key:path}"].family, "served")
        self.assertNotIn("/api/page/{kind}/{key:path}", offenders)
        # every served route is held to it, whatever the warn mode (a switched family's route)
        served = {p for p, b in rb.ROUTES.items() if b.family == "served"}
        self.assertIn("/api/page/history/{key:path}", served)
        self.assertEqual({p: f for p, f in offenders.items() if p in served}, {})
        mode = os.environ.get("PLX_ROUTE_RULE", "warn")
        if offenders:
            print("\nroute rule: " + "; ".join(f"{p}: {', '.join(f)}"
                                               for p, f in sorted(offenders.items())),
                  file=sys.stderr)
        if mode == "fail":
            self.assertEqual(offenders, {})

    def test_a_page_family_is_served_by_one_get_route(self):
        """A served route replaces the old one in the same landing (business rule 11-23): a
        family named by two routes is a switched family whose old route is still alive."""
        twice = {fam: paths for fam, paths in rb.family_routes().items() if len(paths) > 1}
        # hard: a family the served route names has no other route (the switching commit
        # deletes the old one); the older doubles (/api/biz/page beside /api/object) are the
        # debt, printed now and failing with PLX_ROUTE_RULE=fail (SV9)
        served = {p for p, b in rb.ROUTES.items() if b.family == "served"}
        switched = {f: p for f, p in twice.items() if served & set(p)}
        self.assertEqual(rb.family_routes()["history:PurchaseOrder"],
                         ["/api/page/history/{key:path}"])
        self.assertEqual(switched, {}, "a switched family whose old route is still alive: "
                                       "delete the old route's serving code in the same commit")
        if twice:
            print("\nroute rule: a family served by two routes: " + "; ".join(
                f"{f}: {', '.join(p)}" for f, p in sorted(twice.items())), file=sys.stderr)
        if os.environ.get("PLX_ROUTE_RULE", "warn") == "fail":
            self.assertEqual(twice, {})
        for path, b in rb.ROUTES.items():
            self.assertTrue(all(isinstance(f, str) and f for f in b.serves), path)
        # the check itself: a second route naming a family is found
        saved = rb.ROUTES
        try:
            rb.ROUTES = {**saved, "/api/page/{kind}/{key:path}": saved[
                "/api/page/{kind}/{key:path}"]._replace(serves=("record:PurchaseOrder",))}
            self.assertIn("/api/page/{kind}/{key:path}",
                          rb.family_routes()["record:PurchaseOrder"])
            self.assertGreater(len(rb.family_routes()["record:PurchaseOrder"]), 1)
        finally:
            rb.ROUTES = saved

    def test_the_rule_finds_a_forbidden_name_two_calls_deep(self):
        from operator_api import served
        self.assertEqual(reached(served.read), [])
        self.assertIn("access_worker", reached(lambda: probe_one()))

    def test_the_debt_is_stated(self):
        debt = rb.debt()
        n = sum(len(v) for v in debt.values())
        print(f"\nroute_budgets: {n} of {len(rb.ROUTES)} GET routes still computed per "
              "request: " + "; ".join(f"{k}: {len(v)}" for k, v in sorted(debt.items())),
              file=sys.stderr)
        self.assertTrue(all(k in ("SV3", "SV4", "SV5", "SV6", "SV7") for k in debt))


def probe_one() -> None:      # a fixture: reaches the worker one call down
    probe_two()


def probe_two() -> None:
    backend = None
    backend.access_worker.run()  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
