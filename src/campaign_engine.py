"""One-click campaign engine.

Part 1 (run_active_offer_campaign): configured ACTIVE offers -> category-based matching -> send.
Part 2 sending (see offer_generation.send_approved) reuses dispatch() with approved proposals.

Safety rules enforced here, for every send:
  * consent must be 'opted_in'         * real sending needs WHATSAPP_ENABLED + live mode
  * duplicate protection per campaign   * status words never overstate what happened
"""
import json
import re
import uuid
from datetime import date

from . import config, notification_service as ns, offers as offers_mod
from .database import now_iso
from .preferences import customer_preferences

STRATEGY_ACTIVE = "category_based_active_offers"
SUCCESS = ("simulated", "queued", "sent_to_provider", "delivered")


class LiveSendingDisabled(Exception):
    pass


# ---------------------------------------------------------------- matching
def get_recipients(conn):
    return conn.execute("SELECT r.*, c.display_name FROM demo_recipients r "
                        "JOIN customers c ON c.customer_id=r.customer_id ORDER BY r.name").fetchall()


def match_customer(conn, customer_id, department, active, today):
    """Return (matches, notes). Matches are frequent products that have a currently active offer."""
    prefs = customer_preferences(conn, customer_id, department, today)
    frequent = [p for p in prefs if p["frequent"]]
    matches, notes = [], []
    if not frequent:
        notes.append(f"no frequently purchased product in {department}")
        return matches, notes
    by_stock = {}
    for o in active:
        by_stock.setdefault(o["stock_code"], []).append(o)
    for p in frequent:
        cands = by_stock.get(p["stock_code"])
        if cands:
            best = sorted(cands, key=lambda o: -(o["discount_percent"] or 0))[0]
            matches.append({"offer": best, "product_name": p["description"], "purchases": p["purchases"],
                            "reason": f"customer {p['reason']}; active offer {best['offer_code']} applies"})
        else:
            any_offer = offers_mod.offers_any_validity(conn, p["stock_code"])
            why = ("an offer exists but is expired, not yet started or inactive" if any_offer
                   else "no configured offer")
            notes.append(f"{pretty(p['description'])}: {why}")
    return matches[:config.MAX_OFFERS_PER_MESSAGE], notes


def pretty(product_name: str) -> str:
    """'DETTOL ANTISEPTIC LIQUID 500ML' -> 'Dettol Antiseptic Liquid 500ml' (units stay lower-case)."""
    def fix(w):
        if re.fullmatch(r"\d+L", w, re.I):          # '1L' stays '1L' (a lower-case l looks like the digit 1)
            return w.upper()
        return w.lower() if any(ch.isdigit() for ch in w) else w.title()
    return " ".join(fix(w) for w in product_name.split())


def match_all(conn, customer_id, active, today):
    """'All categories': check every department; one combined list of matches, most-bought products first."""
    matches, notes = [], []
    for dept in config.DEPARTMENTS:
        m, n = match_customer(conn, customer_id, dept, active, today)
        matches += m
        notes += [x for x in n if not x.startswith("no frequently purchased product in")]
    if not matches and not notes:
        notes = ["no frequently purchased product in any category"]
    matches.sort(key=lambda m: -m["purchases"])
    return matches[:config.MAX_OFFERS_PER_MESSAGE], notes


def build_message(name, matches) -> str:
    def line(m):
        o = m["offer"]
        return f"{pretty(m['product_name'])}: {offers_mod.offer_text(o)} (valid till {o['valid_to']})"
    if len(matches) == 1:
        m = matches[0]
        o = m["offer"]
        body = (f"Hi {name}! You often buy {pretty(m['product_name'])}. This week: {offers_mod.offer_text(o)}"
                f" (valid till {o['valid_to']}). Visit {config.SHOP_NAME} to check the offer.")
    else:
        body = (f"Hi {name}! Based on your regular purchases, we have these offers:\n"
                + "\n".join("- " + line(m) for m in matches) + f"\nVisit {config.SHOP_NAME} to check the offers.")
    return f"{body} {config.MESSAGE_FOOTER}"


