"""Incrementum web application (operations site). Power BI remains the analytics dashboard.

Python standard library only. Listens on 127.0.0.1 only. Every form carries a per-run token and the Host header
is checked. All business logic lives in the engine modules; this file only draws pages and calls them.
Honesty built in: a 'Test mode' badge whenever messages are not really sent, a 'Simulated' tag on experiment results,
and 'sent to provider' is never shown as 'delivered'.
"""
import hashlib
import html
import json
import os
import secrets
import threading
import time
import uuid
from datetime import date, datetime
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import pandas as pd

from . import auth, campaign_engine as ce, cause_based_analysis as cb, config, database, offer_generation as og
from . import notification_service as ns, offers as offers_mod, whatsapp_tools as wt

ALLOWED_HOSTS = {"127.0.0.1", "localhost"} | {
    h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()
}

E = html.escape


class Raw(str):
    """Marks HTML that is already safe/escaped."""


_ctx = threading.local()       # the signed-in user for the request being handled on this thread


def current_user():
    return getattr(_ctx, "user", None)


def is_admin():
    u = current_user()
    return bool(u and u["role"] == "admin")


# Pages and actions only admins may use. Staff can run campaigns in test mode and see analysis and results.
ADMIN_GET = {"/users", "/setup", "/recipients", "/offers"}
ADMIN_POST = {"/users/create", "/users/toggle", "/users/role", "/users/password", "/offers/add", "/offers/toggle",
              "/recipients/consent", "/setup/test-message", "/setup/check-login"}


ICON = {"/": "\U0001F3E0", "/send": "\U0001F4E8", "/generate": "\u2728", "/analysis": "\U0001F50D", "/segments": "\U0001F465",
        "/results": "\U0001F4C8", "/notifications": "\U0001F514", "/offers": "\U0001F3F7\uFE0F", "/recipients": "\U0001F4CB", "/setup": "\u2699\uFE0F", "/users": "\U0001F510"}


def compact(v):
    v = float(v)
    for lim, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= lim:
            return f"{v / lim:.1f}{suf}".replace(".0", "")
    return f"{v:.0f}"


def hbars(rows, cls="", fmt="{:,.0f}", maxv=None):
    """Horizontal bar chart in plain HTML/CSS. rows: [(label, value)] or [(label, value, colour_class)].
    maxv fixes the full-width value (use 100 for percentages); otherwise the largest value is full width."""
    if not rows:
        return "<p class='muted'>Nothing to show yet.</p>"
    top = maxv or max(r[1] for r in rows) or 1
    return "<div class='bars'>" + "".join(
        f"<div class='bar'><span class='lbl' title='{E(str(r[0]))}'>{E(str(r[0]))}</span><span class='track'><span class='fill {r[2] if len(r) > 2 else cls}' "
        f"style='width:{max(2, min(100, 100 * r[1] / top)):.1f}%'></span></span><span class='val'>{E(fmt.format(r[1]))}</span></div>" for r in rows) + "</div>"


