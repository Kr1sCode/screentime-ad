import os
import secrets
import sqlite3
from pathlib import Path

from werkzeug.security import generate_password_hash

DATA_DIR = Path(os.environ.get("SCREENTIME_DATA_DIR", "/var/lib/screentime-ad"))
KEY_FILE = DATA_DIR / "db.key"
DB_FILE = DATA_DIR / "screentime.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS daily_usage (
    date TEXT PRIMARY KEY,
    seconds_used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bonus_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS machines (
    hostname TEXT PRIMARY KEY,
    os TEXT,
    last_seen TEXT,
    last_active_seconds INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admin (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL
);
"""


def _get_or_create_key() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    key = secrets.token_hex(32)
    KEY_FILE.write_text(key)
    KEY_FILE.chmod(0o600)
    return key


def get_conn() -> sqlite3.Connection:
    import sqlcipher3

    conn = sqlcipher3.connect(str(DB_FILE))
    conn.execute("PRAGMA key = \"x'%s'\"" % _get_or_create_key())
    conn.row_factory = sqlcipher3.dbapi2.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()

    if not conn.execute("SELECT 1 FROM admin WHERE username = 'admin'").fetchone():
        conn.execute(
            "INSERT INTO admin (username, password_hash) VALUES (?, ?)",
            ("admin", generate_password_hash("admin")),
        )

    defaults = {
        "target_username": "",
        "weekday_limits": '{"mon":120,"tue":120,"wed":120,"thu":120,"fri":120,"sat":180,"sun":180}',
        "blocked_ranges": "[]",
        "agent_token": secrets.token_hex(24),
        "ad_lock_enabled": "0",
        "ldap_host": "",
        "ldap_base_dn": "",
        "ldap_bind_dn": "",
        "ldap_bind_password": "",
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()
