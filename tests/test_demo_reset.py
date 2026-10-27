from src import campaign_engine as ce, demo_data, offer_generation as og, offers, product_segmentation as ps
from .helpers import TODAY, make_db
from .test_segmentation import add_real_customers

NF = "Non-Food & Household"


def counts(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]
    return {"real_customers": q("SELECT COUNT(*) FROM customers WHERE is_synthetic=0"),
            "real_items": q("SELECT COUNT(*) FROM transaction_items WHERE is_synthetic=0"),
            "real_products": q("SELECT COUNT(*) FROM products WHERE is_synthetic=0")}


def test_reset_demo_keeps_real_data_and_removes_demo_data_and_logs():
    conn = make_db()
    add_real_customers(conn, n=60)
    ps.run_segmentation(conn, TODAY)
    ce.run_active_offer_campaign(conn, NF, today=TODAY)
    og.propose_offers(conn, NF, TODAY)
    before = counts(conn)
    real_memberships = conn.execute("SELECT COUNT(*) FROM customer_segment_memberships WHERE customer_id NOT LIKE 'DEMO-%'").fetchone()[0]
    demo_data.reset_demo(conn)
    assert counts(conn) == before                                              # real data untouched
    for t in ("notification_logs", "campaigns", "offers", "offer_proposals", "demo_recipients"):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
    assert conn.execute("SELECT COUNT(*) FROM customers WHERE is_synthetic=1").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM customer_features WHERE customer_id LIKE 'DEMO-%'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM customer_segment_memberships WHERE customer_id NOT LIKE 'DEMO-%'").fetchone()[0] == real_memberships
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_demo_can_be_recreated_after_reset_and_works_again():
    conn = make_db()
    add_real_customers(conn, n=30)
    demo_data.reset_demo(conn)
    demo_data.seed(conn, today=TODAY)
    offers.load_from_config(conn)
    conn.execute("UPDATE demo_recipients SET consent_status='opted_in'")
    conn.commit()
    s = ce.run_active_offer_campaign(conn, NF, today=TODAY)
    assert s["simulated"] == 2 and counts(conn)["real_customers"] == 30


def test_reset_is_safe_to_run_twice():
    conn = make_db()
    demo_data.reset_demo(conn)
    demo_data.reset_demo(conn)
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0


# ---------------------------------------------------------------- reset_campaigns (terminal reset between presentations)
import pathlib
import tempfile


def sent_state(conn):
    ce.run_active_offer_campaign(conn, NF, today=TODAY)
    og.propose_offers(conn, NF, TODAY)
    conn.execute("INSERT INTO transactions (invoice, customer_id, invoice_date, country, is_synthetic) VALUES ('POS-AAAA1111','DEMO-MERIN','2026-10-15 12:00:00','India',1)")
    conn.execute("INSERT INTO transaction_items (invoice, stock_code, quantity, price, line_total, is_synthetic) VALUES ('POS-AAAA1111','DET500',1,210,210,1)")
    conn.commit()


def test_reset_campaigns_lets_send_offers_run_again_but_duplicate_protection_still_works():
    conn = make_db()
    first = ce.run_active_offer_campaign(conn, NF, today=TODAY)
    again = ce.run_active_offer_campaign(conn, NF, today=TODAY)
    assert first["simulated"] == 2 and again["simulated"] == 0                           # the existing behaviour, unchanged
    demo_data.reset_campaigns(conn, archive_dir=pathlib.Path(tempfile.mkdtemp()))
    fresh = ce.run_active_offer_campaign(conn, NF, today=TODAY)
    assert fresh["simulated"] == 2 and fresh["suppressed"] == 0                          # sends again after the reset
    assert ce.run_active_offer_campaign(conn, NF, today=TODAY)["simulated"] == 0         # and a second click is still blocked


def test_reset_campaigns_clears_campaign_state_and_keeps_everything_else():
    conn = make_db()
    conn.execute("UPDATE offers SET status='inactive' WHERE offer_code='OFF-LAYS'")      # an operator choice that must survive
    add_real_customers(conn, n=30)
    ps.run_segmentation(conn, TODAY)
    sent_state(conn)
    keep = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
            ("customers", "products", "customer_features", "customer_segment_memberships", "demo_recipients")}
    items_before = conn.execute("SELECT COUNT(*) FROM transaction_items WHERE is_synthetic=1").fetchone()[0]
    offers_before = conn.execute("SELECT offer_code, status FROM offers WHERE origin='configured' ORDER BY 1").fetchall()
    r = demo_data.reset_campaigns(conn, archive_dir=pathlib.Path(tempfile.mkdtemp()))
    assert r["messages"] == 5 and r["campaigns"] == 1 and r["proposals"] == 3 and r["generated_offers"] == 3 and r["counter_purchases"] == 1
    for t in ("notification_logs", "campaigns", "campaign_recipients", "offer_proposals"):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
    assert conn.execute("SELECT COUNT(*) FROM offers WHERE origin='generated'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE invoice LIKE 'POS-%'").fetchone()[0] == 0
    assert {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in keep} == keep
    assert conn.execute("SELECT COUNT(*) FROM transaction_items WHERE is_synthetic=1").fetchone()[0] == items_before - 1   # only the counter purchase went
    assert conn.execute("SELECT offer_code, status FROM offers WHERE origin='configured' ORDER BY 1").fetchall() == offers_before
    assert conn.execute("SELECT COUNT(*) FROM demo_recipients WHERE consent_status='opted_in'").fetchone()[0] == 5    # consent kept
    assert conn.execute("SELECT COUNT(*) FROM customers WHERE is_synthetic=0").fetchone()[0] == 30                    # real data untouched
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reset_campaigns_archives_the_old_log_before_clearing_it():
    import pandas as pd
    conn = make_db()
    sent_state(conn)
    out = pathlib.Path(tempfile.mkdtemp())
    r = demo_data.reset_campaigns(conn, archive_dir=out)
    folder = pathlib.Path(r["archive"])
    assert folder.parent == out and folder.name.startswith("reset_")
    assert len(pd.read_csv(folder / "notification_logs.csv")) == 5 and len(pd.read_csv(folder / "counter_purchases.csv")) == 1
    assert len(pd.read_csv(folder / "campaigns.csv")) == 1 and len(pd.read_csv(folder / "offer_proposals.csv")) == 3
    assert "Hi Merin!" in pd.read_csv(folder / "notification_logs.csv")["message_text"].dropna().str.cat()


def test_reset_campaigns_is_safe_to_repeat_and_archives_nothing_when_there_is_nothing_to_clear():
    conn = make_db()
    out = pathlib.Path(tempfile.mkdtemp())
    r = demo_data.reset_campaigns(conn, archive_dir=out)
    assert r["archive"] is None and list(out.iterdir()) == []
    sent_state(conn)
    demo_data.reset_campaigns(conn, archive_dir=out)
    assert demo_data.reset_campaigns(conn, archive_dir=out)["messages"] == 0


def test_reset_campaigns_command_asks_for_confirmation_and_cancels_cleanly():
    import argparse, builtins, contextlib, io
    from src import main
    conn = make_db()
    sent_state(conn)
    old = builtins.input
    builtins.input = lambda _="": "no"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            main.cmd_reset_campaigns(argparse.Namespace(yes=False), conn)
    finally:
        builtins.input = old
    assert "Cancelled" in out.getvalue() and conn.execute("SELECT COUNT(*) FROM notification_logs").fetchone()[0] == 5
    assert main.build_parser().parse_args(["reset-campaigns", "--yes"]).yes is True
