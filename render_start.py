"""Start script for Render only. Builds demo data, creates the admin, starts the site."""
import os

# Allow Render's own domain in the Host check (Render sets this variable itself).
_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
if _host:
    _old = os.environ.get("ALLOWED_HOSTS", "")
    os.environ["ALLOWED_HOSTS"] = f"{_old},{_host}" if _old else _host
os.environ.setdefault("HOST", "0.0.0.0")
os.environ["WHATSAPP_ENABLED"] = "false"   # never send real messages from the public site

from src import auth, database, webapp          # noqa: E402  (must come after the lines above)
from src.main import main as cli                 # noqa: E402

cli(["setup-demo"])
try:
    cli(["segments"])
except Exception as e:                           # demo still works without segments
    print("Segments step skipped:", e)

conn = database.connect()
database.init_db(conn)
if auth.count_users(conn) == 0:
    pw = os.environ.get("ADMIN_PASSWORD", "")
    if len(pw) < 8:
        raise SystemExit("Set ADMIN_PASSWORD (at least 8 characters) in Render's environment settings.")
    auth.create_user(conn, os.environ.get("ADMIN_USERNAME", "admin"), pw, "admin", "Admin")
conn.close()

webapp.serve(port=int(os.environ.get("PORT", "8000")))