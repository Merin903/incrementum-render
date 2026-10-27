import builtins
import contextlib
import http.client
import io
import pathlib
import re
import tempfile
import threading
from datetime import datetime, timedelta
from urllib.parse import urlencode

from src import auth, config, database, demo_data, offers, powerbi_export, webapp
from .helpers import TODAY, FakeLiveProvider, raises, set_env
from .test_webapp import PAGES, PASSWORD, Site, NF


def raw(site, method, path, form=None, cookie=None, token=True, host=None):
    """Low-level request that also returns the response headers."""
    c = http.client.HTTPConnection("127.0.0.1", site.port, timeout=15)
    headers, body = {"Host": host or f"127.0.0.1:{site.port}"}, None
    if cookie:
        headers["Cookie"] = cookie
    if form is not None:
        f = dict(form)
        if token:
            f["token"] = site.app.token
        body = urlencode(f, doseq=True)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    return r.status, r.read().decode(), {k.lower(): v for k, v in r.getheaders()}


def login(site, username="boss", password=PASSWORD, nxt=None):
    form = {"username": username, "password": password}
    if nxt:
        form["next"] = nxt
    return raw(site, "POST", "/login", form)


def cookie_of(headers):
    return headers["set-cookie"].split(";")[0]


def second_user(site, username, role="staff", name=None):
    conn = database.connect(site.db)
    uid = auth.create_user(conn, username, PASSWORD, role, name)
    cookie = "session=" + auth.create_session(conn, uid)
    conn.close()
    return uid, cookie


# ---------------------------------------------------------------- the login wall
def test_every_page_and_action_redirects_to_the_login_page_when_signed_out():
    s = Site()
    try:
        for path in PAGES + ["/customer?id=C-MERIN", "/results?group=Persuadable"]:
            st, body, h = raw(s, "GET", path)
            assert st == 303 and h["location"].startswith("/login?next=") and body == "", path          # nothing leaks
        for path in ("/send", "/offers/add", "/users/create", "/generate/send", "/setup/test-message", "/logout"):
            st, body, h = raw(s, "POST", path, {"category": NF})
            assert st == 303 and h["location"].startswith("/login"), path
        assert s.rows("SELECT COUNT(*) FROM notification_logs")[0][0] == 0
        st, body, _ = raw(s, "GET", "/login")
        assert st == 200 and "Sign in" in body and "name='username'" in body and "name='password'" in body and "href='/send'" not in body
    finally:
        s.close()


def test_signing_in_sets_a_safe_cookie_and_returns_to_the_page_you_wanted():
    s = Site()
    try:
        st, _, h = raw(s, "GET", "/send?c=All%20categories")
        assert "next=/send%3Fc%3DAll%2520categories" in h["location"]
        st, body, h = login(s, nxt="/send?c=All%20categories")
        assert st == 303 and h["location"] == "/send?c=All%20categories"
        flags = h["set-cookie"]
        assert "HttpOnly" in flags and "SameSite=Lax" in flags and "Path=/" in flags and "Max-Age=43200" in flags
        cookie = cookie_of(h)
        assert raw(s, "GET", "/send", cookie=cookie)[0] == 200
        token = cookie.split("=", 1)[1]
        assert token not in " ".join(str(tuple(r)) for r in s.rows("SELECT * FROM sessions"))      # only a hash is stored
        assert raw(s, "GET", "/login", cookie=cookie)[0] == 303                                       # already signed in -> home
    finally:
        s.close()


def test_wrong_credentials_get_one_generic_message_and_no_cookie():
    s = Site()
    try:
        bad_pw = raw(s, "POST", "/login", {"username": "boss", "password": "wrong-password"})
        bad_user = raw(s, "POST", "/login", {"username": "nobody", "password": "wrong-password"})
        for st, body, h in (bad_pw, bad_user):
            assert st == 200 and auth.GENERIC_FAILURE in body and "set-cookie" not in h
        strip = lambda b: re.sub(r"value='[^']*'", "value=''", b)                                     # ignore only what the visitor typed
        assert strip(bad_pw[1]) == strip(bad_user[1])                                                # otherwise identical: no username guessing
        events = {(r[0], r[1]) for r in s.rows("SELECT event, username FROM auth_events WHERE event='login_failed'")}
        assert {("login_failed", "boss"), ("login_failed", "nobody")} <= events
    finally:
        s.close()