def line_chart(points, prefix="", w=640, h=210):
    """Inline SVG line/area chart. points: [(label, value)]."""
    if len(points) < 2:
        return "<p class='muted'>Not enough history to draw a trend yet.</p>"
    L, R, T, B = 52, 14, 14, 34
    vals = [v for _, v in points]
    top = max(vals) or 1
    xs = [L + i * (w - L - R) / (len(points) - 1) for i in range(len(points))]
    ys = [T + (h - T - B) * (1 - v / top) for v in vals]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area = f"{L},{h - B} " + line + f" {xs[-1]:.1f},{h - B}"
    grid = "".join(f"<line x1='{L}' x2='{w - R}' y1='{T + (h - T - B) * f:.1f}' y2='{T + (h - T - B) * f:.1f}' stroke='#e3e9f2'/>"
                   f"<text x='{L - 6}' y='{T + (h - T - B) * f + 4:.1f}' font-size='11' fill='#6b7a8a' text-anchor='end'>{prefix}{compact(top * (1 - f))}</text>"
                   for f in (0, 0.5, 1))
    dots = "".join(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='3' fill='#1f5a9e'><title>{E(l)}: {prefix}{v:,.0f}</title></circle>"
                   for x, y, (l, v) in zip(xs, ys, points))
    step = max(1, len(points) // 6)
    labs = "".join(f"<text x='{xs[i]:.1f}' y='{h - 12}' font-size='11' fill='#6b7a8a' text-anchor='middle'>{E(points[i][0])}</text>"
                   for i in range(0, len(points), step))
    return (f"<svg viewBox='0 0 {w} {h}' width='100%' role='img'>{grid}<polygon points='{area}' fill='#2a7de1' opacity='.15'/>"
            f"<polyline points='{line}' fill='none' stroke='#1f5a9e' stroke-width='2.5'/>{dots}{labs}</svg>")


NAV = [("/", "Home"), ("/send", "Send Offers"), ("/generate", "Generate Offers"), ("/analysis", "Customer Analysis"),
       ("/segments", "Segments"), ("/results", "Campaign Results"), ("/notifications", "Notifications"),
       ("/offers", "Offers"), ("/recipients", "Recipients"), ("/setup", "Setup"), ("/users", "Users")]

CSS = """
*{box-sizing:border-box}body{font-family:'Segoe UI',Roboto,Arial,sans-serif;margin:0;background:#f3f5f9;color:#1f2a37}
a{color:#1d5fa8;text-decoration:none}a:hover{text-decoration:underline}
.top{background:linear-gradient(90deg,#14325c,#1f5a9e);color:#fff;padding:14px 28px;display:flex;align-items:center;justify-content:space-between}
.brand{font-size:22px;font-weight:700;letter-spacing:.3px}.brand small{display:block;font-size:12px;font-weight:400;opacity:.85}
nav{background:#fff;border-bottom:1px solid #dde3ec;padding:0 20px;display:flex;flex-wrap:wrap}
nav a{padding:12px 10px;color:#33475b;font-size:13px;font-weight:600;border-bottom:3px solid transparent}
nav a:hover{background:#f3f7fc;text-decoration:none}nav a.on{color:#14325c;border-bottom-color:#1f5a9e}
.wrap{max-width:1180px;margin:0 auto;padding:20px}h1{margin:6px 0 4px;font-size:26px;color:#14325c}
.sub{color:#5b6b7c;margin:0 0 16px}.card{background:#fff;border-radius:10px;padding:18px 22px;margin:14px 0;box-shadow:0 1px 4px #1f2a3714}
.card h2{margin:0 0 10px;font-size:18px;color:#14325c}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}@media(max-width:800px){.tiles{grid-template-columns:1fr}}
.kpi{background:#fff;border-radius:10px;padding:16px 18px;box-shadow:0 1px 4px #1f2a3714}.kpi b{display:block;font-size:28px;color:#14325c}
.kpi span{color:#5b6b7c;font-size:13px}.tile{display:block;background:#fff;border-radius:10px;padding:18px;box-shadow:0 1px 4px #1f2a3714;color:#1f2a37}
.tile:hover{box-shadow:0 3px 10px #1f2a3730;text-decoration:none}.tile b{display:block;color:#1f5a9e;font-size:17px;margin-bottom:4px}
table{border-collapse:collapse;width:100%;font-size:14px;margin:8px 0}th,td{border-bottom:1px solid #e6eaf1;padding:8px 10px;text-align:left;vertical-align:top}
th{background:#f0f4fa;color:#33475b;font-weight:600}.badge{display:inline-block;padding:3px 11px;border-radius:12px;font-size:12px;font-weight:700}
.test{background:#fff3cd;color:#7a5b00}.livebadge{background:#d4efdf;color:#145a32}.simtag{background:#e8daef;color:#5b2c6f;margin-left:8px}
.note{background:#eef5fd;border-left:4px solid #1f5a9e;padding:10px 14px;margin:10px 0;font-size:14px;border-radius:4px}
.warnbox{background:#fff6e5;border-left:4px solid #e0a100;padding:10px 14px;margin:10px 0;font-size:14px;border-radius:4px}
.errbox{background:#fdecea;border-left:4px solid #c0392b;padding:10px 14px;margin:10px 0;font-size:14px;border-radius:4px}
button,.btn{padding:9px 18px;border:0;border-radius:7px;background:#1f5a9e;color:#fff;font-size:14px;font-weight:600;cursor:pointer}
button:hover{background:#174a85}button.big{background:#1e8449;font-size:19px;padding:15px 42px}button.big:hover{background:#186a3b}
button.red{background:#c0392b}button.grey{background:#6c7a89}select,input[type=text],input[type=number],input[type=date]{padding:8px;font-size:14px;border:1px solid #c5cedb;border-radius:6px}
.msg{background:#f7f9fc;border:1px solid #dbe2ec;border-radius:8px;padding:10px 14px;margin:8px 0;white-space:pre-wrap;font-size:14px}
.s-simulated{color:#6c3483;font-weight:600}.s-skipped{color:#9a7d0a;font-weight:600}.s-suppressed{color:#a04000;font-weight:600}
.s-failed{color:#c0392b;font-weight:700}.s-sent_to_provider{color:#117a65;font-weight:600}.s-delivered{color:#0b5345;font-weight:700}
.s-proposed{color:#9a7d0a}.s-approved{color:#117a65;font-weight:600}.s-rejected{color:#a93226}.s-opted_in{color:#117a65;font-weight:600}.s-opted_out{color:#a93226;font-weight:600}.s-unknown{color:#9a7d0a}
.row{display:flex;gap:18px;flex-wrap:wrap;align-items:center}.muted{color:#6b7a8a;font-size:13px}footer{text-align:center;color:#8a97a6;font-size:12px;padding:20px}
.ok{color:#1e8449;font-weight:700}.bad{color:#c0392b;font-weight:700}
.hero{background:linear-gradient(120deg,#14325c,#2a7de1);color:#fff;border-radius:14px;padding:26px 30px;margin:4px 0 18px;box-shadow:0 4px 14px #14325c33}
.hero h2{margin:0 0 6px;font-size:24px;color:#fff}.hero p{margin:0;opacity:.92;max-width:760px}
.kpi .ic{font-size:22px;float:right;opacity:.9}.tile{transition:transform .15s,box-shadow .15s}.tile:hover{transform:translateY(-3px)}
.tile .ic{font-size:26px;display:block;margin-bottom:6px}
.chat{background:#e9e2d9;border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:10px;margin:10px 0}
.bubble{background:#dcf8c6;border-radius:10px 10px 10px 2px;padding:8px 12px;max-width:680px;box-shadow:0 1px 1px #0003;white-space:pre-wrap;font-size:14px;line-height:1.4}
.bubble .who{font-weight:700;font-size:12px;color:#075e54;margin-bottom:3px}.bubble .meta{font-size:11px;color:#667781;text-align:right;margin-top:5px}
.bubble.testmsg{background:#fff3cd}.bubble.failed{background:#fadbd8}
.bars .bar{display:flex;align-items:center;gap:10px;margin:7px 0;font-size:13px}.bars .lbl{width:190px;color:#33475b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bars .track{flex:1;background:#e8eff9;border-radius:7px;height:17px;overflow:hidden}.bars .fill{display:block;height:100%;background:linear-gradient(90deg,#2a7de1,#1f5a9e);border-radius:7px}
.bars .fill.green{background:linear-gradient(90deg,#34b36b,#1e8449)}.bars .fill.amber{background:linear-gradient(90deg,#f0b429,#c98a00)}.bars .fill.purple{background:linear-gradient(90deg,#9b6bd3,#6c3483)}
.bars .val{width:96px;text-align:right;font-weight:700;color:#14325c}.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:900px){.two{grid-template-columns:1fr}}
.who{font-size:13px;margin-left:12px}.top .right{display:flex;align-items:center;gap:10px}.top .right form{margin:0}.top button.grey{padding:5px 12px;font-size:12px}
.loginwrap{min-height:100vh;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#14325c,#2a7de1);padding:20px}
.logincard{background:#fff;border-radius:16px;padding:34px 38px;width:100%;max-width:400px;box-shadow:0 14px 40px #0005}
.logincard h1{margin:0 0 4px;font-size:28px}.logincard label{display:block;font-size:13px;color:#33475b;margin:14px 0 4px;font-weight:600}
.logincard input[type=text],.logincard input[type=password]{width:100%;padding:11px;font-size:15px}.logincard button{width:100%;margin-top:20px;padding:12px;font-size:16px}
.card svg text{font-family:'Segoe UI',Arial,sans-serif}button:disabled{opacity:.6;cursor:wait}
"""


class App:
    def __init__(self, db_path=None, today=None, provider_factory=None):
        self.db_path, self.today = db_path, today
        self.token = secrets.token_hex(16)
        self._flashes = {}
        self._cache = {}
        self.provider_factory = provider_factory or ns.get_provider
        c = self.conn()
        try:
            database.init_db(c)                      # creates any missing tables (for example the accounts tables) - never deletes
        finally:
            c.close()

    @property
    def flash(self):
        """The notice for the signed-in session; reading it consumes it (so it shows once, only to that person)."""
        return self._flashes.pop(getattr(_ctx, "sid", ""), "")

    @flash.setter
    def flash(self, value):
        self._flashes[getattr(_ctx, "sid", "")] = value

    def conn(self):
        return database.connect(self.db_path)

    def day(self):
        return self.today or date.today()

    def cached(self, key, fn, ttl=300):
        """Small in-memory cache for slow chart queries (the sales history does not change while the site runs)."""
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._cache[key] = (now, val)
        return val


# ------------------------------------------------------------------ small helpers
def ext_id(cid):                       # how a customer id is shown / linked
    return cid.replace("DEMO-", "C-", 1) if cid.startswith("DEMO-") else cid


def int_id(x):
    return "DEMO-" + x[2:] if x.startswith("C-") else x


def pick_cat(v, default=None, allow_all=False):
    ok = config.DEPARTMENTS + ([config.ALL_CATEGORIES] if allow_all else [])
    return v if v in ok else (default or config.DEPARTMENTS[0])


def cell(v):
    return v if isinstance(v, Raw) else E("" if v is None else str(v))


def table(rows, cols, status_col=None):
    if not rows:
        return "<p class='muted'>Nothing to show yet.</p>"
    head = "".join(f"<th>{E(c)}</th>" for c in cols)
    body = ""
    for r in rows:
        body += "<tr>" + "".join(
            f'<td class="s-{E(str(v))}">{cell(v)}</td>' if status_col == i else f"<td>{cell(v)}</td>" for i, v in enumerate(r)) + "</tr>"
    return f"<table><tr>{head}</tr>{body}</table>"


def cust_link(cid, label=None):
    return Raw(f'<a href="/customer?id={quote(ext_id(cid))}">{E(label or ext_id(cid))}</a>')


def cust_name(conn, cid):
    r = conn.execute("SELECT display_name FROM customers WHERE customer_id=?", (cid,)).fetchone()
    return (r[0] if r and r[0] else f"Customer {ext_id(cid)}")


def form(app, action, inner, method="post", sending=False):
    tok = f'<input type="hidden" name="token" value="{app.token}">' if method == "post" else ""
    js = (" onsubmit=\"var b=this.querySelector('button.big');b.disabled=true;b.textContent='Sending... please wait'\"" if sending else "")
    return f'<form method="{method}" action="{action}"{js}>{tok}{inner}</form>'


def cat_select(name, selected, goto=None, include_all=False):
    js = f' onchange="location=\'{goto}?c=\'+encodeURIComponent(this.value)"' if goto else ""
    opts = "".join(f'<option{" selected" if d == selected else ""}>{E(d)}</option>'
                   for d in ([config.ALL_CATEGORIES] if include_all else []) + config.DEPARTMENTS)
    return f'<select name="{name}"{js}>{opts}</select>'


def delivery_options():
    live_ok = config.WHATSAPP_ENABLED and is_admin()
    live = (f'<label><input type="radio" name="mode" value="live"{"" if live_ok else " disabled"}> Send to phones via WhatsApp'
            f'{"" if live_ok else (" <span class=muted>(admins only)</span>" if config.WHATSAPP_ENABLED else " <span class=muted>(not enabled - see Setup)</span>")}</label>')
    out = ('<div class="row"><label><input type="radio" name="mode" value="simulated" checked> Test mode '
           '<span class="muted">(nothing is sent to phones)</span></label>'
           '<label><input type="radio" name="mode" value="rehearsal"> Practice run <span class="muted">(test mode, repeatable)</span></label>'
           f'{live}</div>')
    if live_ok:
        out += ('<p><label><input type="checkbox" name="confirm" value="yes"> Yes - the recipients agreed, send real WhatsApp messages '
                'now (only used with "Send to phones")</label></p>')
    return out


def page(app, active, title, subtitle, body):
    flash, app.flash = app.flash, ""
    live = config.WHATSAPP_ENABLED
    badge = ('<span class="badge livebadge">WhatsApp sending ON</span>' if live
             else '<span class="badge test">Test mode - messages are not sent to phones</span>')
    shown = NAV if is_admin() else [x for x in NAV if x[0] not in ADMIN_GET]
    nav = "".join(f'<a href="{p}" class="{"on" if p == active else ""}">{ICON.get(p, "")} {E(n)}</a>' for p, n in shown)
    u = current_user()
    chip = (f"<span class='who'>\U0001F464 {E(u['display_name'])} <span class='badge test'>{E(u['role'])}</span></span>"
            f"<form method='post' action='/logout'><input type='hidden' name='token' value='{app.token}'><button class='grey'>Log out</button></form>") if u else ""
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{E(title)} - Incrementum</title><style>{CSS}</style></head><body>"
            f"<div class='top'><div class='brand'>Incrementum<small>Smart offers for local retail</small></div><div class='right'>{badge}{chip}</div></div>"
            f"<nav>{nav}</nav><div class='wrap'><h1>{E(title)}</h1><p class='sub'>{E(subtitle)}</p>{flash}{body}</div>"
            f"<footer>Incrementum &middot; {datetime.now():%d %b %Y}</footer></body></html>")


explain_error = wt.explain_error


