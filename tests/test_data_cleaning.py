import json
import pathlib
import tempfile

import pandas as pd

from src import data_cleaning as dc
from .helpers import make_db

COLS = ["Invoice", "StockCode", "Description", "Quantity", "InvoiceDate", "Price", "Customer ID", "Country"]


def raw_frame():
    rows = [
        ["489434", "85048", "15CM CHRISTMAS GLASS BALL 20 LIGHTS", "12", "2009-12-01 07:45:00", "6.95", "13085.0", "United Kingdom"],   # 0 good
        ["489434", "79323P", "PINK CHERRY LIGHTS", "12", "2009-12-01 07:45:00", "6.75", "13085.0", "United Kingdom"],                   # 1 good
        ["489434", "79323P", "PINK CHERRY LIGHTS", "12", "2009-12-01 07:45:00", "6.75", "13085.0", "United Kingdom"],                   # 2 exact duplicate of 1
        ["C489449", "22350", "CAT BOWL", "-12", "2009-12-01 10:33:00", "2.55", "16321.0", "Australia"],                                # 3 cancellation
        ["A563185", "B", "Adjust bad debt", "1", "2011-08-12 14:50:00", "11062.06", "", "United Kingdom"],                             # 4 non-sales invoice
        ["489500", "22111", "SCOTTIE DOG HOT WATER BOTTLE", "-3", "2009-12-01 11:00:00", "4.95", "12345.0", "United Kingdom"],         # 5 negative qty
        ["489501", "22112", "CHOCOLATE HOT WATER BOTTLE", "2", "2009-12-01 11:00:00", "0", "12345.0", "United Kingdom"],               # 6 zero price
        ["489502", "POST", "POSTAGE", "1", "2009-12-01 11:05:00", "18.00", "12345.0", "United Kingdom"],                               # 7 non-product
        ["489503", "21232", "STRAWBERRY CERAMIC TRINKET BOX", "24", "2009-12-01 12:00:00", "1.25", "", "United Kingdom"],              # 8 guest (no customer)
        ["489504", "21232", "", "6", "2009-12-02 09:00:00", "1.25", "14000.0", "United Kingdom"],                                      # 9 missing description, fillable
        ["489505", "84029G", "  KNITTED UNION FLAG   HOT WATER BOTTLE ", "6", "2009-12-02 09:10:00", "3.39", "14000.0", "United Kingdom"],  # 10 whitespace
        ["489506", "22111", "SCOTTIE DOG HOT WATER BOTTLE", "abc", "2009-12-02 10:00:00", "4.95", "14000.0", "United Kingdom"],       # 11 bad quantity
        ["489507", "22111", "SCOTTIE DOG HOT WATER BOTTLE", "5", "not-a-date", "4.95", "14000.0", "United Kingdom"],                  # 12 bad date
        ["489508", "20001", "DETTOL TEST SOAP", "3", "2009-12-02 10:30:00", "2.00", "99", "United Kingdom"],                           # 13 invalid customer id (too short)
    ]
    return pd.DataFrame(rows, columns=COLS)


def test_each_cleaning_rule_removes_exactly_the_right_rows():
    r = dc.clean_dataframe(raw_frame())
    kept = set(r.sales["invoice"]) | set(r.guest["invoice"])
    assert set(r.returns["invoice"]) == {"C489449"}
    assert kept == {"489434", "489503", "489504", "489505", "489508"}      # 4,5,6,7,11,12 dropped; 3 -> returns
    assert len(r.sales) == 4 and len(r.guest) == 2                           # 0,1,9,10 sales; 8 and 13 guests
    inv = dict(zip(r.reports["invalid_values"]["rule"], r.reports["invalid_values"]["rows"]))
    assert inv["exact duplicate rows"] == 1
    assert inv["price <= 0"] == 1 and inv["quantity <= 0 (non-cancellation)"] == 1
    assert inv["non-product stock code (POST, BANK CHARGES, ...)"] == 1
    assert inv["invoice is not a 6-digit sales invoice"] == 1


def test_customer_ids_normalised_and_invalid_ones_become_guests():
    r = dc.clean_dataframe(raw_frame())
    assert set(r.sales["customer_id"]) == {"13085", "14000"}                 # '13085.0' -> '13085'
    assert set(r.guest["invoice"]) == {"489503", "489508"}                   # blank id, and 2-digit id '99'


def test_text_cleaning_and_canonical_description():
    r = dc.clean_dataframe(raw_frame())
    s = r.sales.set_index("invoice")
    assert "KNITTED UNION FLAG HOT WATER BOTTLE" in set(r.sales["description"])      # trimmed, spaces collapsed
    row = r.sales[(r.sales["invoice"] == "489504")].iloc[0]
    assert row["description"] == "STRAWBERRY CERAMIC TRINKET BOX"                    # filled from same stock code
    assert (r.sales["description"] != "UNKNOWN PRODUCT").all()


def test_derived_fields_and_outliers_flagged_not_removed():
    r = dc.clean_dataframe(raw_frame())
    row = r.sales[r.sales["stock_code"] == "85048"].iloc[0]
    assert row["line_total"] == 83.4 and row["year_month"] == "2009-12" and row["weekday"] == "Tuesday"
    assert {"is_quantity_outlier", "is_price_outlier", "department"} <= set(r.sales.columns)
    assert len(r.sales) == 4


def test_missing_value_report_before_and_after():
    r = dc.clean_dataframe(raw_frame())
    m = r.reports["missing_values"].set_index("column")
    assert m.loc["customer_id", "missing_before"] > 0 and m.loc["customer_id", "missing_after"] == 0


def test_standard_column_names_and_alternate_headers():
    alt = raw_frame().rename(columns={"Price": "UnitPrice", "Customer ID": "CustomerID"})
    assert list(dc.standardize_columns(alt).columns) == dc.REQUIRED


def test_missing_column_raises_clear_error():
    from .helpers import raises
    assert raises(ValueError, dc.clean_dataframe, raw_frame().drop(columns=["Price"]))


def test_run_cleaning_writes_files_reports_and_database_log():
    tmp = pathlib.Path(tempfile.mkdtemp())
    csv = tmp / "raw.csv"
    raw_frame().to_csv(csv, index=False)
    conn = make_db(consent=False)
    dc.run_cleaning(csv, cleaned_dir=tmp / "c", reports_dir=tmp / "r", logs_dir=tmp / "l", conn=conn)
    for f in ("c/cleaned_sales.csv", "c/cleaned_guest_sales.csv", "c/returns_cancellations.csv", "r/profile_original.json",
              "r/missing_values_report.csv", "r/duplicates_report.json", "r/invalid_values_report.csv",
              "r/quality_summary.csv", "l/cleaning_log.json"):
        assert (tmp / f).exists(), f
    assert len(pd.read_csv(tmp / "c/cleaned_sales.csv")) == 4
    assert len(json.loads((tmp / "l/cleaning_log.json").read_text())) >= 10
    run = conn.execute("SELECT * FROM processing_runs WHERE stage='data_cleaning'").fetchone()
    assert run["status"] == "success" and run["execution_mode"] == "B_local_python" and run["rows_out"] == 4
    assert conn.execute("SELECT COUNT(*) FROM data_cleaning_logs").fetchone()[0] >= 10
