"""Configured (Part 1) offers: loading, validity checks, wording. Nothing is invented here."""
import json

from . import config
from .database import now_iso


def load_from_config(conn, path=None) -> int:
    data = json.loads((path or config.ACTIVE_OFFERS_FILE).read_text())["offers"]
    for o in data:
        prod = conn.execute("SELECT department FROM products WHERE stock_code=?", (o["stock_code"],)).fetchone()
        if prod is None:
            raise ValueError(f"Offer {o['offer_code']} refers to unknown product {o['stock_code']}")
        if o.get("discount_percent") is None and not o.get("promo_text"):
            raise ValueError(f"Offer {o['offer_code']} needs a discount_percent or a promo_text")
        conn.execute(
            "INSERT INTO offers (offer_code, stock_code, department, discount_percent, promo_text, valid_from, "
            "valid_to, origin, status, created_at) VALUES (?,?,?,?,?,?,?,'configured','active',?) "
            "ON CONFLICT(offer_code) DO UPDATE SET stock_code=excluded.stock_code, department=excluded.department, "
            "discount_percent=excluded.discount_percent, promo_text=excluded.promo_text, "
            "valid_from=excluded.valid_from, valid_to=excluded.valid_to",
            (o["offer_code"], o["stock_code"], prod["department"], o.get("discount_percent"), o.get("promo_text"),
             o["valid_from"], o["valid_to"], now_iso()))
    conn.commit()
    return len(data)


def active_offers(conn, today, department=None, origin="configured") -> list:
    """Offers that are status=active AND inside their validity window on `today`."""
    sql = ("SELECT o.*, p.description AS product_name FROM offers o JOIN products p ON p.stock_code=o.stock_code "
           "WHERE o.origin=? AND o.status='active' AND o.valid_from<=? AND o.valid_to>=?")
    args = [origin, str(today), str(today)]
    if department:
        sql += " AND o.department=?"
        args.append(department)
    return conn.execute(sql + " ORDER BY o.offer_code", args).fetchall()


def offers_any_validity(conn, stock_code, origin="configured") -> list:
    return conn.execute("SELECT * FROM offers WHERE stock_code=? AND origin=?", (stock_code, origin)).fetchall()


def offer_text(offer) -> str:
    """Only what is configured: a percentage if set, otherwise the configured promo text."""
    if offer["discount_percent"] is not None:
        return f"{offer['discount_percent']:g}% off"
    return offer["promo_text"]