def bubble(who, text, status):
    meta = {"simulated": "Test mode - not sent to a phone", "sent_to_provider": "Accepted by WhatsApp provider",
            "delivered": "Delivered", "failed": "Failed - not sent"}.get(status, status.replace("_", " "))
    cls = " testmsg" if status == "simulated" else (" failed" if status == "failed" else "")
    return f'<div class="bubble{cls}"><div class="who">{E(who)}</div>{E(text)}<div class="meta">{E(meta)}</div></div>'


def summary_html(s):
    label = ("Practice run (test mode)" if s["rehearsal"] else
             ("Sent via WhatsApp" if s["mode"] == "whatsapp" else "Test mode - nothing was sent to phones"))
    rows = [("Recipients with consent", s["eligible_customers"]), ("Recipients processed", s["processed"]),
            ("Messages created in test mode", s["simulated"]),
            ("Messages accepted by WhatsApp provider (not yet confirmed delivered)", s["accepted_by_provider"]),
            ("Confirmed deliveries", s["delivered"]), ("Skipped - no suitable active offer", s["skipped_no_offer"]),
            ("Suppressed - consent / duplicate / phone", s["suppressed"]), ("Failed", s["failed"])]
    out = f"<div class='card' id='result'><h2>Campaign result - {E(label)}</h2>" + table(rows, ["Measure", "Count"])
    if s["suppressed_reasons"]:
        out += "<ul>" + "".join(f"<li>{n} &times; {E(r)}</li>" for r, n in s["suppressed_reasons"].items()) + "</ul>"
    if s["messages"]:
        out += "<div class='chat'>" + "".join(bubble(m["to"], m["text"], m["status"]) for m in s["messages"]) + "</div>"
    if s["failed"]:
        out += "<div class='errbox'>Some messages failed - open the Notifications page for the reason.</div>"
    return out + "<p class='muted'>Delivery is only marked confirmed after the provider reports it (Notifications page).</p></div>"


# ================================================================== data helpers
SUCCESS = ("simulated", "sent_to_provider", "delivered")


def purchases_after(conn, customer_id=None):
    """For every notification that carried an offer: did the customer buy the offered product(s) afterwards?
    This is an OBSERVATION. It does not show that the notification caused the purchase."""
    q = ("SELECT log_id, customer_id, recipient_name, attempted_at, status, matched_offers FROM notification_logs "
         "WHERE status IN ('simulated','sent_to_provider','delivered') AND matched_offers != '[]'")
    args = []
    if customer_id:
        q += " AND customer_id=?"
        args.append(customer_id)
    out = []
    for l in conn.execute(q + " ORDER BY log_id DESC", args).fetchall():
        codes = json.loads(l["matched_offers"] or "[]")
        if not codes:
            continue
        ph = ",".join("?" * len(codes))
        offered = conn.execute(f"SELECT o.stock_code, p.description FROM offers o JOIN products p ON p.stock_code=o.stock_code "
                               f"WHERE o.offer_code IN ({ph})", codes).fetchall()
        stocks = [r[0] for r in offered]
        buys = conn.execute(
            f"SELECT t.invoice_date, p.description, i.quantity, i.line_total FROM transactions t "
            f"JOIN transaction_items i ON i.invoice=t.invoice JOIN products p ON p.stock_code=i.stock_code "
            f"WHERE t.customer_id=? AND t.invoice_date>=? AND i.stock_code IN ({','.join('?' * len(stocks))}) "
            f"ORDER BY t.invoice_date", [l["customer_id"], l["attempted_at"].replace("T", " ")] + stocks).fetchall() if stocks else []
        out.append({"customer_id": l["customer_id"], "name": l["recipient_name"], "at": l["attempted_at"], "status": l["status"],
                    "offered": [r[1] for r in offered], "buys": buys})
    return out


def experiment_summary(conn):
    exp = conn.execute("SELECT * FROM campaign_experiments ORDER BY experiment_id DESC LIMIT 1").fetchone()
    if not exp:
        return None
    g = {r[0]: (r[1], r[2] or 0) for r in conn.execute(
        "SELECT a.group_name, COUNT(*), SUM(o.purchased) FROM experiment_assignments a JOIN experiment_outcomes o "
        "ON o.experiment_id=a.experiment_id AND o.customer_id=a.customer_id WHERE a.experiment_id=? GROUP BY a.group_name",
        (exp["experiment_id"],))}
    stats = None
    if g.get("treatment", (0, 0))[0] and g.get("control", (0, 0))[0]:
        stats = cb.conversion_analysis(g["treatment"][0], g["treatment"][1], g["control"][0], g["control"][1])
    segs = conn.execute("SELECT segment_label, evidence_level, COUNT(*) FROM cause_based_segments WHERE experiment_id=? "
                        "GROUP BY 1,2 ORDER BY 1", (exp["experiment_id"],)).fetchall()
    return {"exp": exp, "stats": stats, "segs": segs}



# ================================================================== pages (GET)
def sales_charts(app):
    """Real sales history charts (cached; the history does not change while the site runs)."""
    def load():
        c = app.conn()
        try:
            monthly = c.execute("SELECT substr(t.invoice_date,1,7), ROUND(SUM(i.line_total),0) FROM transactions t JOIN transaction_items i "
                                "ON i.invoice=t.invoice WHERE t.is_synthetic=0 GROUP BY 1 ORDER BY 1").fetchall()
            cats = c.execute("SELECT p.department, ROUND(SUM(i.line_total),0) FROM transaction_items i JOIN products p ON p.stock_code=i.stock_code "
                             "WHERE i.is_synthetic=0 GROUP BY 1 ORDER BY 2 DESC").fetchall()
            return [tuple(r) for r in monthly], [tuple(r) for r in cats]
        finally:
            c.close()
    return app.cached("sales", load)


def pg_home(app, conn, q):
    n = lambda sql: conn.execute(sql).fetchone()[0]
    today = app.day()
    sent = n("SELECT COUNT(*) FROM notification_logs WHERE status IN ('simulated','sent_to_provider','delivered')")
    kpis = [("\U0001F465", f"{n('SELECT COUNT(*) FROM customers'):,}", "Customers"), ("\U0001F4E6", f"{n('SELECT COUNT(*) FROM products'):,}", "Products"),
            ("\U0001F3F7\uFE0F", len(offers_mod.active_offers(conn, today)), "Active offers today"),
            ("\U0001F9E9", f"{n('SELECT COUNT(DISTINCT customer_id) FROM customer_segment_memberships'):,}", "Customers in segments"),
            ("\U0001F4E8", sent, "Messages created")]
    grid = "".join(f"<div class='kpi'><span class='ic'>{i}</span><b>{v}</b><span>{E(k)}</span></div>" for i, v, k in kpis)
    tiles = [("/send", "Send Offers", "Send every active offer to the customers who regularly buy that kind of product - in one click."),
             ("/generate", "Generate Offers", "Let the system analyse buying behaviour, propose offers, and send only the ones you approve."),
             ("/analysis", "Customer Analysis", "See which products each customer buys most often in a category."),
             ("/segments", "Segments", "Customer groups by preferred products, value and reorder timing - with the reason for each."),
             ("/results", "Campaign Results", "Who bought after a notification, and which customers are Persuadable, Sure Things or Lost Causes."),
             ("/notifications", "Notifications", "Every message attempt with its status, reason and delivery confirmation.")]
    tl = "".join(f"<a class='tile' href='{h}'><span class='ic'>{ICON[h]}</span><b>{E(t)}</b>{E(d)}</a>" for h, t, d in tiles)
    monthly, cats = sales_charts(app)
    trend = line_chart([(m, v) for m, v in monthly], prefix="\u00A3")
    catbars = hbars([(c, v) for c, v in cats], fmt="\u00A3{:,.0f}")
    status = conn.execute("SELECT status, COUNT(*) FROM notification_logs GROUP BY status ORDER BY 2 DESC").fetchall()
    cls = {"simulated": "purple", "skipped": "amber", "suppressed": "amber", "failed": "", "sent_to_provider": "green", "delivered": "green"}
    sbars = hbars([(st.replace("_", " "), c, cls.get(st, "")) for st, c in status]) if status else "<p class='muted'>No messages yet.</p>"
    logs = conn.execute("SELECT attempted_at, recipient_name, department, status FROM notification_logs ORDER BY log_id DESC LIMIT 6").fetchall()
    body = ("<div class='hero'><h2>Offers that reach the right customer</h2><p>Incrementum studies what each customer buys, matches it to the offers "
            "that are running, and sends a personal message - then shows who actually responded.</p></div>"
            f"<div class='grid'>{grid}</div>"
            f"<div class='two'><div class='card'><h2>Sales over time</h2>{trend}</div><div class='card'><h2>Sales by category</h2>{catbars}</div></div>"
            f"<h2 style='margin:22px 0 8px;color:#14325c'>What would you like to do?</h2><div class='tiles'>{tl}</div>"
            f"<div class='two'><div class='card'><h2>Message status</h2>{sbars}</div><div class='card'><h2>Latest activity</h2>"
            + table([(r[0].replace("T", " "), r[1], r[2], r[3].replace("_", " ")) for r in logs], ["Time", "Recipient", "Category", "Status"]) + "</div></div>")
    return page(app, "/", "Home", "Personalised offers, sent to the right customers at the right time.", body)


