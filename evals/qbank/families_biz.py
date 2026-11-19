"""Question families over the curated business tables (schema ``biz``): sales, receivables,
payables, purchasing, stock, batches/QC and master data, with rankings, comparisons,
multi-table joins, follow-ups, impossible and false-premise questions.

Truth reads only rows the AI may read (``ai_ok``) and, for sales orders, the copy of record.
Pools order by ``md5(key || seed)`` so one seed draws one sample.
"""

from __future__ import annotations

from .core import Family, num, spec
from . import grading_helpers as g

SO = "biz.sales_orders WHERE ai_ok AND of_record AND order_type = 'order'"
REC = "%(today)s::date - 730"   # recent window for (entity, month) pools
MONTH = "BETWEEN %(m)s::date AND (%(m)s::date + interval '1 month' - interval '1 day')"


def _so_pool(where: str = "status IN ('open','closed')") -> str:
    return (f"SELECT order_number, customer, extract(epoch FROM order_date) AS w FROM {SO} AND {where} "
            "AND order_number IS NOT NULL ORDER BY md5(order_number || %(seed)s) LIMIT 4000")


def _cust_month_pool(table: str, date_col: str, extra: str = "") -> str:
    return (f"SELECT customer_code, customer, date_trunc('month', {date_col})::date AS m, "
            f"to_char(date_trunc('month', {date_col}), 'FMMonth YYYY') AS month, count(*) AS w FROM {table} "
            f"{extra} AND {date_col} >= {REC} AND customer IS NOT NULL GROUP BY 1, 2, 3, 4 "
            "ORDER BY md5(customer_code || %(seed)s) LIMIT 4000")


def _past_series(table: str, col: str, slot: str) -> str:
    """Identifiers of a real shape just past the end of each numbering series (none exists yet;
    a refresh marks one stale the day it does)."""
    digits = f"regexp_replace(regexp_replace({col}, '[^0-9]*$', ''), '^.*[^0-9]', '')"
    return (f"SELECT pre || lpad((mx + 7000 + k)::text, len, '0') || suf AS {slot}, 1 AS w FROM (SELECT "
            f"regexp_replace({col}, '[0-9]+[^0-9]*$', '') AS pre, regexp_replace({col}, '^.*[0-9]', '') AS suf, "
            f"max({digits}::numeric) AS mx, max(length({digits})) AS len, count(*) AS c FROM {table} WHERE {col} ~ '[0-9]{{3}}' "
            "GROUP BY 1, 2 ORDER BY c DESC LIMIT 4) s, generate_series(1, 12) AS k")


def _owed(rows, params):
    pos = [r for r in rows if r.get("amt") is not None]
    if not pos:
        return spec("nothing open: the answer says nothing is owed", numbers=[num(0, "owed")], zero_ok=True)
    return spec("open balance per currency (net of credits; positive documents alone also accepted)",
                numbers=[num(r["amt"], f"{r['cur']} owed", alts=[r["gross"]] if r.get("gross") is not None else (),
                             currency=r["cur"]) for r in pos])


def _compare(rows, params):
    if len(rows) != 2 or rows[0]["v"] == rows[1]["v"]:
        return None
    win = max(rows, key=lambda r: r["v"])
    return spec("names the larger of the two and states its figure",
                names=[win["who"]], numbers=[num(r["v"], f"{r['who']} amount", currency=r.get("cur")) for r in rows], min_numbers=1)


def _order_billed(rows, params):
    r = g.first(rows)
    if not r or not r["invoices"]:
        return None
    return spec("names the invoice(s) that billed the order and what is still owed on it",
                names=r["invoices"][:10], min_names=1, numbers=[num(r["owed"] or 0, "still owed")], zero_ok=not r["owed"])


def _po_billed(rows, params):
    r = g.first(rows)
    if not r:
        return None
    if not r["docs"]:
        return spec("no supplier invoice carries this PO: the answer says it has not been billed", numbers=[num(0, "billed")], zero_ok=True)
    return spec("supplier invoice(s) for the PO and their total", names=r["docs"][:10], min_names=1,
                numbers=[num(r["total"], "billed", currency=r["cur"])], min_numbers=1)


_LAST_BOUGHT = (
    "SELECT * FROM (SELECT p.supplier, p.order_date AS d FROM biz.purchase_order_lines l JOIN biz.purchase_orders p ON p.po_number = l.po_number "
    "AND p.ai_ok WHERE l.ai_ok AND l.item_code = %(item_code)s AND p.order_date IS NOT NULL AND p.status <> 'draft' "
    "ORDER BY p.order_date DESC, p.po_number DESC LIMIT 1) a, (SELECT p.supplier AS any_supplier FROM biz.purchase_order_lines l "
    "JOIN biz.purchase_orders p ON p.po_number = l.po_number AND p.ai_ok WHERE l.ai_ok AND l.item_code = %(item_code)s "
    "AND p.order_date IS NOT NULL AND p.status <> 'unknown' ORDER BY p.order_date DESC, p.po_number DESC LIMIT 1) b")


def _item_last_bought(rows, params):
    r = g.first(rows)
    if not r or not r["supplier"]:
        return None
    return spec("the supplier of the latest PO holding the item (the latest issued one, or the latest draft)",
                names=[[r["supplier"], r["any_supplier"]]], dates=[r["d"]], min_dates=0)