def test_login_form_needs_the_site_token_and_the_right_host():
    s = Site()
    try:
        assert raw(s, "POST", "/login", {"username": "boss", "password": PASSWORD}, token=False)[0] == 403
        assert raw(s, "POST", "/login", {"username": "boss", "password": PASSWORD}, host="evil.example.com")[0] == 403
        assert raw(s, "GET", "/login", host="evil.example.com")[0] == 403
    finally:
        s.close()


def test_five_wrong_passwords_lock_the_account_even_for_the_right_password():
    s = Site()
    try:
        for _ in range(auth.MAX_FAILED):
            raw(s, "POST", "/login", {"username": "boss", "password": "wrong-password"})
        st, body, h = login(s)
        assert st == 200 and "set-cookie" not in h and auth.GENERIC_FAILURE in body
        s.exec("UPDATE users SET locked_until=NULL WHERE username='boss'")                            # (the lock also expires by itself after 5 minutes)
        assert login(s)[0] == 303
    finally:
        s.close()


def test_login_cannot_be_used_to_redirect_to_another_website():
    s = Site()
    try:
        for evil in ("//evil.example.com", "http://evil.example.com", "https://evil.example.com/x", "\\\\evil.example.com", "/ok\nSet-Cookie: x=1"):
            st, _, h = login(s, nxt=evil)
            assert st == 303 and h["location"] == "/", evil
        assert login(s, nxt="/results#groups")[2]["location"] == "/results#groups"
        assert webapp.safe_next("/segments?seg=1") == "/segments?seg=1" and webapp.safe_next("segments") == "/"
    finally:
        s.close()


def test_logout_ends_the_session_and_needs_the_token():
    s = Site()
    try:
        cookie = cookie_of(login(s)[2])
        assert raw(s, "POST", "/logout", {}, cookie=cookie, token=False)[0] == 403
        assert raw(s, "GET", "/", cookie=cookie)[0] == 200                                           # still signed in
        st, _, h = raw(s, "POST", "/logout", {}, cookie=cookie)
        assert st == 303 and h["location"] == "/login" and "Max-Age=0" in h["set-cookie"]
        assert raw(s, "GET", "/", cookie=cookie)[0] == 303                                           # the old cookie no longer works
        assert "logout" in {r[0] for r in s.rows("SELECT event FROM auth_events")}
    finally:
        s.close()


def test_expired_forged_and_malformed_sessions_are_rejected():
    s = Site()
    try:
        cookie = cookie_of(login(s)[2])
        s.exec("UPDATE sessions SET expires_at='2000-01-01T00:00:00'")
        assert raw(s, "GET", "/", cookie=cookie)[0] == 303
        for bad in ("session=forged", "session=", "other=1", "session=\x00", "garbage;;;===", ""):
            assert raw(s, "GET", "/", cookie=bad or None)[0] == 303, bad
    finally:
        s.close()


def test_security_headers_are_sent():
    s = Site()
    try:
        _, _, h = raw(s, "GET", "/", cookie=s.cookie)
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff" and h["cache-control"] == "no-store"
        assert h["referrer-policy"] == "same-origin"
        assert raw(s, "GET", "/login")[2]["x-frame-options"] == "DENY"
    finally:
        s.close()