def pg_send(app, conn, q):
    today = app.day()
    cat = pick_cat(q.get("c", [""])[0], default=config.ALL_CATEGORIES, allow_all=True)
    everything = cat == config.ALL_CATEGORIES
    active_cat = [o for o in offers_mod.active_offers(conn, today) if everything or o["department"] == cat]
    recips = ce.get_recipients(conn)
    ok = sum(1 for r in recips if r["consent_status"] == "opted_in")
    prev = []
    for r in recips:
        matches, notes = (ce.match_all(conn, r["customer_id"], active_cat, today) if everything
                          else ce.match_customer(conn, r["customer_id"], cat, active_cat, today))
        what = ("; ".join(f"{ce.pretty(m['product_name'])} - {offers_mod.offer_text(m['offer'])}" for m in matches) if matches
                else "Skipped: " + ("; ".join(notes) or "no suitable offer"))
        prev.append((cust_link(r["customer_id"], r["name"]), r["consent_status"].replace("_", " "), what))
    kp = [(len(active_cat), "Active offers" + ("" if everything else " in this category")), (f"{ok} of {len(recips)}", "Recipients with consent"),
          ("WhatsApp" if config.WHATSAPP_ENABLED else "Test mode", "Delivery")]
    grid = "".join(f"<div class='kpi'><b>{v}</b><span>{E(k)}</span></div>" for v, k in kp)
    body = (f"<div class='card'><h2>1. Choose a category</h2><p>{cat_select('c', cat, '/send', include_all=True)}</p>"
            + ("<p class='muted'>Every category is checked. Each customer gets one message listing the active offers for the products they buy regularly.</p></div>"
               if everything else "<p class='muted'>Only offers in this category are sent. Each customer receives offers only for products they buy regularly.</p></div>") +
            f"<div class='grid'>{grid}</div>"
            "<div class='card'><h2>2. Preview - who will receive what</h2>" + table(prev, ["Customer", "Consent", "Message content"], 1) + "</div>"
            "<div class='card'><h2>3. Send</h2>" + form(app, "/send",
            f'<input type="hidden" name="category" value="{E(cat)}">' + delivery_options() +
            '<p><button class="big">Send Offers</button></p>', sending=True) +
            "<p class='muted'>One click processes every recipient. Sending twice does not message anyone twice.</p></div>")
    return page(app, "/send", "Send Offers", "Send all currently active offers to the matching customers in one click.", body)


def pg_offers(app, conn, q):
    rows = conn.execute("SELECT o.offer_id, o.offer_code, o.origin, o.status, o.department, p.description, o.discount_percent, "
                        "o.promo_text, o.valid_from, o.valid_to FROM offers o JOIN products p ON p.stock_code=o.stock_code "
                        "ORDER BY o.origin, o.offer_code").fetchall()
    today = str(app.day())
    out = []
    for r in rows:
        live = r["status"] == "active" and r["valid_from"] and r["valid_from"] <= today <= r["valid_to"]
        state = "Active now" if live else ("Inactive" if r["status"] == "inactive" else r["status"].title() if r["status"] != "active" else "Outside validity dates")
        toggle = ""
        if r["origin"] == "configured":
            lab = "Deactivate" if r["status"] == "active" else "Activate"
            toggle = Raw(form(app, "/offers/toggle", f'<input type="hidden" name="id" value="{r["offer_id"]}"><button class="grey">{lab}</button>'))
        benefit = f"{r['discount_percent']:g}% off" if r["discount_percent"] is not None else (r["promo_text"] or "")
        out.append((r["offer_code"], r["department"], r["description"], benefit, f"{r['valid_from'] or '-'} to {r['valid_to'] or '-'}",
                    "Standard" if r["origin"] == "configured" else "Generated", state, toggle))
    prods = conn.execute("SELECT stock_code, description FROM products WHERE department IN (%s) ORDER BY department, description"
                         % ",".join("?" * len(config.DEPARTMENTS)), config.DEPARTMENTS).fetchall()
    popts = "".join(f"<option value='{E(p[0])}'>{E(p[1])}</option>" for p in prods)
    add = form(app, "/offers/add",
               f"<div class='row'><label>Product <select name='stock_code'>{popts}</select></label>"
               "<label>Discount % <input type='number' name='discount' min='0' max='100' step='0.5' style='width:90px'></label>"
               "<label>or promotion text <input type='text' name='promo' size='32' placeholder='e.g. Special price this week'></label></div>"
               f"<div class='row' style='margin-top:10px'><label>Valid from <input type='date' name='valid_from' value='{today}'></label>"
               f"<label>Valid to <input type='date' name='valid_to' value='{today}'></label><button>Add offer</button></div>")
    body = ("<div class='card'><h2>All offers</h2>" + table(out, ["Code", "Category", "Product", "Offer", "Valid", "Type", "State", ""]) + "</div>"
            "<div class='card'><h2>Add an offer</h2><p class='muted'>Give a discount percentage or a promotion text. Nothing is sent from here; "
            "offers are used when you click Send Offers.</p>" + add + "</div>")
    return page(app, "/offers", "Offers", "The promotions that are available to send.", body)


def pg_recipients(app, conn, q):
    rows = []
    for r in ce.get_recipients(conn):
        has = bool(ns.get_phone(r["phone_env_key"]))
        act = Raw(form(app, "/recipients/consent",
                       f'<input type="hidden" name="name" value="{E(r["name"])}">'
                       '<button class="grey" name="action" value="grant">Consented</button> '
                       '<button class="red" name="action" value="revoke">Opt out</button>'))
        rows.append((cust_link(r["customer_id"], r["name"]), r["consent_status"].replace("_", " "),
                     r["consent_recorded_at"] or "-", Raw("<span class='ok'>set</span>" if has else "<span class='bad'>missing</span>"), act))
    body = ("<div class='card'><h2>Recipients</h2>" + table(rows, ["Customer", "Consent", "Recorded", "Phone number", "Update"], 1) +
            "<p class='muted'>Phone numbers are kept in the private .env file and are never shown or stored in the database. "
            "Only recipients who have agreed receive messages.</p></div>"
            "<div class='card'><h2>Record consent for everyone</h2>" + form(app, "/recipients/consent",
            "<p><label><input type='checkbox' name='yes' value='1'> I confirm every listed person agreed to receive messages</label></p>"
            "<button name='action' value='grant_all'>Record consent for all</button>") + "</div>")
    return page(app, "/recipients", "Recipients", "Who can receive notifications.", body)


def pg_notifications(app, conn, q):
    st = q.get("status", [""])[0]
    counts = conn.execute("SELECT status, COUNT(*) FROM notification_logs GROUP BY status ORDER BY 1").fetchall()
    chips = "".join(f"<a class='badge test' style='margin:2px' href='/notifications?status={E(s)}'>{E(s.replace('_', ' '))}: {n}</a>" for s, n in counts)
    sql = ("SELECT attempted_at, recipient_name, department, status, delivery_mode, message_text, "
           "COALESCE(skip_or_suppression_reason, failure_reason, product_or_category) FROM notification_logs")
    args = []
    if st:
        sql += " WHERE status=?"
        args.append(st)
    rows = []
    for r in conn.execute(sql + " ORDER BY log_id DESC LIMIT 150", args).fetchall():
        msg = Raw(f"<details><summary>view</summary><div class='chat'>{bubble(r[1], r[5], r[3])}</div></details>") if r[5] else ""
        detail = (r[6] or "") + ((" - " + explain_error(r[6])) if r[3] == "failed" and explain_error(r[6]) else "")
        rows.append((r[0].replace("T", " "), r[1], r[2], r[3], "WhatsApp" if r[4] == "whatsapp" else "test mode", msg, detail))
    refresh = form(app, "/notifications/refresh", "<button class='grey'>Check delivery status</button>")
    body = (f"<div class='card'><h2>Summary</h2><p>{chips or 'No notifications yet.'} <a href='/notifications'>show all</a></p>{refresh}"
            "<p class='muted'>'Sent to provider' means WhatsApp accepted the message. It becomes 'delivered' only when the provider confirms it.</p></div>"
            "<div class='card'><h2>Message log</h2>" + table(rows, ["Time", "Recipient", "Category", "Status", "Sent via", "Message", "Details"], 3) + "</div>")
    return page(app, "/notifications", "Notifications", "Every message attempt and what happened to it.", body)


def pg_analysis(app, conn, q):
    cat = pick_cat(q.get("c", [""])[0])
    analysis, groups = og.analyze_category(conn, cat, app.day())
    rows = [(cust_link(x["customer_id"], x["name"]), x["top"]["description"] if x["top"] else "-", x["top"]["purchases"] if x["top"] else "-",
             ", ".join(f"{p['description']} x{p['purchases']}" for p in x["preferences"]) or "none") for x in analysis]
    grp = "".join(f"<li><b>{E(k)}</b>: {E(', '.join(v))}</li>" for k, v in groups.items())
    body = (f"<div class='card'><h2>Category</h2><p>{cat_select('c', cat, '/analysis')}</p></div>"
            "<div class='card'><h2>Purchase behaviour in this category</h2>" +
            table(rows, ["Customer", "Most frequent product", "Purchases", "All products bought (last 90 days)"]) +
            f"<p class='muted'>A product counts as a regular purchase after {config.MIN_PURCHASES} or more separate shopping trips in the last {config.LOOKBACK_DAYS} days.</p></div>"
            f"<div class='card'><h2>Customer groups</h2><ul>{grp}</ul>"
            f"<p><a class='btn' href='/generate?c={quote(cat)}'>Generate offers for this category</a></p></div>")
    return page(app, "/analysis", "Customer Analysis", "What each customer buys most often.", body)


