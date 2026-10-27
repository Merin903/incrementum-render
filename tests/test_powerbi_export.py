import os
import pathlib
import re
import tempfile

import pandas as pd

from src import campaign_engine as ce, product_segmentation as ps, powerbi_export as pbi
from .helpers import TODAY, make_db, set_env
from .test_segmentation import add_real_customers


def exported(env=None):
    conn = make_db()                                               # demo customers, consent granted
    add_real_customers(conn, n=60)
    ce.run_active_offer_campaign(conn, "Non-Food & Household", today=TODAY)
    ps.run_segmentation(conn, TODAY)
    out = pathlib.Path(tempfile.mkdtemp())
    return conn, out, pbi.export_powerbi(conn, out, out / "none", out / "none")


def test_all_tables_exported_and_manifest_matches_files():
    conn, out, m = exported()
    for t in ("fact_sales", "dim_customer", "dim_product", "dim_date", "dim_segment", "fact_segment_membership", "dim_offer",
              "dim_campaign", "fact_notification_log", "dim_experiment", "fact_experiment_outcome", "fact_cause_segment",
              "data_cleaning_log", "processing_runs", "fact_demo_purchases", "fact_offer_proposal"):
        assert (out / f"{t}.csv").exists(), t
    for r in m.itertuples():
        assert len(pd.read_csv(out / r.file)) == r.rows
    assert set(m["data_label"]) >= {"real (Kaggle)", "synthetic (demo customers)"}


def test_star_schema_keys_are_consistent_for_power_bi_relationships():
    conn, out, m = exported()
    rd = lambda n: pd.read_csv(out / f"{n}.csv", dtype=str)
    sales, cust, prod, date = rd("fact_sales"), rd("dim_customer"), rd("dim_product"), rd("dim_date")
    assert set(sales["customer_id"]) <= set(cust["customer_id"]) and set(sales["stock_code"]) <= set(prod["stock_code"])
    assert set(sales["date_key"]) <= set(date["date_key"]) and date["date_key"].is_unique
    assert cust["customer_id"].is_unique and prod["stock_code"].is_unique
    mem, seg = rd("fact_segment_membership"), rd("dim_segment")
    assert set(mem["segment_id"]) <= set(seg["segment_id"]) and set(mem["customer_id"]) <= set(cust["customer_id"])
    log = rd("fact_notification_log")
    assert len(log) == 5 and set(log["customer_id"]) <= set(cust["customer_id"])


def test_real_and_synthetic_data_are_kept_apart():
    conn, out, m = exported()
    sales = pd.read_csv(out / "fact_sales.csv", dtype=str)
    assert not sales["customer_id"].str.startswith("DEMO").any()                       # demo purchases never mixed into sales facts
    assert pd.read_csv(out / "fact_demo_purchases.csv")["customer_id"].str.startswith("DEMO").all()
    cust = pd.read_csv(out / "dim_customer.csv")
    assert set(cust["data_label"]) == {"synthetic demo", "real (Kaggle)"}


def test_notification_statuses_are_exported_unchanged():
    conn, out, m = exported()
    log = pd.read_csv(out / "fact_notification_log.csv")
    assert set(log["status"]) == {"simulated", "skipped"} and (log[log["status"] == "simulated"]["delivery_mode"] == "simulated").all()
    runs = pd.read_csv(out / "processing_runs.csv")
    assert len(runs) == 0 or runs["execution_mode_label"].notna().all()


def test_no_phone_numbers_or_secrets_in_any_export():
    with set_env({"DEMO_PHONE_MERIN": "+919876543210", "TWILIO_AUTH_TOKEN": "secret-token-abc"}):
        conn, out, m = exported()
        blob = "".join(p.read_text() for p in out.glob("*.csv"))
    assert "9876543210" not in blob and "secret-token-abc" not in blob and "+91" not in blob


def test_dim_date_is_continuous():
    conn, out, m = exported()
    d = pd.to_datetime(pd.read_csv(out / "dim_date.csv")["date"])
    assert (d.diff().dropna() == pd.Timedelta(days=1)).all()


def test_copies_existing_python_reports_when_present():
    conn = make_db()
    add_real_customers(conn, n=20)
    base = pathlib.Path(tempfile.mkdtemp())
    (base / "reports").mkdir()
    pd.DataFrame({"metric": ["rows"], "before": [10], "after": [8]}).to_csv(base / "reports" / "quality_summary.csv", index=False)
    m = pbi.export_powerbi(conn, base / "out", base / "none", base / "reports")
    assert (base / "out" / "quality_summary.csv").exists() and "quality_summary" in set(m["table"])
