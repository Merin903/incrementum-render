"""Part 2: analyse customers in a department, PROPOSE offers, require human approval, then send.

Proposals live in offers (origin='generated', status='proposed') + offer_proposals. Part 1 only
reads origin='configured', so proposals can never alter or replace the active offers.
"""
import json
from datetime import date, timedelta

from . import campaign_engine as ce, config
from .database import now_iso
from .preferences import customer_preferences


def load_rules(path=None) -> dict:
    return json.loads((path or config.OFFER_RULES_FILE).read_text())


def analyze_category(conn, department, today=None, rules=None):
    """Per-customer preferences plus groups by top product (what Section B shows)."""
    today = today or date.today()
    rules = rules or load_rules()
    result, groups = [], {}
    for rec in ce.get_recipients(conn):
        prefs = customer_preferences(conn, rec["customer_id"], department, today,
                                     rules["lookback_days"], rules["min_purchases"])
        freq = [p for p in prefs if p["frequent"]]
        top = freq[0] if freq else None
        result.append({"name": rec["name"], "customer_id": rec["customer_id"], "preferences": prefs, "top": top})
        key = top["description"] if top else "(no frequent product)"
        groups.setdefault(key, []).append(rec["name"])
    return result, groups


def _discount_for(purchases, rules):
    for t in sorted(rules["tiers"], key=lambda t: -t["min_purchases"]):
        if purchases >= t["min_purchases"]:
            return t["discount_percent"]
    return None


def propose_offers(conn, department, today=None, rules=None) -> list:
    """Create PROPOSED offers. Nothing is active or sent until approve() is called."""
    today = today or date.today()
    rules = rules or load_rules()
    analysis, _ = analyze_category(conn, department, today, rules)
    created = []
    for a in analysis:
        for p in [x for x in a["preferences"] if x["frequent"]][:rules["max_proposals_per_customer"]]:
            exists = conn.execute("SELECT 1 FROM offer_proposals WHERE customer_id=? AND stock_code=? "
                                  "AND status IN ('proposed','approved') AND notified_at IS NULL",
                                  (a["customer_id"], p["stock_code"])).fetchone()
            discount = _discount_for(p["purchases"], rules)
            if exists or discount is None:
                continue
            code = f"GEN-{a['customer_id']}-{p['stock_code']}-{today}"
            cur = conn.execute(
                "INSERT OR IGNORE INTO offers (offer_code, stock_code, department, discount_percent, origin, status, "
                "created_at) VALUES (?,?,?,?, 'generated','proposed',?)",
                (code, p["stock_code"], department, discount, now_iso()))
            if cur.rowcount == 0:
                continue
            reason = (f"{a['name']} {p['reason']}; rule tier gives {discount:g}% "
                      f"(from config/offer_rules.json)")
            conn.execute("INSERT INTO offer_proposals (offer_id, customer_id, stock_code, department, "
                         "purchase_count, reason, created_at) VALUES (?,?,?,?,?,?,?)",
                         (cur.lastrowid, a["customer_id"], p["stock_code"], department, p["purchases"], reason,
                          now_iso()))
            created.append(code)
    conn.commit()
    return created


def list_proposals(conn, department=None, status=None):
    sql = ("SELECT pr.proposal_id, r.name, p.description AS product, o.discount_percent, pr.purchase_count, "
           "pr.reason, pr.status, o.offer_code, pr.notified_at FROM offer_proposals pr "
           "JOIN offers o ON o.offer_id=pr.offer_id JOIN products p ON p.stock_code=pr.stock_code "
           "JOIN demo_recipients r ON r.customer_id=pr.customer_id WHERE 1=1")
    args = []
    if department:
        sql += " AND pr.department=?"
        args.append(department)
    if status:
        sql += " AND pr.status=?"
        args.append(status)
    return conn.execute(sql + " ORDER BY pr.proposal_id", args).fetchall()


