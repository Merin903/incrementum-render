"""Segmentation System One: product-preference segmentation (+ RFM, k-means, reorder timing, pair rules).

A customer can belong to MANY segments. Every membership stores a plain-language reason.
"""
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

from . import config, feature_engineering as fe
from .database import now_iso

MIN_CATEGORY_INVOICES = 3     # distinct invoices in a category to count as a regular buyer
MIN_CATEGORY_SHARE = 0.30     # share of the customer's classified spend
MIN_PRODUCT_INVOICES = 3      # invoices containing the product to be a "frequent buyer" of it
TOP_REAL_PRODUCT_SEGMENTS = 15
REORDER_WINDOW = (0.8, 1.5)   # due when days-since-last is within 80%-150% of the usual interval
MANAGED_TYPES = ("rfm_rule", "rfm_cluster", "category", "product", "reorder_due")


# ----------------------------------------------------------------- RFM rules
def _score(series, ascending=True):
    ranks = series.rank(method="first", ascending=ascending)
    return pd.qcut(ranks, 5, labels=[1, 2, 3, 4, 5]).astype(int)


def rfm_segments(feats: pd.DataFrame) -> pd.DataFrame:
    """Quintile scores 1-5 (5 best) and a rule-based value label."""
    f = feats[feats["is_synthetic"] == 0].copy()
    if len(f) < 5:
        return pd.DataFrame(columns=["customer_id", "segment", "reason"])
    f["R"], f["F"], f["M"] = _score(f["recency_days"], ascending=False), _score(f["frequency"]), _score(f["monetary"])

    def label(r):
        if r.R >= 4 and r.F >= 4 and r.M >= 4: return "Champions (high value)"
        if r.F >= 4 and r.R >= 3: return "Loyal customers"
        if r.R <= 2 and r.F >= 4: return "At risk (were frequent, now quiet)"
        if r.R <= 2 and r.F <= 2: return "Lost / inactive"
        return "Regular customers"
    f["segment"] = [label(r) for r in f.itertuples()]
    f["reason"] = [f"RFM scores R={r.R} (recency {r.recency_days}d), F={r.F} ({r.frequency} invoices), "
                   f"M={r.M} (spent {r.monetary:,.0f}); quintile scores 1-5, 5 = best" for r in f.itertuples()]
    return f[["customer_id", "segment", "reason"]]


# ----------------------------------------------------------------- k-means
def kmeans_segments(feats: pd.DataFrame, k_range=(3, 4, 5, 6), seed=42):
    f = feats[feats["is_synthetic"] == 0].copy()
    if len(f) < 30:
        return pd.DataFrame(columns=["customer_id", "segment", "reason"]), None
    X = StandardScaler().fit_transform(np.log1p(f[["recency_days", "frequency", "monetary"]].astype(float)))
    sample = np.random.default_rng(seed).choice(len(X), min(3000, len(X)), replace=False)
    best_k, best_s = None, -1
    for k in k_range:
        labels = KMeans(n_clusters=k, n_init=10, random_state=seed).fit_predict(X)
        s = silhouette_score(X[sample], labels[sample])
        if s > best_s:
            best_k, best_s = k, s
    f["cluster"] = KMeans(n_clusters=best_k, n_init=10, random_state=seed).fit_predict(X)
    prof = f.groupby("cluster").agg(n=("customer_id", "count"), recency=("recency_days", "median"),
                                    freq=("frequency", "median"), money=("monetary", "median"))
    order = prof["money"].rank(ascending=False).astype(int)         # cluster 1 = biggest spenders
    names = {c: f"RFM cluster {order[c]}" for c in prof.index}
    f["segment"] = f["cluster"].map(names)
    desc = {names[c]: f"median recency {prof.loc[c,'recency']:.0f}d, {prof.loc[c,'freq']:.0f} invoices, "
                      f"spend {prof.loc[c,'money']:,.0f} ({int(prof.loc[c,'n'])} customers)" for c in prof.index}
    f["reason"] = [f"k-means on log(recency, frequency, monetary); cluster centre ~ {desc[s]}" for s in f["segment"]]
    return f[["customer_id", "segment", "reason"]], {"k": best_k, "silhouette": round(best_s, 3), "profiles": desc}