# ---------------------------------------------------------------- dispatch
def _get_or_create_campaign(conn, key, name, strategy, department, source, mode, rehearsal):
    row = conn.execute("SELECT campaign_id FROM campaigns WHERE campaign_key=?", (key,)).fetchone()
    if row:
        conn.execute("UPDATE campaigns SET status='running' WHERE campaign_id=?", (row[0],))
        return row[0]
    cur = conn.execute("INSERT INTO campaigns (campaign_key, name, strategy, department, source, delivery_mode, "
                       "rehearsal, status, created_at) VALUES (?,?,?,?,?,?,?, 'running', ?)",
                       (key, name, strategy, department, source, mode, int(rehearsal), now_iso()))
    return cur.lastrowid


def _log(conn, cid, rec, strategy, dept, matches, message, mode, status, reason=None, result=None):
    codes = [m["offer"]["offer_code"] for m in matches]
    products = ", ".join(m["product_name"] for m in matches) or None
    sel = "; ".join(m["reason"] for m in matches) or None
    conn.execute(
        "INSERT INTO notification_logs (campaign_id, customer_id, recipient_name, strategy, department, "
        "matched_offers, product_or_category, selection_reason, skip_or_suppression_reason, message_text, "
        "delivery_mode, attempted_at, status, provider_message_id, provider_status, failure_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, rec["customer_id"], rec["name"], strategy, dept, json.dumps(codes), products or dept, sel, reason,
         message, mode, now_iso(), status, getattr(result, "message_id", None),
         getattr(result, "provider_status", None), getattr(result, "error", None)))


def _set_recipient_row(conn, cid, customer_id, status, reason):
    now = now_iso()
    conn.execute("INSERT INTO campaign_recipients (campaign_id, customer_id, status, reason, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?) ON CONFLICT(campaign_id, customer_id) DO UPDATE SET status=excluded.status, "
                 "reason=excluded.reason, updated_at=excluded.updated_at", (cid, customer_id, status, reason, now, now))


def dispatch(conn, *, campaign_key, name, strategy, department, source, mode, rehearsal, plan, provider):
    """plan: {customer_id: {'recipient': row, 'matches': [...], 'notes': [...]}} -> summary dict."""
    cid = _get_or_create_campaign(conn, campaign_key, name, strategy, department, source, mode, rehearsal)
    s = {"campaign_id": cid, "campaign_key": campaign_key, "mode": mode, "rehearsal": rehearsal,
         "eligible_customers": 0, "processed": 0, "simulated": 0, "accepted_by_provider": 0, "delivered": 0,
         "skipped_no_offer": 0, "suppressed": 0, "suppressed_reasons": {}, "failed": 0,
         "products_without_active_offer": 0, "messages": []}

    def suppressed(rec, reason, matches=()):
        s["suppressed"] += 1
        s["suppressed_reasons"][reason] = s["suppressed_reasons"].get(reason, 0) + 1
        _log(conn, cid, rec, strategy, department, matches, None, mode, "suppressed", reason)

    for customer_id, item in plan.items():
        rec, matches, notes = item["recipient"], item["matches"], item["notes"]
        s["processed"] += 1
        s["products_without_active_offer"] += sum(1 for n in notes if "offer" in n)
        if rec["consent_status"] != "opted_in":
            suppressed(rec, f"consent not granted (status: {rec['consent_status']})", matches)
            continue
        s["eligible_customers"] += 1
        if not matches:
            reason = "; ".join(notes) or "no matching active offer"
            s["skipped_no_offer"] += 1
            _log(conn, cid, rec, strategy, department, [], None, mode, "skipped", reason)
            _set_recipient_row(conn, cid, customer_id, "skipped", reason)
            continue
        prev = conn.execute("SELECT status FROM campaign_recipients WHERE campaign_id=? AND customer_id=?",
                            (cid, customer_id)).fetchone()
        if prev and prev["status"] in SUCCESS:
            suppressed(rec, "duplicate: already notified in this campaign", matches)
            continue
        phone = None
        if provider.mode != "simulated":
            phone = ns.get_phone(rec["phone_env_key"])
            if not phone:
                suppressed(rec, "no valid phone number configured in .env", matches)
                continue
        message = build_message(rec["name"], matches)
        result = provider.send(phone, message, rec["name"])
        status = result.status if result.ok else "failed"
        _log(conn, cid, rec, strategy, department, matches, message, mode, status, None, result)
        _set_recipient_row(conn, cid, customer_id, status, result.error)
        s["messages"].append({"to": rec["name"], "status": status, "text": message})
        if status == "simulated":
            s["simulated"] += 1
        elif status == "sent_to_provider":
            s["accepted_by_provider"] += 1
        elif status == "failed":
            s["failed"] += 1
    conn.execute("UPDATE campaigns SET status='completed', finished_at=? WHERE campaign_id=?", (now_iso(), cid))
    conn.commit()
    return s


