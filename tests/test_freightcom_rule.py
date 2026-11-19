"""The one Freightcom rule (``integrations.freightcom_rule``): one table of names and
non-names, run through the Python function, the compiled ``FREIGHTCOM`` drop-in and the SQL
expression Postgres runs at build time and in ``plx.table``, so they cannot drift."""
from __future__ import annotations

import unittest

from integrations import freightcom_rule as rule
from integrations.image_ocr_gpu import touches_freightcom
from operating.ontology_tools_table_items import freightcom_match
from operating.tests import pg_testdb

POSITIVE = [
    "Freightcom", "freightcom", "FREIGHTCOM", "FrEiGhTcOm", "Freight Com", "Freight  com",
    "freight-com", "Freight.com", "Freight_com", "freight/com", "freight com",
    "freight\tcom", "freight\ncom", "freight - com", "freight 2 com", "freight​com",
    "freight⁠ com", "fre​ight­com", "freight́com",
    "Ｆｒｅｉｇｈｔｃｏｍ",       # full-width
    "ｆreight.com", "freight　com", "FREIGHT COM",
    "https://www.freightcom.com/rates", "customer-external-api.ssd-test.freightcom.com",
    "x@freightcom.ca", "ana@Freight.Com", "ops@freight-com.ca", "FreightcomInc",
    "MyFreightcomAccount", "x\\nFreight Com", "%2Ffreight-com.ca", "a\\tfreight.com", "x%0Afreight com", "x\\u002ffreight com", "xfreightcom.com", "freightcominc", "Supplier:Freightcom", "FREIGHTCOM INC",
    "freight cоm",                                                  # Cyrillic o
    "frеightсom",                                              # Cyrillic e, c
    "freight company", "Freight Comments", "Account:Freightcom Inc",                               # over-match, on purpose
    "freight\\ncom", "freight\\u200bcom",
    "Freightcóm", "Freıghtcom", "FreİghtCom", "Fréightcom",
    "&#102;reightcom", "&#x66;reightcom", "&amp;#102;reightcom", "freight&#45;com",
    "freight%2Dcom", "%66reight com", "%2566reightcom", "freight&nbsp;com",
    "_freightcom", "9freight.com", "ops@mail.freight-com.ca", "(Freight.com)", "FREIGHT.COM",
    "\\u0066reightcom", "\\u0046reight.com", "freight\\u002dcom",                                # JSON escapes in raw text
]
NEGATIVE = [
    "", "freight", "freight rates went up", "free commerce", "Fred's company",
    "communication about freight", "com freight", "freight forwarding company",
    "freight forwarder, a company", "ocean freight quote", "Freight Cost 4%",
    "FREIGHT", "freig ht", "dot com", "e-commerce",
    # other companies whose names end in "freight": "freight" follows a letter
    "herculesfreight.com", "ops@herculesfreight.com", "yrcfreight.com", "dispatch@yrcfreight.com",
    "anyfreight.com", "https://www.sunfreight.com/rates", "Xfreight com", "AIRFREIGHT.COM",
]


def sql_flags(cases: list[str]) -> list[bool]:
    import psycopg
    name, dsn = pg_testdb.create_db()
    try:
        params: dict[str, str] = {}

        def bind(value: str) -> str:
            key = f"p{len(params)}"
            params[key] = value
            return f"%({key})s"
        expr = rule.sql_match("t.v", bind)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE TABLE t (i int, v text)")
            for i, text in enumerate(cases):
                c.execute("INSERT INTO t VALUES (%s, %s)", (i, text))
            c.execute("INSERT INTO t VALUES (%s, NULL)", (len(cases),))
            rows = c.execute(f"SELECT i, {expr} FROM t ORDER BY i", params).fetchall()
        assert rows[-1][1] is None   # NULL stays NULL: callers COALESCE it
        return [bool(r[1]) for r in rows[:-1]]
    finally:
        pg_testdb.drop_db(name)


ESCAPES = ["%20", "%2566", "&#160;", "&#x66;", "&amp;#102;", "&nbsp;", "\\u200b", "\\n",
           "%", "&", "\\"]


def timing_cases() -> list[str]:
    cases = []
    for esc in ESCAPES:
        for tail in ("", "com"):
            cases.append("<td>Freight" + esc * 10000 + "</td>" + tail)
    cases.append("freight" + "".join(ESCAPES) * 10000 + "x")
    cases.append(("freight" + "".join(ESCAPES) * 20 + "z ") * 1000)        # ~1 MB
    return cases


