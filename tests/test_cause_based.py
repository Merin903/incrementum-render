import numpy as np
import pandas as pd

from src import campaign_engine as ce, cause_based_analysis as cb, feature_engineering as fe
from .helpers import make_db
from .helpers_big import make_experiment_db

_CACHE = {}


def big():
    if "c" not in _CACHE:
        conn = make_experiment_db()
        _CACHE["c"], _CACHE["lines"] = conn, fe.load_lines(conn)
    return _CACHE["c"], _CACHE["lines"]


def test_conversion_analysis_matches_hand_calculation():
    r = cb.conversion_analysis(100, 20, 100, 10)
    assert r["difference"] == 0.10 and r["conv_treatment"] == 0.2 and r["estimated_incremental_purchases"] == 10
    assert abs(r["p_value"] - 0.0477) < 0.001                       # pooled z = 1.98
    # Wald se = sqrt(.2*.8/100 + .1*.9/100) = 0.05, so the 95% CI is 0.10 +/- 0.098 = [0.002, 0.198]
    assert abs(r["ci_low"] - 0.002) < 0.001 and abs(r["ci_high"] - 0.198) < 0.001
    assert cb.conversion_analysis(50, 0, 50, 0)["p_value"] == 1.0   # no purchases anywhere: no evidence


def test_no_leakage_features_use_only_data_before_cutoff():
    conn, lines = big()
    pre, win, cutoff, end = cb.split_lines(lines[lines["is_synthetic"] == 0], 30)
    assert pre["day"].max() <= cutoff < win["day"].min() and win["day"].max() == end
    assert (end - cutoff).days == 30


def test_assignment_is_random_reproducible_and_excludes_opt_outs():
    conn, lines = big()
    a = cb.run_experiment(conn, lines=lines, seed=7)
    b = cb.run_experiment(conn, lines=lines, seed=7)
    c = cb.run_experiment(conn, lines=lines, seed=8)
    ta, tb, tc = (set(x["outcomes"].query("treated==1")["customer_id"]) for x in (a, b, c))
    assert ta == tb and ta != tc
    o = a["outcomes"]
    assert abs(o["treated"].mean() - 0.5) < 0.02 and o["customer_id"].is_unique
    optout = set(a["segments"].query("segment_label=='Do-not-disturb'")["customer_id"])
    assert optout and not (optout & set(o["customer_id"]))           # opted-out customers are never assigned


def test_control_outcomes_are_real_and_treatment_only_adds_simulated_purchases():
    conn, lines = big()
    r = cb.run_experiment(conn, lines=lines, seed=3)
    real = lines[lines["is_synthetic"] == 0]
    _, win, _, _ = cb.split_lines(real, 30)
    buyers = set(win[win["department"] == r["category"]]["customer_id"])
    o = r["outcomes"]
    assert (o["real_purchase"] == o["customer_id"].isin(buyers).astype(int)).all()      # baseline = real behaviour
    ctrl = o[o["treated"] == 0]
    assert (ctrl["purchased"] == ctrl["real_purchase"]).all() and (ctrl["simulated_extra"] == 0).all()
    trt = o[o["treated"] == 1]
    assert (trt["purchased"] >= trt["real_purchase"]).all()                               # treatment never removes a real purchase
    assert ((trt["simulated_extra"] == 1) <= (trt["real_purchase"] == 0)).all()           # extra only for non-buyers


def test_zero_simulated_effect_gives_no_significant_difference():
    conn, lines = big()
    r = cb.run_experiment(conn, lines=lines, seed=5, max_lift=0.0)
    s = r["stats"]
    assert s["ci_low"] <= 0 <= s["ci_high"] and s["p_value"] > 0.05
    assert (r["outcomes"]["simulated_extra"] == 0).all()


def test_positive_simulated_effect_is_detected_with_uncertainty_reported():
    conn, lines = big()
    r = cb.run_experiment(conn, lines=lines, seed=5, max_lift=0.6)
    s = r["stats"]
    assert s["difference"] > 0 and s["ci_low"] > 0 and s["p_value"] < 0.05
    assert s["n_treatment"] > 200 and s["n_control"] > 200 and s["is_simulated"] == 1
    assert s["observed_purchases_after_notification"] >= s["n_treatment"] * s["conv_control"] * 0.5   # observed != incremental
    assert s["estimated_incremental_purchases"] < s["observed_purchases_after_notification"]


def test_cause_segments_are_valid_and_explained():
    conn, lines = big()
    r = cb.run_experiment(conn, lines=lines, seed=5, max_lift=0.6)
    seg = r["segments"]
    assert set(seg["segment_label"]) <= set(cb.LABELS) and (seg["basis"].str.len() > 20).all()
    assert set(seg["evidence_level"]) <= {"simulated", "insufficient"}          # never 'observed': no real campaign exists
    assert {"Persuadable", "Lost Cause"} <= set(seg["segment_label"])
    assert r["uplift_groups"] is not None and len(r["uplift_groups"]) == 5


