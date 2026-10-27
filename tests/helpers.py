import os
from datetime import date

from src import config, database, demo_data, offers

TODAY = date(2026, 10, 15)


def make_db(consent=True):
    conn = database.connect(":memory:")
    database.init_db(conn)
    demo_data.seed(conn, today=TODAY)
    offers.load_from_config(conn)
    if consent:
        conn.execute("UPDATE demo_recipients SET consent_status='opted_in'")
        conn.commit()
    return conn


class FakeLiveProvider:
    mode = "whatsapp"
    def __init__(self, fail_for=(), final_status="queued"):
        self.fail_for, self.final_status, self.sent = set(fail_for), final_status, []
    def send(self, to, body, recipient_name=""):
        from src.notification_service import ProviderResult
        if recipient_name in self.fail_for:
            return ProviderResult(False, "failed", error="simulated provider error")
        self.sent.append((to, recipient_name))
        return ProviderResult(True, "sent_to_provider", f"SM{len(self.sent):04d}", "queued")
    def fetch_status(self, message_id):
        return self.final_status


class set_env:
    """Temporarily set environment variables / config flags (no pytest fixtures needed)."""
    def __init__(self, env=None, whatsapp_enabled=None):
        self.env, self.wa = env or {}, whatsapp_enabled
    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.old_wa = config.WHATSAPP_ENABLED
        if self.wa is not None:
            config.WHATSAPP_ENABLED = self.wa
    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        config.WHATSAPP_ENABLED = self.old_wa


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc:
        return True
    return False