# ----------------------------------------------------------------- category / product preference
def category_segments(lines: pd.DataFrame) -> pd.DataFrame:
    cl = lines[lines["department"] != config.UNCERTAIN]
    g = cl.groupby(["customer_id", "department"]).agg(invoices=("invoice", "nunique"), spend=("line_total", "sum")).reset_index()
    total = g.groupby("customer_id")["spend"].transform("sum")
    g["share"] = g["spend"] / total
    g = g[(g["invoices"] >= MIN_CATEGORY_INVOICES) & (g["share"] >= MIN_CATEGORY_SHARE)].copy()
    g["segment"] = g["department"] + " preference"
    g["reason"] = [f"{r.share:.0%} of classified spend ({r.spend:,.0f}) and {r.invoices} separate purchases in {r.department}"
                   for r in g.itertuples()]
    g["strength"] = g["share"]
    return g[["customer_id", "segment", "reason", "strength", "department"]]


def product_segments(lines: pd.DataFrame) -> pd.DataFrame:
    g = lines.groupby(["customer_id", "stock_code", "description", "is_synthetic"])["invoice"].nunique().reset_index(name="invoices")
    g = g[g["invoices"] >= MIN_PRODUCT_INVOICES]
    counts = g.groupby(["stock_code", "description", "is_synthetic"])["customer_id"].nunique().reset_index(name="buyers")
    keep = pd.concat([counts[counts["is_synthetic"] == 1],
                      counts[counts["is_synthetic"] == 0].sort_values("buyers", ascending=False).head(TOP_REAL_PRODUCT_SEGMENTS)])
    g = g.merge(keep[["stock_code"]], on="stock_code")
    g["segment"] = "Frequent buyers of " + g["description"]
    g["reason"] = [f"bought {r.description} on {r.invoices} separate invoices (threshold {MIN_PRODUCT_INVOICES})" for r in g.itertuples()]
    g["strength"] = g["invoices"].astype(float)
    return g[["customer_id", "segment", "reason", "strength", "stock_code"]]


def reorder_segments(feats: pd.DataFrame) -> pd.DataFrame:
    f = feats.dropna(subset=["avg_reorder_interval_days"]).copy()
    lo, hi = REORDER_WINDOW
    due = f[(f["recency_days"] >= lo * f["avg_reorder_interval_days"]) & (f["recency_days"] <= hi * f["avg_reorder_interval_days"])].copy()
    due["segment"] = "Approaching expected reorder"
    due["reason"] = [f"RULE-BASED ESTIMATE: usually shops every ~{r.avg_reorder_interval_days:.0f} days; last purchase "
                     f"{r.recency_days} days ago (window {lo:.0%}-{hi:.0%} of the usual gap)" for r in due.itertuples()]
    return due[["customer_id", "segment", "reason"]]


# ----------------------------------------------------------------- association rules (pairs)
def pair_rules(lines: pd.DataFrame, top_n=100, min_pair_count=30, min_lift=1.2, keep=50) -> pd.DataFrame:
    real = lines[lines["is_synthetic"] == 0]
    cols = ["antecedent", "consequent", "pair_count", "support", "confidence", "lift"]
    if real.empty:
        return pd.DataFrame(columns=cols)
    top = real.groupby("description")["invoice"].nunique().sort_values(ascending=False).head(top_n).index
    b = real[real["description"].isin(top)][["invoice", "description"]].drop_duplicates()
    m = pd.crosstab(b["invoice"], b["description"]).clip(upper=1)
    n = real["invoice"].nunique()
    co = m.T.values @ m.values
    item_n = np.diag(co).astype(float)
    rows = []
    for i, a in enumerate(m.columns):
        for j, c in enumerate(m.columns):
            if i != j and co[i, j] >= min_pair_count:
                conf = co[i, j] / item_n[i]
                lift = conf / (item_n[j] / n)
                if lift >= min_lift:
                    rows.append((a, c, int(co[i, j]), co[i, j] / n, conf, lift))
    out = pd.DataFrame(rows, columns=cols).sort_values("lift", ascending=False).head(keep)
    return out.round({"support": 4, "confidence": 3, "lift": 2})


