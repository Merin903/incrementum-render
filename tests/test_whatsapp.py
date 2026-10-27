"""Tests the REAL TwilioWhatsAppProvider code against a stand-in for the twilio library (no network, no account).
What this proves: the exact calls our code makes. It does NOT prove Twilio accepts them - that needs a real account."""
import sys
import time
import types

from src import config, notification_service as ns, whatsapp_tools as wt
from .helpers import set_env
from .test_webapp import Site, NF

SID = "ACaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ENV = {"TWILIO_ACCOUNT_SID": SID, "TWILIO_AUTH_TOKEN": "tok", "TWILIO_WHATSAPP_FROM": "+14155238886", "WHATSAPP_MIN_INTERVAL": "0"}


class FakeTwilio:
    """Installs fake 'twilio' modules for the duration of a with-block and records every messages.create call."""
    def __init__(self, fail_with=None, status="queued"):
        self.calls, self.fail_with, self.status = [], fail_with, status

    def __enter__(self):
        outer = self
        class Messages:
            def create(self, **kw):
                outer.calls.append(kw)
                if outer.fail_with:
                    raise Exception(outer.fail_with)
                return types.SimpleNamespace(sid=f"SM{len(outer.calls):03d}", status=outer.status)
            def __call__(self, sid):
                return types.SimpleNamespace(fetch=lambda: types.SimpleNamespace(status="delivered"))
        class Accounts:
            def __call__(self, sid):
                def fetch():
                    if outer.fail_with == "AUTH":
                        raise Exception("Unable to create record: Authenticate (20003)")
                    return types.SimpleNamespace(friendly_name="Incrementum Test", status="active", type="Trial")
                return types.SimpleNamespace(fetch=fetch)
        class Client:
            def __init__(self, sid, token):
                self.account_sid, self.messages, self.api = sid, Messages(), types.SimpleNamespace(accounts=Accounts())
        rest = types.ModuleType("twilio.rest"); rest.Client = Client
        tw = types.ModuleType("twilio"); tw.rest = rest
        self.saved = {k: sys.modules.get(k) for k in ("twilio", "twilio.rest")}
        sys.modules["twilio"], sys.modules["twilio.rest"] = tw, rest
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_provider_makes_the_exact_whatsapp_api_call():
    with FakeTwilio() as t, set_env(ENV):
        res = ns.TwilioWhatsAppProvider().send("+919876501234", "Hello there", "Merin")
    assert res.ok and res.status == "sent_to_provider" and res.message_id == "SM001" and res.provider_status == "queued"
    assert t.calls == [{"from_": "whatsapp:+14155238886", "to": "whatsapp:+919876501234", "body": "Hello there"}]


def test_sender_is_accepted_with_or_without_the_whatsapp_prefix_and_spaces():
    for raw in ("whatsapp:+14155238886", "+14155238886", " +1 415 523 8886 ", "whatsapp:+1 415 523 8886"):
        with FakeTwilio() as t, set_env({**ENV, "TWILIO_WHATSAPP_FROM": raw}):
            ns.TwilioWhatsAppProvider().send("+919876501234", "x")
        assert t.calls[0]["from_"] == "whatsapp:+14155238886", raw


def test_sends_are_paced_to_respect_the_sandbox_rate_limit():
    with FakeTwilio(), set_env({**ENV, "WHATSAPP_MIN_INTERVAL": "0.3"}):
        p = ns.TwilioWhatsAppProvider()
        p.send("+919876501234", "a")
        t0 = time.monotonic()
        p.send("+919876505678", "b")
        assert time.monotonic() - t0 >= 0.25


def test_provider_errors_become_failed_results_with_a_useful_hint():
    with FakeTwilio(fail_with="Unable to create record: Twilio could not deliver (63016) outside the allowed window") as t, set_env(ENV):
        res = ns.TwilioWhatsAppProvider().send("+919876501234", "x")
    assert not res.ok and res.status == "failed" and "63016" in res.error
    assert "24 hours" in wt.explain_error(res.error)


def test_missing_credentials_or_package_give_clear_setup_errors():
    with FakeTwilio(), set_env({**ENV, "TWILIO_AUTH_TOKEN": ""}):
        try:
            ns.TwilioWhatsAppProvider()
            assert False, "should have refused"
        except ns.ProviderConfigError as e:
            assert "credentials missing" in str(e)
    saved = sys.modules.get("twilio"), sys.modules.get("twilio.rest")
    sys.modules["twilio"], sys.modules["twilio.rest"] = None, None              # makes 'import twilio' fail
    try:
        with set_env(ENV):
            try:
                ns.TwilioWhatsAppProvider()
                assert False, "should have refused"
            except ns.ProviderConfigError as e:
                assert "not installed" in str(e)
    finally:
        for k, v in zip(("twilio", "twilio.rest"), saved):
            sys.modules.pop(k, None)
            if v is not None:
                sys.modules[k] = v


