"""Power BI export: one stable folder of tidy CSV tables (star schema). Python did ALL the cleaning, analysis,
segmentation and offer logic; Power BI only displays these tables.

Every table is labelled in export_manifest.csv as real (Kaggle), synthetic (demo customers) or simulated
(experiment / notification simulator) so the dashboard can show that distinction.
"""
import shutil
from datetime import datetime

import pandas as pd

from . import config

SQL = {
    "fact_sales": ("real (Kaggle)",
        "SELECT t.invoice, substr(t.invoice_date,1,10) AS invoice_date, "
        "CAST(replace(substr(t.invoice_date,1,10),'-','') AS INTEGER) AS date_key, t.customer_id, i.stock_code, "
        "i.quantity, i.price, i.line_total, t.country FROM transaction_items i JOIN transactions t ON t.invoice=i.invoice "
        "WHERE t.is_synthetic=0"),
    "fact_demo_purchases": ("synthetic (demo customers)",
        "SELECT t.invoice, substr(t.invoice_date,1,10) AS invoice_date, t.customer_id, i.stock_code, i.quantity, i.price, "
        "i.line_total FROM transaction_items i JOIN transactions t ON t.invoice=i.invoice WHERE t.is_synthetic=1"),
    "dim_customer": ("mixed: real + synthetic (see data_label column)",
        "SELECT c.customer_id, c.country, c.display_name, c.is_synthetic, "
        "CASE c.is_synthetic WHEN 1 THEN 'synthetic demo' ELSE 'real (Kaggle)' END AS data_label, "
        "f.recency_days, f.frequency, f.monetary, f.avg_basket_value, f.avg_quantity, f.last_purchase_date, "
        "f.top_category, f.top_product, f.avg_reorder_interval_days, "
        "(SELECT s.segment_name FROM customer_segment_memberships m JOIN product_preference_segments s USING(segment_id) "
        " WHERE m.customer_id=c.customer_id AND s.segment_type='rfm_rule' LIMIT 1) AS rfm_segment, "
        "(SELECT s.segment_name FROM customer_segment_memberships m JOIN product_preference_segments s USING(segment_id) "
        " WHERE m.customer_id=c.customer_id AND s.segment_type='rfm_cluster' LIMIT 1) AS kmeans_cluster "
        "FROM customers c LEFT JOIN customer_features f ON f.customer_id=c.customer_id"),
    "dim_product": ("mixed: real + synthetic (derived categories)",
        "SELECT stock_code, description, department AS category, category_confidence, category_source, needs_review, "
        "is_synthetic FROM products"),
    "dim_segment": ("derived by Python",
        "SELECT segment_id, segment_name, segment_type, product_or_category, rule_description FROM product_preference_segments"),
    "fact_segment_membership": ("derived by Python",
        "SELECT membership_id, customer_id, segment_id, strength, reason FROM customer_segment_memberships"),
    "dim_offer": ("configured (demo) + generated proposals",
        "SELECT o.offer_id, o.offer_code, o.stock_code, p.description AS product_name, o.department, o.discount_percent, "
        "o.promo_text, o.valid_from, o.valid_to, o.origin, o.status FROM offers o JOIN products p ON p.stock_code=o.stock_code"),
    "fact_offer_proposal": ("synthetic demo customers; proposals until approved",
        "SELECT pr.proposal_id, o.offer_code, pr.customer_id, pr.stock_code, pr.department, pr.purchase_count, pr.reason, "
        "pr.status, pr.created_at, pr.decided_at, pr.notified_at FROM offer_proposals pr JOIN offers o ON o.offer_id=pr.offer_id"),
    "dim_campaign": ("simulated / live per delivery_mode",
        "SELECT campaign_id, campaign_key, name, strategy, department, source, delivery_mode, rehearsal, status, created_at, "
        "finished_at FROM campaigns"),
    "fact_notification_log": ("simulated unless delivery_mode = whatsapp",
        "SELECT log_id, campaign_id, customer_id, recipient_name, strategy, department, matched_offers, product_or_category, "
        "selection_reason, skip_or_suppression_reason, message_text, delivery_mode, attempted_at, status, "
        "provider_message_id, provider_status, failure_reason FROM notification_logs"),
    "dim_experiment": ("simulated",
        "SELECT experiment_id, name, hypothesis, eligibility_rules, assignment_method, observation_window_days, "
        "outcome_definition, is_simulated, created_at FROM campaign_experiments"),
    "fact_experiment_outcome": ("control = real behaviour; treatment = simulated",
        "SELECT a.experiment_id, a.customer_id, a.group_name, o.purchased, o.purchase_date, o.revenue, o.is_simulated "
        "FROM experiment_assignments a JOIN experiment_outcomes o ON o.experiment_id=a.experiment_id "
        "AND o.customer_id=a.customer_id"),
    "fact_cause_segment": ("simulated experiment; evidence_level says how reliable",
        "SELECT id, customer_id, experiment_id, segment_label, evidence_level, basis FROM cause_based_segments"),
    "data_cleaning_log": ("real pipeline log",
        "SELECT log_id, run_id, step_order, step_name, decision, rows_before, rows_after, rows_affected, logged_at "
        "FROM data_cleaning_logs"),
    "processing_runs": ("real pipeline log",
        "SELECT run_id, stage, execution_mode, CASE execution_mode WHEN 'A_hadoop_spark' THEN 'Mode A: Hadoop HDFS + Spark' "
        "ELSE 'Mode B: local Python (Hadoop/Spark not used)' END AS execution_mode_label, started_at, finished_at, "
        "duration_seconds, rows_in, rows_out, status, notes FROM processing_runs"),
}
COPY_FILES = [  # (folder attribute, file, label)
    ("processed", "experiment_summary.csv", "simulated"), ("processed", "uplift_quintiles.csv", "simulated"),
    ("processed", "association_rules.csv", "derived by Python (real)"), ("processed", "segment_summary.csv", "derived by Python"),
    ("reports", "quality_summary.csv", "real pipeline report"), ("reports", "missing_values_report.csv", "real pipeline report"),
    ("reports", "invalid_values_report.csv", "real pipeline report"),
]