class Python(unittest.TestCase):
    def test_runs_of_escapes_match_in_linear_time(self):
        import time
        for text in timing_cases():
            with self.subTest(n=len(text), head=text[:30]):
                t = time.monotonic()
                rule.names(text)
                self.assertLess(time.monotonic() - t, 0.5)

    def test_names_and_non_names(self):
        for text in POSITIVE:
            with self.subTest(text=text):
                self.assertTrue(rule.names(text), text)
                self.assertIsNotNone(rule.FREIGHTCOM.search(text))
                self.assertTrue(touches_freightcom(text))
        for text in NEGATIVE:
            with self.subTest(text=text):
                self.assertFalse(rule.names(text), text)
                self.assertIsNone(rule.FREIGHTCOM.search(text))
                self.assertFalse(touches_freightcom(text))

    def test_redaction_leaves_other_text_alone(self):
        for text in ("m² of café ﬁle", "Ｆull ｗidth", "freight rates"):
            self.assertEqual(rule.FREIGHTCOM.sub("W", text), text)

    def test_records_are_read_value_by_value(self):
        self.assertTrue(touches_freightcom({"subject": "re: freight\ncom", "n": 1}))
        self.assertTrue(touches_freightcom({"to": [{"address": "x@freightcom.ca"}]}))
        self.assertTrue(touches_freightcom({"freight-com": 1}))          # a key too
        self.assertTrue(touches_freightcom(b"Freight.com"))
        self.assertTrue(touches_freightcom(None, "ok", ["freight_com"]))
        self.assertFalse(touches_freightcom(None, {"a": "freight", "b": "com"}, 12))

    def test_redaction_removes_every_form(self):
        for text in ("Freight.com quotes", "via freight-com", "Ｆreightcom"):
            out = rule.FREIGHTCOM.sub("WITHHELD", text)
            self.assertIn("WITHHELD", out)
            self.assertFalse(rule.names(out), out)


class Sql(unittest.TestCase):
    def test_sql_agrees_with_python_on_every_case(self):
        cases = POSITIVE + NEGATIVE
        got = sql_flags(cases)
        self.assertEqual(len(got), len(cases))
        for text, flag in zip(cases, got):
            with self.subTest(text=text):
                self.assertEqual(flag, rule.names(text), text)

    def test_runs_of_escapes_match_in_linear_time(self):
        import time
        import psycopg
        cases = timing_cases()
        name, dsn = pg_testdb.create_db()
        try:
            params: dict[str, str] = {}

            def bind(value):
                params[f"p{len(params)}"] = value
                return f"%(p{len(params) - 1})s"
            expr = rule.sql_match("t.v", bind)
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute("CREATE TABLE t (i int, v text)")
                for i, text in enumerate(cases):
                    c.execute("INSERT INTO t VALUES (%s, %s)", (i, text))
                for i, text in enumerate(cases):
                    t = time.monotonic()
                    got = c.execute(f"SELECT {expr} FROM t WHERE i = {i}", params).fetchone()[0]
                    self.assertLess(time.monotonic() - t, 0.5, text[:30])
                    self.assertEqual(bool(got), rule.names(text), text[:30])
        finally:
            pg_testdb.drop_db(name)

    def test_literal_bound_expression_for_the_comms_build(self):
        """business_comms inlines the expression with SQL literals (no parameters): the
        ``%`` of the URL-escape alternatives must survive."""
        import psycopg
        from operating.pg.sql_views import _literal
        expr = rule.sql_match("t.v", _literal)
        name, dsn = pg_testdb.create_db()
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute("CREATE TABLE t (v text)")
                for v in ("freight%2Dcom", "Freightcóm", "plain freight"):
                    c.execute("INSERT INTO t VALUES (%s)", (v,))
                got = [r[0] for r in c.execute(f"SELECT {expr} FROM t ORDER BY v")]
        finally:
            pg_testdb.drop_db(name)
        self.assertEqual(got, [True, True, False])   # Freightcóm, freight%2Dcom, plain

    def test_table_items_expression_is_the_rule(self):
        class Q:
            def __init__(self):
                self.params = {}

            def bind(self, value):
                self.params[f"p{len(self.params)}"] = value
                return f"%(p{len(self.params) - 1})s"
        q = Q()
        sql = freightcom_match("c.value", q)
        self.assertIn("normalize(", sql)
        self.assertIn(rule.PATTERN, q.params.values())


if __name__ == "__main__":
    unittest.main()