def pg_generate(app, conn, q):
    cat = pick_cat(q.get("c", [""])[0])
    props = og.list_proposals(conn, cat)
    rows = ""
    for r in props:
        rows += (f'<tr><td><input type="checkbox" name="id" value="{r["proposal_id"]}"></td><td>{r["proposal_id"]}</td>'
                 f'<td>{E(r["name"])}</td><td>{E(r["product"])}</td><td>{r["discount_percent"]:g}%</td>'
                 f'<td class="s-{E(r["status"])}">{E(r["status"])}</td><td>{"sent" if r["notified_at"] else ""}</td>'
                 f'<td>{E(r["reason"].replace(" (from config/offer_rules.json)", " (configured discount rules)"))}</td></tr>')
    plist = (form(app, "/generate/decide", f'<input type="hidden" name="category" value="{E(cat)}">'
                 "<table><tr><th></th><th>#</th><th>Customer</th><th>Product</th><th>Discount</th><th>Status</th><th>Sent</th><th>Why proposed</th></tr>"
                 f"{rows}</table><button name='action' value='approve'>Approve selected</button> "
                 "<button class='red' name='action' value='reject'>Reject selected</button>") if props else "<p class='muted'>No proposals yet - generate them above.</p>")
    body = (f"<div class='card'><h2>1. Category</h2><p>{cat_select('c', cat, '/generate')}</p></div>"
            "<div class='card'><h2>2. Generate proposals</h2><p>The system looks at each customer's regular purchases in this category and proposes an offer "
            "using the configured discount rules. Proposals are <b>not active</b> and <b>nothing is sent</b> until you approve them.</p>" +
            form(app, "/generate/propose", f'<input type="hidden" name="category" value="{E(cat)}"><button>Generate offer proposals</button>') + "</div>"
            "<div class='card'><h2>3. Review and approve</h2>" + plist + "</div>"
            "<div class='card'><h2>4. Send the approved offers</h2>" + form(app, "/generate/send",
            f'<input type="hidden" name="category" value="{E(cat)}">' + delivery_options() + '<p><button class="big">Send approved offers</button></p>', sending=True) +
            "<p class='muted'>Only approved offers are sent. This never changes the standard offers on the Send Offers page.</p></div>")
    return page(app, "/generate", "Generate Offers", "Propose offers from buying behaviour, review them, then send.", body)


TYPE_LABEL = {"category": "Category preference", "product": "Frequent buyers of a product", "rfm_rule": "Customer value (RFM)",
              "rfm_cluster": "Behaviour clusters (k-means)", "reorder_due": "Due to reorder"}


def pg_segments(app, conn, q):
    sid = q.get("seg", [""])[0]
    rows = conn.execute("SELECT s.segment_id, s.segment_name, s.segment_type, s.rule_description, COUNT(m.customer_id) FROM "
                        "product_preference_segments s LEFT JOIN customer_segment_memberships m USING(segment_id) "
                        "GROUP BY s.segment_id ORDER BY s.segment_type, 5 DESC").fetchall()
    top = sorted(((r[1], r[4]) for r in rows), key=lambda x: -x[1])[:8]
    body = ("<div class='note'>One customer can belong to several groups. Every membership has a stated reason.</div>"
            + ("<div class='card'><h2>Largest segments</h2>" + hbars(top) + "</div>" if top else ""))
    cur = None
    for stype in TYPE_LABEL:
        seg = [(Raw(f'<a href="/segments?seg={r[0]}#members">{E(r[1])}</a>'), r[4]) for r in rows if r[2] == stype]
        if seg:
            rule = next(r[3] for r in rows if r[2] == stype)
            body += f"<div class='card'><h2>{TYPE_LABEL[stype]}</h2><p class='muted'>{E(rule)}</p>" + table(seg, ["Segment", "Customers"]) + "</div>"
    if sid.isdigit():
        s = conn.execute("SELECT segment_name, rule_description FROM product_preference_segments WHERE segment_id=?", (int(sid),)).fetchone()
        if s:
            n = q.get("n", ["25"])[0]
            n = min(max(int(n), 25), 500) if n.isdigit() else 25
            total = conn.execute("SELECT COUNT(*) FROM customer_segment_memberships WHERE segment_id=?", (int(sid),)).fetchone()[0]
            mem = conn.execute("SELECT m.customer_id, c.display_name, m.reason FROM customer_segment_memberships m JOIN customers c "
                               "ON c.customer_id=m.customer_id WHERE m.segment_id=? ORDER BY m.strength DESC, m.customer_id LIMIT ?",
                               (int(sid), n)).fetchall()
            more = f" <a href='/segments?seg={int(sid)}&n={min(n * 4, 500)}#members'>show more</a>" if total > n else ""
            members = (f"<div class='card' id='members'><h2>{E(s[0])}</h2><p class='muted'>{total:,} customers in this segment; showing {len(mem)}.{more} "
                       f"<a href='/segments'>close</a></p>" + table([(cust_link(r[0], r[1]), r[2]) for r in mem],
                                                                    ["Customer", "Why this customer is in the segment"]) + "</div>")
            body = members + body
    return page(app, "/segments", "Segments", "Customer groups based on what and how they buy.", body)


def pg_customer(app, conn, q):
    cid = int_id(q.get("id", [""])[0])
    f = conn.execute("SELECT * FROM customer_features WHERE customer_id=?", (cid,)).fetchone()
    c = conn.execute("SELECT * FROM customers WHERE customer_id=?", (cid,)).fetchone()
    if not c:
        return page(app, "/segments", "Customer not found", "", "<div class='card'>No customer with that id.</div>")
    name = c["display_name"] or f"Customer {ext_id(cid)}"
    feats = ([(k.replace("_", " ").capitalize(), v) for k, v in dict(f).items() if k not in ("customer_id", "computed_at")] if f else [])
    segs = conn.execute("SELECT s.segment_name, s.segment_type, m.reason FROM customer_segment_memberships m JOIN product_preference_segments s "
                        "USING(segment_id) WHERE m.customer_id=? ORDER BY s.segment_type, s.segment_name", (cid,)).fetchall()
    pa = purchases_after(conn, cid)
    resp = [(r["at"].replace("T", " "), r["status"].replace("_", " "), ", ".join(r["offered"]),
             "; ".join(f"{b[1]} x{b[2]} ({b[0][:16]})" for b in r["buys"]) or "Not yet") for r in pa]
    body = ("<div class='card'><h2>Profile</h2>" + (table(feats, ["Measure", "Value"]) if feats else "<p class='muted'>Features not computed yet.</p>") + "</div>"
            "<div class='card'><h2>Segments and why</h2>" + table([(TYPE_LABEL.get(s[1], s[1]), s[0], s[2]) for s in segs], ["Type", "Segment", "Reason"]) + "</div>"
            "<div class='card'><h2>Notifications and purchases afterwards</h2>" + table(resp, ["Notified", "Status", "Offered", "Bought afterwards"]) + "</div>")
    return page(app, "/segments", name, f"Customer {ext_id(cid)}", body)


