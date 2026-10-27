"""MODE B - plain Pandas on local files. Produces the SAME output tables as spark_processing.
Every message says 'local Pandas': this module never claims to use Hadoop or Spark."""
import time

import pandas as pd

from . import config, database

OUTPUTS = ["sales_summary", "monthly_sales", "top_products", "department_sales", "customer_rfm_base"]


def compute(sales: pd.DataFrame) -> dict:
    sales = sales.copy()
    sales["customer_id"] = sales["customer_id"].astype(str)
    sales["day"] = pd.to_datetime(sales["invoice_date"]).dt.normalize()
    n_inv = sales["invoice"].nunique()
    total = sales["line_total"].sum()
    out = {"sales_summary": pd.DataFrame([{
        "total_sales": round(total, 2), "transactions": n_inv, "customers": sales["customer_id"].nunique(),
        "products": sales["stock_code"].nunique(), "avg_transaction_value": round(total / n_inv, 2)}])}
    m = sales.groupby("year_month").agg(sales=("line_total", "sum"), transactions=("invoice", "nunique"),
                                        customers=("customer_id", "nunique")).reset_index()
    m["sales"] = m["sales"].round(2)
    out["monthly_sales"] = m.sort_values("year_month")
    t = sales.groupby(["stock_code", "description"]).agg(units=("quantity", "sum"), revenue=("line_total", "sum"),
                                                         transactions=("invoice", "nunique")).reset_index()
    t["revenue"] = t["revenue"].round(2)
    out["top_products"] = t.sort_values("revenue", ascending=False).head(50)
    d = sales.groupby("department").agg(sales=("line_total", "sum"), products=("stock_code", "nunique"),
                                        customers=("customer_id", "nunique")).reset_index()
    d["sales"] = d["sales"].round(2)
    out["department_sales"] = d.sort_values("sales", ascending=False)
    inv = sales.groupby(["customer_id", "invoice"]).agg(d=("day", "min"), t=("line_total", "sum"),
                                                        q=("quantity", "sum")).reset_index()
    ref = sales["day"].max() + pd.Timedelta(days=1)
    r = inv.groupby("customer_id").agg(last_purchase_date=("d", "max"), frequency=("invoice", "count"),
                                       monetary=("t", "sum"), avg_basket_value=("t", "mean"),
                                       avg_quantity=("q", "mean")).reset_index()
    r["recency_days"] = (ref - r["last_purchase_date"]).dt.days
    r["last_purchase_date"] = r["last_purchase_date"].dt.date.astype(str)
    for c in ("monetary", "avg_basket_value", "avg_quantity"):
        r[c] = r[c].round(2)
    out["customer_rfm_base"] = r[["customer_id", "last_purchase_date", "recency_days", "frequency", "monetary",
                                  "avg_basket_value", "avg_quantity"]]
    return out


def run_local(conn=None, cleaned=None, out_dir=None):
    out_dir = out_dir or config.PROCESSED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    own = conn is None
    conn = conn or database.connect()
    t0 = time.time()
    run_id = database.start_run(conn, "processing_pandas", "B_local_python",
                                "LOCAL Pandas processing - Hadoop/Spark NOT used")
    try:
        sales = pd.read_csv(cleaned or (config.CLEANED_DIR / "cleaned_sales.csv"), dtype={"customer_id": str})
        res = compute(sales)
        for name, df in res.items():
            df.to_csv(out_dir / f"{name}.csv", index=False)
        database.finish_run(conn, run_id, t0, len(sales), len(res["customer_rfm_base"]))
        return res
    except Exception as e:
        database.finish_run(conn, run_id, t0, status="failed", notes=str(e)[:300])
        raise
    finally:
        if own:
            conn.close()