def test_login_page_explains_how_to_create_the_first_account_when_none_exist():
    path = pathlib.Path(tempfile.mkdtemp()) / "empty.db"
    conn = database.connect(path); database.init_db(conn); conn.close()
    app = webapp.App(path, today=TODAY)
    srv = webapp.make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        c.request("GET", "/login", headers={"Host": f"127.0.0.1:{port}"})
        body = c.getresponse().read().decode()
        assert "No accounts exist yet" in body and "create-user --username" in body
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- roles
def test_staff_can_run_campaigns_but_not_manage_offers_recipients_setup_or_users():
    s = Site(role="staff")
    try:
        for path in ("/", "/send", "/generate", "/analysis", "/segments", "/results", "/notifications"):
            assert raw(s, "GET", path, cookie=s.cookie)[0] == 200, path
        for path in ("/users", "/setup", "/recipients", "/offers"):
            st, body, _ = raw(s, "GET", path, cookie=s.cookie)
            assert st == 403 and "does not have access" in body, path
        home = raw(s, "GET", "/", cookie=s.cookie)[1]
        for hidden in ("href=\"/users\"", "href=\"/setup\"", "href=\"/recipients\"", "href=\"/offers\""):
            assert hidden not in home                                                                # navigation hides them
        assert "Staffer" in home and "staff" in home and "Log out" in home
        before = s.rows("SELECT COUNT(*) FROM offers")[0][0]
        for path, form in (("/offers/add", {"stock_code": "DET500", "discount": "5", "valid_from": "2026-10-01", "valid_to": "2026-12-31"}),
                           ("/offers/toggle", {"id": "1"}), ("/recipients/consent", {"action": "grant_all", "yes": "1"}),
                           ("/users/create", {"username": "sneaky", "password": PASSWORD, "role": "admin"}),
                           ("/users/role", {"id": "1", "role": "staff"}), ("/setup/test-message", {"name": "Merin", "confirm": "yes"}),
                           ("/setup/check-login", {})):
            assert raw(s, "POST", path, form, cookie=s.cookie)[0] == 403, path
        assert s.rows("SELECT COUNT(*) FROM offers")[0][0] == before and s.rows("SELECT COUNT(*) FROM users WHERE username='sneaky'")[0][0] == 0
        s.post("/send", {"category": "All categories", "mode": "simulated"})                          # staff may send in test mode
        assert s.rows("SELECT COUNT(*) FROM notification_logs WHERE status='simulated'")[0][0] == 5
    finally:
        s.close()


def test_only_admins_can_send_to_phones_even_when_switched_on():
    s = Site(role="staff")
    try:
        with set_env({"DEMO_PHONE_MERIN": "+919876501234", "DEMO_PHONE_HANNA": "+919876505678"}, whatsapp_enabled=True):
            page = raw(s, "GET", "/send", cookie=s.cookie)[1]
            assert "admins only" in page and 'value="live" disabled' in page
            body = s.post("/send", {"category": NF, "mode": "live", "confirm": "yes"})
            assert "Only admins can send to phones" in body and s.provider.sent == []
            assert s.rows("SELECT COUNT(*) FROM notification_logs")[0][0] == 0
    finally:
        s.close()
    a = Site(role="admin")
    try:
        with set_env({"DEMO_PHONE_MERIN": "+919876501234", "DEMO_PHONE_HANNA": "+919876505678"}, whatsapp_enabled=True):
            assert 'value="live" disabled' not in raw(a, "GET", "/send", cookie=a.cookie)[1]
            a.post("/send", {"category": NF, "mode": "live", "confirm": "yes"})
            assert len(a.provider.sent) == 2
    finally:
        a.close()


# ---------------------------------------------------------------- account management (admin)
def test_admin_sees_users_and_sign_in_activity_and_can_create_accounts():
    s = Site()
    try:
        page = s.get("/users")
        assert "Accounts" in page and "boss (you)" in page and "Recent sign-in activity" in page and "user created" in page
        assert "scrypt" not in page and PASSWORD not in page                                          # hashes and passwords never shown
        body = s.post("/users/create", {"username": "hanna.k", "display_name": "Hanna K", "role": "staff", "password": PASSWORD})
        assert "hanna.k</b> created" in body and "Hanna K" in body
        for bad, msg in (({"username": "x", "password": PASSWORD}, "3-32 characters"), ({"username": "okname", "password": "short"}, "at least 8"),
                         ({"username": "hanna.k", "password": PASSWORD}, "already taken"), ({"username": "okname", "password": "password"}, "too easy"),
                         ({"username": "okname", "password": PASSWORD, "role": "owner"}, "The role must be")):
            assert msg in s.post("/users/create", {"role": "staff", **bad}), bad
        st, body, h = login(s, "hanna.k")                                                              # the new account works, as staff
        assert st == 303
        assert raw(s, "GET", "/users", cookie=cookie_of(h))[0] == 403
        assert raw(s, "GET", "/send", cookie=cookie_of(h))[0] == 200
    finally:
        s.close()