def pg_results(app, conn, q):
    pa = purchases_after(conn)
    n_notified, n_bought = len(pa), sum(1 for r in pa if r["buys"])
    rate = f"{100 * n_bought / n_notified:.0f}%" if n_notified else "-"
    kp = [(n_notified, "Notifications with an offer"), (n_bought, "Followed by a purchase of the offered product"), (rate, "Share that purchased afterwards")]
    grid = "".join(f"<div class='kpi'><b>{v}</b><span>{E(k)}</span></div>" for v, k in kp)
    rows = [(cust_link(r["customer_id"], r["name"]), r["at"].replace("T", " "), r["status"].replace("_", " "), ", ".join(r["offered"]),
             Raw("<span class='ok'>Yes</span>" if r["buys"] else "<span class='muted'>Not yet</span>"),
             "; ".join(f"{b[1]} x{b[2]} at {b[0][:16]}" for b in r["buys"])) for r in pa]
    custs = conn.execute("SELECT r.name, r.customer_id FROM demo_recipients r ORDER BY r.name").fetchall()
    prods = conn.execute("SELECT stock_code, description FROM products WHERE department IN (%s) ORDER BY description" % ",".join("?" * len(config.DEPARTMENTS)),
                         config.DEPARTMENTS).fetchall()
    rec = form(app, "/results/record-purchase",
               "<div class='row'><label>Customer <select name='customer_id'>" + "".join(f"<option value='{E(ext_id(c[1]))}'>{E(c[0])}</option>" for c in custs) +
               "</select></label><label>Product <select name='stock_code'>" + "".join(f"<option value='{E(p[0])}'>{E(p[1])}</option>" for p in prods) +
               "</select></label><label>Quantity <input type='number' name='qty' value='1' min='1' max='50' style='width:70px'></label><button>Record purchase</button></div>")
    body = ("<div class='card' id='response'><h2>Customers who bought after receiving a notification</h2>"
            f"<div class='grid'>{grid}</div>" + table(rows, ["Customer", "Notified", "Status", "Offered", "Bought afterwards?", "Purchase"]) +
            "<div class='note'>This is an <b>observation</b>: the customer bought after the message. It does not prove the message caused the purchase - "
            "the customer may have bought anyway. The experiment below is how that question is tested.</div>"
            "<h3>Record a purchase at the counter</h3>" + rec + "</div>")
    ex = experiment_summary(conn)
    if ex is None:
        body += "<div class='card'><h2>Campaign experiment</h2><p class='muted'>No experiment has been run yet.</p></div>"
    else:
        e, st = ex["exp"], ex["stats"]
        body += (f"<div class='card'><h2>Campaign experiment <span class='badge simtag'>Simulated</span></h2>"
                 "<div class='warnbox'>Customers were split at random into a notified group and a comparison group. The comparison group's results are real "
                 "purchase history. The notified group's response to the message is <b>simulated</b>, so these figures illustrate the method; they are not a "
                 "measured campaign result.</div>" f"<p class='muted'>{E(e['hypothesis'] or '')}</p>")
        if st:
            ek = [(f"{st['n_treatment']:,}", "Notified group"), (f"{st['n_control']:,}", "Comparison group"), (f"{st['conv_treatment']:.1%}", "Bought - notified"),
                  (f"{st['conv_control']:.1%}", "Bought - comparison"), (f"{st['difference']:+.1%}", "Difference"),
                  (f"{st['ci_low']:+.1%} to {st['ci_high']:+.1%}", "95% interval"), ("<0.0001" if st["p_value"] < 0.0001 else f"{st['p_value']:.4f}", "p-value")]
            body += "<div class='grid'>" + "".join(f"<div class='kpi'><b style='font-size:22px'>{v}</b><span>{E(k)}</span></div>" for v, k in ek) + "</div>"
            body += ("<h3 style='margin-bottom:0'>Share who bought in the next " + str(e["observation_window_days"]) + " days</h3>" +
                     hbars([("Notified group", 100 * st["conv_treatment"], "green"), ("Comparison group", 100 * st["conv_control"], "amber")],
                           fmt="{:.1f}%", maxv=100))
        body += "</div>"
        defs = {"Persuadable": "likely to buy because of the offer", "Sure Thing": "likely to buy anyway - no discount needed",
                "Lost Cause": "unlikely to buy either way", "Do-not-disturb": "opted out; no promotional messages"}
        seg_rows = [(Raw(f'<a href="/results?group={quote(sg[0])}#groups">{E(sg[0])}</a>'), defs.get(sg[0], ""), sg[1], sg[2]) for sg in ex["segs"]]
        tot = {}
        for sg in ex["segs"]:
            tot[sg[0]] = tot.get(sg[0], 0) + sg[2]
        gcls = {"Persuadable": "green", "Sure Thing": "", "Lost Cause": "amber", "Do-not-disturb": "purple"}
        bars = hbars([(k, v, gcls.get(k, "")) for k, v in tot.items()])
        body += ("<div class='card' id='groups'><h2>Customer response groups <span class='badge simtag'>Simulated</span></h2>" + bars +
                 table(seg_rows, ["Group (click to see customers)", "Meaning", "Evidence", "Customers"]) +
                 "<div class='note'>Evidence 'insufficient' means the model that separates the groups did not pass its validation check on this sample, so the labels are "
                 "provisional - read them as an illustration of the method, not as reliable per-customer facts.</div></div>")
        grp = q.get("group", [""])[0]
        if grp in defs:
            n = q.get("n", ["25"])[0]
            n = min(max(int(n), 25), 500) if n.isdigit() else 25
            total = conn.execute("SELECT COUNT(*) FROM cause_based_segments WHERE experiment_id=? AND segment_label=?", (e["experiment_id"], grp)).fetchone()[0]
            mem = conn.execute("SELECT s.customer_id, c.display_name, s.evidence_level, s.basis FROM cause_based_segments s JOIN customers c "
                               "ON c.customer_id=s.customer_id WHERE s.experiment_id=? AND s.segment_label=? ORDER BY s.customer_id LIMIT ?",
                               (e["experiment_id"], grp, n)).fetchall()
            more = f" <a href='/results?group={quote(grp)}&n={min(n * 4, 500)}#members'>show more</a>" if total > n else ""
            body += (f"<div class='card' id='members'><h2>{E(grp)} - {E(defs[grp])} <span class='badge simtag'>Simulated</span></h2>"
                     f"<p class='muted'>{total:,} customers; showing {len(mem)}.{more} <a href='/results#groups'>close</a></p>" +
                     table([(cust_link(m[0], m[1]), m[2], m[3]) for m in mem], ["Customer", "Evidence", "Basis"]) + "</div>")
        qp = config.PROCESSED_DIR / "uplift_quintiles.csv"
        if qp.exists():
            u = pd.read_csv(qp)
            body += ("<div class='card'><h2>Does the model rank customers correctly?</h2>" + table(
                [(int(r.predicted_uplift_quintile), r.n_treatment, r.n_control, f"{r.conv_treatment:.1%}", f"{r.conv_control:.1%}", f"{r.observed_uplift:+.1%}",
                  f"{r.mean_predicted_uplift:+.1%}") for r in u.itertuples()],
                ["Predicted group (1 = lowest)", "Notified", "Comparison", "Bought - notified", "Bought - comparison", "Observed uplift", "Predicted uplift"]) + "</div>")
    opt = conn.execute("SELECT name FROM demo_recipients WHERE consent_status='opted_out' ORDER BY name").fetchall()
    body += ("<div class='card'><h2>Recipients who opted out of messages</h2>" + (table([(o[0],) for o in opt], ["Opted out - receive no promotional messages"])
             if opt else "<p class='muted'>None of the current recipients has opted out. (The Do-not-disturb figure in the experiment above is a simulated opt-out rate.)</p>") + "</div>")
    return page(app, "/results", "Campaign Results", "Who responded, and what the evidence says.", body)


def pg_setup(app, conn, q):
    items = wt.readiness(conn)
    rows = [(i, Raw("<span class='ok'>Ready</span>" if ok else "<span class='bad'>Missing</span>")) for i, ok in items]
    names = "".join(f"<option>{E(r['name'])}</option>" for r in ce.get_recipients(conn))
    check = form(app, "/setup/check-login", "<button class='grey'>Check Twilio login</button>")
    test = form(app, "/setup/test-message", f"<div class='row'><label>Send to <select name='name'>{names}</select></label>"
                "<label><input type='checkbox' name='confirm' value='yes'> Yes, send a real WhatsApp test message now</label><button>Send test message</button></div>")
    body = ("<div class='card'><h2>WhatsApp checklist</h2>" + table(rows, ["Requirement", "Status"]) +
            f"<p class='muted'>Settings are read from the private .env file when the server starts, so restart the server after editing it.</p></div>"
            "<div class='warnbox'><b>Before every presentation:</b> " + E(wt.REMINDER) + " Free-form messages (like offers) are only delivered inside that 24-hour window.</div>"
            "<div class='card'><h2>Check the Twilio login</h2><p class='muted'>Confirms the Account SID and Auth Token work. Sends nothing.</p>" + check + "</div>"
            "<div class='card'><h2>Send a test message</h2><p class='muted'>Use this before presenting to confirm a phone receives messages. "
            "It is not part of any campaign.</p>" + test + "</div>")
    return page(app, "/setup", "Setup", "Connect WhatsApp so messages reach phones.", body)


def safe_next(value):
    """Only allow redirects to pages on this site."""
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value and "\n" not in value else "/"


def login_page(app, error="", next_url="/", empty=False, username=""):
    msg = (f"<div class='errbox'>{E(error)}</div>" if error else "")
    if empty:
        msg += ("<div class='warnbox'>No accounts exist yet. Create the first admin account in the terminal:<br>"
                "<code>python -m src.main create-user --username YOURNAME --role admin</code></div>")
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Sign in - Incrementum</title><style>{CSS}</style></head><body><div class='loginwrap'><div class='logincard'>"
            "<div class='brand' style='color:#14325c'>Incrementum<small style='color:#6b7a8a'>Smart offers for local retail</small></div>"
            "<h1 style='margin-top:18px'>Sign in</h1><p class='sub'>Use your Incrementum account.</p>" + msg +
            f"<form method='post' action='/login'><input type='hidden' name='token' value='{app.token}'>"
            f"<input type='hidden' name='next' value='{E(next_url)}'>"
            f"<label for='u'>Username</label><input id='u' type='text' name='username' autocomplete='username' autofocus required maxlength='64' value='{E(username[:64])}'>"
            "<label for='p'>Password</label><input id='p' type='password' name='password' autocomplete='current-password' required maxlength='200'>"
            "<label style='font-weight:400'><input type='checkbox' onclick=\"document.getElementById('p').type=this.checked?'text':'password'\"> Show password</label>"
            "<button>Sign in</button></form></div></div></body></html>")


