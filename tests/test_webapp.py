import http.client
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlencode, urlparse

from src import auth, config, database, demo_data, offers, product_segmentation as ps, webapp, notification_service as ns
from .helpers import FakeLiveProvider, TODAY, set_env

NF, SN = "Non-Food & Household", "Frozen & Snacks"
PAGES = [p for p, _ in webapp.NAV]


class SandboxFail:
    mode = "whatsapp"
    def send(self, to, body, recipient_name=""):
        return ns.ProviderResult(False, "failed", error="Twilio 63015: number has not joined the sandbox")
    def fetch_status(self, mid):
        return "queued"


PASSWORD = "Correct-horse-42"


class Site:
    def __init__(self, consent=True, name_override=None, provider=None, role="admin"):
        self.db = Path(tempfile.mkdtemp()) / "t.db"
        conn = database.connect(self.db)
        database.init_db(conn)
        demo_data.seed(conn, today=TODAY)
        offers.load_from_config(conn)
        if consent:
            conn.execute("UPDATE demo_recipients SET consent_status='opted_in'")
        if name_override:
            conn.execute("UPDATE demo_recipients SET name=? WHERE name='Adwoith'", (name_override,))
        conn.commit()
        self.admin_id = auth.create_user(conn, "boss", PASSWORD, "admin", "Boss")
        uid = self.admin_id if role == "admin" else auth.create_user(conn, "staffer", PASSWORD, "staff", "Staffer")
        self.cookie = "session=" + auth.create_session(conn, uid)                      # tests start signed in
        conn.close()
        self.provider = provider or FakeLiveProvider()
        self.app = webapp.App(self.db, today=TODAY, provider_factory=lambda mode: self.provider)
        self.srv = webapp.make_server(self.app, 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def req(self, method, path, form=None, host=None, token=True, signed_in=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers, body = {"Host": host or f"127.0.0.1:{self.port}"}, None
        if signed_in and self.cookie:
            headers["Cookie"] = self.cookie
        if form is not None:
            f = dict(form)
            if token:
                f["token"] = self.app.token
            body = urlencode(f, doseq=True)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        return r.status, r.read().decode(), r.getheader("Location")

    def get(self, path):
        st, body, _ = self.req("GET", path)
        assert st == 200, (path, st)
        return body

    def post(self, path, form):
        """Click a button: POST, follow the redirect, return the page that shows the result."""
        st, _, loc = self.req("POST", path, form)
        assert st == 303, (path, st)
        return self.get(loc.split("#")[0])

    def rows(self, sql):
        c = database.connect(self.db)
        try:
            return c.execute(sql).fetchall()
        finally:
            c.close()

    def exec(self, sql, args=()):
        c = database.connect(self.db)
        c.execute(sql, args); c.commit(); c.close()

    def close(self):
        self.srv.shutdown()


def simulated(s, n_expected):
    return s.rows("SELECT COUNT(*) FROM notification_logs WHERE status='simulated'")[0][0] == n_expected


# ---------------------------------------------------------------- site structure and wording
def test_every_page_loads_with_navigation_and_no_internal_wording():
    s = Site()
    try:
        for path in PAGES + ["/customer?id=C-MERIN", "/segments?seg=1", "/analysis?c=Pantry+%26+Dry+Goods"]:
            body = s.get(path)
            low = body.lower()
            for banned in ("demo", "section a", "section b", "synthetic"):
                assert banned not in low, (path, banned)
            for p in PAGES:
                assert f'href="{p}"' in body, (path, p)                   # navigation on every page
            assert "+91" not in body and "DEMO-" not in body             # no phone numbers, no internal ids
    finally:
        s.close()


def test_home_page_shows_figures_and_links_to_each_workflow():
    s = Site()
    try:
        body = s.get("/")
        assert "Active offers today" in body and "Customers" in body
        for target in ("/send", "/generate", "/analysis", "/segments", "/results", "/notifications"):
            assert f"href='{target}'" in body
    finally:
        s.close()


def test_unknown_pages_and_favicon():
    s = Site()
    try:
        assert s.req("GET", "/nope")[0] == 404 and s.req("POST", "/nope", {})[0] == 404 and s.req("GET", "/favicon.ico")[0] == 204
    finally:
        s.close()


# ---------------------------------------------------------------- Send Offers
def test_send_page_previews_who_gets_what_before_sending():
    s = Site()
    try:
        body = s.get("/send?c=Non-Food+%26+Household")
        assert "Dettol" in body and "20% off" in body and "Harpic" in body and "Skipped" in body
        assert simulated(s, 0) and s.rows("SELECT COUNT(*) FROM notification_logs")[0][0] == 0      # a preview sends nothing
    finally:
        s.close()


def test_send_offers_button_processes_everyone_in_one_click():
    s = Site()
    try:
        body = s.post("/send", {"category": NF, "mode": "simulated"})
        assert "Hi Merin!" in body and "20% off" in body and "Hi Hanna!" in body and "15% off" in body
        assert "nothing was sent to phones" in body
        assert dict(s.rows("SELECT status, COUNT(*) FROM notification_logs GROUP BY status")) == {"simulated": 2, "skipped": 3}
    finally:
        s.close()


def test_other_category_offers_are_never_sent_for_the_chosen_category():
    s = Site()
    try:
        body = s.post("/send", {"category": NF, "mode": "simulated"})
        assert "Coca-Cola" not in body and "Lays" not in body
        body = s.post("/send", {"category": SN, "mode": "simulated"})
        assert "Hi Delna!" in body and "Coca-Cola" in body and "Lays" in body
    finally:
        s.close()


def test_second_click_sends_nothing_new_and_practice_runs_repeat():
    s = Site()
    try:
        s.post("/send", {"category": NF, "mode": "simulated"})
        body = s.post("/send", {"category": NF, "mode": "simulated"})
        assert "duplicate" in body and simulated(s, 2)
        s.post("/send", {"category": NF, "mode": "rehearsal"})
        body = s.post("/send", {"category": NF, "mode": "rehearsal"})
        assert "Practice run" in body and simulated(s, 6)
    finally:
        s.close()


def test_sending_to_phones_needs_the_switch_the_tick_and_goes_through_the_provider():
    s = Site()
    try:
        with set_env(whatsapp_enabled=False):
            assert "switched off" in s.post("/send", {"category": NF, "mode": "live", "confirm": "yes"})
        with set_env({"DEMO_PHONE_MERIN": "+919876501234", "DEMO_PHONE_HANNA": "+919876505678"}, whatsapp_enabled=True):
            assert "Tick the confirmation box" in s.post("/send", {"category": NF, "mode": "live"})
            assert s.rows("SELECT COUNT(*) FROM notification_logs")[0][0] == 0
            body = s.post("/send", {"category": NF, "mode": "live", "confirm": "yes"})
        assert len(s.provider.sent) == 2 and "accepted by whatsapp provider" in body.lower()
        assert {r[0] for r in s.rows("SELECT status FROM notification_logs WHERE status!='skipped'")} == {"sent_to_provider"}
        assert s.rows("SELECT COUNT(*) FROM notification_logs WHERE status='delivered'")[0][0] == 0      # accepted is not delivered
    finally:
        s.close()


def test_delivery_status_only_becomes_delivered_when_provider_confirms():
    s = Site()
    try:
        with set_env({"DEMO_PHONE_MERIN": "+919876501234", "DEMO_PHONE_HANNA": "+919876505678"}, whatsapp_enabled=True):
            s.post("/send", {"category": NF, "mode": "live", "confirm": "yes"})
            s.provider.final_status = "queued"
            assert "0 message(s) newly confirmed" in s.post("/notifications/refresh", {})
            s.provider.final_status = "delivered"
            body = s.post("/notifications/refresh", {})
        assert "2 message(s) newly confirmed" in body and "delivered" in body
    finally:
        s.close()


def test_failed_sends_show_a_plain_language_reason():
    s = Site(provider=SandboxFail())
    try:
        with set_env({"DEMO_PHONE_MERIN": "+919876501234", "DEMO_PHONE_HANNA": "+919876505678"}, whatsapp_enabled=True):
            s.post("/send", {"category": NF, "mode": "live", "confirm": "yes"})
        body = s.get("/notifications")
        assert "failed" in body and "has not joined the Twilio WhatsApp sandbox" in body
    finally:
        s.close()


# ---------------------------------------------------------------- security
def test_forms_without_token_and_requests_with_wrong_host_are_rejected():
    s = Site()
    try:
        assert s.req("POST", "/send", {"category": NF, "mode": "simulated"}, token=False)[0] == 403
        assert s.req("GET", "/", host="evil.example.com")[0] == 403
        assert s.req("POST", "/send", {"category": NF}, host="evil.example.com")[0] == 403
        assert s.rows("SELECT COUNT(*) FROM notification_logs")[0][0] == 0
    finally:
        s.close()


def test_names_are_html_escaped_everywhere():
    s = Site(name_override="<script>alert(1)</script>")
    try:
        for path in ("/recipients", "/send", "/analysis", "/setup"):
            body = s.get(path)
            assert "<script>alert(1)</script>" not in body, path
        assert "&lt;script&gt;" in s.get("/recipients")
    finally:
        s.close()


# ---------------------------------------------------------------- recipients and consent
def test_consent_flows_and_opt_out_blocks_sending():
    s = Site(consent=False)
    try:
        assert "No consent was recorded" in s.post("/recipients/consent", {"action": "grant_all"})
        body = s.post("/send", {"category": NF, "mode": "simulated"})
        assert "consent not granted" in body and simulated(s, 0)
        s.post("/recipients/consent", {"action": "grant_all", "yes": "1"})
        s.post("/recipients/consent", {"action": "revoke", "name": "Hanna"})
        body = s.post("/send", {"category": NF, "mode": "rehearsal"})
        assert "Hi Merin!" in body and "Hi Hanna!" not in body and "opted_out" in body
        assert "Hanna" in s.get("/results") and "opted out" in s.get("/results").lower()
    finally:
        s.close()


# ---------------------------------------------------------------- Offers page
def test_adding_validating_and_deactivating_offers():
    s = Site()
    try:
        base = {"stock_code": "DET500", "valid_from": "2026-10-01", "valid_to": "2026-12-31"}
        assert "Give a discount percentage or a promotion text" in s.post("/offers/add", {**base, "discount": "", "promo": ""})
        assert "between 0 and 100" in s.post("/offers/add", {**base, "discount": "150", "promo": ""})
        assert "cannot be before" in s.post("/offers/add", {**base, "discount": "5", "valid_from": "2026-12-31", "valid_to": "2026-10-01"})
        assert "Check the discount number" in s.post("/offers/add", {**base, "discount": "abc"})
        assert s.rows("SELECT COUNT(*) FROM offers")[0][0] == 7
        body = s.post("/offers/add", {**base, "discount": "25", "promo": ""})
        assert "OFF-DET500-2026-10-01" in body and s.rows("SELECT COUNT(*) FROM offers")[0][0] == 8
        oid = s.rows("SELECT offer_id FROM offers WHERE offer_code='OFF-DETTOL'")[0][0]
        assert "inactive" in s.post("/offers/toggle", {"id": str(oid)})
        # Dettol is now off, so Merin no longer gets Dettol (the new 25% Dettol offer is a second active one for the same product)
        s.exec("UPDATE offers SET status='inactive' WHERE offer_code LIKE 'OFF-DET500%'")
        body = s.post("/send", {"category": NF, "mode": "simulated"})
        assert "Hi Merin!" not in body and "Hi Hanna!" in body
    finally:
        s.close()


# ---------------------------------------------------------------- Generate Offers
def test_generate_offers_requires_approval_and_leaves_standard_offers_alone():
    s = Site()
    try:
        before = s.rows("SELECT offer_code, status FROM offers WHERE origin='configured' ORDER BY 1")
        assert "not active" in s.post("/generate/propose", {"category": NF})
        assert "No proposals yet" not in s.get("/generate?c=Non-Food+%26+Household")
        s.post("/generate/send", {"category": NF, "mode": "simulated"})
        assert simulated(s, 0)                                                   # nothing approved yet -> nothing sent
        merin = s.rows("SELECT pr.proposal_id FROM offer_proposals pr JOIN demo_recipients r ON r.customer_id=pr.customer_id WHERE r.name='Merin'")[0][0]
        others = [str(r[0]) for r in s.rows("SELECT proposal_id FROM offer_proposals") if r[0] != merin]
        s.post("/generate/decide", {"category": NF, "action": "approve", "id": [str(merin)]})
        s.post("/generate/decide", {"category": NF, "action": "reject", "id": others})
        body = s.post("/generate/send", {"category": NF, "mode": "simulated"})
        assert "Hi Merin!" in body and "10% off" in body and "Hi Hanna!" not in body and simulated(s, 1)
        assert s.rows("SELECT offer_code, status FROM offers WHERE origin='configured' ORDER BY 1") == before
        assert "10% off" not in s.post("/send", {"category": NF, "mode": "simulated"})      # standard sending still uses standard offers only
    finally:
        s.close()


# ---------------------------------------------------------------- analysis and segments
def test_analysis_and_segment_pages_show_reasons():
    s = Site()
    try:
        body = s.get("/analysis?c=Non-Food+%26+Household")
        assert "DETTOL ANTISEPTIC LIQUID 500ML" in body and "Customer groups" in body
        conn = database.connect(s.db)
        ps.run_segmentation(conn, TODAY); conn.close()
        seg = s.get("/segments")
        assert "Category preference" in seg and "Frequent buyers" in seg
        sid = s.rows("SELECT segment_id FROM product_preference_segments WHERE segment_name LIKE '%DETTOL%' LIMIT 1")[0][0]
        members = s.get(f"/segments?seg={sid}")
        assert "Merin" in members and "separate invoices" in members
        assert f"/segments?seg={sid}#members" in seg                                  # links jump straight to the member list
        cust = s.get("/customer?id=C-MERIN")
        assert "Segments and why" in cust and "Non-Food &amp; Household" in cust
        assert "not found" in s.get("/customer?id=nobody").lower()
    finally:
        s.close()


# ---------------------------------------------------------------- Campaign Results
def test_purchases_after_notification_are_observed_but_not_called_proof_of_cause():
    s = Site()
    try:
        s.post("/send", {"category": NF, "mode": "simulated"})
        body = s.get("/results")
        assert "Customers who bought after receiving a notification" in body and "Not yet" in body and "0%" in body
        assert "does not prove the message caused the purchase" in body
        merin = "C-MERIN"
        s.post("/results/record-purchase", {"customer_id": merin, "stock_code": "LAY52", "qty": "1"})      # unrelated product
        assert "0%" in s.get("/results")
        body = s.post("/results/record-purchase", {"customer_id": merin, "stock_code": "DET500", "qty": "2"})
        assert "Recorded: Merin bought 2" in body
        body = s.get("/results")
        assert "Yes" in body and "50%" in body                                    # 1 of the 2 notified customers bought the offered product
        assert "DETTOL ANTISEPTIC LIQUID 500ML" in body
        assert "Choose a customer" in s.post("/results/record-purchase", {"customer_id": merin, "stock_code": "NOPE", "qty": "1"})
        assert "Choose a customer" in s.post("/results/record-purchase", {"customer_id": merin, "stock_code": "DET500", "qty": "999"})
    finally:
        s.close()


def test_purchase_before_notification_does_not_count_as_a_response():
    s = Site()
    try:
        body = s.post("/send", {"category": NF, "mode": "simulated"})
        assert "Not yet" in s.get("/results")                                      # Merin's old Dettol purchases pre-date the message
        assert "Yes" not in re.sub(r"<[^>]+>", " ", s.get("/results").split("Bought afterwards?")[1].split("</table>")[0])
    finally:
        s.close()


def test_experiment_results_are_always_labelled_simulated_with_evidence_level():
    s = Site()
    try:
        assert "No experiment has been run" in s.get("/results")
        s.exec("INSERT INTO campaign_experiments (experiment_id, name, hypothesis, observation_window_days, is_simulated, created_at) "
               "VALUES (1,'SIM-test','A notification raises purchases (SIMULATED effect)',30,1,'2026-10-18T00:00:00')")
        for i in range(40):
            cid = f"9{i:04d}"
            s.exec("INSERT INTO customers (customer_id, country, is_synthetic) VALUES (?, 'UK', 0)", (cid,))
            grp = "treatment" if i % 2 == 0 else "control"
            s.exec("INSERT INTO experiment_assignments (experiment_id, customer_id, group_name, assigned_at) VALUES (1,?,?,'x')", (cid, grp))
            s.exec("INSERT INTO experiment_outcomes (experiment_id, customer_id, purchased, is_simulated) VALUES (1,?,?,?)",
                   (cid, 1 if (i % 2 == 0 and i % 4 != 2) or i % 5 == 1 else 0, 1 if grp == "treatment" else 0))
            s.exec("INSERT INTO cause_based_segments (customer_id, experiment_id, segment_label, evidence_level, basis, assigned_at) "
                   "VALUES (?,1,?,?,'basis text here for the label','x')", (cid, ("Persuadable", "Sure Thing", "Lost Cause")[i % 3], "insufficient"))
        body = s.get("/results")
        assert "Simulated" in body and body.count("simtag") >= 2
        assert "Persuadable" in body and "Sure Thing" in body and "Lost Cause" in body and "insufficient" in body
        assert "provisional" in body and "95% interval" in body and "p-value" in body
        assert "20" in body                                                       # 20 notified / 20 comparison customers
    finally:
        s.close()


# ---------------------------------------------------------------- Setup and test message
def test_setup_checklist_and_test_message_safeguards():
    s = Site()
    try:
        body = s.get("/setup")
        assert "WhatsApp checklist" in body and "Missing" in body
        assert "switched off" in s.post("/setup/test-message", {"name": "Merin", "confirm": "yes"})
        with set_env(whatsapp_enabled=True):
            assert "Tick the confirmation box" in s.post("/setup/test-message", {"name": "Merin"})
            assert "No valid phone number" in s.post("/setup/test-message", {"name": "Merin", "confirm": "yes"})
            with set_env({"DEMO_PHONE_MERIN": "+919876501234"}):
                body = s.post("/setup/test-message", {"name": "Merin", "confirm": "yes"})
                assert "accepted by the provider" in body and "not proof of delivery" in body
                assert s.provider.sent == [("+919876501234", "Merin")]
        s.exec("UPDATE demo_recipients SET consent_status='opted_out' WHERE name='Merin'")
        with set_env({"DEMO_PHONE_MERIN": "+919876501234"}, whatsapp_enabled=True):
            assert "has not agreed" in s.post("/setup/test-message", {"name": "Merin", "confirm": "yes"})
    finally:
        s.close()


def test_failed_test_message_explains_the_sandbox_problem():
    s = Site(provider=SandboxFail())
    try:
        with set_env({"DEMO_PHONE_MERIN": "+919876501234"}, whatsapp_enabled=True):
            body = s.post("/setup/test-message", {"name": "Merin", "confirm": "yes"})
        assert "failed" in body and "has not joined the Twilio WhatsApp sandbox" in body
    finally:
        s.close()


def test_segment_member_list_is_short_by_default_and_expandable():
    s = Site()
    try:
        conn = database.connect(s.db)
        ps.run_segmentation(conn, TODAY)
        sid = conn.execute("SELECT segment_id FROM product_preference_segments LIMIT 1").fetchone()[0]
        conn.execute("INSERT INTO product_preference_segments (segment_id, segment_name, segment_type, rule_description, created_at) VALUES (999,'Big','category','r','x')")
        for i in range(60):
            conn.execute("INSERT INTO customers (customer_id, country, is_synthetic) VALUES (?, 'UK', 0)", (f"8{i:04d}",))
            conn.execute("INSERT INTO customer_segment_memberships (customer_id, segment_id, strength, reason) VALUES (?,999,?,'because')", (f"8{i:04d}", float(i)))
        conn.commit(); conn.close()
        body = s.get("/segments?seg=999")
        assert "60 customers in this segment; showing 25" in body and "show more" in body
        assert "showing 60" in s.get("/segments?seg=999&n=100") and "show more" not in s.get("/segments?seg=999&n=100").split("members")[1].split("</div>")[0]
    finally:
        s.close()


def test_send_page_defaults_to_all_categories_and_still_offers_each_single_category():
    s = Site()
    try:
        body = s.get("/send")
        assert "<option selected>All categories</option>" in body
        import html
        for d in config.DEPARTMENTS:
            assert f"<option>{html.escape(d)}</option>" in body                     # existing choices are still there
        for who in ("Merin", "Hanna", "Delna", "Angel", "Adwoith"):
            assert who in body
        assert "Skipped" not in body                                                 # everyone has a matching offer in all-categories view
        assert "Every category is checked" in body
        single = s.get("/send?c=Non-Food+%26+Household")
        assert "Skipped" in single and "<option selected>Non-Food &amp; Household</option>" in single
    finally:
        s.close()


def test_one_click_on_all_categories_sends_a_message_to_all_five():
    s = Site()
    try:
        body = s.post("/send", {"category": "All categories", "mode": "simulated"})
        for who in ("Merin", "Hanna", "Delna", "Angel", "Adwoith"):
            assert f"Hi {who}!" in body
        assert dict(s.rows("SELECT status, COUNT(*) FROM notification_logs GROUP BY status")) == {"simulated": 5}
        assert "nothing was sent to phones" in body
        again = s.post("/send", {"category": "All categories", "mode": "simulated"})
        assert "duplicate" in again and simulated(s, 5)
    finally:
        s.close()


def test_generate_page_does_not_offer_all_categories():
    s = Site()
    try:
        assert "All categories" not in s.get("/generate")
        st, _, _ = s.req("POST", "/generate/send", {"category": "All categories", "mode": "simulated"})
        assert st == 303 and "Please choose a category" in s.get("/generate")
    finally:
        s.close()


# ---------------------------------------------------------------- charts, bubbles, drill-down
def seed_experiment(s, n=40):
    s.exec("INSERT INTO campaign_experiments (experiment_id, name, hypothesis, observation_window_days, is_simulated, created_at) "
           "VALUES (1,'SIM-test','A notification raises purchases (SIMULATED effect)',30,1,'2026-10-18T00:00:00')")
    for i in range(n):
        cid = f"9{i:04d}"
        grp = "treatment" if i % 2 == 0 else "control"
        s.exec("INSERT INTO customers (customer_id, country, is_synthetic) VALUES (?, 'UK', 0)", (cid,))
        s.exec("INSERT INTO experiment_assignments (experiment_id, customer_id, group_name, assigned_at) VALUES (1,?,?,'x')", (cid, grp))
        s.exec("INSERT INTO experiment_outcomes (experiment_id, customer_id, purchased, is_simulated) VALUES (1,?,?,?)",
               (cid, 1 if (i % 2 == 0 and i % 4 != 2) or i % 5 == 1 else 0, 1 if grp == "treatment" else 0))
        s.exec("INSERT INTO cause_based_segments (customer_id, experiment_id, segment_label, evidence_level, basis, assigned_at) "
               "VALUES (?,1,?,?,?,'x')", (cid, ("Persuadable", "Sure Thing", "Lost Cause")[i % 3], "insufficient", f"basis text number {i}"))


def test_chart_helpers_draw_safe_html_and_svg():
    assert webapp.compact(1234567) == "1.2M" and webapp.compact(950) == "950" and webapp.compact(2_000_000) == "2M"
    bars = webapp.hbars([("Big", 100), ("Half", 50), ("<b>x</b>", 10)])
    assert "width:100.0%" in bars and "width:50.0%" in bars and "<b>x</b>" not in bars and "&lt;b&gt;x&lt;/b&gt;" in bars
    svg = webapp.line_chart([("2010-01", 10), ("2010-02", 40), ("2010-03", 25)], prefix="\u00a3")
    assert svg.startswith("<svg") and "<polyline" in svg and "<title>2010-02: \u00a340</title>" in svg
    assert "Not enough history" in webapp.line_chart([("2010-01", 10)]) and "Nothing to show" in webapp.hbars([])
    assert "<script" not in webapp.line_chart([("<script>", 1), ("b", 2)])


def test_home_page_draws_sales_charts_from_the_real_sales_history():
    s = Site()
    try:
        assert "Not enough history" in s.get("/")                                          # nothing real to chart yet
        s.exec("INSERT INTO products (stock_code, description, department, is_synthetic) VALUES ('R1','PINK CHERRY LIGHTS','Lights & Candles',0)")
        s.exec("INSERT INTO customers (customer_id, country, is_synthetic) VALUES ('12345','UK',0)")
        for i, d in enumerate(("2010-01-05", "2010-02-05", "2010-03-05")):
            s.exec("INSERT INTO transactions (invoice, customer_id, invoice_date, country, is_synthetic) VALUES (?, '12345', ?, 'UK', 0)", (f"50000{i}", d + " 10:00:00"))
            s.exec("INSERT INTO transaction_items (invoice, stock_code, quantity, price, line_total, is_synthetic) VALUES (?, 'R1', 2, 5, ?, 0)", (f"50000{i}", 10.0 * (i + 1)))
        s.app._cache.clear()
        body = s.get("/")
        assert "<svg" in body and "2010-02: \u00a320" in body and "Lights &amp; Candles" in body and "\u00a360" in body
        assert "Message status" in body and "hero" in body
    finally:
        s.close()


def test_send_result_shows_whatsapp_style_bubbles_and_a_sending_state():
    s = Site()
    try:
        assert "Sending... please wait" in s.get("/send")                                   # button feedback while sending
        body = s.post("/send", {"category": "All categories", "mode": "simulated"})
        assert body.count('class="bubble testmsg"') == 5 and "Test mode - not sent to a phone" in body
        assert "class='chat'" in body and "Hi Delna!" in body
        assert "Sending... please wait" in s.get("/generate")
        notif = s.get("/notifications")
        assert 'class="bubble testmsg"' in notif                                            # message details are bubbles too
    finally:
        s.close()


def test_response_groups_have_bars_and_clickable_member_lists():
    s = Site()
    try:
        seed_experiment(s, 40)
        body = s.get("/results")
        assert "/results?group=Persuadable#groups" in body and "class='bars'" in body and "click to see customers" in body
        assert "basis text number" not in body                                              # members are hidden until a group is chosen
        grp = s.get("/results?group=Persuadable")
        assert "14 customers; showing 14" in grp or "13 customers; showing 13" in grp
        assert "basis text number 0" in grp and "Simulated" in grp and "/customer?id=90000" in grp
        assert "Sure Thing" in grp                                                          # other groups still listed
        assert "customers; showing" not in s.get("/results?group=Nonsense")
        s2 = s.get("/results?group=Lost+Cause&n=25")
        assert "showing 13" in s2 or "showing 14" in s2
    finally:
        s.close()


def test_segments_page_has_a_largest_segments_chart():
    s = Site()
    try:
        assert "Largest segments" not in s.get("/segments")                                 # no segments computed yet
        conn = database.connect(s.db)
        ps.run_segmentation(conn, TODAY); conn.close()
        body = s.get("/segments")
        assert "Largest segments" in body and "class='bars'" in body
    finally:
        s.close()


def test_navigation_shows_an_icon_for_every_page():
    s = Site()
    try:
        body = s.get("/")
        for path in PAGES:
            assert webapp.ICON[path] in body, path
    finally:
        s.close()


def test_bars_are_drawn_as_blocks_with_values_relative_to_each_other():
    html_ = webapp.hbars([("A", 80, "green"), ("B", 20, "amber")])
    assert "fill green" in html_ and "fill amber" in html_ and "width:100.0%" in html_ and "width:25.0%" in html_    # drawn together, comparable
    assert "display:block" in webapp.CSS.split(".bars .fill{")[1].split("}")[0]                                      # inline spans would ignore the width
    pct = webapp.hbars([("n", 62.0), ("c", 40.0)], fmt="{:.1f}%", maxv=100)
    assert "width:62.0%" in pct and "width:40.0%" in pct and "62.0%" in pct


def test_messages_are_still_shown_after_the_website_is_closed_and_started_again():
    s = Site()
    try:
        s.post("/send", {"category": "All categories", "mode": "simulated"})
    finally:
        s.close()                                                                      # "close the website"
    app2 = webapp.App(s.db, today=TODAY)                                               # "run it again": a brand-new server on the same database
    srv2 = webapp.make_server(app2, 0)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    try:
        port = srv2.server_address[1]
        for path in ("/", "/notifications", "/results"):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            c.request("GET", path, headers={"Host": f"127.0.0.1:{port}", "Cookie": s.cookie})        # same sign-in still valid after the restart
            body = c.getresponse().read().decode()
            assert "Merin" in body and "Delna" in body, path                           # earlier messages are still there
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        c.request("POST", "/send", body=urlencode({"category": "All categories", "mode": "simulated", "token": app2.token}),
                  headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/x-www-form-urlencoded", "Cookie": s.cookie})
        assert c.getresponse().status == 303
        conn = database.connect(s.db)
        assert conn.execute("SELECT COUNT(*) FROM notification_logs WHERE status='simulated'").fetchone()[0] == 5      # duplicate protection survives restarts
        assert conn.execute("SELECT COUNT(*) FROM notification_logs WHERE status='suppressed'").fetchone()[0] == 5
        conn.close()
    finally:
        srv2.shutdown()
