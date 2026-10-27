import json

from src import campaign_engine as ce, config, notification_service as ns
from src.offers import offer_text
from .helpers import FakeLiveProvider, TODAY, make_db, raises, set_env

NF = "Non-Food & Household"


def logs(conn, **where):
    q = "SELECT * FROM notification_logs"
    if where:
        q += " WHERE " + " AND ".join(f"{k}=?" for k in where)
    return conn.execute(q, list(where.values())).fetchall()


def test_one_click_matches_different_offers_per_customer():
    conn = make_db()
    s = ce.run_active_offer_campaign(conn, NF, today=TODAY)
    assert s["processed"] == 5 and s["simulated"] == 2 and s["skipped_no_offer"] == 3
    msgs = {m["to"]: m["text"] for m in s["messages"]}
    assert "Dettol" in msgs["Merin"] and "20% off" in msgs["Merin"]
    assert "Harpic" in msgs["Hanna"] and "15% off" in msgs["Hanna"]
    assert "Harpic" not in msgs["Merin"] and "Dettol" not in msgs["Hanna"]


def test_only_selected_department_offers_are_sent():
    conn = make_db()
    s = ce.run_active_offer_campaign(conn, NF, today=TODAY)
    for row in logs(conn, status="simulated"):
        for code in json.loads(row["matched_offers"]):
            dept = conn.execute("SELECT department FROM offers WHERE offer_code=?", (code,)).fetchone()[0]
            assert dept == NF
    assert not any("Coca-Cola" in m["text"] or "Lays" in m["text"] for m in s["messages"])


def test_snacks_department_same_logic_multiple_offers():
    conn = make_db()
    s = ce.run_active_offer_campaign(conn, "Frozen & Snacks", today=TODAY)
    msgs = {m["to"]: m["text"] for m in s["messages"]}
    assert "Coca-Cola" in msgs["Delna"] and "Lays" in msgs["Delna"]          # two matched offers, one message
    assert "Coca-Cola" in msgs["Merin"] and "Lays" not in msgs["Merin"]


def test_skip_reasons_recorded_expired_and_missing_offers():
    conn = make_db()
    ce.run_active_offer_campaign(conn, NF, today=TODAY)
    adw = logs(conn, recipient_name="Adwoith")[0]
    assert adw["status"] == "skipped" and "expired" in adw["skip_or_suppression_reason"]
    conn.execute("DELETE FROM offers WHERE offer_code='OFF-RICE'")              # no rice offer configured any more
    conn.commit()
    ce.run_active_offer_campaign(conn, "Pantry & Dry Goods", today=TODAY)
    angel = [r for r in logs(conn, recipient_name="Angel") if r["department"] == "Pantry & Dry Goods"][0]
    assert angel["status"] == "skipped" and "no configured offer" in angel["skip_or_suppression_reason"]


def test_no_consent_means_suppressed_not_sent():
    conn = make_db(consent=False)
    s = ce.run_active_offer_campaign(conn, NF, today=TODAY)
    assert s["suppressed"] == 5 and s["simulated"] == 0 and s["eligible_customers"] == 0
    conn.execute("UPDATE demo_recipients SET consent_status='opted_in' WHERE name='Merin'")
    conn.execute("UPDATE demo_recipients SET consent_status='opted_out' WHERE name='Hanna'")
    conn.commit()
    s = ce.run_active_offer_campaign(conn, NF, today=TODAY, rehearsal=True)
    assert s["simulated"] == 1 and any("opted_out" in r for r in s["suppressed_reasons"])


def test_duplicate_prevention_on_repeat_click():
    conn = make_db()
    ce.run_active_offer_campaign(conn, NF, today=TODAY)
    s2 = ce.run_active_offer_campaign(conn, NF, today=TODAY)
    assert s2["simulated"] == 0
    assert any("duplicate" in r for r in s2["suppressed_reasons"]) and s2["suppressed"] == 2
    assert len(logs(conn, status="simulated")) == 2                              # still only 2 real sends


def test_rehearsal_mode_is_repeatable_and_never_live():
    conn = make_db()
    with set_env(whatsapp_enabled=True):
        a = ce.run_active_offer_campaign(conn, NF, mode="whatsapp", provider=FakeLiveProvider(), today=TODAY, rehearsal=True)
        b = ce.run_active_offer_campaign(conn, NF, mode="whatsapp", provider=FakeLiveProvider(), today=TODAY, rehearsal=True)
    assert a["mode"] == "simulated" and a["simulated"] == 2 and b["simulated"] == 2
    assert a["campaign_id"] != b["campaign_id"]