# ----------------------------------------------------------------- persistence
def _save(conn, frames: dict) -> dict:
    """frames: {segment_type: DataFrame[customer_id, segment, reason, (strength)]}. Replaces managed segments."""
    q = ",".join("?" * len(MANAGED_TYPES))
    conn.execute(f"DELETE FROM customer_segment_memberships WHERE segment_id IN "
                 f"(SELECT segment_id FROM product_preference_segments WHERE segment_type IN ({q}))", MANAGED_TYPES)
    conn.execute(f"DELETE FROM product_preference_segments WHERE segment_type IN ({q})", MANAGED_TYPES)
    stamp, counts = now_iso(), {}
    for stype, df in frames.items():
        if df is None or df.empty:
            counts[stype] = 0
            continue
        for name in df["segment"].unique():
            conn.execute("INSERT OR IGNORE INTO product_preference_segments (segment_name, segment_type, "
                         "product_or_category, rule_description, created_at) VALUES (?,?,?,?,?)",
                         (name, stype, name, RULES[stype], stamp))
        ids = dict(conn.execute("SELECT segment_name, segment_id FROM product_preference_segments").fetchall())
        strength = df["strength"] if "strength" in df else pd.Series([None] * len(df), index=df.index)
        conn.executemany("INSERT OR IGNORE INTO customer_segment_memberships (customer_id, segment_id, strength, reason) "
                         "VALUES (?,?,?,?)", [(c, ids[s], None if pd.isna(st) else float(st), r)
                                             for c, s, r, st in zip(df["customer_id"], df["segment"], df["reason"], strength)])
        counts[stype] = len(df)
    conn.commit()
    return counts


RULES = {
    "rfm_rule": "Quintile RFM scores with fixed rules (Champions, Loyal, At risk, Lost, Regular)",
    "rfm_cluster": "k-means (scikit-learn) on standardised log recency, frequency, monetary; k chosen by silhouette",
    "category": f">= {MIN_CATEGORY_INVOICES} purchases in the category AND >= {MIN_CATEGORY_SHARE:.0%} of the customer's classified spend",
    "product": f">= {MIN_PRODUCT_INVOICES} separate invoices containing the product",
    "reorder_due": "Days since last purchase within 80%-150% of the customer's average gap (rule-based estimate)",
}


def run_segmentation(conn, today=None) -> dict:
    lines = fe.load_lines(conn)
    feats = fe.compute_features(lines, today)
    fe.save_features(conn, feats)
    km, km_info = kmeans_segments(feats)
    counts = _save(conn, {"rfm_rule": rfm_segments(feats), "rfm_cluster": km, "category": category_segments(lines),
                          "product": product_segments(lines), "reorder_due": reorder_segments(feats)})
    return {"customers": len(feats), "memberships": counts, "kmeans": km_info, "rules": pair_rules(lines), "features": feats}


def explain_customer(conn, customer_id) -> dict:
    feat = conn.execute("SELECT * FROM customer_features WHERE customer_id=?", (customer_id,)).fetchone()
    segs = conn.execute("SELECT s.segment_name, s.segment_type, m.reason FROM customer_segment_memberships m "
                        "JOIN product_preference_segments s ON s.segment_id=m.segment_id WHERE m.customer_id=? "
                        "ORDER BY s.segment_type, s.segment_name", (customer_id,)).fetchall()
    return {"features": dict(feat) if feat else None, "segments": [dict(s) for s in segs]}


def export_segmentation(conn, rules, out_dir=None):
    """CSV tables for Power BI (Python produced them; Power BI only displays them)."""
    out_dir = out_dir or config.PROCESSED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.read_sql_query("SELECT f.*, c.is_synthetic FROM customer_features f JOIN customers c USING(customer_id)", conn
                      ).to_csv(out_dir / "customer_features.csv", index=False)
    pd.read_sql_query("SELECT s.segment_name, s.segment_type, s.rule_description, COUNT(m.customer_id) AS customers, "
                      "ROUND(SUM(f.monetary),2) AS total_spend, ROUND(AVG(f.monetary),2) AS avg_spend "
                      "FROM product_preference_segments s LEFT JOIN customer_segment_memberships m USING(segment_id) "
                      "LEFT JOIN customer_features f ON f.customer_id=m.customer_id GROUP BY s.segment_id "
                      "ORDER BY s.segment_type, customers DESC", conn).to_csv(out_dir / "segment_summary.csv", index=False)
    pd.read_sql_query("SELECT m.customer_id, s.segment_name, s.segment_type, m.strength, m.reason "
                      "FROM customer_segment_memberships m JOIN product_preference_segments s USING(segment_id)", conn
                      ).to_csv(out_dir / "segment_memberships.csv", index=False)
    rules.to_csv(out_dir / "association_rules.csv", index=False)
