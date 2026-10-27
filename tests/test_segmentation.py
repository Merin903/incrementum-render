import numpy as np
import pandas as pd

from src import feature_engineering as fe, product_segmentation as ps
from .helpers import TODAY, make_db

REAL_PRODUCTS = [("R1", "PINK CHERRY LIGHTS", "Lights & Candles", 8.0), ("R2", "VANILLA CANDLE", "Lights & Candles", 6.0),
                 ("R3", "RED MUG", "Kitchen & Dining", 4.0), ("R4", "TEA TOWEL", "Kitchen & Dining", 5.0),
                 ("R5", "GIFT BAG", "Bags & Storage", 3.0), ("R6", "MYSTERY THING", "Other / Uncertain", 9.0)]


def add_real_customers(conn, n=90, seed=0):
    """Real-style (is_synthetic=0) customers with varied activity over 2010."""
    rng = np.random.default_rng(seed)
    for code, desc, dept, _ in REAL_PRODUCTS:
        conn.execute("INSERT INTO products (stock_code, description, department, is_synthetic) VALUES (?,?,?,0)", (code, desc, dept))
    price = {c: p for c, _, _, p in REAL_PRODUCTS}
    inv = 100000
    for i in range(n):
        cid = str(12000 + i)
        conn.execute("INSERT INTO customers (customer_id, country, is_synthetic) VALUES (?,?,0)", (cid, "United Kingdom"))
        favourite = REAL_PRODUCTS[i % 3][0]                      # R1, R2 or R3
        for _ in range(int(rng.integers(1, 20))):
            inv += 1
            day = pd.Timestamp("2010-01-01") + pd.Timedelta(days=int(rng.integers(0, 360)))
            conn.execute("INSERT INTO transactions (invoice, customer_id, invoice_date, country, is_synthetic) VALUES (?,?,?,?,0)",
                         (str(inv), cid, day.strftime("%Y-%m-%d 10:00:00"), "United Kingdom"))
            for code in {favourite, REAL_PRODUCTS[int(rng.integers(0, 6))][0]}:
                conn.execute("INSERT INTO transaction_items (invoice, stock_code, quantity, price, line_total, is_synthetic) "
                             "VALUES (?,?,?,?,?,0)", (str(inv), code, 2, price[code], 2 * price[code]))
    conn.commit()


def demo_features():
    conn = make_db(consent=False)
    lines = fe.load_lines(conn)
    return conn, lines, fe.compute_features(lines, TODAY)


def test_features_for_a_demo_customer_match_hand_calculation():
    conn, lines, feats = demo_features()
    m = feats.set_index("customer_id").loc["DEMO-MERIN"]
    assert m["frequency"] == 8                                   # 5 Dettol + 2 Coca-Cola + 1 bread invoices
    assert m["monetary"] == 2350.0 and m["avg_basket_value"] == round(2350 / 8, 2)
    assert m["recency_days"] == 6                                # last purchase 5 days ago, reference = today + 1
    assert m["top_category"] == "Non-Food & Household" and m["top_product"] == "DETTOL ANTISEPTIC LIQUID 500ML"
    assert m["avg_reorder_interval_days"] == round(55 / 7, 2)    # purchase days 5..60 days ago: (60-5)/7 gaps


def test_real_customers_use_dataset_end_as_reference_date():
    conn = make_db(consent=False)
    add_real_customers(conn, n=10)
    lines = fe.load_lines(conn)
    feats = fe.compute_features(lines, TODAY).set_index("customer_id")
    last_real = lines[lines["is_synthetic"] == 0]["day"].max()
    row = feats[feats["is_synthetic"] == 0].sort_values("last_purchase_date").iloc[-1]
    assert row["recency_days"] == (last_real + pd.Timedelta(days=1) - pd.Timestamp(row["last_purchase_date"])).days
    assert feats.loc["DEMO-MERIN", "recency_days"] == 6          # demo customers still relative to today


def test_category_preference_segments_have_reasons_and_allow_multiple():
    conn, lines, _ = demo_features()
    seg = ps.category_segments(lines)
    merin = seg[seg["customer_id"] == "DEMO-MERIN"]
    assert list(merin["segment"]) == ["Non-Food & Household preference"]       # Coca-Cola share 7% is too small
    assert "separate purchases" in merin["reason"].iloc[0]
    assert (seg["reason"].str.len() > 10).all()


def test_a_customer_can_belong_to_several_category_segments():
    rows = []
    for i in range(3):                                                         # 3 invoices in each of two categories
        rows += [("c1", f"L{i}", "Lights & Candles", 50.0), ("c1", f"K{i}", "Kitchen & Dining", 50.0)]
    lines = pd.DataFrame(rows, columns=["customer_id", "invoice", "department", "line_total"])
    seg = ps.category_segments(lines)
    assert set(seg["segment"]) == {"Lights & Candles preference", "Kitchen & Dining preference"}


