from datetime import datetime, timedelta

from src import auth, database
from .helpers import raises

NOW = datetime(2026, 10, 21, 10, 0, 0)
GOOD = "Correct-horse-42"


def db():
    c = database.connect(":memory:")
    database.init_db(c)
    return c


def test_passwords_are_hashed_salted_and_verified_in_constant_time_form():
    h1, h2 = auth.hash_password(GOOD), auth.hash_password(GOOD)
    assert h1 != h2 and GOOD not in h1 and h1.startswith("scrypt$")              # random salt, never the plain text
    assert auth.verify_password(GOOD, h1) and not auth.verify_password("wrong-password", h1)
    for broken in ("", "plain", "scrypt$1$2", "md5$a$b$c$d$e", "scrypt$x$y$z$!!$??"):
        assert auth.verify_password(GOOD, broken) is False


def test_username_and_password_rules():
    c = db()
    assert raises(ValueError, auth.create_user, c, "ab", GOOD)                     # too short
    assert raises(ValueError, auth.create_user, c, "has space", GOOD)
    assert raises(ValueError, auth.create_user, c, "merin", "short1")               # password too short
    assert raises(ValueError, auth.create_user, c, "merin", "password")             # too common
    assert raises(ValueError, auth.create_user, c, "merin_user", "Merin_User")     # equals the username (any case)
    assert raises(ValueError, auth.create_user, c, "merin", GOOD, role="owner")
    assert raises(ValueError, auth.create_user, c, "merin", "x" * 129)
    assert auth.count_users(c) == 0


def test_usernames_are_unique_ignoring_case_and_passwords_are_never_stored_plainly():
    c = db()
    auth.create_user(c, "Merin", GOOD, "admin", "Merin B")
    assert raises(ValueError, auth.create_user, c, "merin", GOOD)
    row = c.execute("SELECT * FROM users").fetchone()
    assert GOOD not in " ".join(str(v) for v in tuple(row)) and row["display_name"] == "Merin B" and row["role"] == "admin"


def test_login_success_failure_and_no_hint_about_which_part_was_wrong():
    c = db()
    auth.create_user(c, "merin", GOOD, "admin")
    user, why = auth.authenticate(c, "MERIN", GOOD, NOW)                           # username is case-insensitive
    assert user["username"] == "merin" and why is None and user["last_login_at"] == "2026-10-21T10:00:00"
    assert auth.authenticate(c, "merin", "wrong-password", NOW)[0] is None
    assert auth.authenticate(c, "nobody", GOOD, NOW)[0] is None
    assert auth.authenticate(c, "", "", NOW)[0] is None and auth.authenticate(c, None, None, NOW)[0] is None
    assert "locked" in auth.GENERIC_FAILURE and "username" in auth.GENERIC_FAILURE        # one generic message for every failure


def test_account_locks_after_five_wrong_passwords_and_unlocks_after_five_minutes():
    c = db()
    auth.create_user(c, "merin", GOOD, "staff")
    for i in range(auth.MAX_FAILED):
        assert auth.authenticate(c, "merin", "wrong-password", NOW + timedelta(seconds=i))[1] in ("bad_password",)
    assert auth.authenticate(c, "merin", GOOD, NOW + timedelta(minutes=1)) == (None, "locked")      # even the right password is refused
    assert auth.authenticate(c, "merin", GOOD, NOW + timedelta(minutes=4, seconds=50)) == (None, "locked")
    user, why = auth.authenticate(c, "merin", GOOD, NOW + timedelta(minutes=6))
    assert user and why is None                                                    # lock expired
    assert c.execute("SELECT failed_attempts, locked_until FROM users").fetchone()[:] == (0, None)
    assert "locked" in {r["event"] for r in c.execute("SELECT event FROM auth_events")}


def test_a_successful_login_resets_the_failed_attempt_counter():
    c = db()
    auth.create_user(c, "merin", GOOD)
    for _ in range(4):
        auth.authenticate(c, "merin", "wrong-password", NOW)
    assert auth.authenticate(c, "merin", GOOD, NOW)[0]
    for _ in range(4):
        auth.authenticate(c, "merin", "wrong-password", NOW)                       # would have locked without the reset
    assert auth.authenticate(c, "merin", GOOD, NOW)[0]


def test_sessions_store_only_a_hash_expire_and_can_be_ended():
    c = db()
    uid = auth.create_user(c, "merin", GOOD, "admin")
    token = auth.create_session(c, uid, NOW)
    stored = " ".join(str(tuple(r)) for r in c.execute("SELECT * FROM sessions"))
    assert token not in stored and len(token) >= 40                                # the cookie value is not in the database
    assert auth.session_user(c, token, NOW + timedelta(hours=1))["username"] == "merin"
    assert auth.session_user(c, token, NOW + timedelta(hours=auth.SESSION_HOURS, seconds=1)) is None       # expired
    assert auth.session_user(c, "forged-token", NOW) is None and auth.session_user(c, "", NOW) is None and auth.session_user(c, None, NOW) is None
    auth.end_session(c, token)
    assert auth.session_user(c, token, NOW) is None


def test_disabled_users_cannot_log_in_and_lose_their_sessions():
    c = db()
    auth.create_user(c, "boss", GOOD, "admin")
    uid = auth.create_user(c, "staffer", GOOD, "staff")
    token = auth.create_session(c, uid, NOW)
    auth.set_active(c, uid, False, NOW)
    assert auth.session_user(c, token, NOW) is None
    assert auth.authenticate(c, "staffer", GOOD, NOW) == (None, "disabled")
    auth.set_active(c, uid, True, NOW)
    assert auth.authenticate(c, "staffer", GOOD, NOW)[0]


def test_the_last_active_admin_cannot_be_disabled_or_demoted():
    c = db()
    a1 = auth.create_user(c, "admin1", GOOD, "admin")
    assert raises(ValueError, auth.set_active, c, a1, False)
    assert raises(ValueError, auth.set_role, c, a1, "staff")
    a2 = auth.create_user(c, "admin2", GOOD, "admin")
    auth.set_role(c, a1, "staff")                                                  # fine now: another admin remains
    assert raises(ValueError, auth.set_active, c, a2, False)
    assert raises(ValueError, auth.set_role, c, 999, "admin") and raises(ValueError, auth.set_role, c, a2, "owner")


def test_password_change_signs_the_person_out_clears_lock_and_checks_the_rules():
    c = db()
    uid = auth.create_user(c, "merin", GOOD)
    token = auth.create_session(c, uid, NOW)
    for _ in range(auth.MAX_FAILED):
        auth.authenticate(c, "merin", "wrong-password", NOW)
    assert raises(ValueError, auth.set_password, c, uid, "short")
    auth.set_password(c, uid, "Another-pass-77", NOW)
    assert auth.session_user(c, token, NOW) is None                                # old sessions are gone
    assert auth.authenticate(c, "merin", GOOD, NOW)[0] is None
    assert auth.authenticate(c, "merin", "Another-pass-77", NOW)[0]                # not locked any more
    assert raises(ValueError, auth.set_password, c, 999, "Another-pass-77")


def test_the_audit_trail_records_events_without_any_password():
    c = db()
    uid = auth.create_user(c, "merin", GOOD, "admin")
    auth.set_password(c, uid, "Another-pass-77", NOW)
    auth.authenticate(c, "merin", "wrong-password", NOW)
    events = c.execute("SELECT * FROM auth_events").fetchall()
    assert {"user_created", "password_changed"} <= {e["event"] for e in events}
    assert GOOD not in " ".join(str(tuple(e)) for e in events) and "Another-pass-77" not in " ".join(str(tuple(e)) for e in events)
