"""Stage 2 - Python data cleaning for the Kaggle 'Online Retail II' file.

Every decision is logged. Rows are never silently dropped: each removal has a named rule and a count.
Cancellations (invoice starts with 'C') are kept in a separate returns file. Rows with no valid customer id
are kept in a separate guest file (usable for product/sales analysis, not for customer analysis).
Outliers are FLAGGED, not removed (wholesale buyers legitimately order large quantities).
"""
import json
import re
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import categorization, config, database

COLMAP = {"invoice": "invoice", "stockcode": "stock_code", "description": "description", "quantity": "quantity",
          "invoicedate": "invoice_date", "price": "price", "unitprice": "price", "customerid": "customer_id",
          "country": "country"}
REQUIRED = ["invoice", "stock_code", "description", "quantity", "invoice_date", "price", "customer_id", "country"]
TEXT_COLS = ["invoice", "stock_code", "description", "customer_id", "country"]


class CleaningLog:
    def __init__(self):
        self.steps = []

    def add(self, name, decision, before, after):
        self.steps.append({"step_order": len(self.steps) + 1, "step_name": name, "decision": decision,
                           "rows_before": int(before), "rows_after": int(after), "rows_affected": int(before - after)})


@dataclass
class CleaningResult:
    sales: pd.DataFrame            # valid sales with a valid customer id (used for customer analytics)
    guest: pd.DataFrame            # valid sales, no usable customer id
    returns: pd.DataFrame          # cancellations / returns (invoice starts with C)
    log: list = field(default_factory=list)
    reports: dict = field(default_factory=dict)


def standardize_columns(df):
    out = df.copy()
    out.columns = [COLMAP.get(re.sub(r"[^a-z]", "", str(c).lower()), str(c).lower()) for c in out.columns]
    missing = [c for c in REQUIRED if c not in out.columns]
    if missing:
        raise ValueError(f"Missing expected columns: {missing}. Found: {list(out.columns)}")
    return out[REQUIRED]


def missing_report(df):
    n = max(len(df), 1)
    return pd.DataFrame({"column": df.columns, "missing": [int(df[c].isna().sum()) for c in df.columns],
                         "missing_pct": [round(100 * df[c].isna().sum() / n, 2) for c in df.columns]})


def profile(raw):
    return {"rows": int(len(raw)), "columns": [str(c) for c in raw.columns],
            "dtypes": {str(c): str(t) for c, t in raw.dtypes.items()},
            "sample_records": raw.head(5).astype(str).to_dict(orient="records")}


