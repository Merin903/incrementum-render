"""SQLite access helpers: connect, create tables, inspect."""
import sqlite3
from datetime import datetime

from . import config


def connect(path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path) if path is not None else str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn) -> None:
    conn.executescript(config.SCHEMA_FILE.read_text())
    conn.commit()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def inspect_db(conn) -> list:
    """Table names, row counts and column names - used by `inspect-db`."""
    out = []
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for n in names:
        rows = conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0]
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({n})")]
        out.append({"table": n, "rows": rows, "columns": cols})
    return out


def start_run(conn, stage, mode, notes=None) -> int:
    """Record a processing run; mode is 'A_hadoop_spark' or 'B_local_python'."""
    rid = conn.execute("INSERT INTO processing_runs (stage, execution_mode, started_at, notes) VALUES (?,?,?,?)",
                       (stage, mode, now_iso(), notes)).lastrowid
    conn.commit()
    return rid


def finish_run(conn, run_id, started_epoch, rows_in=None, rows_out=None, status="success", notes=None):
    import time
    conn.execute("UPDATE processing_runs SET finished_at=?, duration_seconds=?, rows_in=?, rows_out=?, status=?, "
                 "notes=COALESCE(?, notes) WHERE run_id=?",
                 (now_iso(), round(time.time() - started_epoch, 2), rows_in, rows_out, status, notes, run_id))
    conn.commit()
