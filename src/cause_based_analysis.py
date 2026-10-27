"""Segmentation System Two: cause-based campaign response (Persuadable / Sure Thing / Lost Cause / Do-not-disturb).

HONESTY RULES (also shown in every report):
  * The Kaggle data contains NO campaign history, so no real notification experiment exists.
  * This module runs a clearly labelled SIMULATED experiment:
      - customer eligibility and features use ONLY purchases BEFORE a cut-off date (no leakage);
      - the control group's outcome is REAL: did they actually buy in the category in the window after the cut-off?
      - the treatment group's outcome = their real behaviour PLUS a SIMULATED extra response to a notification,
        with effect sizes that are ASSUMPTIONS set by the developer (max_lift), not findings;
      - opt-out flags are simulated too (the dataset has no consent information).
  * Random assignment is what makes the treatment-minus-control difference an estimate of incremental impact.
    A purchase after a message does NOT by itself show the message caused it.
"""
import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.ensemble import GradientBoostingClassifier

from . import config, feature_engineering as fe, product_segmentation as ps
from .database import now_iso

DEFAULTS = dict(window_days=30, seed=42, treat_share=0.5, optout_rate=0.03, max_lift=0.35,
                uplift_threshold=0.05, sure_thing_p=0.30, min_arm=200)
FEATURES = ["recency_days", "frequency", "monetary", "avg_basket_value", "reorder_gap", "cat_invoices", "cat_share"]
LABELS = ("Persuadable", "Sure Thing", "Lost Cause", "Do-not-disturb")


def split_lines(lines, window_days):
    end = lines["day"].max()
    cutoff = end - pd.Timedelta(days=window_days)
    return lines[lines["day"] <= cutoff], lines[lines["day"] > cutoff], cutoff, end


def category_stats(pre, category):
    cl = pre[pre["department"] != config.UNCERTAIN]
    total = cl.groupby("customer_id")["line_total"].sum()
    c = cl[cl["department"] == category].groupby("customer_id").agg(
        cat_invoices=("invoice", "nunique"), cat_spend=("line_total", "sum"))
    c["cat_share"] = c["cat_spend"] / total.reindex(c.index)
    return c.reset_index()


def conversion_analysis(n_t, x_t, n_c, x_c) -> dict:
    """Two-proportion comparison: rates, difference, 95% CI (Wald) and z-test p-value."""
    p_t, p_c = x_t / n_t, x_c / n_c
    diff = p_t - p_c
    se = np.sqrt(p_t * (1 - p_t) / n_t + p_c * (1 - p_c) / n_c)
    pooled = (x_t + x_c) / (n_t + n_c)
    se_pool = np.sqrt(pooled * (1 - pooled) * (1 / n_t + 1 / n_c))
    p_value = 1.0 if se_pool == 0 else float(2 * norm.sf(abs(diff / se_pool)))
    return {"n_treatment": int(n_t), "n_control": int(n_c), "conv_treatment": p_t, "conv_control": p_c,
            "difference": diff, "ci_low": diff - 1.96 * se, "ci_high": diff + 1.96 * se, "p_value": p_value,
            "estimated_incremental_purchases": diff * n_t}


def crossfit_uplift(X, y, t, seed, min_arm):
    """Two-model (T-learner) uplift, 2-fold cross-fitted so no customer is scored by a model that saw them."""
    if (t == 1).sum() < min_arm or (t == 0).sum() < min_arm:
        return None
    rng = np.random.default_rng(seed)
    fold = np.zeros(len(X), int)
    for g in (0, 1):
        idx = np.where(t == g)[0]
        rng.shuffle(idx)
        fold[idx[1::2]] = 1
    p_t, p_c = np.zeros(len(X)), np.zeros(len(X))
    for f in (0, 1):
        te = fold == f
        for g, out in ((1, p_t), (0, p_c)):
            m = (fold != f) & (t == g)
            if y[m].min() == y[m].max():
                out[te] = float(y[m][0])
            else:
                out[te] = GradientBoostingClassifier(n_estimators=60, max_depth=1, learning_rate=0.1, min_samples_leaf=25,
                                                     subsample=0.8, random_state=seed).fit(X[m], y[m]).predict_proba(X[te])[:, 1]
    return p_t - p_c, p_c


