from datetime import date

from src import offers
from src.preferences import customer_preferences
from .helpers import TODAY, make_db


def test_preferences_frequent_vs_occasional():
    conn = make_db()
    merin = {p["stock_code"]: p for p in customer_preferences(conn, "DEMO-MERIN", "Non-Food & Household", TODAY)}
    assert merin["DET500"]["purchases"] == 5 and merin["DET500"]["frequent"]
    hanna = {p["stock_code"]: p for p in customer_preferences(conn, "DEMO-HANNA", "Non-Food & Household", TODAY)}
    assert hanna["HAR750"]["frequent"] and not hanna["DET500"]["frequent"]   # Dettol bought only once


def test_lookback_window_excludes_old_purchases():
    conn = make_db()
    prefs = customer_preferences(conn, "DEMO-MERIN", "Non-Food & Household", TODAY, lookback_days=20)
    assert {p["stock_code"]: p["purchases"] for p in prefs}["DET500"] == 2     # days 5 and 18 only


def test_active_offer_validity_and_expiry():
    conn = make_db()
    codes = {o["offer_code"] for o in offers.active_offers(conn, TODAY)}
    assert "OFF-DETTOL" in codes and "OFF-SURF-EXPIRED" not in codes
    assert offers.active_offers(conn, date(2027, 1, 5)) == []                    # everything expired
    assert offers.active_offers(conn, date(2026, 9, 15), department="Non-Food & Household")[0]["offer_code"] == "OFF-SURF-EXPIRED"


def test_offer_text_uses_only_configured_values():
    conn = make_db()
    by = {o["offer_code"]: o for o in offers.active_offers(conn, TODAY)}
    assert offers.offer_text(by["OFF-DETTOL"]) == "20% off"
    assert "%" not in offers.offer_text(by["OFF-COKE"])            # no invented percentage


def test_offer_without_discount_or_text_rejected():
    import json, tempfile, pathlib
    from .helpers import raises
    conn = make_db()
    p = pathlib.Path(tempfile.mkdtemp()) / "o.json"
    p.write_text(json.dumps({"offers": [{"offer_code": "X", "stock_code": "DET500", "valid_from": "2026-10-01", "valid_to": "2026-12-31"}]}))
    assert raises(ValueError, offers.load_from_config, conn, p)