def build_dim_date(sales: pd.DataFrame) -> pd.DataFrame:
    d = pd.to_datetime(sales["invoice_date"]) if len(sales) else pd.Series([pd.Timestamp.today().normalize()])
    r = pd.date_range(d.min(), d.max(), freq="D")
    return pd.DataFrame({"date_key": r.strftime("%Y%m%d").astype(int), "date": r.strftime("%Y-%m-%d"), "year": r.year,
                         "quarter": r.quarter, "month_number": r.month, "month_name": r.strftime("%B"),
                         "year_month": r.strftime("%Y-%m"), "iso_week": r.isocalendar().week.to_numpy(),
                         "day_of_week": r.strftime("%A"), "is_weekend": (r.dayofweek >= 5).astype(int)})


def export_powerbi(conn, out_dir=None, processed_dir=None, reports_dir=None) -> pd.DataFrame:
    out_dir = out_dir or config.POWERBI_DIR
    processed_dir = processed_dir or config.PROCESSED_DIR
    reports_dir = reports_dir or config.REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    manifest = []
    for name, (label, sql) in SQL.items():
        df = pd.read_sql_query(sql, conn)
        df.to_csv(out_dir / f"{name}.csv", index=False)
        manifest.append((name, f"{name}.csv", len(df), len(df.columns), label, stamp))
        if name == "fact_sales":
            dd = build_dim_date(df)
            dd.to_csv(out_dir / "dim_date.csv", index=False)
            manifest.append(("dim_date", "dim_date.csv", len(dd), len(dd.columns), "generated calendar", stamp))
    for folder, fname, label in COPY_FILES:
        src = (processed_dir if folder == "processed" else reports_dir) / fname
        if src.exists():
            shutil.copyfile(src, out_dir / fname)
            df = pd.read_csv(src)
            manifest.append((fname[:-4], fname, len(df), len(df.columns), label, stamp))
    m = pd.DataFrame(manifest, columns=["table", "file", "rows", "columns", "data_label", "exported_at"])
    m.to_csv(out_dir / "export_manifest.csv", index=False)
    return m