def pg_users(app, conn, q):
    me = current_user()
    rows = []
    for u in auth.list_users(conn):
        own = u["user_id"] == me["user_id"]
        act = "disable" if u["is_active"] else "enable"
        tog = "" if own else form(app, "/users/toggle", f"<input type='hidden' name='id' value='{u['user_id']}'><button class='grey' name='action' value='{act}'>{act.title()}</button>")
        role = "" if own else form(app, "/users/role", f"<input type='hidden' name='id' value='{u['user_id']}'><select name='role'>"
                                   + "".join(f"<option{' selected' if r == u['role'] else ''}>{r}</option>" for r in auth.ROLES) + "</select> <button class='grey'>Set role</button>")
        pw = form(app, "/users/password", f"<input type='hidden' name='id' value='{u['user_id']}'><input type='password' name='password' placeholder='new password' size='14' autocomplete='new-password'> <button class='grey'>Set password</button>")
        rows.append((u["username"] + (" (you)" if own else ""), u["display_name"], u["role"], "active" if u["is_active"] else "disabled",
                     (u["last_login_at"] or "never").replace("T", " "), Raw(f"<div class='row' style='gap:8px'>{tog}{role}{pw}</div>")))
    ev = conn.execute("SELECT at, username, event, detail FROM auth_events ORDER BY event_id DESC LIMIT 15").fetchall()
    roles_note = ("<p class='muted'><b>admin</b>: everything, including accounts, offers, recipients, WhatsApp setup and sending to phones. "
                  "<b>staff</b>: run campaigns in test mode, generate and approve offers, view analysis, segments, results and notifications.</p>")
    add = form(app, "/users/create", "<div class='row'><label>Username <input type='text' name='username' maxlength='32' required></label>"
               "<label>Name <input type='text' name='display_name' maxlength='60'></label>"
               f"<label>Role <select name='role'>{''.join(f'<option>{r}</option>' for r in reversed(auth.ROLES))}</select></label>"
               f"<label>Password <input type='password' name='password' autocomplete='new-password' required> <span class='muted'>at least {auth.MIN_PASSWORD} characters</span></label>"
               "<button>Create account</button></div>")
    body = ("<div class='card'><h2>Accounts</h2>" + roles_note + table(rows, ["Username", "Name", "Role", "Status", "Last sign-in", "Manage"], 3) + "</div>"
            "<div class='card'><h2>Add an account</h2>" + add + "</div>"
            "<div class='card'><h2>Recent sign-in activity</h2>" + table([(e[0].replace("T", " "), e[1] or "", e[2].replace("_", " "), (e[3] or "").replace("_", " ")) for e in ev],
                                                                          ["Time", "Username", "Event", "Detail"]) + "</div>")
    return page(app, "/users", "Users", "Who can sign in, and what they may do.", body)


def act_user_create(app, form):
    f = lambda k: form.get(k, [""])[0]
    conn = app.conn()
    try:
        auth.create_user(conn, f("username"), f("password"), f("role") or "staff", f("display_name") or None)
        return "/users", note(f"Account <b>{E(f('username').strip())}</b> created.")
    except ValueError as e:
        return "/users", err(str(e))
    finally:
        conn.close()


def _uid(form):
    v = form.get("id", [""])[0]
    return int(v) if v.isdigit() else -1


def act_user_toggle(app, form):
    uid, action = _uid(form), form.get("action", [""])[0]
    if uid == current_user()["user_id"]:
        return "/users", err("You cannot disable your own account.")
    conn = app.conn()
    try:
        auth.set_active(conn, uid, action == "enable")
        return "/users", note("Account enabled." if action == "enable" else "Account disabled and signed out.")
    except ValueError as e:
        return "/users", err(str(e))
    finally:
        conn.close()


def act_user_role(app, form):
    uid = _uid(form)
    if uid == current_user()["user_id"]:
        return "/users", err("You cannot change your own role - ask another admin.")
    conn = app.conn()
    try:
        auth.set_role(conn, uid, form.get("role", [""])[0])
        return "/users", note("Role updated.")
    except ValueError as e:
        return "/users", err(str(e))
    finally:
        conn.close()


def act_user_password(app, form):
    uid = _uid(form)
    conn = app.conn()
    try:
        auth.set_password(conn, uid, form.get("password", [""])[0])
        return "/users", note("Password changed. That person has been signed out everywhere.")
    except ValueError as e:
        return "/users", err(str(e))
    finally:
        conn.close()


ROUTES = {"/": pg_home, "/send": pg_send, "/offers": pg_offers, "/recipients": pg_recipients, "/notifications": pg_notifications,
          "/analysis": pg_analysis, "/generate": pg_generate, "/segments": pg_segments, "/customer": pg_customer,
          "/results": pg_results, "/setup": pg_setup, "/users": pg_users}


# ================================================================== actions (POST)
def note(msg, kind="note"):
    return Raw(f"<div class='{kind}'>{msg}</div>")


def err(msg):
    return note(E(msg), "errbox")


def do_send(app, form, kind):
    cat, mode = form.get("category", [""])[0], form.get("mode", ["simulated"])[0]
    if cat not in config.DEPARTMENTS + ([config.ALL_CATEGORIES] if kind == "active" else []):
        return err("Please choose a category first.")
    if mode == "live" and not is_admin():
        return note("<b>Not sent.</b> Only admins can send to phones.", "warnbox")
    if mode == "live" and form.get("confirm", [""])[0] != "yes":
        return note("<b>Not sent.</b> Tick the confirmation box to send real WhatsApp messages.", "warnbox")
    conn = app.conn()
    try:
        provider = None
        if mode == "live" and config.WHATSAPP_ENABLED:
            provider = app.provider_factory("whatsapp")
        fn = ce.run_active_offer_campaign if kind == "active" else og.send_approved
        s = fn(conn, cat, mode="whatsapp" if mode == "live" else "simulated", provider=provider,
               rehearsal=(mode == "rehearsal"), today=app.day())
        return Raw(summary_html(s))
    except ce.LiveSendingDisabled:
        return note("<b>Not sent.</b> Sending to phones is switched off. Turn it on in Setup first.", "warnbox")
    except ns.ProviderConfigError as e:
        return note(f"<b>Not sent.</b> WhatsApp is not fully set up: {E(str(e))} (see Setup).", "warnbox")
    finally:
        conn.close()


def act_offers_add(app, form):
    f = lambda k: form.get(k, [""])[0].strip()
    conn = app.conn()
    try:
        prod = conn.execute("SELECT department FROM products WHERE stock_code=?", (f("stock_code"),)).fetchone()
        if not prod or prod[0] not in config.DEPARTMENTS:
            return "/offers", err("Choose a product from the list.")
        if not f("discount") and not f("promo"):
            return "/offers", err("Give a discount percentage or a promotion text - nothing is invented for you.")
        try:
            d = float(f("discount")) if f("discount") else None
            vf, vt = date.fromisoformat(f("valid_from")), date.fromisoformat(f("valid_to"))
        except ValueError:
            return "/offers", err("Check the discount number and the dates.")
        if d is not None and not 0 <= d <= 100:
            return "/offers", err("The discount must be between 0 and 100.")
        if vf > vt:
            return "/offers", err("'Valid to' cannot be before 'Valid from'.")
        code, i = f"OFF-{f('stock_code')}-{vf}", 1
        while conn.execute("SELECT 1 FROM offers WHERE offer_code=?", (code,)).fetchone():
            i += 1
            code = f"OFF-{f('stock_code')}-{vf}-{i}"
        conn.execute("INSERT INTO offers (offer_code, stock_code, department, discount_percent, promo_text, valid_from, valid_to, "
                     "origin, status, created_at) VALUES (?,?,?,?,?,?,?,'configured','active',?)",
                     (code, f("stock_code"), prod[0], d, f("promo") or None, str(vf), str(vt), database.now_iso()))
        conn.commit()
        return "/offers", note(f"Offer <b>{E(code)}</b> added.")
    finally:
        conn.close()


def act_offers_toggle(app, form):
    oid = form.get("id", [""])[0]
    conn = app.conn()
    try:
        r = conn.execute("SELECT status FROM offers WHERE offer_id=? AND origin='configured'", (oid,)).fetchone() if oid.isdigit() else None
        if not r:
            return "/offers", err("Offer not found.")
        new = "inactive" if r[0] == "active" else "active"
        conn.execute("UPDATE offers SET status=? WHERE offer_id=?", (new, int(oid)))
        conn.commit()
        return "/offers", note(f"Offer is now {new}.")
    finally:
        conn.close()


def act_consent(app, form):
    action, name = form.get("action", [""])[0], form.get("name", [""])[0]
    conn = app.conn()
    try:
        now = database.now_iso()
        if action == "grant_all":
            if not form.get("yes"):
                return "/recipients", note("Tick the confirmation box first. No consent was recorded.", "warnbox")
            conn.execute("UPDATE demo_recipients SET consent_status='opted_in', consent_recorded_at=?", (now,))
            msg = "Consent recorded for everyone listed."
        elif action in ("grant", "revoke"):
            n = conn.execute("UPDATE demo_recipients SET consent_status=?, consent_recorded_at=? WHERE name=?",
                             ("opted_in" if action == "grant" else "opted_out", now, name)).rowcount
            msg = f"{E(name)} updated." if n else "Recipient not found."
        else:
            return "/recipients", err("Unknown action.")
        conn.commit()
        return "/recipients", note(msg)
    finally:
        conn.close()


