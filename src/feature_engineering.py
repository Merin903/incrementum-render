"""Customer-level features, computed in Python from the relational database.

How each feature is calculated (also printed by `python -m src.main features --explain`):
"""
from datetime import date

import numpy as np
import pandas as pd

from . import config
from .database import now_iso

EXPLANATIONS = {
    "recency_days": "Days from the customer's last purchase to the reference date. Lower = more recent.",
    "frequency": "Number of distinct invoices (shopping trips) the customer made.",
    "monetary": "Total money spent: the sum of quantity x price over all the customer's lines.",
    "avg_basket_value": "monetary / frequency: average spend per invoice.",
    "avg_quantity": "Average number of units per invoice.",
    "last_purchase_date": "Date of the customer's most recent invoice.",
    "top_category": "Category with the most spend among CLASSIFIED products (uncertain products are ignored).",
    "top_product": "Product appearing on the most of the customer's invoices (ties: higher spend).",
    "avg_reorder_interval_days": "Average gap in days between the customer's consecutive purchase days. Needs at "
                                 "least 3 purchase days (2 gaps); otherwise left empty. A RULE-BASED ESTIMATE, not a forecast.",
}
REFERENCE_NOTE = ("Reference date: real Kaggle customers use (last invoice date in the dataset + 1 day), because the "
                  "data ends in 2011; synthetic demo customers use today's date.")


def load_lines(conn) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT t.customer_id, t.invoice, t.invoice_date, i.stock_code, i.quantity, i.line_total, "
        "p.description, p.department, c.is_synthetic "
        "FROM transaction_items i JOIN transactions t ON t.invoice=i.invoice "
        "JOIN products p ON p.stock_code=i.stock_code JOIN customers c ON c.customer_id=t.customer_id", conn)
    df["day"] = pd.to_datetime(df["invoice_date"]).dt.normalize()
    return df


def reference_dates(lines, today=None) -> dict:
    today = pd.Timestamp(today or date.today())
    real = lines[lines["is_synthetic"] == 0]
    return {0: (real["day"].max() + pd.Timedelta(days=1)) if len(real) else today,
            1: today + pd.Timedelta(days=1)}


def compute_features(lines: pd.DataFrame, today=None) -> pd.DataFrame:
    refs = reference_dates(lines, today)
    inv = lines.groupby(["customer_id", "invoice"]).agg(
        day=("day", "min"), total=("line_total", "sum"), qty=("quantity", "sum"),
        synthetic=("is_synthetic", "max")).reset_index()
    f = inv.groupby("customer_id").agg(
        frequency=("invoice", "count"), monetary=("total", "sum"), avg_basket_value=("total", "mean"),
        avg_quantity=("qty", "mean"), last_day=("day", "max"), synthetic=("synthetic", "max")).reset_index()
    f["recency_days"] = [(refs[int(s)] - d).days for s, d in zip(f["synthetic"], f["last_day"])]

    gaps = {}
    for cid, days in inv.groupby("customer_id")["day"]:
        d = np.sort(days.dt.normalize().unique())
        if len(d) >= 3:
            gaps[cid] = float(np.mean(np.diff(d).astype("timedelta64[D]").astype(int)))
    f["avg_reorder_interval_days"] = f["customer_id"].map(gaps)

    classified = lines[lines["department"] != config.UNCERTAIN]
    top_cat = classified.groupby(["customer_id", "department"])["line_total"].sum().reset_index()
    top_cat = top_cat.sort_values(["customer_id", "line_total"], ascending=[True, False]).drop_duplicates("customer_id")
    f["top_category"] = f["customer_id"].map(dict(zip(top_cat["customer_id"], top_cat["department"])))
    tp = lines.groupby(["customer_id", "description"]).agg(n=("invoice", "nunique"), spend=("line_total", "sum")).reset_index()
    tp = tp.sort_values(["customer_id", "n", "spend"], ascending=[True, False, False]).drop_duplicates("customer_id")
    f["top_product"] = f["customer_id"].map(dict(zip(tp["customer_id"], tp["description"])))

    f["last_purchase_date"] = f["last_day"].dt.date.astype(str)
    for c in ("monetary", "avg_basket_value", "avg_quantity", "avg_reorder_interval_days"):
        f[c] = f[c].round(2)
    cols = ["customer_id", "recency_days", "frequency", "monetary", "avg_basket_value", "avg_quantity",
            "last_purchase_date", "top_category", "top_product", "avg_reorder_interval_days"]
    return f[cols].merge(inv.groupby("customer_id")["synthetic"].max().rename("is_synthetic").reset_index(), on="customer_id")


def save_features(conn, feats: pd.DataFrame) -> int:
    conn.execute("DELETE FROM customer_features")
    stamp = now_iso()
    rows = [(r.customer_id, int(r.recency_days), int(r.frequency), float(r.monetary), float(r.avg_basket_value),
             float(r.avg_quantity), r.last_purchase_date, r.top_category, r.top_product,
             None if pd.isna(r.avg_reorder_interval_days) else float(r.avg_reorder_interval_days), stamp)
            for r in feats.itertuples()]
    conn.executemany("INSERT INTO customer_features (customer_id, recency_days, frequency, monetary, avg_basket_value, "
                     "avg_quantity, last_purchase_date, top_category, top_product, avg_reorder_interval_days, "
                     "computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)