def clean_dataframe(raw) -> CleaningResult:
    log, invalid = CleaningLog(), []
    reports = {"profile": profile(raw)}
    df = standardize_columns(raw)
    n = len(df)
    log.add("load", "Loaded raw rows; standardised column names to snake_case", n, n)

    for c in TEXT_COLS:                                     # trim whitespace, treat '' as missing
        df[c] = df[c].astype("object").str.strip().replace("", np.nan)
    missing_before = missing_report(df)

    d_ok = df["invoice_date"].notna()
    df["invoice_date"] = pd.to_datetime(df["invoice_date"], errors="coerce")
    bad_date = int((d_ok & df["invoice_date"].isna()).sum())
    for c in ("quantity", "price"):
        before_ok = df[c].notna()
        df[c] = pd.to_numeric(df[c], errors="coerce")
        invalid.append((f"non-numeric {c}", int((before_ok & df[c].isna()).sum())))
    invalid.append(("unparseable invoice_date", bad_date))
    log.add("types", "Converted invoice_date to datetime, quantity/price to numbers (bad values become missing)", n, n)

    # 1. required fields
    req_missing = df[["invoice", "stock_code", "invoice_date", "quantity", "price"]].isna().any(axis=1)
    invalid.append(("missing invoice/stock_code/date/quantity/price", int(req_missing.sum())))
    before = len(df)
    df = df[~req_missing]
    log.add("required_fields", "Dropped rows missing invoice, stock code, date, quantity or price", before, len(df))

    # 2. exact duplicates
    dup = df.duplicated()
    before = len(df)
    dup_examples = df[dup].head(5).astype(str).to_dict(orient="records")
    df = df[~dup]
    log.add("duplicates", "Dropped exact duplicate rows (all 8 columns identical), keeping the first", before, len(df))
    reports["duplicates"] = {"duplicate_rows_removed": int(dup.sum()), "examples": dup_examples}

    # 3. cancellations -> separate returns table
    df["invoice"] = df["invoice"].str.upper()
    is_cancel = df["invoice"].str.startswith("C")
    returns = df[is_cancel].copy()
    before = len(df)
    df = df[~is_cancel]
    log.add("cancellations", "Moved invoices starting with 'C' (returns/cancellations) to a separate file", before, len(df))

    # 4. non-standard invoice numbers (e.g. 'A563185' bad-debt adjustments)
    bad_inv = ~df["invoice"].str.match(config.VALID_INVOICE.pattern)
    invalid.append(("invoice is not a 6-digit sales invoice", int(bad_inv.sum())))
    before = len(df)
    df = df[~bad_inv]
    log.add("invoice_format", "Dropped non-sales documents whose invoice is not 6 digits", before, len(df))

    # 5. invalid quantity / price
    bad_q = df["quantity"] <= 0
    invalid.append(("quantity <= 0 (non-cancellation)", int(bad_q.sum())))
    before = len(df)
    df = df[~bad_q]
    bad_p = df["price"] <= 0
    invalid.append(("price <= 0", int(bad_p.sum())))
    df = df[~bad_p]
    log.add("quantity_price", "Dropped rows with quantity <= 0 or price <= 0 (adjustments, free/damaged stock)", before, len(df))

    # 6. non-product stock codes (postage, fees, samples ...)
    df["stock_code"] = df["stock_code"].str.upper()
    non_prod = df["stock_code"].isin(config.NON_PRODUCT_CODES) | df["stock_code"].str.match(config.NON_PRODUCT_PATTERN.pattern)
    invalid.append(("non-product stock code (POST, BANK CHARGES, ...)", int(non_prod.sum())))
    before = len(df)
    df = df[~non_prod]
    log.add("non_products", "Dropped fee/postage/sample/adjustment stock codes - not real products", before, len(df))

    # 7. descriptions: normalise and use one canonical description per stock code
    df["description"] = df["description"].str.upper().str.replace(r"\s+", " ", regex=True)
    canon = df.dropna(subset=["description"]).groupby("stock_code")["description"].agg(lambda s: s.mode().iat[0])
    n_missing_desc = int(df["description"].isna().sum())
    df["description"] = df["stock_code"].map(canon)
    still = int(df["description"].isna().sum())
    df["description"] = df["description"].fillna("UNKNOWN PRODUCT")
    log.add("descriptions", f"Upper-cased text; one canonical description per stock code (most frequent). "
            f"{n_missing_desc} missing filled from the same stock code; {still} left as UNKNOWN PRODUCT", len(df), len(df))

    # 8. customer id: '13085.0' -> '13085'; invalid -> missing
    cid = df["customer_id"].str.replace(r"\.0$", "", regex=True)
    cid = cid.where(cid.str.match(config.VALID_CUSTOMER.pattern, na=False), np.nan)
    invalid.append(("customer_id missing or invalid", int(cid.isna().sum())))
    df["customer_id"] = cid

    # 9. derived fields
    df["line_total"] = (df["quantity"] * df["price"]).round(2)
    df["invoice_day"] = df["invoice_date"].dt.date.astype(str)
    df["year_month"] = df["invoice_date"].dt.strftime("%Y-%m")
    df["weekday"] = df["invoice_date"].dt.day_name()
    df["hour"] = df["invoice_date"].dt.hour
    dept = {d: categorization.classify_giftware(d) for d in df["description"].unique()}
    df["department"] = df["description"].map(lambda d: dept[d][0])
    df["department_needs_review"] = df["description"].map(lambda d: dept[d][2])
    log.add("derived", "Added line_total, invoice_day, year_month, weekday, hour and rule-based gift-ware category (column 'department')", len(df), len(df))

    # 10. outliers - flagged only
    q_hi = df["quantity"].quantile(config.OUTLIER_QUANTILE)
    p_hi = df["price"].quantile(config.OUTLIER_QUANTILE)
    df["is_quantity_outlier"] = (df["quantity"] > q_hi).astype(int)
    df["is_price_outlier"] = (df["price"] > p_hi).astype(int)
    log.add("outliers", f"Flagged (NOT removed) quantity > {q_hi:g} and price > {p_hi:g} "
            f"(99.9th percentiles): {int(df['is_quantity_outlier'].sum())} and {int(df['is_price_outlier'].sum())} rows",
            len(df), len(df))

    # 11. split customers / guests
    guest_mask = df["customer_id"].isna()
    guest, sales = df[guest_mask].copy(), df[~guest_mask].copy()
    log.add("customer_split", "Rows without a valid customer id moved to the guest file (no customer analysis possible)",
            len(df), len(sales))

    after_missing = missing_report(sales)
    reports["missing_values"] = missing_before.merge(after_missing, on="column", how="left",
                                                    suffixes=("_before", "_after")).fillna(0)
    reports["invalid_values"] = pd.DataFrame(invalid + [("exact duplicate rows", int(dup.sum()))],
                                             columns=["rule", "rows"])
    reports["quality_summary"] = pd.DataFrame([
        ("rows", n, len(sales)), ("rows incl. guest sales", n, len(sales) + len(guest)),
        ("return/cancellation rows (separate file)", 0, len(returns)),
        ("distinct invoices", raw.iloc[:, 0].nunique(), sales["invoice"].nunique()),
        ("distinct customers", df_nunique(raw, "customer"), sales["customer_id"].nunique()),
        ("distinct products", df_nunique(raw, "stock"), sales["stock_code"].nunique()),
        ("total sales value", None, round(float(sales["line_total"].sum()), 2)),
    ], columns=["metric", "before", "after"], dtype=object)
    return CleaningResult(sales, guest, returns, log.steps, reports)