FAMILIES = [
    # ---- sales orders ----------------------------------------------------------------
    Family("so_status", "sales", "SalesOrder", "lookup", _so_pool(),
           f"SELECT status FROM {SO} AND order_number = %(order_number)s",
           ("What's the status of order {order_number}?", "Is sales order {order_number} still open?",
            "where are we on SO {order_number}", "Has order {order_number} shipped?", "status of {order_number}?"),
           g.state("status", "the order's status")),
    Family("so_customer", "sales", "SalesOrder", "lookup", _so_pool(),
           f"SELECT customer FROM {SO} AND order_number = %(order_number)s",
           ("Who is order {order_number} for?", "Which customer placed sales order {order_number}?",
            "whose order is {order_number}", "Customer on SO {order_number}?"), g.text("customer", "the order's customer")),
    Family("so_value", "sales", "SalesOrder", "lookup", _so_pool(),
           f"SELECT value, value_currency FROM {SO} AND order_number = %(order_number)s AND value IS NOT NULL",
           ("What's the total value of sales order {order_number}?", "How much is order {order_number} worth?",
            "order {order_number} total $?", "What did {customer} order on {order_number}, in dollars?"),
           g.number("value", "the order's value", cur="value_currency")),
    Family("so_date", "sales", "SalesOrder", "lookup", _so_pool(),
           f"SELECT order_date FROM {SO} AND order_number = %(order_number)s",
           ("When was order {order_number} placed?", "What date did {customer} place order {order_number}?",
            "order date for {order_number}?"), g.date("order_date", "the order date")),
    Family("so_items", "sales", "SalesOrderLine", "lookup", _so_pool(),
           "SELECT item_code, item_name FROM biz.sales_order_lines WHERE ai_ok AND of_record AND order_number = %(order_number)s "
           "AND item_code IS NOT NULL ORDER BY line_amount DESC NULLS LAST",
           ("What items are on order {order_number}?", "What did {customer} order on {order_number}?",
            "list the products on SO {order_number}", "Which SKUs are on sales order {order_number}?"),
           g.members("item_code", "the order's items (code or name)", alt_col="item_name")),
    Family("so_by_cpo", "sales", "SalesOrder", "lookup",
           f"SELECT customer_po, customer, count(*) AS w FROM {SO} AND customer_po IS NOT NULL AND length(customer_po) >= 4 "
           "GROUP BY 1, 2 HAVING count(*) <= 3 ORDER BY md5(customer_po || %(seed)s) LIMIT 4000",
           f"SELECT order_number FROM {SO} AND customer_po = %(customer_po)s",
           ("Which of our orders is for {customer}'s PO {customer_po}?", "What sales order matches customer PO {customer_po}?",
            "we got PO {customer_po} from {customer} - what's our order number?"),
           g.members("order_number", "the order(s) carrying that customer PO", need=1)),
    Family("cust_open_orders", "sales", "SalesOrder", "count",
           f"SELECT customer_code, customer, count(*) FILTER (WHERE status = 'open') AS w FROM {SO} AND order_date >= {REC} "
           "AND customer IS NOT NULL GROUP BY 1, 2 ORDER BY md5(customer_code || %(seed)s) LIMIT 4000",
           f"SELECT count(*) AS n, array_agg(order_number ORDER BY order_date DESC) AS orders FROM {SO} AND status = 'open' "
           "AND customer_code = %(customer_code)s",
           ("How many open orders does {customer} have?", "What open orders does {customer} have with us?",
            "open SOs for {customer}?", "Is anything still open for {customer}?"),
           g.count("open orders of the customer", names_col="orders")),
    Family("cust_orders_month", "sales", "SalesOrder", "count", _cust_month_pool("biz.sales_orders", "order_date",
           "WHERE ai_ok AND of_record AND order_type = 'order'"),
           f"SELECT count(*) AS n FROM {SO} AND customer_code = %(customer_code)s AND order_date {MONTH}",
           ("How many orders did {customer} place in {month}?", "How many sales orders came in from {customer} in {month}?",
            "{customer} orders in {month} - how many?"), g.count("orders placed in the month")),
    Family("cust_last_order", "sales", "SalesOrder", "lookup",
           f"SELECT customer_code, customer, count(*) AS w FROM {SO} AND customer IS NOT NULL GROUP BY 1, 2 "
           "ORDER BY md5(customer_code || %(seed)s) LIMIT 4000",
           f"SELECT max(order_date) AS d FROM {SO} AND customer_code = %(customer_code)s",
           ("When did {customer} last order from us?", "What was the date of {customer}'s most recent order?",
            "last order from {customer}?"), g.date("d", "date of the latest order")),
    # ---- invoices and revenue ----------------------------------------------------------
    Family("inv_total", "sales", "SalesInvoice", "lookup",
           "SELECT invoice_number, customer, extract(epoch FROM invoice_date) AS w FROM biz.sales_invoices WHERE ai_ok AND NOT pilot "
           "AND total IS NOT NULL ORDER BY md5(invoice_number || %(seed)s) LIMIT 4000",
           "SELECT total, total_currency FROM biz.sales_invoices WHERE ai_ok AND NOT pilot AND invoice_number = %(invoice_number)s",
           ("What's the total on invoice {invoice_number}?", "How much was invoice {invoice_number} for?",
            "amount of inv {invoice_number}?", "What did we bill {customer} on invoice {invoice_number}?"),
           g.number("total", "the invoice total", cur="total_currency")),
    Family("inv_order", "sales", "SalesInvoice", "lookup",
           "SELECT invoice_number, customer, extract(epoch FROM invoice_date) AS w FROM biz.sales_invoices WHERE ai_ok AND NOT pilot "
           "AND order_number IS NOT NULL ORDER BY md5(invoice_number || %(seed)s) LIMIT 4000",
           "SELECT order_number FROM biz.sales_invoices WHERE ai_ok AND NOT pilot AND invoice_number = %(invoice_number)s",
           ("Which order did invoice {invoice_number} bill?", "What sales order is invoice {invoice_number} for?",
            "order # behind invoice {invoice_number}?"), g.text("order_number", "the billed order")),
    Family("inv_date", "sales", "SalesInvoice", "lookup",
           "SELECT invoice_number, customer, extract(epoch FROM invoice_date) AS w FROM biz.sales_invoices WHERE ai_ok AND NOT pilot "
           "ORDER BY md5(invoice_number || %(seed)s) LIMIT 4000",
           "SELECT invoice_date FROM biz.sales_invoices WHERE ai_ok AND NOT pilot AND invoice_number = %(invoice_number)s",
           ("When did we issue invoice {invoice_number}?", "What's the date on invoice {invoice_number}?",
            "invoice {invoice_number} date"), g.date("invoice_date", "the invoice date")),
    Family("cust_billed_month", "sales", "SalesInvoice", "sum",
           _cust_month_pool("biz.sales_invoices", "invoice_date", "WHERE ai_ok AND NOT pilot"),
           "SELECT total_currency AS cur, sum(total) AS amt FROM biz.sales_invoices WHERE ai_ok AND NOT pilot "
           f"AND customer_code = %(customer_code)s AND invoice_date {MONTH} GROUP BY 1",
           ("How much did we invoice {customer} in {month}?", "What were our sales to {customer} in {month}?",
            "{customer} billings {month}?", "Total invoiced to {customer} during {month}?"),
           g.money_by_currency("amt", "cur", "invoiced in the month per currency")),
    Family("item_units_year", "sales", "SalesInvoiceLine", "sum",
           "SELECT item_code, max(item_name) AS item_name, extract(year FROM invoice_date)::int AS year, count(*) AS w "
           "FROM biz.sales_invoice_lines WHERE ai_ok AND NOT pilot AND line_type = 'item' AND item_code IS NOT NULL "
           "AND invoice_date >= %(today)s::date - 1100 GROUP BY 1, 3 ORDER BY md5(item_code || %(seed)s) LIMIT 4000",
           "SELECT sum(quantity) AS q FROM biz.sales_invoice_lines WHERE ai_ok AND NOT pilot AND line_type = 'item' "
           "AND item_code = %(item_code)s AND extract(year FROM invoice_date) = %(year)s",
           ("How many units of {item_code} did we sell in {year}?", "How much {item_name} ({item_code}) did we invoice in {year}?",
            "qty sold of {item_code} in {year}?"), g.number("q", "invoiced quantity in the year", tol=0.02)),
    Family("top_customers_month", "sales", "SalesInvoice", "rank",
           "SELECT DISTINCT total_currency AS currency, date_trunc('month', invoice_date)::date AS m, "
           "to_char(date_trunc('month', invoice_date), 'FMMonth YYYY') AS month FROM biz.sales_invoices WHERE ai_ok AND NOT pilot "
           "AND invoice_date >= %(today)s::date - 730 AND total_currency IS NOT NULL",
           "SELECT customer, sum(total) AS v, total_currency AS cur FROM biz.sales_invoices WHERE ai_ok AND NOT pilot "
           f"AND total_currency = %(currency)s AND invoice_date {MONTH} GROUP BY customer, total_currency ORDER BY v DESC LIMIT 5",
           ("Who were our top 5 customers by {currency} sales in {month}?", "Rank our biggest customers in {month} ({currency} invoices).",
            "top customers {month}, {currency}?"), g.ranked("customer", "top customers by invoiced amount in that currency", value_col="v", cur="cur")),
    Family("top_items_year", "sales", "SalesInvoiceLine", "rank",
           "SELECT DISTINCT revenue_currency AS currency, extract(year FROM invoice_date)::int AS year FROM biz.sales_invoice_lines "
           "WHERE ai_ok AND NOT pilot AND invoice_date >= %(today)s::date - 1500 AND revenue_currency IS NOT NULL",
           "SELECT item_code, max(item_name) AS item_name, sum(revenue) AS v FROM biz.sales_invoice_lines WHERE ai_ok AND NOT pilot "
           "AND line_type = 'item' AND revenue_currency = %(currency)s AND extract(year FROM invoice_date) = %(year)s "
           "GROUP BY item_code ORDER BY v DESC NULLS LAST LIMIT 5",
           ("What were our 5 best-selling products by {currency} revenue in {year}?", "Top selling items in {year} ({currency} sales)?",
            "which products made the most {currency} revenue in {year}"),
           lambda rows, p: g.members("item_code", "top items by revenue (code or name)", alt_col="item_name")(rows, p) if len(rows) >= 3 else None),
    Family("compare_customers_year", "sales", "SalesInvoice", "compare",
           "WITH c AS (SELECT customer_code, customer, cur, year, row_number() OVER (PARTITION BY cur, year ORDER BY md5(customer_code || %(seed)s)) AS k "
           "FROM (SELECT DISTINCT customer_code, customer, total_currency AS cur, extract(year FROM invoice_date)::int AS year FROM biz.sales_invoices "
           "WHERE ai_ok AND NOT pilot AND invoice_date >= %(today)s::date - 1100 AND customer IS NOT NULL AND total_currency IS NOT NULL) d) "
           "SELECT a.customer_code AS a_code, a.customer AS a, b.customer_code AS b_code, b.customer AS b, a.cur AS currency, a.year "
           "FROM c a JOIN c b ON b.cur = a.cur AND b.year = a.year AND b.k = a.k + 1 WHERE a.k %% 2 = 1 AND a.customer <> b.customer LIMIT 4000",
           "SELECT customer AS who, sum(total) AS v, total_currency AS cur FROM biz.sales_invoices WHERE ai_ok AND NOT pilot "
           "AND customer_code IN (%(a_code)s, %(b_code)s) AND total_currency = %(currency)s AND extract(year FROM invoice_date) = %(year)s "
           "GROUP BY customer, total_currency",
           ("Did we invoice more to {a} or to {b} in {year}?", "Who bought more from us in {year}, {a} or {b}?",
            "compare {year} sales: {a} vs {b}"), _compare),
    # ---- receivables -----------------------------------------------------------------------
    Family("ar_owed", "ar", "CustomerBalance", "sum",
           "SELECT customer_code, customer, count(*) AS w FROM biz.customer_balances WHERE ai_ok AND customer IS NOT NULL GROUP BY 1, 2 "
           "ORDER BY md5(customer_code || %(seed)s) LIMIT 4000",
           "SELECT amount_due_currency AS cur, sum(amount_due) AS amt, sum(amount_due) FILTER (WHERE amount_due > 0) AS gross "
           "FROM biz.customer_balances WHERE ai_ok AND customer_code = %(customer_code)s GROUP BY 1",
           ("How much does {customer} owe us?", "What's {customer}'s open balance?", "does {customer} owe us anything",
            "AR balance for {customer}?"), _owed),
    Family("ar_zero", "ar", "CustomerBalance", "sum",
           "SELECT c.customer_code, c.name AS customer, 0 AS w FROM biz.customers c WHERE c.ai_ok AND c.name IS NOT NULL "
           "AND EXISTS (SELECT 1 FROM biz.sales_invoices i WHERE i.customer_code = c.customer_code AND i.invoice_date >= %(today)s::date - 365) "
           "AND NOT EXISTS (SELECT 1 FROM biz.customer_balances b WHERE b.customer_code = c.customer_code) "
           "ORDER BY md5(c.customer_code || %(seed)s) LIMIT 4000",
           "SELECT amount_due_currency AS cur, sum(amount_due) AS amt, sum(amount_due) FILTER (WHERE amount_due > 0) AS gross "
           "FROM biz.customer_balances WHERE ai_ok AND customer_code = %(customer_code)s GROUP BY 1",
           ("Does {customer} owe us any money right now?", "Is there an open balance on {customer}'s account?"), _owed, n=25),
    Family("ar_overdue", "ar", "CustomerBalance", "sum",
           "SELECT customer_code, customer, count(*) FILTER (WHERE days_overdue > 0) AS w FROM biz.customer_balances WHERE ai_ok "
           "AND customer IS NOT NULL GROUP BY 1, 2 ORDER BY md5(customer_code || %(seed)s) LIMIT 4000",
           "SELECT amount_due_currency AS cur, sum(amount_due) FILTER (WHERE amount_due > 0) AS amt, sum(amount_due) AS gross "
           "FROM biz.customer_balances WHERE ai_ok AND customer_code = %(customer_code)s AND days_overdue > 0 GROUP BY 1",
           ("How much of {customer}'s balance is overdue?", "Is {customer} late paying us? How much is past due?",
            "overdue AR for {customer}"), _owed),
    Family("ar_doc_open", "ar", "CustomerBalance", "lookup",
           "SELECT document_number, customer, days_overdue AS w FROM biz.customer_balances WHERE ai_ok AND document_type = 'invoice' "
           "AND document_number IS NOT NULL ORDER BY md5(document_number || %(seed)s) LIMIT 4000",
           "SELECT sum(amount_due) AS amt, max(amount_due_currency) AS cur FROM biz.customer_balances WHERE ai_ok "
           "AND document_number = %(document_number)s HAVING count(DISTINCT amount_due_currency) = 1",
           ("How much is still open on invoice {document_number}?", "What's left to collect on {customer}'s invoice {document_number}?",
            "balance remaining inv {document_number}"), g.number("amt", "amount still due", cur="cur")),
    Family("ar_oldest", "ar", "CustomerBalance", "lookup",
           "SELECT customer_code, customer, count(*) AS w FROM biz.customer_balances WHERE ai_ok AND amount_due > 0 AND customer IS NOT NULL "
           "GROUP BY 1, 2 ORDER BY md5(customer_code || %(seed)s) LIMIT 4000",
           "SELECT document_number, document_date FROM biz.customer_balances WHERE ai_ok AND amount_due > 0 AND customer_code = %(customer_code)s "
           "AND document_type = 'invoice' ORDER BY document_date NULLS LAST, document_number LIMIT 1",
           ("What's {customer}'s oldest unpaid invoice?", "Which open invoice of {customer} has been outstanding longest?"),
           lambda rows, p: spec("the oldest open invoice", names=[rows[0]["document_number"]], dates=[rows[0]["document_date"]], min_dates=0) if rows else None),
    Family("ar_top", "ar", "CustomerBalance", "rank",
           "SELECT DISTINCT amount_due_currency AS currency FROM biz.customer_balances WHERE ai_ok AND amount_due_currency IS NOT NULL",
           "SELECT customer, sum(amount_due) AS v, amount_due_currency AS cur FROM biz.customer_balances WHERE ai_ok "
           "AND amount_due_currency = %(currency)s GROUP BY customer, amount_due_currency ORDER BY v DESC LIMIT 5",
           ("Which 5 customers owe us the most in {currency}?", "Biggest {currency} receivables by customer?",
            "who owes us the most ({currency})"), g.ranked("customer", "largest balances in that currency", value_col="v", cur="cur"), n=4),
    Family("ar_bucket", "ar", "CustomerBalance", "sum",
           "SELECT DISTINCT amount_due_currency AS currency, aging_bucket AS bucket FROM biz.customer_balances WHERE ai_ok "
           "AND aging_bucket IS NOT NULL AND amount_due_currency IS NOT NULL",
           "SELECT amount_due_currency AS cur, sum(amount_due) AS amt, sum(amount_due) FILTER (WHERE amount_due > 0) AS gross "
           "FROM biz.customer_balances WHERE ai_ok AND amount_due_currency = %(currency)s AND aging_bucket = %(bucket)s GROUP BY 1",
           ("How much {currency} receivable is in the {bucket} aging bucket?", "What's our {currency} AR aged {bucket} days?",
            "{bucket} bucket total, {currency} AR"), _owed, n=12),
    # ---- payables ------------------------------------------------------------------------
    Family("ap_owed", "ap", "SupplierBalance", "sum",
           "SELECT supplier_code, supplier, count(*) AS w FROM biz.supplier_balances WHERE ai_ok AND supplier IS NOT NULL GROUP BY 1, 2 "
           "ORDER BY md5(supplier_code || %(seed)s) LIMIT 4000",
           "SELECT amount_due_currency AS cur, sum(amount_due) AS amt, sum(amount_due) FILTER (WHERE amount_due > 0) AS gross "
           "FROM biz.supplier_balances WHERE ai_ok AND supplier_code = %(supplier_code)s GROUP BY 1",
           ("How much do we owe {supplier}?", "What's our open balance with {supplier}?", "AP owing to {supplier}?",
            "do we owe {supplier} anything"), _owed),
    Family("ap_inv_amount", "ap", "SupplierInvoice", "lookup",
           "SELECT supplier, document_number, extract(epoch FROM document_date) AS w FROM biz.supplier_invoices WHERE ai_ok AND NOT pilot "
           "AND document_number IS NOT NULL AND supplier IS NOT NULL ORDER BY md5(document_number || %(seed)s) LIMIT 4000",
           "SELECT original_amount, original_amount_currency FROM biz.supplier_invoices WHERE ai_ok AND NOT pilot "
           "AND document_number = %(document_number)s AND supplier = %(supplier)s",
           ("What was the amount of {supplier}'s invoice {document_number}?", "How much did {supplier} bill us on {document_number}?",
            "{supplier} inv {document_number} amount?"), g.number("original_amount", "the invoice amount", cur="original_amount_currency")),
    Family("ap_count_month", "ap", "SupplierInvoice", "count",
           "SELECT supplier_code, supplier, date_trunc('month', document_date)::date AS m, to_char(date_trunc('month', document_date), "
           "'FMMonth YYYY') AS month, count(*) AS w FROM biz.supplier_invoices WHERE ai_ok AND NOT pilot AND document_date >= "
           f"{REC} AND supplier IS NOT NULL GROUP BY 1, 2, 3, 4 ORDER BY md5(supplier_code || %(seed)s) LIMIT 4000",
           "SELECT count(*) AS n FROM biz.supplier_invoices WHERE ai_ok AND NOT pilot AND supplier_code = %(supplier_code)s "
           f"AND document_date {MONTH}",
           ("How many invoices did {supplier} send us in {month}?", "Number of {supplier} bills dated {month}?"),
           g.count("supplier invoices dated in the month")),
    Family("ap_top", "ap", "SupplierBalance", "rank",
           "SELECT DISTINCT amount_due_currency AS currency FROM biz.supplier_balances WHERE ai_ok AND amount_due_currency IS NOT NULL",
           "SELECT supplier, sum(amount_due) AS v, amount_due_currency AS cur FROM biz.supplier_balances WHERE ai_ok "
           "AND amount_due_currency = %(currency)s GROUP BY supplier, amount_due_currency ORDER BY v DESC LIMIT 5",
           ("Which suppliers do we owe the most in {currency}?", "top 5 {currency} payables by vendor"),
           g.ranked("supplier", "largest payables in that currency", value_col="v", cur="cur"), n=4),
    # ---- purchasing -------------------------------------------------------------------------
    *[Family(fid, "purchasing", "PurchaseOrder", "lookup",
             "SELECT po_number, supplier, extract(epoch FROM order_date) AS w FROM biz.purchase_orders WHERE ai_ok AND po_number IS NOT NULL "
             "AND status <> 'unknown' AND po_number IN (SELECT po_number FROM biz.purchase_orders GROUP BY 1 HAVING count(*) = 1) ORDER BY md5(po_number || %(seed)s) LIMIT 4000",
             f"SELECT {cols} FROM biz.purchase_orders WHERE ai_ok AND po_number = %(po_number)s", asks, grade)
      for fid, cols, asks, grade in (
          ("po_status", "status", ("What's the status of PO {po_number}?", "Is purchase order {po_number} still open?",
                                   "where's PO {po_number} at", "status on {po_number}?"), g.state("status", "the PO's status")),
          ("po_supplier", "supplier", ("Who is PO {po_number} with?", "Which supplier is purchase order {po_number} for?",
                                       "vendor on {po_number}?"), g.text("supplier", "the PO's supplier")),
          ("po_total", "total, total_currency", ("What's the total of PO {po_number}?", "How much is purchase order {po_number} worth?",
                                                 "{po_number} PO value"), g.number("total", "the PO total", cur="total_currency")),
          ("po_date", "order_date", ("When did we place PO {po_number}?", "What date was purchase order {po_number} issued?"),
           g.date("order_date", "the PO date")))],
    Family("po_items", "purchasing", "PurchaseOrderLine", "lookup",
           "SELECT po_number, supplier, count(*) AS w FROM biz.purchase_order_lines WHERE ai_ok AND item_code IS NOT NULL GROUP BY 1, 2 "
           "ORDER BY md5(po_number || %(seed)s) LIMIT 4000",
           "SELECT item_code, description FROM biz.purchase_order_lines WHERE ai_ok AND po_number = %(po_number)s AND item_code IS NOT NULL "
           "ORDER BY line_total DESC NULLS LAST",
           ("What items are on PO {po_number}?", "What did we order from {supplier} on {po_number}?", "lines on purchase order {po_number}"),
           g.members("item_code", "the PO's items (code or description)", alt_col="description")),
    Family("sup_open_pos", "purchasing", "PurchaseOrder", "count",
           "SELECT supplier_handle, supplier, count(*) FILTER (WHERE status = 'open') AS w FROM biz.purchase_orders WHERE ai_ok "
           "AND supplier IS NOT NULL AND supplier_handle IS NOT NULL AND order_date >= %(today)s::date - 1100 GROUP BY 1, 2 "
           "ORDER BY md5(supplier_handle || %(seed)s) LIMIT 4000",
           "SELECT count(*) AS n, array_agg(po_number ORDER BY order_date DESC) AS pos FROM biz.purchase_orders WHERE ai_ok "
           "AND status = 'open' AND supplier_handle = %(supplier_handle)s",
           ("How many open POs do we have with {supplier}?", "What purchase orders are still open with {supplier}?",
            "open POs {supplier}?"), g.count("open POs with the supplier", names_col="pos")),
    Family("po_count_month", "purchasing", "PurchaseOrder", "count",
           "SELECT supplier_handle, supplier, date_trunc('month', order_date)::date AS m, to_char(date_trunc('month', order_date), "
           f"'FMMonth YYYY') AS month, count(*) AS w FROM biz.purchase_orders WHERE ai_ok AND order_date >= {REC} AND supplier IS NOT NULL "
           "AND supplier_handle IS NOT NULL GROUP BY 1, 2, 3, 4 ORDER BY md5(supplier_handle || %(seed)s) LIMIT 4000",
           "SELECT count(*) AS n FROM biz.purchase_orders WHERE ai_ok AND supplier_handle = %(supplier_handle)s AND order_date "
           f"{MONTH} AND status <> 'unknown'",
           ("How many POs did we place with {supplier} in {month}?", "purchase orders to {supplier} in {month}?"),
           g.count("POs placed in the month")),
    Family("item_last_bought", "purchasing", "PurchaseOrderLine", "multi",
           "SELECT l.item_code, max(l.description) AS description, count(*) AS w FROM biz.purchase_order_lines l JOIN biz.purchase_orders p "
           "ON p.po_number = l.po_number AND p.status <> 'draft' AND p.order_date IS NOT NULL WHERE l.ai_ok AND l.item_code IS NOT NULL GROUP BY 1 ORDER BY md5(l.item_code || %(seed)s) LIMIT 4000",
           _LAST_BOUGHT,
           ("When did we last buy {item_code}, and from whom?", "Who did we last order {description} from?",
            "last PO for item {item_code} - which supplier?"), _item_last_bought),
    # ---- stock and products --------------------------------------------------------------------
    Family("onhand", "stock", "Stock", "lookup",
           "SELECT item_code, name, on_hand AS w FROM biz.stock WHERE ai_ok AND item_code IS NOT NULL AND name IS NOT NULL "
           "ORDER BY md5(item_code || %(seed)s) LIMIT 4000",
           "SELECT on_hand FROM biz.stock WHERE ai_ok AND item_code = %(item_code)s",
           ("How many {item_code} do we have in stock?", "What's on hand for {name}?", "stock level of {item_code}?",
            "do we have any {name} left?"), lambda rows, p: (g.number("on_hand", "on-hand quantity", tol=0.02)(rows, p)
                                                           if rows and rows[0]["on_hand"] else spec("nothing on hand", numbers=[num(0, "on hand")], zero_ok=True) if rows else None)),
    Family("item_name_of", "stock", "Product", "lookup",
           "SELECT item_code, 1 AS w FROM biz.products WHERE ai_ok AND item_code IS NOT NULL AND name IS NOT NULL GROUP BY 1 "
           "HAVING count(DISTINCT name) = 1 ORDER BY md5(item_code || %(seed)s) LIMIT 4000",
           "SELECT max(name) AS name FROM biz.products WHERE ai_ok AND item_code = %(item_code)s",
           ("What is item {item_code}?", "What product has the code {item_code}?", "{item_code} = which product?"),
           g.text("name", "the item's name")),
    Family("item_code_of", "stock", "Product", "lookup",
           "SELECT name, 1 AS w FROM biz.products WHERE ai_ok AND item_code IS NOT NULL AND length(name) >= 8 GROUP BY 1 "
           "HAVING count(DISTINCT item_code) = 1 ORDER BY md5(name || %(seed)s) LIMIT 4000",
           "SELECT max(item_code) AS item_code FROM biz.products WHERE ai_ok AND name = %(name)s",
           ("What's the item code for {name}?", "What SKU is {name}?"), g.text("item_code", "the item code")),
    Family("item_price", "stock", "Product", "lookup",
           "SELECT item_code, name, 1 AS w FROM biz.products WHERE ai_ok AND unit_price IS NOT NULL AND unit_price > 0 AND item_code IN "
           "(SELECT item_code FROM biz.products WHERE ai_ok AND unit_price > 0 GROUP BY 1 HAVING count(DISTINCT unit_price) = 1) "
           "ORDER BY md5(item_code || %(seed)s) LIMIT 4000",
           "SELECT max(unit_price) AS unit_price, max(unit_price_currency) AS cur FROM biz.products WHERE ai_ok AND item_code = %(item_code)s "
           "AND unit_price > 0",
           ("What's the unit price of {item_code}?", "How much do we sell {name} for?", "price on {item_code}?"),
           g.number("unit_price", "the unit price", cur="cur")),
    Family("type_out_of_stock", "stock", "Stock", "count",
           "SELECT DISTINCT item_type, 1 AS w FROM biz.stock WHERE ai_ok AND item_type IS NOT NULL",
           "SELECT count(*) AS n FROM biz.stock WHERE ai_ok AND item_type = %(item_type)s AND coalesce(on_hand, 0) <= 0",
           ("How many {item_type} items are out of stock?", "count of {item_type} items with nothing on hand?"),
           g.count("items of the type with nothing on hand"), n=8),
    Family("type_top_stock", "stock", "Stock", "rank",
           "SELECT DISTINCT item_type, 1 AS w FROM biz.stock WHERE ai_ok AND item_type IS NOT NULL",
           "SELECT item_code, name, on_hand FROM biz.stock WHERE ai_ok AND item_type = %(item_type)s AND on_hand IS NOT NULL "
           "ORDER BY on_hand DESC LIMIT 5",
           ("Which 5 {item_type} items do we have the most of on hand?", "biggest {item_type} stock positions?"),
           lambda rows, p: g.members("item_code", "largest on-hand items of the type", alt_col="name")(rows, p) if len(rows) >= 3 else None, n=8),
    # ---- batches and QC ----------------------------------------------------------------------
    *[Family(fid, "quality", "Batch", "lookup",
             "SELECT batch_code, product, extract(epoch FROM coalesce(realized_date, scheduled_date)) AS w FROM biz.batches WHERE ai_ok "
             f"AND batch_code IS NOT NULL AND {cond} AND batch_code IN (SELECT batch_code FROM biz.batches GROUP BY 1 HAVING count(*) = 1) "
             "ORDER BY md5(batch_code || %(seed)s) LIMIT 4000",
             f"SELECT {col} FROM biz.batches WHERE ai_ok AND batch_code = %(batch_code)s", asks, grade)
      for fid, col, cond, asks, grade in (
          ("batch_status", "status", "status IS NOT NULL", ("What's the status of batch {batch_code}?", "has batch {batch_code} been made yet?",
                                                            "batch {batch_code} status"), g.state("status", "the batch status")),
          ("batch_quality", "quality_status", "quality_status IS NOT NULL",
           ("Has batch {batch_code} been released by QC?", "What's the QC status of batch {batch_code}?", "is {batch_code} on hold?"),
           g.state("quality_status", "the batch's quality status")),
          ("batch_product", "product", "product IS NOT NULL", ("What product is batch {batch_code}?", "batch {batch_code} - what are we making?"),
           g.text("product", "the batch's product")),
          ("batch_qty", "produced_quantity", "produced_quantity > 0", ("How much did batch {batch_code} produce?", "yield of batch {batch_code}?"),
           g.number("produced_quantity", "produced quantity", tol=0.01)),
          ("batch_ph", "qc_ph_24h", "qc_ph_24h ~ '^[0-9]+([.][0-9]+)?$'", ("What was the 24-hour pH on batch {batch_code}?",
                                                                            "pH reading for batch {batch_code} at 24h?"),
           g.number("qc_ph_24h", "24 h pH", tol=0.005)),
          ("batch_tested_by", "tested_by", "tested_by IS NOT NULL", ("Who tested batch {batch_code}?", "who did the QC on {batch_code}?"),
           g.text("tested_by", "the tester")))],
    Family("product_batches_month", "quality", "Batch", "count",
           "SELECT product, date_trunc('month', realized_date)::date AS m, to_char(date_trunc('month', realized_date), 'FMMonth YYYY') AS month, "
           "count(*) AS w FROM biz.batches WHERE ai_ok AND product IS NOT NULL AND realized_date >= %(today)s::date - 1100 GROUP BY 1, 2, 3 "
           "ORDER BY md5(product || %(seed)s) LIMIT 4000",
           f"SELECT count(*) AS n FROM biz.batches WHERE ai_ok AND product = %(product)s AND realized_date {MONTH} AND status = 'Realized'",
           ("How many batches of {product} did we make in {month}?", "{product} batches produced in {month}?"),
           g.count("realized batches of the product in the month")),
    Family("product_on_hold", "quality", "Batch", "count",
           "SELECT product, count(*) AS w FROM biz.batches WHERE ai_ok AND product IS NOT NULL GROUP BY 1 ORDER BY md5(product || %(seed)s) LIMIT 4000",
           "SELECT count(*) AS n, array_agg(batch_code ORDER BY batch_code) AS codes FROM biz.batches WHERE ai_ok AND product = %(product)s "
           "AND quality_status = 'On-Hold' AND status NOT IN ('Cancelled')",
           ("How many {product} batches are on hold?", "Which {product} batches are on QC hold right now?"),
           g.count("on-hold batches of the product (not cancelled)", names_col="codes")),
    # ---- master data -------------------------------------------------------------------------------
    *[Family(fid, "master", "Customer", "lookup",
             f"SELECT customer_code, name AS customer, 1 AS w FROM biz.customers WHERE ai_ok AND name IS NOT NULL AND {col} IS NOT NULL "
             "AND name IN (SELECT name FROM biz.customers GROUP BY 1 HAVING count(*) = 1) ORDER BY md5(customer_code || %(seed)s) LIMIT 4000",
             f"SELECT {col}{', ' + cur if cur else ''} FROM biz.customers WHERE ai_ok AND customer_code = %(customer_code)s", asks, grade)
      for fid, col, cur, asks, grade in (
          ("cust_salesperson", "salesperson", None, ("Who is the salesperson for {customer}?", "who handles the {customer} account?"),
           g.text("salesperson", "the customer's salesperson")),
          ("cust_credit_limit", "credit_limit", "credit_limit_currency", ("What's {customer}'s credit limit?", "credit limit for {customer}?"),
           g.number("credit_limit", "credit limit", cur="credit_limit_currency")),
          ("cust_currency", "currency", None, ("What currency do we bill {customer} in?", "is {customer} a USD or CAD account?"),
           g.text("currency", "the billing currency")))],
    *[Family(fid, "master", "Supplier", "lookup",
             f"SELECT supplier_code, name AS supplier, 1 AS w FROM biz.suppliers WHERE ai_ok AND name IS NOT NULL AND {col} IS NOT NULL "
             "AND name IN (SELECT name FROM biz.suppliers GROUP BY 1 HAVING count(*) = 1) ORDER BY md5(supplier_code || %(seed)s) LIMIT 4000",
             f"SELECT {col} FROM biz.suppliers WHERE ai_ok AND supplier_code = %(supplier_code)s", asks, grade)
      for fid, col, asks, grade in (
          ("sup_lead_time", "lead_time_days", ("What's {supplier}'s lead time?", "how long does {supplier} take to deliver?"),
           g.number("lead_time_days", "lead time in days")),
          ("sup_currency", "currency", ("What currency does {supplier} bill us in?",), g.text("currency", "the supplier's currency")))],
    # ---- across tables ------------------------------------------------------------------------------
    Family("order_billed_owed", "sales", "SalesOrder", "multi", _so_pool("status = 'closed'"),
           "SELECT (SELECT array_agg(DISTINCT invoice_number) FROM biz.sales_invoices WHERE ai_ok AND NOT pilot AND order_number = %(order_number)s) "
           "AS invoices, (SELECT sum(amount_due) FROM biz.customer_balances WHERE ai_ok AND order_number = %(order_number)s) AS owed",
           ("Has order {order_number} been invoiced, and is anything still owed on it?", "Which invoice billed SO {order_number}? Is it paid?",
            "{order_number}: invoiced? still owing?"), _order_billed),
    Family("po_billed", "purchasing", "PurchaseOrder", "multi",
           "SELECT po_number, supplier, extract(epoch FROM order_date) AS w FROM biz.purchase_orders WHERE ai_ok AND status IN ('open','closed') "
           "ORDER BY md5(po_number || %(seed)s) LIMIT 4000",
           "SELECT array_agg(DISTINCT document_number) FILTER (WHERE document_number IS NOT NULL) AS docs, sum(original_amount) AS total, "
           "max(original_amount_currency) AS cur FROM biz.supplier_invoices WHERE ai_ok AND NOT pilot AND po_number = %(po_number)s "
           "HAVING count(DISTINCT original_amount_currency) <= 1",
           ("Have we been billed for PO {po_number}? How much?", "Which supplier invoices are against purchase order {po_number}?",
            "has {supplier} invoiced PO {po_number} yet"), _po_billed),
    # ---- follow-up chains (graded on the last turn) -------------------------------------------------
    Family("fu_order_customer_owed", "sales", "SalesOrder", "followup", _so_pool(),
           "SELECT amount_due_currency AS cur, sum(amount_due) AS amt, sum(amount_due) FILTER (WHERE amount_due > 0) AS gross "
           f"FROM biz.customer_balances WHERE ai_ok AND customer_code = (SELECT customer_code FROM {SO} AND order_number = %(order_number)s) GROUP BY 1",
           ("What's the status of order {order_number}? || Who is the customer? || How much do they owe us right now?",
            "Look up SO {order_number} || whose is it || and what's their open balance with us?"), _owed),
    Family("fu_po_supplier_open", "purchasing", "PurchaseOrder", "followup",
           "SELECT po_number, supplier, extract(epoch FROM order_date) AS w FROM biz.purchase_orders WHERE ai_ok AND supplier_handle IS NOT NULL "
           "AND status IN ('open','closed') ORDER BY md5(po_number || %(seed)s) LIMIT 4000",
           "SELECT count(*) AS n, array_agg(po_number ORDER BY order_date DESC) AS pos FROM biz.purchase_orders WHERE ai_ok AND status = 'open' "
           "AND supplier_handle = (SELECT supplier_handle FROM biz.purchase_orders WHERE ai_ok AND po_number = %(po_number)s LIMIT 1)",
           ("Who is PO {po_number} with? || What other POs are open with them?",
            "which vendor is on {po_number} || how many open orders do we have with that vendor"),
           g.count("open POs with the PO's supplier", names_col="pos")),
    Family("fu_item_stock_supplier", "stock", "Stock", "followup",
           "SELECT DISTINCT s.item_code, s.on_hand AS w FROM biz.stock s JOIN biz.purchase_order_lines l ON l.item_code = s.item_code AND l.ai_ok "
           "JOIN biz.purchase_orders p ON p.po_number = l.po_number AND p.status <> 'draft' AND p.order_date IS NOT NULL "
           "WHERE s.ai_ok ORDER BY 1 LIMIT 4000",
           _LAST_BOUGHT,
           ("How many {item_code} do we have on hand? || When did we last buy it? || From which supplier?",
            "stock on {item_code}? || who did we buy it from last time"), _item_last_bought),
    Family("fu_batch_product_hold", "quality", "Batch", "followup",
           "SELECT batch_code, product, 1 AS w FROM biz.batches WHERE ai_ok AND product IS NOT NULL AND batch_code IN "
           "(SELECT batch_code FROM biz.batches GROUP BY 1 HAVING count(*) = 1) ORDER BY md5(batch_code || %(seed)s) LIMIT 4000",
           "SELECT count(*) AS n FROM biz.batches WHERE ai_ok AND quality_status = 'On-Hold' AND status NOT IN ('Cancelled') "
           "AND product = (SELECT product FROM biz.batches WHERE ai_ok AND batch_code = %(batch_code)s)",
           ("What product is batch {batch_code}? || How many batches of that product are on hold?",),
           g.count("on-hold batches of the batch's product")),
    # ---- impossible: an identifier of a real shape that exists nowhere ---------------------------
    *[Family(fid, system, typ, "impossible", _past_series(table, col, slot),
             f"SELECT count(*) AS n FROM {table} WHERE {col} = %({slot})s", asks,
             g.missing("no such record: the answer says it cannot be found (no invented details)"), n=25)
      for fid, system, typ, table, col, slot, asks in (
          ("imp_order", "sales", "SalesOrder", "biz.sales_orders", "order_number", "order_number",
           ("What's the status of order {order_number}?", "who is order {order_number} for?")),
          ("imp_po", "purchasing", "PurchaseOrder", "biz.purchase_orders", "po_number", "po_number",
           ("When will PO {po_number} arrive?", "status of purchase order {po_number}?")),
          ("imp_invoice", "sales", "SalesInvoice", "biz.sales_invoices", "invoice_number", "invoice_number",
           ("How much is invoice {invoice_number} for?", "Has invoice {invoice_number} been paid?")),
          ("imp_batch", "quality", "Batch", "biz.batches", "batch_code", "batch_code",
           ("Has batch {batch_code} passed QC?", "what product is batch {batch_code}")))],
    Family("imp_future_sales", "sales", "SalesInvoice", "impossible",
           "SELECT DISTINCT extract(year FROM %(today)s::date)::int + k AS year, 1 AS w FROM generate_series(2, 4) AS k",
           "SELECT count(*) AS n FROM biz.sales_invoices WHERE extract(year FROM invoice_date) = %(year)s",
           ("What will our total revenue be in {year}?", "Exactly how many orders will we invoice in {year}?"),
           g.missing("a future year: the records cannot say; the answer says it cannot be determined (an estimate labelled as such is fine)"), n=3),
    # ---- false premises -------------------------------------------------------------------------------
    Family("trap_cancelled", "sales", "SalesOrder", "trap", _so_pool("status = 'closed'"),
           f"SELECT status = 'closed' AS ok FROM {SO} AND order_number = %(order_number)s",
           ("Why was order {order_number} cancelled?", "Who cancelled sales order {order_number}?"),
           g.premise("the order was not cancelled (it is closed/shipped): the answer corrects the premise",
                     ("not cancel", "wasn't cancel", "was not cancel", "isn't cancel", "is not cancel", "no record of", "no indication",
                      "closed", "shipped", "invoiced", "completed", "fulfilled"))),
    Family("trap_paid", "ar", "CustomerBalance", "trap",
           "SELECT document_number, customer, days_overdue AS w FROM biz.customer_balances WHERE ai_ok AND document_type = 'invoice' "
           "AND amount_due > 0 ORDER BY md5(document_number || %(seed)s) LIMIT 4000",
           "SELECT bool_and(amount_due > 0) AS ok FROM biz.customer_balances WHERE ai_ok AND document_number = %(document_number)s",
           ("When did {customer} pay invoice {document_number}?", "What date was invoice {document_number} paid?"),
           g.premise("the invoice is still open: the answer says it has not been paid", g.NOT_PAID)),
    Family("trap_po_received", "purchasing", "PurchaseOrder", "trap",
           "SELECT p.po_number, p.supplier, 1 AS w FROM biz.purchase_orders p WHERE p.ai_ok AND p.status = 'open' AND NOT EXISTS "
           "(SELECT 1 FROM biz.purchase_order_lines l WHERE l.po_number = p.po_number AND coalesce(l.received_quantity, 0) > 0) "
           "ORDER BY md5(p.po_number || %(seed)s) LIMIT 4000",
           "SELECT status = 'open' AND NOT EXISTS (SELECT 1 FROM biz.purchase_order_lines l WHERE l.po_number = %(po_number)s "
           "AND coalesce(l.received_quantity, 0) > 0) AS ok FROM biz.purchase_orders WHERE ai_ok AND po_number = %(po_number)s",
           ("When did PO {po_number} arrive?", "What day did we receive {supplier}'s PO {po_number}?"),
           g.premise("nothing has been received on the PO: the answer says so",
                     ("not yet", "not been received", "hasn't", "has not", "no receipt", "nothing received", "not received",
                      "still open", "outstanding", "no record of", "awaiting"))),
]
