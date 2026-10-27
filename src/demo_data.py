"""SYNTHETIC demo data: a small supermarket catalogue and purchase histories for five demo customers.

Everything created here is flagged is_synthetic=1. It is NOT real customer behaviour; the
Kaggle dataset is online gift-ware and contains none of these products.
"""
import json
from datetime import date, datetime, timedelta

from . import categorization, config
from .database import now_iso

PRODUCTS = [  # (stock_code, description, unit price)
    ("DET500", "DETTOL ANTISEPTIC LIQUID 500ML", 210.0),
    ("HAR750", "HARPIC TOILET CLEANER 750ML", 130.0),
    ("SRF1KG", "SURF EXCEL DETERGENT POWDER 1KG", 125.0),
    ("LIZ500", "LIZOL FLOOR CLEANER 500ML", 115.0),
    ("COK750", "COCA-COLA 750ML", 40.0),
    ("LAY52",  "LAYS CLASSIC CHIPS 52G", 20.0),
    ("ICE1L",  "VADILAL ICE CREAM 1L", 220.0),
    ("RICE5",  "BASMATI RICE 5KG", 650.0),
    ("OIL1L",  "SUNFLOWER OIL 1L", 160.0),
    ("DAL1KG", "TOOR DAL 1KG", 150.0),
    ("MLK1L",  "FULL CREAM MILK 1L", 62.0),
    ("CRD400", "FRESH CURD 400G", 45.0),
    ("EGG12",  "EGGS TRAY 12", 84.0),
    ("BRD400", "WHOLE WHEAT BREAD 400G", 45.0),
]

# customer_id -> list of (stock_code, [days_ago, ...]); one invoice per (product, day)
HISTORIES = {
    "DEMO-MERIN":   [("DET500", [5, 18, 30, 45, 60]), ("COK750", [10, 40]), ("BRD400", [12])],
    "DEMO-HANNA":   [("HAR750", [7, 21, 37, 55]), ("RICE5", [14, 50]), ("DET500", [25])],
    "DEMO-DELNA":   [("COK750", [3, 11, 20, 33, 47]), ("LAY52", [3, 11, 33, 47]), ("ICE1L", [28])],
    "DEMO-ANGEL":   [("RICE5", [9, 40, 70]), ("OIL1L", [9, 40, 70]), ("DAL1KG", [15, 50]), ("HAR750", [22])],
    "DEMO-ADWOITH": [("MLK1L", [2, 6, 10, 15, 20, 26]), ("CRD400", [6, 15, 26, 40]), ("EGG12", [10, 30, 55]),
                     ("SRF1KG", [18, 44])],
}


def seed(conn, today=None, recipients_file=None) -> dict:
    today = today or date.today()
    cfg = json.loads((recipients_file or config.RECIPIENTS_FILE).read_text())["recipients"]
    price = {c: p for c, _, p in PRODUCTS}
    for code, desc, _ in PRODUCTS:
        conn.execute("INSERT OR IGNORE INTO products (stock_code, description, is_synthetic) VALUES (?,?,1)",
                     (code, desc))
    for r in cfg:
        conn.execute("INSERT OR IGNORE INTO customers (customer_id, country, display_name, is_synthetic, source) "
                     "VALUES (?,?,?,1,'synthetic_demo')", (r["customer_id"], "India (demo)", r["name"]))
        status = "opted_in" if r.get("consent") else "unknown"
        conn.execute("INSERT OR IGNORE INTO demo_recipients (name, customer_id, phone_env_key, consent_status, "
                     "consent_recorded_at) VALUES (?,?,?,?,?)",
                     (r["name"], r["customer_id"], r["phone_env_key"], status, now_iso() if r.get("consent") else None))
    n_inv = 0
    for cust, plan in HISTORIES.items():
        for code, days in plan:
            for d in days:
                inv = f"DEMO-{cust.split('-')[1]}-{code}-{d}"
                when = (today - timedelta(days=d)).isoformat() + " 10:00:00"
                conn.execute("INSERT OR IGNORE INTO transactions (invoice, customer_id, invoice_date, country, "
                             "is_synthetic) VALUES (?,?,?,?,1)", (inv, cust, when, "India (demo)"))
                qty = 2
                conn.execute("INSERT INTO transaction_items (invoice, stock_code, quantity, price, line_total, "
                             "is_synthetic) SELECT ?,?,?,?,?,1 WHERE NOT EXISTS "
                             "(SELECT 1 FROM transaction_items WHERE invoice=? AND stock_code=?)",
                             (inv, code, qty, price[code], qty * price[code], inv, code))
                n_inv += 1
    conn.commit()
    categorization.apply_to_products(conn, synthetic_only=True)
    return {"products": len(PRODUCTS), "customers": len(HISTORIES), "invoices": n_inv}


