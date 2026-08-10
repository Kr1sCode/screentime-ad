import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Warsaw")
WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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


def get_weekday_limits(conn: sqlite3.Connection) -> dict:
    raw = get_config(conn, "weekday_limits", "")
    if not raw:
        return {k: 120 for k in WEEKDAY_KEYS}
    limits = json.loads(raw)
    return {k: int(limits.get(k, 120)) for k in WEEKDAY_KEYS}


def set_weekday_limits(conn: sqlite3.Connection, limits: dict) -> None:
    clean = {k: max(0, int(limits.get(k, 120))) for k in WEEKDAY_KEYS}
    set_config(conn, "weekday_limits", json.dumps(clean))


def get_blocked_ranges(conn: sqlite3.Connection) -> list:
    return json.loads(get_config(conn, "blocked_ranges", "[]"))


def set_blocked_ranges(conn: sqlite3.Connection, ranges: list) -> None:
    clean = [{"start": r["start"], "end": r["end"]} for r in ranges if r.get("start") and r.get("end")]
    set_config(conn, "blocked_ranges", json.dumps(clean))


def _parse_hhmm(s: str):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _minutes_in_range(now_minutes: int, start_minutes: int, end_minutes: int) -> bool:
    if start_minutes <= end_minutes:
        return start_minutes <= now_minutes < end_minutes
    return now_minutes >= start_minutes or now_minutes < end_minutes  # wraps midnight


def is_blocked_now(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    now = now or datetime.now(TZ)
    now_minutes = now.hour * 60 + now.minute
    for r in get_blocked_ranges(conn):
        if _minutes_in_range(now_minutes, _parse_hhmm(r["start"]), _parse_hhmm(r["end"])):
            return True
    return False


def blocked_range_starts_soon(conn: sqlite3.Connection, within_seconds: int = 300, now: datetime | None = None) -> bool:
    now = now or datetime.now(TZ)
    for r in get_blocked_ranges(conn):
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=_parse_hhmm(r["start"]))
        if start_dt < now:
            start_dt += timedelta(days=1)
        if 0 <= (start_dt - now).total_seconds() <= within_seconds:
            return True
    return False


def should_be_locked(conn: sqlite3.Connection) -> bool:
    return is_blocked_now(conn) or remaining_seconds(conn) <= 0


def get_daily_limit_seconds(conn: sqlite3.Connection, date: str | None = None) -> int:
    date = date or today_str()
    weekday = datetime.strptime(date, "%Y-%m-%d").weekday()  # 0=Mon .. 6=Sun
    key = WEEKDAY_KEYS[weekday]
    return get_weekday_limits(conn)[key] * 60


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
    row = conn.execute("SELECT minutes FROM daily_bonus WHERE date=?", (date,)).fetchone()
    return (row["minutes"] if row else 0) * 60


def _set_bonus_minutes(conn: sqlite3.Connection, date: str, minutes: int) -> None:
    conn.execute(
        "INSERT INTO daily_bonus (date, minutes) VALUES (?, ?) "
        "ON CONFLICT(date) DO UPDATE SET minutes = excluded.minutes",
        (date, minutes),
    )
    conn.commit()


def add_bonus_minutes(conn: sqlite3.Connection, date: str, delta_minutes: int) -> None:
    current = get_bonus_seconds(conn, date) // 60
    _set_bonus_minutes(conn, date, current + delta_minutes)


def set_remaining_minutes(conn: sqlite3.Connection, minutes: int, date: str | None = None) -> None:
    """Ustawia pozostały czas NA TERAZ na dokładnie `minutes` (nie dodaje)."""
    date = date or today_str()
    set_manual_lock(conn, False, date)
    limit = get_daily_limit_seconds(conn, date)
    used = get_used_seconds(conn, date)
    needed_bonus_seconds = max(0, minutes) * 60 - limit + used
    _set_bonus_minutes(conn, date, needed_bonus_seconds // 60)


def is_manually_locked(conn: sqlite3.Connection, date: str | None = None) -> bool:
    date = date or today_str()
    return get_config(conn, "manual_lock_date", "") == date


def set_manual_lock(conn: sqlite3.Connection, locked: bool, date: str | None = None) -> None:
    date = date or today_str()
    set_config(conn, "manual_lock_date", date if locked else "")


def remaining_seconds(conn: sqlite3.Connection, date: str | None = None) -> int:
    date = date or today_str()
    if is_manually_locked(conn, date):
        return 0
    limit = get_daily_limit_seconds(conn, date) + get_bonus_seconds(conn, date)
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