def test_live_sending_blocked_unless_enabled():
    conn = make_db()
    with set_env(whatsapp_enabled=False):
        assert raises(ce.LiveSendingDisabled, ce.run_active_offer_campaign, conn, NF, mode="whatsapp",
                      provider=FakeLiveProvider(), today=TODAY)
    assert logs(conn) == []


def test_live_accepted_is_not_delivered_until_confirmed():
    conn = make_db()
    prov = FakeLiveProvider()
    env = {"DEMO_PHONE_MERIN": "+919876501234", "DEMO_PHONE_HANNA": "+919876505678"}
    with set_env(env, whatsapp_enabled=True):
        s = ce.run_active_offer_campaign(conn, NF, mode="whatsapp", provider=prov, today=TODAY)
    assert s["accepted_by_provider"] == 2 and s["delivered"] == 0
    assert {r["status"] for r in logs(conn, delivery_mode="whatsapp") if r["status"] != "skipped"} == {"sent_to_provider"}
    prov.final_status = "queued"
    assert ce.refresh_delivery_status(conn, prov) == 0                              # still not confirmed
    prov.final_status = "delivered"
    assert ce.refresh_delivery_status(conn, prov) == 2
    assert len(logs(conn, status="delivered")) == 2


def test_missing_phone_suppressed_in_live_mode():
    conn = make_db()
    with set_env({"DEMO_PHONE_MERIN": "+919876501234", "DEMO_PHONE_HANNA": "not-a-number"}, whatsapp_enabled=True):
        s = ce.run_active_offer_campaign(conn, NF, mode="whatsapp", provider=FakeLiveProvider(), today=TODAY)
    assert s["accepted_by_provider"] == 1 and any("phone" in r for r in s["suppressed_reasons"])


def test_provider_failure_is_logged_and_retry_succeeds():
    conn = make_db()
    env = {"DEMO_PHONE_MERIN": "+919876501234", "DEMO_PHONE_HANNA": "+919876505678"}
    with set_env(env, whatsapp_enabled=True):
        s1 = ce.run_active_offer_campaign(conn, NF, mode="whatsapp", provider=FakeLiveProvider(fail_for={"Hanna"}), today=TODAY)
        assert s1["failed"] == 1 and s1["accepted_by_provider"] == 1
        failed = logs(conn, status="failed")[0]
        assert failed["recipient_name"] == "Hanna" and "provider error" in failed["failure_reason"]
        s2 = ce.run_active_offer_campaign(conn, NF, mode="whatsapp", provider=FakeLiveProvider(), today=TODAY)
    assert s2["accepted_by_provider"] == 1 and s2["suppressed"] == 1          # Merin duplicate-blocked, Hanna retried


def test_phone_numbers_never_stored_in_database():
    conn = make_db()
    with set_env({"DEMO_PHONE_MERIN": "+919999999999", "DEMO_PHONE_HANNA": "+918888888888"}, whatsapp_enabled=True):
        pass
    with set_env({"DEMO_PHONE_MERIN": "+919876543210", "DEMO_PHONE_HANNA": "+919123456780"}, whatsapp_enabled=True):
        prov = FakeLiveProvider()
        s = ce.run_active_offer_campaign(conn, NF, mode="whatsapp", provider=prov, today=TODAY)
    assert s["accepted_by_provider"] == 2 and len(prov.sent) == 2          # messages really were sent
    dump = " ".join(str(tuple(r)) for t in ("notification_logs", "demo_recipients", "customers", "campaign_recipients")
                    for r in conn.execute(f"SELECT * FROM {t}"))
    assert "9876543210" not in dump and "9123456780" not in dump


def test_simulated_provider_status_is_labelled():
    conn = make_db()
    ce.run_active_offer_campaign(conn, NF, today=TODAY)
    r = logs(conn, status="simulated")[0]
    assert r["delivery_mode"] == "simulated" and r["provider_status"] == "simulated_not_sent"


def test_message_never_invents_discount():
    conn = make_db()
    s = ce.run_active_offer_campaign(conn, "Frozen & Snacks", today=TODAY)
    delna = [m for m in s["messages"] if m["to"] == "Delna"][0]["text"]
    assert "%" not in delna and "Special promotional price" in delna


def test_product_names_are_formatted_nicely_in_messages():
    assert ce.pretty("DETTOL ANTISEPTIC LIQUID 500ML") == "Dettol Antiseptic Liquid 500ml"
    assert ce.pretty("COCA-COLA 750ML") == "Coca-Cola 750ml"
    assert ce.pretty("FULL CREAM MILK 1L") == "Full Cream Milk 1L" and ce.pretty("BASMATI RICE 5KG") == "Basmati Rice 5kg"