def act_refresh(app, form):
    try:
        provider = app.provider_factory("whatsapp")
    except ns.ProviderConfigError as e:
        return "/notifications", note(f"Cannot check delivery: {E(str(e))}", "warnbox")
    conn = app.conn()
    try:
        n = ce.refresh_delivery_status(conn, provider)
        return "/notifications", note(f"{n} message(s) newly confirmed as delivered by the provider.")
    finally:
        conn.close()


def act_propose(app, form):
    cat = form.get("category", [""])[0]
    if cat not in config.DEPARTMENTS:
        return "/generate", err("Choose a category.")
    conn = app.conn()
    try:
        made = og.propose_offers(conn, cat, app.day())
    finally:
        conn.close()
    return f"/generate?c={quote(cat)}", note(f"Created {len(made)} proposal(s). They are <b>not active</b> and will <b>not</b> be sent until you approve them.")


def act_decide(app, form):
    cat, act = form.get("category", [""])[0], form.get("action", [""])[0]
    ids = [int(x) for x in form.get("id", []) if x.isdigit()]
    conn = app.conn()
    try:
        n = og.approve(conn, ids, app.day()) if act == "approve" else og.reject(conn, ids) if act == "reject" else 0
    finally:
        conn.close()
    msg = note(f"{n} proposal(s) {E(act)}d.") if act in ("approve", "reject") else err("Unknown action.")
    return f"/generate?c={quote(cat)}" if cat in config.DEPARTMENTS else "/generate", msg


def act_record_purchase(app, form):
    f = lambda k: form.get(k, [""])[0]
    conn = app.conn()
    try:
        cid = int_id(f("customer_id"))
        cust = conn.execute("SELECT r.name FROM demo_recipients r WHERE r.customer_id=?", (cid,)).fetchone()
        prod = conn.execute("SELECT description FROM products WHERE stock_code=?", (f("stock_code"),)).fetchone()
        price = conn.execute("SELECT price FROM transaction_items WHERE stock_code=? ORDER BY item_id DESC LIMIT 1", (f("stock_code"),)).fetchone()
        if not (cust and prod and price) or not f("qty").isdigit() or not 1 <= int(f("qty")) <= 50:
            return "/results#response", err("Choose a customer, a product and a quantity between 1 and 50.")
        qty, inv = int(f("qty")), f"POS-{uuid.uuid4().hex[:8].upper()}"
        conn.execute("INSERT INTO transactions (invoice, customer_id, invoice_date, country, is_synthetic) VALUES (?,?,?,?,1)",
                     (inv, cid, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "India"))
        conn.execute("INSERT INTO transaction_items (invoice, stock_code, quantity, price, line_total, is_synthetic) VALUES (?,?,?,?,?,1)",
                     (inv, f("stock_code"), qty, price[0], round(qty * price[0], 2)))
        conn.commit()
        return "/results#response", note(f"Recorded: {E(cust[0])} bought {qty} &times; {E(prod[0])}.")
    finally:
        conn.close()


def act_test_message(app, form):
    conn = app.conn()
    try:
        kind, text = wt.test_message(conn, form.get("name", [""])[0], form.get("confirm", [""])[0] == "yes", app.provider_factory)
    finally:
        conn.close()
    return "/setup", note(E(text), {"ok": "note", "warn": "warnbox", "err": "errbox"}[kind])


def act_check_login(app, form):
    ok, msg = wt.check_login(app.provider_factory)
    hint = "" if ok else " " + explain_error(msg)
    return "/setup", note(E(msg + hint), "note" if ok else "errbox")


POST_ROUTES = {"/offers/add": act_offers_add, "/offers/toggle": act_offers_toggle, "/recipients/consent": act_consent,
               "/notifications/refresh": act_refresh, "/generate/propose": act_propose, "/generate/decide": act_decide,
               "/results/record-purchase": act_record_purchase, "/setup/test-message": act_test_message,
               "/setup/check-login": act_check_login, "/users/create": act_user_create, "/users/toggle": act_user_toggle,
               "/users/role": act_user_role, "/users/password": act_user_password}


def make_handler(app):
    SESSION_COOKIE = "session"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _host_ok(self):
                        return (self.headers.get("Host") or "").split(":")[0] in ALLOWED_HOSTS

        def _send(self, code, body="", extra=None, cookie=None):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Cache-Control", "no-store")
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _who(self):
            """(cookie token, signed-in user row or None) for this request."""
            try:
                c = SimpleCookie(self.headers.get("Cookie") or "")
            except CookieError:
                return "", None
            tok = c[SESSION_COOKIE].value if SESSION_COOKIE in c else ""
            conn = app.conn()
            try:
                return tok, auth.session_user(conn, tok)
            finally:
                conn.close()

        def _enter(self, tok, user):
            _ctx.user = user
            _ctx.sid = hashlib.sha256(tok.encode()).hexdigest()[:16]

        def _leave(self):
            _ctx.user = None
            _ctx.sid = ""

        def _forbidden(self):
            self._send(403, page(app, "", "Not allowed", "", "<div class='card'>Your account does not have access to this page. "
                                                              "Ask an admin if you need it.</div>"))

        def _redirect_to_login(self, back):
            self._send(303, "", extra={"Location": "/login?next=" + quote(back, safe="/")})

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, "Forbidden host")
            u = urlparse(self.path)
            if u.path == "/favicon.ico":
                return self._send(204)
            tok, user = self._who()
            if u.path == "/login":
                if user:
                    return self._send(303, "", extra={"Location": "/"})
                conn = app.conn()
                try:
                    empty = auth.count_users(conn) == 0
                finally:
                    conn.close()
                nxt = safe_next(parse_qs(u.query).get("next", ["/"])[0])
                return self._send(200, login_page(app, next_url=nxt, empty=empty))
            if not user:
                return self._redirect_to_login(self.path)
            self._enter(tok, user)
            try:
                if u.path in ADMIN_GET and user["role"] != "admin":
                    return self._forbidden()
                fn = ROUTES.get(u.path)
                if not fn:
                    return self._send(404, "Page not found")
                conn = app.conn()
                try:
                    self._send(200, fn(app, conn, parse_qs(u.query)))
                finally:
                    conn.close()
            finally:
                self._leave()

        def _login(self, form):
            f = lambda k: form.get(k, [""])[0]
            nxt = safe_next(f("next") or "/")
            conn = app.conn()
            try:
                user, why = auth.authenticate(conn, f("username"), f("password"))
                if user is None:
                    auth.record_event(conn, f("username")[:64], "login_failed", why)
                    return self._send(200, login_page(app, auth.GENERIC_FAILURE, nxt, empty=auth.count_users(conn) == 0, username=f("username")))
                token = auth.create_session(conn, user["user_id"])
                auth.record_event(conn, user["username"], "login_ok", "")
            finally:
                conn.close()
            cookie = f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={auth.SESSION_HOURS * 3600}"
            self._send(303, "", extra={"Location": nxt}, cookie=cookie)

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, "Forbidden host")
            form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
            if form.get("token", [""])[0] != app.token:
                return self._send(403, "Bad or missing form token - reload the page")
            path = urlparse(self.path).path
            tok, user = self._who()
            if path == "/login":
                return self._login(form)
            if not user:
                return self._redirect_to_login("/")
            self._enter(tok, user)
            try:
                if path == "/logout":
                    conn = app.conn()
                    try:
                        auth.end_session(conn, tok)
                        auth.record_event(conn, user["username"], "logout", "")
                    finally:
                        conn.close()
                    return self._send(303, "", extra={"Location": "/login"}, cookie=f"{SESSION_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
                if path in ADMIN_POST and user["role"] != "admin":
                    return self._forbidden()
                if path in ("/send", "/generate/send"):
                    kind = "active" if path == "/send" else "approved"
                    app.flash = do_send(app, form, kind)
                    cat = form.get("category", [""])[0]
                    back = "/send" if kind == "active" else "/generate"
                    valid = config.DEPARTMENTS + ([config.ALL_CATEGORIES] if kind == "active" else [])
                    dest = (f"{back}?c={quote(cat)}" if cat in valid else back) + "#result"
                elif path in POST_ROUTES:
                    dest, app.flash = POST_ROUTES[path](app, form)
                else:
                    return self._send(404, "Page not found")
                self._send(303, "", extra={"Location": dest})
            finally:
                self._leave()
    return H


def make_server(app, port=8000):
        return ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), port), make_handler(app))


def serve(port=8000, open_browser=False):
    app = App()
    srv = make_server(app, port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Incrementum is running at {url}   (Ctrl+C to stop)")
    c = app.conn()
    try:
        if auth.count_users(c) == 0:
            print("No accounts yet. Create the first admin:  python -m src.main create-user --username YOURNAME --role admin")
    finally:
        c.close()
    print("Messages:", "WhatsApp sending is ON in .env (each send still needs a confirmation tick)" if config.WHATSAPP_ENABLED
          else "test mode - nothing is sent to phones")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