def run_experiment(conn, category=None, lines=None, **overrides) -> dict:
    P = {**DEFAULTS, **overrides}
    lines = fe.load_lines(conn) if lines is None else lines
    real = lines[lines["is_synthetic"] == 0]
    pre, win, cutoff, end = split_lines(real, P["window_days"])
    notes = ["SIMULATED EXPERIMENT: control outcomes are real observed purchases; treatment-group outcomes include a "
             f"SIMULATED response (assumed max_lift={P['max_lift']}). Opt-outs are simulated. "
             "Neither is a real campaign result."]

    if category is None:                                   # default: category with the most eligible customers
        seg = ps.category_segments(pre)
        category = seg["department"].value_counts().index[0]
    cs = category_stats(pre, category)
    cs = cs[(cs["cat_invoices"] >= ps.MIN_CATEGORY_INVOICES) & (cs["cat_share"] >= ps.MIN_CATEGORY_SHARE)]
    feats = fe.compute_features(pre, cutoff)
    df = feats.merge(cs, on="customer_id").reset_index(drop=True)
    df["reorder_gap"] = df["avg_reorder_interval_days"].fillna(-1)

    rng = np.random.default_rng(P["seed"])
    df["opted_out"] = rng.random(len(df)) < P["optout_rate"]
    pool = df[~df["opted_out"]].copy()
    ids = pool["customer_id"].to_numpy().copy()
    rng.shuffle(ids)
    n_treat = int(round(len(ids) * P["treat_share"]))
    treated = set(ids[:n_treat])
    pool["treated"] = pool["customer_id"].isin(treated).astype(int)

    in_cat = win[win["department"] == category]
    real_buy = in_cat.groupby("customer_id").agg(first=("day", "min"), revenue=("line_total", "sum"))
    pool["real_purchase"] = pool["customer_id"].isin(real_buy.index).astype(int)
    responsiveness = (np.clip(pool["cat_invoices"] / 8, 0, 1) * np.clip(1 - pool["recency_days"] / 270, 0, 1))
    q = P["max_lift"] * responsiveness.to_numpy()
    flip = (rng.random(len(pool)) < q) & (pool["treated"].to_numpy() == 1) & (pool["real_purchase"].to_numpy() == 0)
    pool["simulated_extra"] = flip.astype(int)
    pool["purchased"] = ((pool["real_purchase"] == 1) | flip).astype(int)

    stats = conversion_analysis(int(pool["treated"].sum()), int(pool.loc[pool["treated"] == 1, "purchased"].sum()),
                                int((pool["treated"] == 0).sum()), int(pool.loc[pool["treated"] == 0, "purchased"].sum()))
    stats.update({"category": category, "cutoff": str(cutoff.date()), "window_end": str(end.date()),
                  "is_simulated": 1, "opted_out_excluded": int(df["opted_out"].sum()),
                  "observed_purchases_after_notification": int(pool.loc[pool["treated"] == 1, "purchased"].sum())})

    uplift_groups, seg_rows = None, []
    X = pool[FEATURES].to_numpy(float)
    res = crossfit_uplift(X, pool["purchased"].to_numpy(), pool["treated"].to_numpy(), P["seed"], P["min_arm"])
    if res is None:
        notes.append(f"INSUFFICIENT DATA for uplift modelling (needs >= {P['min_arm']} customers per arm); only the "
                     "baseline treatment-vs-control comparison is reported and no Persuadable/Sure Thing/Lost Cause "
                     "labels were assigned.")
    else:
        uplift, p_c = res
        pool["pred_uplift"], pool["pred_p_control"] = uplift, p_c
        pool["quintile"] = pd.qcut(pd.Series(uplift).rank(method="first"), 5, labels=[1, 2, 3, 4, 5]).astype(int).to_numpy()
        rows = []
        for qn, g in pool.groupby("quintile"):
            t, c = g[g["treated"] == 1], g[g["treated"] == 0]
            rows.append({"predicted_uplift_quintile": int(qn), "n_treatment": len(t), "n_control": len(c),
                         "conv_treatment": t["purchased"].mean(), "conv_control": c["purchased"].mean(),
                         "observed_uplift": t["purchased"].mean() - c["purchased"].mean(),
                         "mean_predicted_uplift": g["pred_uplift"].mean()})
        uplift_groups = pd.DataFrame(rows)
        # Validation gate: did the model's TOP predicted-uplift quintile really respond more than its BOTTOM quintile?
        g = uplift_groups.set_index("predicted_uplift_quintile")
        var = lambda k: (g.loc[k, "conv_treatment"] * (1 - g.loc[k, "conv_treatment"]) / g.loc[k, "n_treatment"]
                         + g.loc[k, "conv_control"] * (1 - g.loc[k, "conv_control"]) / g.loc[k, "n_control"])
        gap = g.loc[5, "observed_uplift"] - g.loc[1, "observed_uplift"]
        z = float(gap / np.sqrt(var(5) + var(1)))
        passed = bool(z >= 1.645)                                    # one-sided 5% level
        stats.update({"validation_top_minus_bottom_quintile": float(gap), "validation_z": z, "validation_passed": int(passed)})
        evidence = "simulated" if passed else "insufficient"
        for r in pool.itertuples():
            if r.pred_uplift >= P["uplift_threshold"]:
                label = "Persuadable"
            elif r.pred_p_control >= P["sure_thing_p"]:
                label = "Sure Thing"
            else:
                label = "Lost Cause"
            head = ("SIMULATED experiment, model validated at group level" if passed else
                    "PROVISIONAL - uplift model NOT validated (top predicted quintile did not respond more than the bottom one "
                    "beyond chance); do not act on this label")
            seg_rows.append((r.customer_id, label, evidence, f"{head}; model-predicted purchase chance WITHOUT a message "
                             f"{r.pred_p_control:.0%}, predicted uplift {r.pred_uplift:+.1%} (cross-fitted two-model estimate)"))
        notes.append(f"Validation gate: top-minus-bottom quintile observed uplift {gap:+.1%} (z={z:.2f}); "
                     + ("PASSED - labels are marked 'simulated' evidence." if passed else
                        "FAILED - labels are marked 'insufficient' (provisional). Individual labels are uncertain either way; "
                        "read the quintile table as the group-level evidence."))
    for r in df[df["opted_out"]].itertuples():
        seg_rows.append((r.customer_id, "Do-not-disturb", "simulated", f"SIMULATED opt-out flag (random {P['optout_rate']:.0%}); "
                         "the dataset has no real consent records"))
    segments = pd.DataFrame(seg_rows, columns=["customer_id", "segment_label", "evidence_level", "basis"])

    outcomes = pool[["customer_id", "treated", "purchased", "real_purchase", "simulated_extra"]].copy()
    outcomes["purchase_date"] = outcomes["customer_id"].map(real_buy["first"].dt.date.astype(str))
    outcomes["revenue"] = outcomes["customer_id"].map(real_buy["revenue"]).fillna(0.0)
    sim_rev = pool.set_index("customer_id")["avg_basket_value"]
    mask = outcomes["simulated_extra"] == 1
    outcomes.loc[mask, "revenue"] = outcomes.loc[mask, "customer_id"].map(sim_rev).to_numpy()
    outcomes.loc[mask, "purchase_date"] = str((cutoff + pd.Timedelta(days=P["window_days"] // 2)).date())
    return {"params": P, "category": category, "stats": stats, "uplift_groups": uplift_groups, "segments": segments,
            "outcomes": outcomes, "notes": notes, "n_eligible": len(df)}


def save_experiment(conn, res) -> int:
    P, cat, stats = res["params"], res["category"], res["stats"]
    name = f"SIM-{cat}-notification-test"
    old = conn.execute("SELECT experiment_id FROM campaign_experiments WHERE name=?", (name,)).fetchall()
    for (eid,) in old:
        for t in ("experiment_outcomes", "experiment_assignments", "cause_based_segments"):
            conn.execute(f"DELETE FROM {t} WHERE experiment_id=?", (eid,))
        conn.execute("DELETE FROM campaign_experiments WHERE experiment_id=?", (eid,))
    eid = conn.execute(
        "INSERT INTO campaign_experiments (name, hypothesis, eligibility_rules, assignment_method, "
        "observation_window_days, outcome_definition, is_simulated, created_at) VALUES (?,?,?,?,?,?,1,?)",
        (name, f"A promotional notification increases purchases in '{cat}' within {P['window_days']} days "
               "(SIMULATED treatment effect; assumed, not measured)",
         f">= {ps.MIN_CATEGORY_INVOICES} purchases and >= {ps.MIN_CATEGORY_SHARE:.0%} of classified spend in '{cat}' before "
         f"{stats['cutoff']}; simulated opt-outs excluded",
         f"Seeded random assignment, {P['treat_share']:.0%} treatment (seed {P['seed']})", P["window_days"],
         f"Any purchase in '{cat}' between {stats['cutoff']} and {stats['window_end']}", now_iso())).lastrowid
    o = res["outcomes"]
    known = {r[0] for r in conn.execute("SELECT customer_id FROM customers")}
    o = o[o["customer_id"].isin(known)]
    stamp = now_iso()
    conn.executemany("INSERT INTO experiment_assignments (experiment_id, customer_id, group_name, assigned_at) VALUES (?,?,?,?)",
                     [(eid, c, "treatment" if t else "control", stamp) for c, t in zip(o["customer_id"], o["treated"])])
    conn.executemany("INSERT INTO experiment_outcomes (experiment_id, customer_id, purchased, purchase_date, revenue, "
                     "is_simulated) VALUES (?,?,?,?,?,?)",
                     [(eid, r.customer_id, int(r.purchased), r.purchase_date if isinstance(r.purchase_date, str) else None,
                       float(r.revenue), int(r.treated)) for r in o.itertuples()])   # control rows are REAL (0); treated rows SIMULATED (1)
    seg = res["segments"]
    conn.executemany("INSERT INTO cause_based_segments (customer_id, experiment_id, segment_label, evidence_level, basis, "
                     "assigned_at) VALUES (?,?,?,?,?,?)",
                     [(c, eid, l, ev, b, stamp) for c, l, ev, b in zip(seg["customer_id"], seg["segment_label"],
                                                                      seg["evidence_level"], seg["basis"]) if c in known])
    conn.commit()
    return eid


def export_experiment(conn, res, eid, out_dir=None):
    out_dir = out_dir or config.PROCESSED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    s = {k: v for k, v in res["stats"].items()}
    s["data_label"] = "SIMULATED (control = real observed behaviour; treatment response simulated)"
    pd.DataFrame([s]).to_csv(out_dir / "experiment_summary.csv", index=False)
    if res["uplift_groups"] is not None:
        res["uplift_groups"].to_csv(out_dir / "uplift_quintiles.csv", index=False)
    pd.read_sql_query("SELECT segment_label, evidence_level, COUNT(*) AS customers FROM cause_based_segments "
                      "WHERE experiment_id=? GROUP BY segment_label, evidence_level", conn, params=(eid,)
                      ).assign(data_label="SIMULATED").to_csv(out_dir / "cause_segment_counts.csv", index=False)
    pd.read_sql_query("SELECT customer_id, segment_label, evidence_level, basis FROM cause_based_segments "
                      "WHERE experiment_id=?", conn, params=(eid,)).to_csv(out_dir / "cause_segments.csv", index=False)
