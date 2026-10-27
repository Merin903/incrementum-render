import pathlib
import tempfile

import pandas as pd

from src import data_cleaning as dc, hdfs_manager, local_processing as lp, loader
from .helpers import make_db
from .test_data_cleaning import raw_frame


def cleaned_csv():
    tmp = pathlib.Path(tempfile.mkdtemp())
    raw = tmp / "raw.csv"
    raw_frame().to_csv(raw, index=False)
    conn = make_db(consent=False)
    dc.run_cleaning(raw, cleaned_dir=tmp / "c", reports_dir=tmp / "r", logs_dir=tmp / "l", conn=conn)
    return tmp, conn, tmp / "c" / "cleaned_sales.csv"


def test_loader_inserts_real_rows_and_keeps_demo_data():
    tmp, conn, csv = cleaned_csv()
    counts = loader.load_cleaned_to_db(conn, csv)
    assert counts["customers"] == 2 and counts["transactions"] == 3 and counts["transaction_items"] == 4
    assert conn.execute("SELECT COUNT(*) FROM customers WHERE is_synthetic=1").fetchone()[0] == 5      # demo untouched
    assert conn.execute("SELECT COUNT(*) FROM transaction_items WHERE is_synthetic=1").fetchone()[0] == 49
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []                                   # integrity holds


def test_loader_is_repeatable_without_duplicating():
    tmp, conn, csv = cleaned_csv()
    a = loader.load_cleaned_to_db(conn, csv)
    b = loader.load_cleaned_to_db(conn, csv)
    assert a["transaction_items"] == b["transaction_items"] == 4
    assert conn.execute("SELECT COUNT(*) FROM transaction_items").fetchone()[0] == 49 + 4


def test_loader_works_in_small_chunks():
    tmp, conn, csv = cleaned_csv()
    assert loader.load_cleaned_to_db(conn, csv, chunk=1)["transaction_items"] == 4


def test_loader_records_a_processing_run():
    tmp, conn, csv = cleaned_csv()
    loader.load_cleaned_to_db(conn, csv)
    run = conn.execute("SELECT * FROM processing_runs WHERE stage='load_database'").fetchone()
    assert run["status"] == "success" and run["rows_in"] == 4


def test_local_processing_numbers_are_correct():
    tmp, conn, csv = cleaned_csv()
    res = lp.run_local(conn, csv, tmp / "p")
    s = res["sales_summary"].iloc[0]
    sales = pd.read_csv(csv)
    assert s["total_sales"] == round(sales["line_total"].sum(), 2) and s["transactions"] == 3 and s["customers"] == 2
    rfm = res["customer_rfm_base"].set_index("customer_id")
    assert rfm.loc["13085", "frequency"] == 1 and rfm.loc["13085", "monetary"] == 164.4   # 12*6.95 + 12*6.75
    # reference date = last invoice day in the data + 1 day (2009-12-03), so recency is 2 and 1 days
    assert rfm.loc["13085", "recency_days"] == 2 and rfm.loc["14000", "recency_days"] == 1
    for name in lp.OUTPUTS:
        assert (tmp / "p" / f"{name}.csv").exists()


def test_local_processing_is_labelled_as_local_not_spark():
    tmp, conn, csv = cleaned_csv()
    lp.run_local(conn, csv, tmp / "p")
    run = conn.execute("SELECT * FROM processing_runs WHERE stage='processing_pandas'").fetchone()
    assert run["execution_mode"] == "B_local_python" and "NOT used" in run["notes"]


def test_hdfs_manager_reports_unavailable_without_pretending():
    import os
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = "/nonexistent"
    try:
        assert hdfs_manager.is_available() is False
    finally:
        os.environ["PATH"] = old
