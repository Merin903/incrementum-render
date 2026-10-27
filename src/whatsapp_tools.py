"""Shared WhatsApp helpers used by the website (Setup page) and the command line (whatsapp-check / whatsapp-test).

Twilio sandbox rules these checks remind you about (see Twilio docs, 'Twilio Sandbox for WhatsApp'):
  * every phone must have joined the sandbox (send "join <your sandbox keyword>" to the sandbox number), valid 3 days;
  * free-form messages (like offers) only work within 24 HOURS of that phone's last message TO the sandbox number;
  * one message every 3 seconds.
"""
import os

from . import config, database, notification_service as ns

REMINDER = ("Each phone must have messaged the Twilio sandbox number within the last 24 hours (for example send 'hi' or the "
            "'join ...' message again shortly before presenting). The join itself expires after 3 days.")


def twilio_installed() -> bool:
    try:
        import twilio  # noqa: F401
        return True
    except ImportError:
        return False


def credentials_present() -> bool:
    return bool(os.getenv("TWILIO_ACCOUNT_SID", "").strip() and os.getenv("TWILIO_AUTH_TOKEN", "").strip()
                and os.getenv("TWILIO_WHATSAPP_FROM", "").strip())


def check_login(provider_factory=None):
    """(ok, message): do the Twilio credentials work? Sends nothing."""
    factory = provider_factory or ns.get_provider
    try:
        p = factory("whatsapp")
    except ns.ProviderConfigError as e:
        return False, str(e)
    check = getattr(p, "check_account", None)
    return check() if check else (True, "Provider reachable")


def explain_error(text) -> str:
    t = (text or "").lower()
    if "63016" in t or "outside the allowed window" in t or "24" in t and "window" in t:
        return ("This phone has not messaged the sandbox number in the last 24 hours, so WhatsApp only allows templates. Ask the person to "
                "send 'hi' (or the join message) to the sandbox number, then try again.")
    if "63015" in t or "sandbox" in t:
        return "This phone has not joined the Twilio WhatsApp sandbox yet - ask the person to send the join message from their WhatsApp."
    if "20003" in t or "authenticate" in t or "unauthorized" in t:
        return "Twilio login details look wrong - check the Account SID and Auth Token in .env."
    if "63007" in t or "could not find a channel" in t:
        return "The sender number is not a WhatsApp sender on this account - check TWILIO_WHATSAPP_FROM (the sandbox number)."
    if "21211" in t or "invalid 'to'" in t:
        return "The phone number looks invalid - use the international format, for example +91XXXXXXXXXX."
    return ""


def test_message(conn, name, confirmed, provider_factory=None, log_dir=None):
    """Send one real WhatsApp test message to a consenting recipient. Returns (kind, text); kind is 'ok' | 'warn' | 'err'.
    Test messages are not part of any campaign; each attempt is appended to outputs/logs/connection_tests.log."""
    if not config.WHATSAPP_ENABLED:
        return "warn", "Sending to phones is switched off. Set WHATSAPP_ENABLED=true in .env and restart."
    if not confirmed:
        return "warn", "Tick the confirmation box to send a real test message."
    r = conn.execute("SELECT * FROM demo_recipients WHERE name=?", (name,)).fetchone()
    if not r:
        return "err", "Recipient not found."
    if r["consent_status"] != "opted_in":
        return "warn", f"{name} has not agreed to receive messages yet (Recipients page)."
    phone = ns.get_phone(r["phone_env_key"])
    if not phone:
        return "warn", f"No valid phone number for {name} in .env ({r['phone_env_key']})."
    try:
        res = (provider_factory or ns.get_provider)("whatsapp").send(
            phone, "Test message from Incrementum. If you can read this, WhatsApp is connected.", name)
    except ns.ProviderConfigError as e:
        return "warn", f"WhatsApp is not fully set up: {e}"
    try:
        d = log_dir or config.LOGS_DIR
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "connection_tests.log", "a", encoding="utf-8") as fh:
            fh.write(f"{database.now_iso()}\t{name}\t{res.status}\t{res.message_id}\t{res.provider_status}\t{res.error or ''}\n")
    except OSError:
        pass
    if res.ok:
        return "ok", (f"Test message to {name} was accepted by the provider (id {res.message_id}). Check the phone - "
                      "acceptance is not proof of delivery.")
    return "err", f"Test message to {name} failed: {res.error or ''} {explain_error(res.error)}".strip()


def readiness(conn):
    """Checklist rows: (label, ok) for Setup page and `whatsapp-check`."""
    from . import campaign_engine as ce
    items = [("Real sending switched on (WHATSAPP_ENABLED=true in .env)", config.WHATSAPP_ENABLED),
             ("Twilio package installed", twilio_installed()),
             ("Twilio Account SID, Auth Token and sender number set", credentials_present())]
    for r in ce.get_recipients(conn):
        items.append((f"Phone number set for {r['name']}", bool(ns.get_phone(r["phone_env_key"]))))
        items.append((f"{r['name']} has agreed to receive messages", r["consent_status"] == "opted_in"))
    return items
