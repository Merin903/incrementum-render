"""Load the cleaned Kaggle sales into the relational database (chunked, repeatable)."""
import time

import pandas as pd

from . import config, database

CHUNK = 200_000


def load_cleaned_to_db(conn, path=None, replace=True, chunk=CHUNK) -> dict:
    path = path or (config.CLEANED_DIR / "cleaned_sales.csv")
    t0 = time.time()
    run_id = database.start_run(conn, "load_database", "B_local_python", "Pandas chunks -> SQLite")
    try:
        if replace:  # remove only previously loaded REAL rows; synthetic demo data is never touched
            conn.execute("DELETE FROM transaction_items WHERE is_synthetic=0")
            conn.execute("DELETE FROM transactions WHERE is_synthetic=0")
            conn.execute("DELETE FROM customers WHERE is_synthetic=0 AND customer_id NOT IN "
                         "(SELECT customer_id FROM demo_recipients)")
            conn.execute("DELETE FROM products WHERE is_synthetic=0 AND stock_code NOT IN (SELECT stock_code FROM offers)")
        rows_in = 0
        dtypes = {"invoice": str, "stock_code": str, "customer_id": str}
        for df in pd.read_csv(path, dtype=dtypes, chunksize=chunk):
            rows_in += len(df)
            c = df.drop_duplicates("customer_id")
            conn.executemany("INSERT OR IGNORE INTO customers (customer_id, country, is_synthetic, source) "
                             "VALUES (?,?,0,'kaggle_online_retail_ii')", list(zip(c.customer_id, c.country)))
            p = df.drop_duplicates("stock_code")
            conn.executemany("INSERT OR IGNORE INTO products (stock_code, description, department, category_source, "
                             "needs_review, is_synthetic) VALUES (?,?,?,'rule',?,0)",
                             list(zip(p.stock_code, p.description, p.department, p.department_needs_review.astype(int))))
            t = df.drop_duplicates("invoice")
            conn.executemany("INSERT OR IGNORE INTO transactions (invoice, customer_id, invoice_date, country, "
                             "is_synthetic) VALUES (?,?,?,?,0)", list(zip(t.invoice, t.customer_id, t.invoice_date, t.country)))
            conn.executemany("INSERT INTO transaction_items (invoice, stock_code, quantity, price, line_total, "
                             "is_synthetic) VALUES (?,?,?,?,?,0)",
                             list(zip(df.invoice, df.stock_code, df.quantity.round().astype(int), df.price, df.line_total)))
            conn.commit()
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE is_synthetic=0").fetchone()[0]
                  for t in ("transactions", "transaction_items", "products")}
        counts["customers"] = conn.execute("SELECT COUNT(*) FROM customers WHERE is_synthetic=0").fetchone()[0]
        database.finish_run(conn, run_id, t0, rows_in, counts["transaction_items"])
        counts["rows_read"] = rows_in
        return counts
    except Exception as e:
        database.finish_run(conn, run_id, t0, status="failed", notes=str(e)[:300])
        raise
