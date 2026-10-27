"""Accounts, passwords and sessions for the website (standard library only).

* Passwords: scrypt with a random per-user salt (never stored in plain text); compared in constant time.
* Login: after 5 wrong passwords an account is locked for 5 minutes. Error messages never reveal whether a username exists.
* Sessions: a random token in an HttpOnly cookie; only its SHA-256 hash is stored in the database; sessions expire after 12 hours.
* Roles: 'admin' (everything, including accounts, offers, recipients, WhatsApp setup and live sending) and 'staff'
  (run campaigns in test mode, view analysis and results).
* There is no sign-up page and no default password: accounts are created by an admin (or in the terminal).
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MIN_PASSWORD = 8
MAX_FAILED, LOCK_MINUTES, SESSION_HOURS = 5, 5, 12
ROLES = ("admin", "staff")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
COMMON = {"password", "password1", "12345678", "123456789", "qwertyui", "qwerty123", "iloveyou", "admin123", "letmein1"}
GENERIC_FAILURE = "Invalid username or password, or the account is locked or disabled."


def _iso(dt) -> str:
    return dt.isoformat(timespec="seconds")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32,
                            maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(dk, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


_DUMMY = None


def _dummy_hash() -> str:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("not-a-real-password")
    return _DUMMY


def check_password_rules(password: str, username: str = "") -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"The password must be at least {MIN_PASSWORD} characters.")
    if len(password) > 128:
        raise ValueError("The password is too long (128 characters at most).")
    if password.lower() in COMMON or password.lower() == username.lower():
        raise ValueError("That password is too easy to guess - choose another one.")


def record_event(conn, username, event, detail="", now=None) -> None:
    conn.execute("INSERT INTO auth_events (at, username, event, detail) VALUES (?,?,?,?)", (_iso(now or datetime.now()), username, event, detail))
    conn.commit()


def create_user(conn, username, password, role="staff", display_name=None, now=None):
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise ValueError("The username must be 3-32 characters: letters, digits, dot, dash or underscore.")
    if role not in ROLES:
        raise ValueError("The role must be 'admin' or 'staff'.")
    check_password_rules(password, username)
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise ValueError("That username is already taken.")
    cur = conn.execute("INSERT INTO users (username, display_name, password_hash, role, created_at) VALUES (?,?,?,?,?)",
                       (username, (display_name or username).strip()[:60] or username, hash_password(password), role, _iso(now or datetime.now())))
    conn.commit()
    record_event(conn, username, "user_created", f"role={role}", now)
    return cur.lastrowid


def count_users(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def authenticate(conn, username, password, now=None):
    """Returns (user_row, None) on success, else (None, reason). Reasons are for the audit log only - never shown to the visitor."""
    now = now or datetime.now()
    u = conn.execute("SELECT * FROM users WHERE username=?", ((username or "").strip(),)).fetchone()
    ok = verify_password(password or "", u["password_hash"] if u else _dummy_hash())     # same work whether or not the user exists
    if u is None:
        return None, "unknown_user"
    if u["locked_until"] and u["locked_until"] > _iso(now):
        return None, "locked"
    if not u["is_active"]:
        return None, "disabled"
    if not ok:
        failed = u["failed_attempts"] + 1
        if failed >= MAX_FAILED:
            conn.execute("UPDATE users SET failed_attempts=0, locked_until=? WHERE user_id=?", (_iso(now + timedelta(minutes=LOCK_MINUTES)), u["user_id"]))
            record_event(conn, u["username"], "locked", f"{MAX_FAILED} wrong passwords", now)
        else:
            conn.execute("UPDATE users SET failed_attempts=? WHERE user_id=?", (failed, u["user_id"]))
        conn.commit()
        return None, "bad_password"
    conn.execute("UPDATE users SET failed_attempts=0, locked_until=NULL, last_login_at=? WHERE user_id=?", (_iso(now), u["user_id"]))
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE user_id=?", (u["user_id"],)).fetchone(), None


def create_session(conn, user_id, now=None) -> str:
    now = now or datetime.now()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(now),))              # tidy up expired sessions
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                 (hashlib.sha256(token.encode()).hexdigest(), user_id, _iso(now), _iso(now + timedelta(hours=SESSION_HOURS))))
    conn.commit()
    return token


def session_user(conn, token, now=None):
    if not token:
        return None
    return conn.execute("SELECT u.* FROM sessions s JOIN users u ON u.user_id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.is_active=1",
                        (hashlib.sha256(token.encode()).hexdigest(), _iso(now or datetime.now()))).fetchone()


def end_session(conn, token) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        conn.commit()


def _active_admins(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1").fetchone()[0]


def set_password(conn, user_id, password, now=None) -> None:
    u = conn.execute("SELECT username FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not u:
        raise ValueError("User not found.")
    check_password_rules(password, u["username"])
    conn.execute("UPDATE users SET password_hash=?, failed_attempts=0, locked_until=NULL WHERE user_id=?", (hash_password(password), user_id))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))                      # a password change signs the person out everywhere
    conn.commit()
    record_event(conn, u["username"], "password_changed", "", now)


def set_active(conn, user_id, active: bool, now=None) -> None:
    u = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not u:
        raise ValueError("User not found.")
    if not active and u["role"] == "admin" and u["is_active"] and _active_admins(conn) <= 1:
        raise ValueError("You cannot disable the last active admin.")
    conn.execute("UPDATE users SET is_active=? WHERE user_id=?", (int(active), user_id))
    if not active:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()
    record_event(conn, u["username"], "user_enabled" if active else "user_disabled", "", now)


def set_role(conn, user_id, role, now=None) -> None:
    u = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not u or role not in ROLES:
        raise ValueError("User or role not found.")
    if role != "admin" and u["role"] == "admin" and u["is_active"] and _active_admins(conn) <= 1:
        raise ValueError("You cannot remove the last active admin.")
    conn.execute("UPDATE users SET role=? WHERE user_id=?", (role, user_id))
    conn.commit()
    record_event(conn, u["username"], "role_changed", f"role={role}", now)


def list_users(conn):
    return conn.execute("SELECT user_id, username, display_name, role, is_active, last_login_at, created_at FROM users ORDER BY username").fetchall()