def test_disabling_resetting_and_protecting_accounts():
    s = Site()
    try:
        uid, staff_cookie = second_user(s, "staffer")
        assert raw(s, "GET", "/send", cookie=staff_cookie)[0] == 200
        assert "disabled and signed out" in s.post("/users/toggle", {"id": str(uid), "action": "disable"})
        assert raw(s, "GET", "/send", cookie=staff_cookie)[0] == 303 and login(s, "staffer")[1].count(auth.GENERIC_FAILURE) == 1
        s.post("/users/toggle", {"id": str(uid), "action": "enable"})
        assert login(s, "staffer")[0] == 303
        cookie = cookie_of(login(s, "staffer")[2])
        assert "signed out everywhere" in s.post("/users/password", {"id": str(uid), "password": "Brand-new-pass-9"})
        assert raw(s, "GET", "/send", cookie=cookie)[0] == 303                                         # old sessions ended
        assert login(s, "staffer", PASSWORD)[0] == 200 and login(s, "staffer", "Brand-new-pass-9")[0] == 303
        assert "at least 8" in s.post("/users/password", {"id": str(uid), "password": "tiny"})
        assert "cannot disable your own" in s.post("/users/toggle", {"id": str(s.admin_id), "action": "disable"})
        assert "cannot change your own role" in s.post("/users/role", {"id": str(s.admin_id), "role": "staff"})
        assert "Role updated" in s.post("/users/role", {"id": str(uid), "role": "admin"})
        assert raw(s, "GET", "/users", cookie=cookie_of(login(s, "staffer", "Brand-new-pass-9")[2]))[0] == 200     # now an admin
    finally:
        s.close()


def test_admins_can_manage_other_admins_but_the_site_always_keeps_an_active_admin():
    s = Site()
    try:
        other, _ = second_user(s, "second", "admin")
        assert "Account disabled" in s.post("/users/toggle", {"id": str(other), "action": "disable"})      # allowed: 'boss' remains
        assert s.rows("SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1")[0][0] == 1
        assert "cannot disable your own" in s.post("/users/toggle", {"id": str(s.admin_id), "action": "disable"})
        assert "cannot change your own role" in s.post("/users/role", {"id": str(s.admin_id), "role": "staff"})
        assert s.rows("SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1")[0][0] == 1
        assert "Role updated" in s.post("/users/role", {"id": str(other), "role": "staff"})
    finally:
        s.close()


def test_notices_are_private_to_each_signed_in_person():
    s = Site()
    try:
        _, staff_cookie = second_user(s, "staffer")
        st, _, _ = raw(s, "POST", "/offers/toggle", {"id": "99999"}, cookie=s.cookie)               # produces the notice "Offer not found."
        assert st == 303
        assert "Offer not found." not in raw(s, "GET", "/send", cookie=staff_cookie)[1]              # not shown to someone else
        assert "Offer not found." in raw(s, "GET", "/offers", cookie=s.cookie)[1]                    # shown to the person who acted
        assert "Offer not found." not in raw(s, "GET", "/offers", cookie=s.cookie)[1]                # ...and only once
    finally:
        s.close()