def test_placeholder_phone_numbers_are_rejected():
    env = {"P1": "+910000000000", "P2": "+91" + "1" * 10, "P3": "+919037527284", "P4": "9037527284"}
    with set_env(env):
        assert ns.get_phone("P1") is None and ns.get_phone("P2") is None     # all-zero / repeated-digit placeholders
        assert ns.get_phone("P4") is None                                    # missing +91 prefix
        assert ns.get_phone("P3") == "+919037527284"


# ---------------------------------------------------------------- "All categories"
ALL = config.ALL_CATEGORIES


def test_all_categories_reaches_every_customer_with_their_own_offers_in_one_campaign():
    conn = make_db()
    s = ce.run_active_offer_campaign(conn, ALL, today=TODAY)
    assert s["processed"] == 5 and s["simulated"] == 5 and s["skipped_no_offer"] == 0
    assert len(s["messages"]) == 5                                              # exactly one message per customer
    msgs = {m["to"]: m["text"] for m in s["messages"]}
    assert "Dettol" in msgs["Merin"] and "Coca-Cola" in msgs["Merin"] and "Lays" not in msgs["Merin"] and "Harpic" not in msgs["Merin"]
    assert "Harpic" in msgs["Hanna"] and "Basmati" in msgs["Hanna"] and "Dettol" not in msgs["Hanna"]       # Hanna bought Dettol only once
    assert "Coca-Cola" in msgs["Delna"] and "Lays" in msgs["Delna"]
    assert "Basmati" in msgs["Angel"] and "Sunflower" not in msgs["Angel"] and "Toor" not in msgs["Angel"]  # no offer for oil or dal
    assert "Milk" in msgs["Adwoith"] and "Surf" not in msgs["Adwoith"]                                       # Surf Excel offer is expired
    assert all(r["department"] == ALL for r in logs(conn))


def test_all_categories_repeat_click_is_blocked_but_it_is_a_separate_campaign_from_a_single_category():
    conn = make_db()
    ce.run_active_offer_campaign(conn, ALL, today=TODAY)
    again = ce.run_active_offer_campaign(conn, ALL, today=TODAY)
    assert again["simulated"] == 0 and again["suppressed"] == 5
    single = ce.run_active_offer_campaign(conn, NF, today=TODAY)                 # different campaign key -> not blocked
    assert single["simulated"] == 2
    assert len(logs(conn, status="simulated")) == 7


def test_all_categories_respects_consent_and_the_per_message_cap():
    conn = make_db(consent=False)
    conn.execute("UPDATE demo_recipients SET consent_status='opted_in' WHERE name IN ('Merin','Delna')")
    conn.commit()
    s = ce.run_active_offer_campaign(conn, ALL, today=TODAY, rehearsal=True)
    assert s["simulated"] == 2 and s["suppressed"] == 3
    old = config.MAX_OFFERS_PER_MESSAGE
    config.MAX_OFFERS_PER_MESSAGE = 1
    try:
        s = ce.run_active_offer_campaign(conn, ALL, today=TODAY, rehearsal=True)
    finally:
        config.MAX_OFFERS_PER_MESSAGE = old
    delna = [m["text"] for m in s["messages"] if m["to"] == "Delna"][0]
    assert "Coca-Cola" in delna and "Lays" not in delna                          # most-bought product wins when capped


def test_all_categories_explains_why_nothing_matched():
    conn = make_db()
    conn.execute("UPDATE offers SET status='inactive'")
    conn.commit()
    ce.run_active_offer_campaign(conn, ALL, today=TODAY)
    reasons = {r["recipient_name"]: r["skip_or_suppression_reason"] for r in logs(conn, status="skipped")}
    assert len(reasons) == 5 and "expired, not yet started or inactive" in reasons["Merin"]
    conn.execute("DELETE FROM transaction_items WHERE invoice LIKE 'DEMO-DELNA%'")
    conn.commit()
    ce.run_active_offer_campaign(conn, ALL, today=TODAY, rehearsal=True)
    delna = [r for r in logs(conn, recipient_name="Delna", status="skipped")][-1]
    assert "no frequently purchased product in any category" in delna["skip_or_suppression_reason"]


def test_unknown_category_is_still_rejected():
    from .helpers import raises
    assert raises(ValueError, ce.run_active_offer_campaign, make_db(), "Made up", today=TODAY)


def test_command_line_accepts_all_categories_only_for_send_offers():
    import contextlib, io
    from src import main
    p = main.build_parser()
    assert p.parse_args(["send-offers", "--category", ALL]).category == ALL
    rejected = False
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            p.parse_args(["send-approved", "--category", ALL])
        except SystemExit:
            rejected = True
    assert rejected                                                              # approved offers are still sent per category
