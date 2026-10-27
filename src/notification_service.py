"""Notification providers. Status words are used strictly:
   simulated        - message was only written to the local simulated inbox (nothing left this computer)
   sent_to_provider - the provider ACCEPTED it (e.g. Twilio queued it). NOT proof of delivery.
   delivered        - only set after the provider reports delivery (see refresh_delivery_status)
"""
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from . import config

PHONE_RE = re.compile(r"^\+\d{10,15}$")


@dataclass
class ProviderResult:
    ok: bool
    status: str                       # 'simulated' | 'sent_to_provider' | 'failed'
    message_id: Optional[str] = None
    provider_status: Optional[str] = None
    error: Optional[str] = None


class ProviderConfigError(Exception):
    pass


def get_phone(env_key):
    """Return the number from .env, or None if missing, malformed or an obvious placeholder (+910000000000)."""
    value = re.sub(r"[\s\-()]", "", os.getenv(env_key or "", ""))   # tolerate "+91 98765 43210" typed with spaces
    if not PHONE_RE.match(value):
        return None
    digits = value[1:]
    if len(set(digits[2:])) <= 1:          # e.g. all zeros / all the same digit after the country code
        return None
    return value


class SimulatedProvider:
    mode = "simulated"

    def __init__(self, inbox_file=None):
        self.inbox_file = inbox_file or (config.LOGS_DIR / "simulated_inbox.txt")

    def send(self, to, body, recipient_name=""):
        try:
            self.inbox_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.inbox_file, "a", encoding="utf-8") as f:
                f.write(f"--- SIMULATED (not sent) to {recipient_name} ---\n{body}\n\n")
        except OSError:
            pass  # the database log is the source of truth
        return ProviderResult(True, "simulated", f"SIM-{uuid.uuid4().hex[:10]}", "simulated_not_sent")

    def fetch_status(self, message_id):
        return "simulated_not_sent"


class TwilioWhatsAppProvider:
    """Optional real provider (Twilio WhatsApp sandbox). Needs credentials in .env.

    Sandbox rules (Twilio docs): only people who joined the sandbox can be messaged; the join lasts 3 days; free-form
    messages only work within 24 hours of the person's last message TO the sandbox number; the sandbox sends at most
    one message every 3 seconds (so sends are paced)."""
    mode = "whatsapp"

    def __init__(self):
        sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
        token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
        sender = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
        self.sender = re.sub(r"^whatsapp:", "", sender).replace(" ", "")      # accept "whatsapp:+1415..." or "+1415..."
        if not (sid and token and self.sender):
            raise ProviderConfigError("Twilio credentials missing: set TWILIO_ACCOUNT_SID, "
                                      "TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM in .env")
        try:
            from twilio.rest import Client
        except ImportError as e:
            raise ProviderConfigError("The 'twilio' package is not installed (run: uv pip install twilio)") from e
        self.client = Client(sid, token)
        try:
            self.min_interval = float(os.getenv("WHATSAPP_MIN_INTERVAL", "3"))
        except ValueError:
            self.min_interval = 3.0
        self._last = None

    def _pace(self):
        if self._last is not None:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)

    def check_account(self):
        """Confirms the Account SID / Auth Token work, without sending anything."""
        try:
            acc = self.client.api.accounts(self.client.account_sid).fetch()
            return True, f"Connected to Twilio account '{acc.friendly_name}' (status: {acc.status}, type: {acc.type})"
        except Exception as e:
            return False, str(e)[:300]

    def send(self, to, body, recipient_name=""):
        self._pace()
        try:
            msg = self.client.messages.create(from_=f"whatsapp:{self.sender}", to=f"whatsapp:{to}", body=body)
            return ProviderResult(True, "sent_to_provider", msg.sid, msg.status)
        except Exception as e:  # network / auth / sandbox-not-joined / outside-24h-window errors
            return ProviderResult(False, "failed", error=str(e)[:300])
        finally:
            self._last = time.monotonic()

    def fetch_status(self, message_id):
        return self.client.messages(message_id).fetch().status


def is_confirmed_delivery(provider_status) -> bool:
    return (provider_status or "").lower() in {"delivered", "read"}


def get_provider(mode):
    if mode == "simulated":
        return SimulatedProvider()
    if mode == "whatsapp":
        return TwilioWhatsAppProvider()
    raise ValueError(f"Unknown mode {mode}")
