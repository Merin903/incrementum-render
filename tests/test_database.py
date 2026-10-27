from src import database
from .helpers import make_db, raises
import sqlite3


def test_tables_exist_and_seed_data_loaded():
    conn = make_db()
    info = {t["table"]: t["rows"] for t in database.inspect_db(conn)}
    for t in ("customers", "products", "transactions", "transaction_items", "offers", "campaigns",
              "campaign_recipients", "notification_logs", "offer_proposals", "demo_recipients"):
        assert t in info
    assert info["customers"] == 5 and info["products"] == 14 and info["transaction_items"] == 49


def test_constraints_enforced():
    conn = make_db()
    assert raises(sqlite3.IntegrityError, conn.execute,
                  "INSERT INTO transaction_items (invoice, stock_code, quantity, price, line_total) VALUES ('NOPE','DET500',1,1,1)")
    assert raises(sqlite3.IntegrityError, conn.execute,
                  "INSERT INTO notification_logs (campaign_id, customer_id, delivery_mode, attempted_at, status) VALUES (1,'DEMO-MERIN','simulated','x','delivered_maybe')")


def test_synthetic_flag_set_on_demo_rows():
    conn = make_db()
    assert conn.execute("SELECT COUNT(*) FROM customers WHERE is_synthetic=0").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE is_synthetic=0").fetchone()[0] == 0