def test_failed_validation_marks_labels_provisional():
    conn, lines = big()
    r = cb.run_experiment(conn, lines=lines, seed=5, max_lift=0.6)             # z ~ 0.0 for this seed: model not validated
    assert r["stats"]["validation_passed"] == 0 and r["stats"]["validation_z"] < 1.645
    model = r["segments"][r["segments"]["segment_label"] != "Do-not-disturb"]
    assert (model["evidence_level"] == "insufficient").all() and model["basis"].str.contains("PROVISIONAL").all()
    assert any("FAILED" in n for n in r["notes"])


def test_passed_validation_marks_labels_simulated_evidence():
    conn, lines = big()
    r = cb.run_experiment(conn, lines=lines, seed=6, max_lift=0.6)             # z ~ 2.4 for this seed: group-level signal found
    assert r["stats"]["validation_passed"] == 1 and r["stats"]["validation_z"] >= 1.645
    model = r["segments"][r["segments"]["segment_label"] != "Do-not-disturb"]
    assert (model["evidence_level"] == "simulated").all() and not model["basis"].str.contains("PROVISIONAL").any()
    g = r["uplift_groups"].sort_values("predicted_uplift_quintile")
    assert g["observed_uplift"].iloc[-1] > g["observed_uplift"].iloc[0]


def test_validation_gate_rarely_passes_when_there_is_no_effect():
    conn, lines = big()
    passes = sum(cb.run_experiment(conn, lines=lines, seed=s, max_lift=0.0)["stats"]["validation_passed"] for s in range(20, 32))
    assert passes <= 2                                                          # about 5% false-positive rate by design


def test_uplift_quintile_table_is_well_formed():
    conn, lines = big()
    g = cb.run_experiment(conn, lines=lines, seed=5)["uplift_groups"].sort_values("predicted_uplift_quintile")
    assert list(g["predicted_uplift_quintile"]) == [1, 2, 3, 4, 5] and g["mean_predicted_uplift"].is_monotonic_increasing
    assert (g["n_treatment"] > 20).all() and (g["n_control"] > 20).all()


def test_insufficient_data_skips_uplift_and_says_so():
    conn, lines = big()
    r = cb.run_experiment(conn, lines=lines, seed=5, min_arm=10_000)
    assert r["uplift_groups"] is None
    assert not set(r["segments"]["segment_label"]) - {"Do-not-disturb"}       # no Persuadable/Sure Thing/Lost Cause claimed
    assert any("INSUFFICIENT DATA" in n for n in r["notes"])


def test_saved_experiment_is_flagged_repeatable_and_sends_nothing():
    conn = make_experiment_db(n_customers=900, seed=2)
    lines = fe.load_lines(conn)
    r = cb.run_experiment(conn, lines=lines, seed=1, min_arm=100)
    e1 = cb.save_experiment(conn, r)
    e2 = cb.save_experiment(conn, cb.run_experiment(conn, lines=lines, seed=1, min_arm=100))
    assert conn.execute("SELECT COUNT(*) FROM campaign_experiments").fetchone()[0] == 1
    assert conn.execute("SELECT is_simulated FROM campaign_experiments").fetchone()[0] == 1
    n_assign = conn.execute("SELECT COUNT(*) FROM experiment_assignments").fetchone()[0]
    assert n_assign == len(r["outcomes"])
    flags = dict(conn.execute("SELECT a.group_name, MAX(o.is_simulated) FROM experiment_outcomes o JOIN experiment_assignments a "
                              "ON a.experiment_id=o.experiment_id AND a.customer_id=o.customer_id GROUP BY a.group_name").fetchall())
    assert flags == {"control": 0, "treatment": 1}                             # control = real, treatment = simulated
    assert conn.execute("SELECT COUNT(*) FROM cause_based_segments WHERE evidence_level='observed'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM notification_logs").fetchone()[0] == 0   # an experiment never messages anyone
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_export_files_are_labelled_simulated():
    import pathlib, tempfile
    conn = make_experiment_db(n_customers=900, seed=2)
    r = cb.run_experiment(conn, lines=fe.load_lines(conn), seed=1, min_arm=100)
    eid = cb.save_experiment(conn, r)
    out = pathlib.Path(tempfile.mkdtemp())
    cb.export_experiment(conn, r, eid, out)
    s = pd.read_csv(out / "experiment_summary.csv").iloc[0]
    assert "SIMULATED" in s["data_label"] and s["is_simulated"] == 1
    assert (pd.read_csv(out / "cause_segment_counts.csv")["data_label"] == "SIMULATED").all()