def test_product_preference_segments_only_for_frequent_buyers():
    conn, lines, _ = demo_features()
    seg = ps.product_segments(lines)
    dettol = set(seg[seg["segment"] == "Frequent buyers of DETTOL ANTISEPTIC LIQUID 500ML"]["customer_id"])
    assert dettol == {"DEMO-MERIN"}                                            # Hanna bought it once only


def test_rfm_rules_and_kmeans_on_real_like_customers():
    conn = make_db(consent=False)
    add_real_customers(conn, n=90)
    feats = fe.compute_features(fe.load_lines(conn), TODAY)
    rfm = ps.rfm_segments(feats)
    assert len(rfm) == 90 and set(rfm["segment"]) <= {"Champions (high value)", "Loyal customers",
                                                        "At risk (were frequent, now quiet)", "Lost / inactive", "Regular customers"}
    km, info = ps.kmeans_segments(feats)
    assert 3 <= info["k"] <= 6 and len(km) == 90 and km["segment"].nunique() == info["k"]
    assert km["reason"].str.contains("cluster centre").all()
    assert "DEMO-MERIN" not in set(rfm["customer_id"]) | set(km["customer_id"])   # demo customers are not clustered


def test_reorder_due_rule_and_insufficient_history():
    feats = pd.DataFrame({"customer_id": ["a", "b", "c"], "recency_days": [10, 100, 5],
                          "avg_reorder_interval_days": [10.0, 10.0, np.nan]})
    due = ps.reorder_segments(feats)
    assert list(due["customer_id"]) == ["a"]                     # b is overdue far beyond 150%; c has no estimate
    assert "RULE-BASED ESTIMATE" in due["reason"].iloc[0]


def test_pair_rules_find_a_real_co_purchase_pattern():
    rows = []
    for i in range(300):
        if i < 150:
            rows.append((f"I{i}", "TEA TOWEL"))                # towel on half of all invoices
            if i < 90:
                rows.append((f"I{i}", "RED MUG"))              # the mug is ONLY ever bought with the towel
        else:
            rows.append((f"I{i}", "FILLER"))
        if i % 7 == 0:
            rows.append((f"I{i}", "GIFT BAG"))                 # unrelated
    lines = pd.DataFrame(rows, columns=["invoice", "description"]).assign(is_synthetic=0)
    rules = ps.pair_rules(lines, min_pair_count=30)
    top = rules.iloc[0]
    assert {top["antecedent"], top["consequent"]} == {"TEA TOWEL", "RED MUG"} and top["lift"] == 2.0   # 1.0 / (150/300)
    row = rules[(rules["antecedent"] == "RED MUG") & (rules["consequent"] == "TEA TOWEL")].iloc[0]
    assert row["confidence"] == 1.0 and row["pair_count"] == 90


def test_full_segmentation_run_is_repeatable_and_explainable():
    conn = make_db(consent=False)
    add_real_customers(conn, n=90)
    a = ps.run_segmentation(conn, TODAY)
    n1 = conn.execute("SELECT COUNT(*) FROM customer_segment_memberships").fetchone()[0]
    b = ps.run_segmentation(conn, TODAY)
    n2 = conn.execute("SELECT COUNT(*) FROM customer_segment_memberships").fetchone()[0]
    assert n1 == n2 > 0 and a["customers"] == b["customers"] == 95
    assert conn.execute("SELECT COUNT(*) FROM customer_features").fetchone()[0] == 95
    assert conn.execute("SELECT COUNT(*) FROM customer_segment_memberships WHERE reason IS NULL OR reason=''").fetchone()[0] == 0
    ex = ps.explain_customer(conn, "DEMO-MERIN")
    names = {s["segment_name"] for s in ex["segments"]}
    assert "Non-Food & Household preference" in names and "Frequent buyers of DETTOL ANTISEPTIC LIQUID 500ML" in names
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_export_writes_power_bi_tables():
    import pathlib, tempfile
    conn = make_db(consent=False)
    add_real_customers(conn, n=90)
    res = ps.run_segmentation(conn, TODAY)
    out = pathlib.Path(tempfile.mkdtemp())
    ps.export_segmentation(conn, res["rules"], out)
    for f in ("customer_features.csv", "segment_summary.csv", "segment_memberships.csv", "association_rules.csv"):
        assert (out / f).exists(), f
    summ = pd.read_csv(out / "segment_summary.csv")
    assert summ["customers"].sum() == len(pd.read_csv(out / "segment_memberships.csv"))
