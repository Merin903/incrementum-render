import numpy as np
import pandas as pd

from src import database

CATS = ["Lights & Candles", "Kitchen & Dining", "Home Decor"]


def make_experiment_db(n_customers=1500, seed=1):
    """Real-style (is_synthetic=0) customer base over 2010, used only to test the experiment code."""
    conn = database.connect(":memory:")
    database.init_db(conn)
    rng = np.random.default_rng(seed)
    prods = []
    for i in range(30):
        prods.append((f"P{i}", f"ITEM {i}", CATS[i % 3] if i < 27 else "Other / Uncertain"))
    conn.executemany("INSERT INTO products (stock_code, description, department, is_synthetic) VALUES (?,?,?,0)", prods)
    by_cat = {c: [p[0] for p in prods if p[2] == c] for c in CATS}
    conn.executemany("INSERT INTO customers (customer_id, country, is_synthetic) VALUES (?,'UK',0)",
                     [(str(10000 + i),) for i in range(n_customers)])
    tx, items, inv = [], [], 0
    for i in range(n_customers):
        cid, fav = str(10000 + i), CATS[i % 3]
        for _ in range(max(1, int(rng.lognormal(2.0, 0.6)))):
            inv += 1
            day = pd.Timestamp("2010-01-01") + pd.Timedelta(days=int(rng.integers(0, 365)))
            tx.append((str(inv), cid, day.strftime("%Y-%m-%d 11:00:00")))
            for _ in range(int(rng.integers(1, 4))):
                cat = fav if rng.random() < 0.75 else CATS[int(rng.integers(0, 3))]
                code = str(rng.choice(by_cat[cat]))
                q = int(rng.integers(1, 6))
                items.append((str(inv), code, q, 3.0, 3.0 * q))
    conn.executemany("INSERT INTO transactions (invoice, customer_id, invoice_date, country, is_synthetic) VALUES (?,?,?,'UK',0)", tx)
    conn.executemany("INSERT INTO transaction_items (invoice, stock_code, quantity, price, line_total, is_synthetic) VALUES (?,?,?,?,?,0)", items)
    conn.commit()
    return conn