def _campaign_key(prefix, department, today, mode, rehearsal):
    key = f"{prefix}:{department}:{today}:{mode}"
    return f"{key}:rehearsal-{uuid.uuid4().hex[:6]}" if rehearsal else key


def run_active_offer_campaign(conn, department, mode="simulated", provider=None, today=None, rehearsal=False):
    """THE 'Send Offers' operation: one call processes every demo recipient."""
    today = today or date.today()
    everything = department == config.ALL_CATEGORIES
    if not everything and department not in config.DEPARTMENTS:
        raise ValueError(f"Choose one of: {[config.ALL_CATEGORIES] + config.DEPARTMENTS}")
    if rehearsal:
        mode, provider = "simulated", ns.SimulatedProvider()   # rehearsals can never send real messages
    if mode == "whatsapp" and not config.WHATSAPP_ENABLED:
        raise LiveSendingDisabled("Real sending is disabled. Set WHATSAPP_ENABLED=true in .env to allow it.")
    provider = provider or ns.get_provider(mode)
    active = offers_mod.active_offers(conn, today, None if everything else department)
    plan = {}
    for rec in get_recipients(conn):
        if everything:
            matches, notes = match_all(conn, rec["customer_id"], active, today)
        else:
            matches, notes = match_customer(conn, rec["customer_id"], department, active, today)
        plan[rec["customer_id"]] = {"recipient": rec, "matches": matches, "notes": notes}
    summary = dispatch(conn, campaign_key=_campaign_key("active", department, today, mode, rehearsal),
                       name=f"Active offers - {department} - {today}", strategy=STRATEGY_ACTIVE,
                       department=department, source="configured_active", mode=mode, rehearsal=rehearsal,
                       plan=plan, provider=provider)
    summary["active_offers_available"] = len(active)
    summary["customers_matched"] = sum(1 for p in plan.values() if p["matches"])
    return summary


def refresh_delivery_status(conn, provider) -> int:
    """Ask the provider about messages it accepted; mark 'delivered' ONLY if it confirms."""
    n = 0
    rows = conn.execute("SELECT log_id, campaign_id, customer_id, provider_message_id FROM notification_logs "
                        "WHERE status='sent_to_provider' AND provider_message_id IS NOT NULL").fetchall()
    for r in rows:
        try:
            pstatus = provider.fetch_status(r["provider_message_id"])
        except Exception:
            continue
        new = "delivered" if ns.is_confirmed_delivery(pstatus) else "sent_to_provider"
        conn.execute("UPDATE notification_logs SET provider_status=?, status=? WHERE log_id=?",
                     (pstatus, new, r["log_id"]))
        if new == "delivered":
            conn.execute("UPDATE campaign_recipients SET status='delivered', updated_at=? "
                         "WHERE campaign_id=? AND customer_id=?", (now_iso(), r["campaign_id"], r["customer_id"]))
            n += 1
    conn.commit()
    return n