def reset_demo(conn) -> dict:
    """Remove ONLY the synthetic demo data, campaigns and audit logs; the real Kaggle data, its segments and the
    experiment are kept. Re-seed afterwards with seed() and re-run `segments`."""
    demo = "(SELECT customer_id FROM customers WHERE is_synthetic=1)"
    for sql in (
        "DELETE FROM notification_logs", "DELETE FROM campaign_recipients", "DELETE FROM campaigns",
        "DELETE FROM offer_proposals", "DELETE FROM offers", "DELETE FROM demo_recipients",
        f"DELETE FROM customer_segment_memberships WHERE customer_id IN {demo}",
        f"DELETE FROM customer_features WHERE customer_id IN {demo}",
        f"DELETE FROM cause_based_segments WHERE customer_id IN {demo}",
        f"DELETE FROM experiment_outcomes WHERE customer_id IN {demo}",
        f"DELETE FROM experiment_assignments WHERE customer_id IN {demo}",
        "DELETE FROM transaction_items WHERE is_synthetic=1", "DELETE FROM transactions WHERE is_synthetic=1",
        "DELETE FROM customers WHERE is_synthetic=1", "DELETE FROM products WHERE is_synthetic=1",
        "DELETE FROM product_preference_segments WHERE segment_id NOT IN "
        "(SELECT segment_id FROM customer_segment_memberships)",
    ):
        conn.execute(sql)
    conn.commit()
    return {"real_customers_kept": conn.execute("SELECT COUNT(*) FROM customers WHERE is_synthetic=0").fetchone()[0],
            "real_items_kept": conn.execute("SELECT COUNT(*) FROM transaction_items WHERE is_synthetic=0").fetchone()[0]}


def reset_campaigns(conn, archive_dir=None) -> dict:
    """Terminal-only reset between presentations. Clears campaign state so Send Offers can run again from scratch:
    message log, campaigns (and their duplicate-protection keys), offer proposals, generated offers, and purchases recorded
    at the counter. KEEPS consent, configured offers (and whether they are active), customers, segments, experiment results
    and the sample purchase history. The old log is archived to CSV files first, so nothing is lost."""
    import pandas as pd
    from . import config
    q = lambda sql: conn.execute(sql).fetchone()[0]
    counts = {"messages": q("SELECT COUNT(*) FROM notification_logs"), "campaigns": q("SELECT COUNT(*) FROM campaigns"),
              "proposals": q("SELECT COUNT(*) FROM offer_proposals"),
              "generated_offers": q("SELECT COUNT(*) FROM offers WHERE origin='generated'"),
              "counter_purchases": q("SELECT COUNT(*) FROM transactions WHERE invoice LIKE 'POS-%'"), "archive": None}
    if any(v for k, v in counts.items() if k != "archive"):
        out = (archive_dir or config.LOGS_DIR / "archive") / f"reset_{datetime.now():%Y%m%d_%H%M%S}"
        out.mkdir(parents=True, exist_ok=True)
        pd.read_sql_query("SELECT * FROM notification_logs", conn).to_csv(out / "notification_logs.csv", index=False)
        pd.read_sql_query("SELECT * FROM campaigns", conn).to_csv(out / "campaigns.csv", index=False)
        pd.read_sql_query("SELECT * FROM offer_proposals", conn).to_csv(out / "offer_proposals.csv", index=False)
        pd.read_sql_query("SELECT t.invoice, t.customer_id, t.invoice_date, i.stock_code, i.quantity, i.line_total FROM transactions t "
                          "JOIN transaction_items i ON i.invoice=t.invoice WHERE t.invoice LIKE 'POS-%'", conn
                          ).to_csv(out / "counter_purchases.csv", index=False)
        counts["archive"] = str(out)
    for sql in ("UPDATE campaign_experiments SET campaign_id=NULL WHERE campaign_id IS NOT NULL",
                "DELETE FROM notification_logs", "DELETE FROM campaign_recipients", "DELETE FROM campaigns",
                "DELETE FROM offer_proposals", "DELETE FROM offers WHERE origin='generated'",
                "DELETE FROM transaction_items WHERE invoice IN (SELECT invoice FROM transactions WHERE invoice LIKE 'POS-%')",
                "DELETE FROM transactions WHERE invoice LIKE 'POS-%'"):
        conn.execute(sql)
    conn.commit()
    return counts
