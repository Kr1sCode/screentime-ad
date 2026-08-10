import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Warsaw")


def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def get_config(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_daily_limit_seconds(conn: sqlite3.Connection) -> int:
    return int(get_config(conn, "daily_limit_minutes", "120")) * 60


def get_used_seconds(conn: sqlite3.Connection, date: str) -> int:
    row = conn.execute("SELECT seconds_used FROM daily_usage WHERE date=?", (date,)).fetchone()
    return row["seconds_used"] if row else 0


def add_used_seconds(conn: sqlite3.Connection, date: str, delta: int) -> None:
    if delta <= 0:
        return
    conn.execute(
        "INSERT INTO daily_usage (date, seconds_used) VALUES (?, ?) "
        "ON CONFLICT(date) DO UPDATE SET seconds_used = seconds_used + excluded.seconds_used",
        (date, delta),
    )
    conn.commit()


def get_bonus_seconds(conn: sqlite3.Connection, date: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(minutes), 0) AS m FROM bonus_events WHERE date=?", (date,)
    ).fetchone()
    return (row["m"] or 0) * 60


def add_bonus(conn: sqlite3.Connection, date: str, minutes: int, note: str = "") -> None:
    conn.execute(
        "INSERT INTO bonus_events (date, minutes, note, created_at) VALUES (?, ?, ?, ?)",
        (date, minutes, note, datetime.now(TZ).isoformat()),
    )
    conn.commit()


def remaining_seconds(conn: sqlite3.Connection, date: str | None = None) -> int:
    date = date or today_str()
    limit = get_daily_limit_seconds(conn) + get_bonus_seconds(conn, date)
    used = get_used_seconds(conn, date)
    return max(0, limit - used)


def touch_machine(conn: sqlite3.Connection, hostname: str, os_name: str, active_seconds: int) -> None:
    conn.execute(
        "INSERT INTO machines (hostname, os, last_seen, last_active_seconds) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(hostname) DO UPDATE SET os=excluded.os, last_seen=excluded.last_seen, "
        "last_active_seconds=excluded.last_active_seconds",
        (hostname, os_name, datetime.now(TZ).isoformat(), active_seconds),
    )
    conn.commit()


def list_machines(conn: sqlite3.Connection):
    return [dict(r) for r in conn.execute("SELECT * FROM machines ORDER BY last_seen DESC").fetchall()]


def history(conn: sqlite3.Connection, days: int = 7):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM daily_usage ORDER BY date DESC LIMIT ?", (days,)
        ).fetchall()
    ]