def approve(conn, proposal_ids=None, today=None, rules=None) -> int:
    """Make proposals valid and active. proposal_ids=None approves every pending proposal."""
    today = today or date.today()
    rules = rules or load_rules()
    pending = [r["proposal_id"] for r in list_proposals(conn, status="proposed")]
    ids = pending if proposal_ids is None else [i for i in proposal_ids if i in pending]
    for pid in ids:
        oid = conn.execute("SELECT offer_id FROM offer_proposals WHERE proposal_id=?", (pid,)).fetchone()[0]
        conn.execute("UPDATE offers SET status='active', valid_from=?, valid_to=? WHERE offer_id=?",
                     (str(today), str(today + timedelta(days=rules["valid_days_after_approval"])), oid))
        conn.execute("UPDATE offer_proposals SET status='approved', decided_at=? WHERE proposal_id=?",
                     (now_iso(), pid))
    conn.commit()
    return len(ids)


def reject(conn, proposal_ids=None) -> int:
    pending = [r["proposal_id"] for r in list_proposals(conn, status="proposed")]
    ids = pending if proposal_ids is None else [i for i in proposal_ids if i in pending]
    for pid in ids:
        oid = conn.execute("SELECT offer_id FROM offer_proposals WHERE proposal_id=?", (pid,)).fetchone()[0]
        conn.execute("UPDATE offers SET status='rejected' WHERE offer_id=?", (oid,))
        conn.execute("UPDATE offer_proposals SET status='rejected', decided_at=? WHERE proposal_id=?",
                     (now_iso(), pid))
    conn.commit()
    return len(ids)


def send_approved(conn, department, mode="simulated", provider=None, today=None, rehearsal=False):
    """Send notifications for APPROVED, active, in-date generated offers only."""
    from . import notification_service as ns
    today = today or date.today()
    if rehearsal:
        mode, provider = "simulated", ns.SimulatedProvider()   # rehearsals can never send real messages
    if mode == "whatsapp" and not config.WHATSAPP_ENABLED:
        raise ce.LiveSendingDisabled("Real sending is disabled. Set WHATSAPP_ENABLED=true in .env to allow it.")
    provider = provider or ns.get_provider(mode)
    rows = conn.execute(
        "SELECT pr.proposal_id, pr.customer_id, pr.reason, o.*, p.description AS product_name "
        "FROM offer_proposals pr JOIN offers o ON o.offer_id=pr.offer_id JOIN products p ON p.stock_code=o.stock_code "
        "WHERE pr.status='approved' AND pr.notified_at IS NULL AND pr.department=? AND o.origin='generated' "
        "AND o.status='active' AND o.valid_from<=? AND o.valid_to>=?", (department, str(today), str(today))).fetchall()
    by_cust = {}
    for r in rows:
        by_cust.setdefault(r["customer_id"], []).append(
            {"offer": r, "product_name": r["product_name"], "reason": f"approved proposal #{r['proposal_id']}: {r['reason']}"})
    plan = {}
    for rec in ce.get_recipients(conn):
        matches = by_cust.get(rec["customer_id"], [])
        plan[rec["customer_id"]] = {"recipient": rec, "matches": matches,
                                    "notes": [] if matches else ["no approved generated offer for this customer"]}
    summary = ce.dispatch(conn, campaign_key=ce._campaign_key("generated", department, today, mode, rehearsal),
                          name=f"Approved generated offers - {department} - {today}",
                          strategy="generated_proposals_approved", department=department,
                          source="approved_generated", mode=mode, rehearsal=rehearsal, plan=plan, provider=provider)
    if not rehearsal:  # rehearsals are practice only; do not consume the proposals
        for cust, matches in by_cust.items():
            ok = conn.execute("SELECT 1 FROM campaign_recipients WHERE campaign_id=? AND customer_id=? AND status IN "
                              "('simulated','queued','sent_to_provider','delivered')",
                              (summary["campaign_id"], cust)).fetchone()
            if ok:
                for m in matches:
                    conn.execute("UPDATE offer_proposals SET notified_at=? WHERE proposal_id=?",
                                 (now_iso(), m["offer"]["proposal_id"]))
        conn.commit()
    return summary
