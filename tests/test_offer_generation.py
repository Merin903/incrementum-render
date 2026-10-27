from src import offer_generation as og, offers
from .helpers import TODAY, make_db

NF = "Non-Food & Household"


def test_analysis_groups_customers_by_top_product():
    conn = make_db()
    analysis, groups = og.analyze_category(conn, NF, TODAY)
    assert "DETTOL ANTISEPTIC LIQUID 500ML" in groups and groups["DETTOL ANTISEPTIC LIQUID 500ML"] == ["Merin"]
    assert "HARPIC TOILET CLEANER 750ML" in groups


def test_proposals_are_not_active_and_follow_rules():
    conn = make_db()
    created = og.propose_offers(conn, NF, TODAY)
    rows = {r["name"]: r for r in og.list_proposals(conn, NF)}
    assert set(rows) == {"Merin", "Hanna", "Adwoith"} or set(rows) >= {"Merin", "Hanna"}
    assert rows["Merin"]["discount_percent"] == 10 and rows["Hanna"]["discount_percent"] == 10   # 4+ purchases -> 10% tier
    assert all(r["status"] == "proposed" for r in rows.values())
    assert offers.active_offers(conn, TODAY, origin="generated") == []                           # nothing active yet
    assert len(created) == len(rows)


def test_unapproved_proposals_are_never_sent():
    from src import offer_generation as og2
    conn = make_db()
    og.propose_offers(conn, NF, TODAY)
    s = og2.send_approved(conn, NF, today=TODAY)
    assert s["simulated"] == 0 and s["skipped_no_offer"] == 5


def test_approve_then_send_only_approved():
    conn = make_db()
    og.propose_offers(conn, NF, TODAY)
    ids = {r["name"]: r["proposal_id"] for r in og.list_proposals(conn, NF)}
    assert og.approve(conn, [ids["Merin"]], TODAY) == 1
    assert og.reject(conn, [ids["Hanna"]]) == 1
    s = og.send_approved(conn, NF, today=TODAY)
    assert s["simulated"] == 1 and s["messages"][0]["to"] == "Merin" and "Dettol" in s["messages"][0]["text"].title()
    assert "10% off" in s["messages"][0]["text"]
    by = {r["name"]: r["status"] for r in og.list_proposals(conn, NF)}
    assert by["Merin"] == "approved" and by["Hanna"] == "rejected"
    s2 = og.send_approved(conn, NF, today=TODAY)          # already notified -> not sent again
    assert s2["simulated"] == 0


def test_generated_offers_do_not_touch_configured_active_offers():
    conn = make_db()
    before = [tuple(o) for o in offers.active_offers(conn, TODAY)]
    og.propose_offers(conn, NF, TODAY)
    og.approve(conn, None, TODAY)
    after = [tuple(o) for o in offers.active_offers(conn, TODAY)]
    assert before == after
    from src import campaign_engine as ce
    s = ce.run_active_offer_campaign(conn, NF, today=TODAY)          # Part 1 still uses only configured offers
    assert all("10% off" not in m["text"] for m in s["messages"])


def test_expired_approved_offer_not_sent():
    from datetime import timedelta
    conn = make_db()
    og.propose_offers(conn, NF, TODAY)
    og.approve(conn, None, TODAY)
    s = og.send_approved(conn, NF, today=TODAY + timedelta(days=30))
    assert s["simulated"] == 0


def test_no_discount_when_no_rule_tier_matches():
    conn = make_db()
    rules = og.load_rules()
    rules["tiers"] = []
    assert og.propose_offers(conn, NF, TODAY, rules) == []