def test_names_on_accounts_are_escaped_everywhere():
    s = Site()
    try:
        s.post("/users/create", {"username": "xss.user", "display_name": "<script>alert(1)</script>", "role": "staff", "password": PASSWORD})
        assert "<script>alert(1)</script>" not in s.get("/users") and "&lt;script&gt;" in s.get("/users")
        cookie = cookie_of(login(s, "xss.user")[2])
        assert "<script>alert(1)</script>" not in raw(s, "GET", "/", cookie=cookie)[1]                  # the header shows the name safely
        raw(s, "POST", "/login", {"username": "<img src=x onerror=alert(1)>", "password": "x"})
        assert "<img src=x" not in s.get("/users")                                                      # attempted usernames in the activity log too
    finally:
        s.close()


# ---------------------------------------------------------------- accounts stay private and survive the demo resets
def test_account_data_is_never_exported_and_survives_the_demo_resets():
    s = Site()
    try:
        conn = database.connect(s.db)
        out = pathlib.Path(tempfile.mkdtemp())
        powerbi_export.export_powerbi(conn, out, out / "n", out / "n")
        blob = "".join(p.read_text() for p in out.glob("*.csv"))
        assert "scrypt$" not in blob and "boss" not in blob
        assert not (out / "users.csv").exists() and not (out / "sessions.csv").exists() and not (out / "auth_events.csv").exists()
        n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        demo_data.reset_demo(conn)
        demo_data.reset_campaigns(conn, archive_dir=out)
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == n and conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 1
        conn.close()
        assert raw(s, "GET", "/", cookie=s.cookie)[0] == 200                                              # still signed in after the resets
    finally:
        s.close()


# ---------------------------------------------------------------- terminal commands
def test_terminal_account_commands_ask_for_the_password_hidden_and_check_it_twice():
    from src import main
    import getpass
    conn = database.connect(":memory:"); database.init_db(conn)
    answers, old = [], getpass.getpass
    getpass.getpass = lambda prompt="": answers.pop(0)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            answers[:] = [PASSWORD, "different-typo"]
            main.cmd_create_user(type("A", (), {"username": "merin", "role": "admin", "name": "Merin"})(), conn)
            assert "did not match" in out.getvalue() and auth.count_users(conn) == 0
            answers[:] = [PASSWORD, PASSWORD]
            main.cmd_create_user(type("A", (), {"username": "merin", "role": "admin", "name": "Merin"})(), conn)
            assert auth.count_users(conn) == 1 and "created with role 'admin'" in out.getvalue()
            answers[:] = ["short", "short"]
            main.cmd_create_user(type("A", (), {"username": "other", "role": "staff", "name": None})(), conn)
            assert "Not created" in out.getvalue() and auth.count_users(conn) == 1
            answers[:] = ["Another-pass-77", "Another-pass-77"]
            main.cmd_set_password(type("A", (), {"username": "merin"})(), conn)
            assert "Password changed" in out.getvalue() and auth.authenticate(conn, "merin", "Another-pass-77")[0]
            main.cmd_set_password(type("A", (), {"username": "ghost"})(), conn)
            assert "No such account" in out.getvalue()
            main.cmd_list_users(None, conn)
            assert "merin" in out.getvalue() and "Another-pass-77" not in out.getvalue() and "scrypt" not in out.getvalue()
    finally:
        getpass.getpass = old
    p = main.build_parser()
    a = p.parse_args(["create-user", "--username", "x", "--role", "admin"])
    assert a.role == "admin" and p.parse_args(["create-user", "--username", "x"]).role == "staff"
    assert not any(o.option_strings == ["--password"] for sub in p._subparsers._group_actions[0].choices.values() for o in sub._actions)   # no password on the command line


def test_the_username_is_kept_after_a_failed_sign_in_and_shown_safely():
    s = Site()
    try:
        st, body, _ = raw(s, "POST", "/login", {"username": "boss", "password": "wrong-password"})
        assert "value='boss'" in body and "wrong-password" not in body                  # username kept, the password never echoed back
        st, body, _ = raw(s, "POST", "/login", {"username": "'><script>alert(1)</script>", "password": "x"})
        assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body
        assert "bad password" in s.get("/users")                                         # the activity log reads as words, not codes
    finally:
        s.close()