def df_nunique(raw, key):
    for c in raw.columns:
        if key in str(c).lower():
            return raw[c].nunique()
    return None


def run_cleaning(path=None, sample=None, cleaned_dir=None, reports_dir=None, logs_dir=None, conn=None):
    path = path or config.RAW_FILE
    cleaned_dir, reports_dir, logs_dir = (cleaned_dir or config.CLEANED_DIR, reports_dir or config.REPORTS_DIR,
                                          logs_dir or config.LOGS_DIR)
    for d in (cleaned_dir, reports_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)
    own = conn is None
    conn = conn or database.connect()
    database.init_db(conn)
    started = time.time()
    run_id = conn.execute("INSERT INTO processing_runs (stage, execution_mode, started_at, notes) VALUES "
                          "('data_cleaning','B_local_python',?,?)",
                          (database.now_iso(), "Pandas on local files (Hadoop/Spark NOT used for this step)")).lastrowid
    conn.commit()
    try:
        raw = pd.read_csv(path, dtype=str, nrows=sample, keep_default_na=True)
        res = clean_dataframe(raw)
        res.sales.to_csv(cleaned_dir / "cleaned_sales.csv", index=False)
        res.guest.to_csv(cleaned_dir / "cleaned_guest_sales.csv", index=False)
        res.returns.to_csv(cleaned_dir / "returns_cancellations.csv", index=False)
        (reports_dir / "profile_original.json").write_text(json.dumps(res.reports["profile"], indent=2))
        (reports_dir / "duplicates_report.json").write_text(json.dumps(res.reports["duplicates"], indent=2))
        res.reports["missing_values"].to_csv(reports_dir / "missing_values_report.csv", index=False)
        res.reports["invalid_values"].to_csv(reports_dir / "invalid_values_report.csv", index=False)
        res.reports["quality_summary"].to_csv(reports_dir / "quality_summary.csv", index=False)
        (logs_dir / "cleaning_log.json").write_text(json.dumps(res.log, indent=2))
        now = database.now_iso()
        for s in res.log:
            conn.execute("INSERT INTO data_cleaning_logs (run_id, step_order, step_name, decision, rows_before, "
                         "rows_after, rows_affected, logged_at) VALUES (?,?,?,?,?,?,?,?)",
                         (run_id, s["step_order"], s["step_name"], s["decision"], s["rows_before"], s["rows_after"],
                          s["rows_affected"], now))
        conn.execute("UPDATE processing_runs SET finished_at=?, duration_seconds=?, rows_in=?, rows_out=?, "
                     "status='success' WHERE run_id=?", (now, round(time.time() - started, 2), len(raw), len(res.sales), run_id))
        conn.commit()
        return res
    except Exception as e:
        conn.execute("UPDATE processing_runs SET finished_at=?, status='failed', notes=? WHERE run_id=?",
                     (database.now_iso(), str(e)[:300], run_id))
        conn.commit()
        raise
    finally:
        if own:
            conn.close()