def test_credential_check_reports_account_or_the_login_problem():
    with FakeTwilio() as t, set_env(ENV):
        ok, msg = wt.check_login()
    assert ok and "Incrementum Test" in msg and "Trial" in msg and t.calls == []          # sends nothing
    with FakeTwilio(fail_with="AUTH") as t, set_env(ENV):
        ok, msg = wt.check_login()
    assert not ok and "20003" in msg and "Auth Token" in wt.explain_error(msg)
    with set_env({**ENV, "TWILIO_ACCOUNT_SID": ""}):
        ok, msg = wt.check_login()
    assert not ok and "credentials missing" in msg


def test_phone_numbers_typed_with_spaces_are_accepted_but_placeholders_are_not():
    with set_env({"P1": "+91 98765 43210", "P2": "98765 43210", "P3": "+910000000000", "P4": "+91-98765-43210"}):
        assert ns.get_phone("P1") == "+919876543210" and ns.get_phone("P4") == "+919876543210"
        assert ns.get_phone("P2") is None and ns.get_phone("P3") is None


def test_error_hints_cover_the_common_whatsapp_failures():
    assert "joined the Twilio WhatsApp sandbox" in wt.explain_error("Twilio 63015: channel sandbox can only send to joined numbers")
    assert "24 hours" in wt.explain_error("63016 Failed to send freeform message because you are outside the allowed window")
    assert "TWILIO_WHATSAPP_FROM" in wt.explain_error("63007: Twilio could not find a Channel with the specified From address")
    assert "international format" in wt.explain_error("21211 Invalid 'To' Phone Number")
    assert wt.explain_error("something unrelated") == ""


def test_end_to_end_live_send_through_the_real_provider_class_and_the_website():
    phones = {"DEMO_PHONE_MERIN": "+919876500001", "DEMO_PHONE_HANNA": "+919876500002", "DEMO_PHONE_DELNA": "+919876500003",
              "DEMO_PHONE_ANGEL": "+919876500004", "DEMO_PHONE_ADWOITH": "+919876500005"}
    with FakeTwilio() as t, set_env({**ENV, **phones}, whatsapp_enabled=True):
        s = Site(provider=ns.TwilioWhatsAppProvider())
        try:
            body = s.post("/send", {"category": "All categories", "mode": "live", "confirm": "yes"})
            assert len(t.calls) == 5 and {c["to"] for c in t.calls} == {f"whatsapp:{v}" for v in phones.values()}
            assert all(c["from_"] == "whatsapp:+14155238886" for c in t.calls)
            merin = [c for c in t.calls if c["to"] == "whatsapp:+919876500001"][0]["body"]
            assert "Dettol" in merin and "Coca-Cola" in merin and "Hi Merin!" in merin
            st = dict(s.rows("SELECT status, COUNT(*) FROM notification_logs GROUP BY status"))
            assert st == {"sent_to_provider": 5}                                       # accepted, NOT 'delivered'
            assert "accepted by whatsapp provider" in body.lower()
            assert "5 message(s) newly confirmed" in s.post("/notifications/refresh", {})   # fake provider reports 'delivered'
            assert s.rows("SELECT COUNT(*) FROM notification_logs WHERE status='delivered'")[0][0] == 5
            assert "9876500001" not in " ".join(str(tuple(r)) for r in s.rows("SELECT * FROM notification_logs"))
        finally:
            s.close()


def test_web_setup_page_has_the_24_hour_reminder_and_login_check():
    s = Site()
    try:
        page = s.get("/setup")
        assert "within the last 24 hours" in page and "Check Twilio login" in page and "Before every presentation" in page
        class WithCheck:
            mode = "whatsapp"
            def check_account(self):
                return True, "Connected to Twilio account 'X' (status: active, type: Trial)"
        s.provider = WithCheck()
        assert "Connected to Twilio account" in s.post("/setup/check-login", {})
        def refuse(mode):
            raise ns.ProviderConfigError("Twilio credentials missing: set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM in .env")
        s.app.provider_factory = refuse
        assert "credentials missing" in s.post("/setup/check-login", {})
    finally:
        s.close()


def test_command_line_has_whatsapp_check_and_test():
    from src import main
    p = main.build_parser()
    assert p.parse_args(["whatsapp-check"]).cmd == "whatsapp-check"
    a = p.parse_args(["whatsapp-test", "--name", "Merin", "--yes"])
    assert a.name == "Merin" and a.yes
