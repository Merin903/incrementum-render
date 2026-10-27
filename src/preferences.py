"""Customer product preferences inside one department (simple frequency analysis)."""
from datetime import date, timedelta

from . import config


def customer_preferences(conn, customer_id, department, today=None, lookback_days=None, min_purchases=None):
    today = today or date.today()
    lookback_days = lookback_days or config.LOOKBACK_DAYS
    min_purchases = min_purchases or config.MIN_PURCHASES
    start = today - timedelta(days=lookback_days)
    rows = conn.execute(
        """SELECT p.stock_code, p.description, COUNT(DISTINCT t.invoice) AS purchases,
                  SUM(i.quantity) AS quantity, MAX(t.invoice_date) AS last_purchase
           FROM transactions t
           JOIN transaction_items i ON i.invoice = t.invoice
           JOIN products p ON p.stock_code = i.stock_code
           WHERE t.customer_id = ? AND p.department = ?
             AND date(t.invoice_date) >= date(?) AND date(t.invoice_date) <= date(?)
           GROUP BY p.stock_code, p.description
           ORDER BY purchases DESC, quantity DESC, last_purchase DESC""",
        (customer_id, department, str(start), str(today))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["frequent"] = d["purchases"] >= min_purchases
        d["reason"] = (f"bought {d['description']} on {d['purchases']} separate occasion(s) in the last "
                       f"{lookback_days} days")
        out.append(d)
    return out
