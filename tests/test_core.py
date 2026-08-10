"""Self-check for app/core.py quota math. Run: python3 tests/test_core.py"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import core  # noqa: E402


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE daily_usage (date TEXT PRIMARY KEY, seconds_used INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE bonus_events (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
                                    minutes INTEGER NOT NULL, note TEXT, created_at TEXT NOT NULL);
        CREATE TABLE machines (hostname TEXT PRIMARY KEY, os TEXT, last_seen TEXT, last_active_seconds INTEGER DEFAULT 0);
        """
    )
    core.set_config(conn, "daily_limit_minutes", "120")
    return conn


def test_fresh_day_has_full_quota():
    conn = make_conn()
    assert core.remaining_seconds(conn, "2026-08-10") == 120 * 60


def test_usage_reduces_remaining():
    conn = make_conn()
    core.add_used_seconds(conn, "2026-08-10", 60 * 60)
    assert core.remaining_seconds(conn, "2026-08-10") == 60 * 60


def test_usage_never_goes_negative():
    conn = make_conn()
    core.add_used_seconds(conn, "2026-08-10", 999 * 60)
    assert core.remaining_seconds(conn, "2026-08-10") == 0


def test_bonus_extends_quota():
    conn = make_conn()
    core.add_used_seconds(conn, "2026-08-10", 120 * 60)
    assert core.remaining_seconds(conn, "2026-08-10") == 0
    core.add_bonus(conn, "2026-08-10", 15, "test bonus")
    assert core.remaining_seconds(conn, "2026-08-10") == 15 * 60


def test_lock_now_zeroes_remaining_via_negative_bonus():
    conn = make_conn()
    core.add_bonus(conn, "2026-08-10", -1_000_000, "zablokuj teraz")
    assert core.remaining_seconds(conn, "2026-08-10") == 0


def test_days_are_independent():
    conn = make_conn()
    core.add_used_seconds(conn, "2026-08-10", 120 * 60)
    assert core.remaining_seconds(conn, "2026-08-11") == 120 * 60


def test_weekday_limits_apply_per_day():
    conn = make_conn()
    core.set_weekday_limits(conn, {"mon": 30, "tue": 120, "wed": 120, "thu": 120,
                                     "fri": 120, "sat": 240, "sun": 240})
    assert core.remaining_seconds(conn, "2026-08-10") == 30 * 60  # 2026-08-10 = poniedziałek
    assert core.remaining_seconds(conn, "2026-08-15") == 240 * 60  # sobota


def test_blocked_range_same_day():
    conn = make_conn()
    core.set_blocked_ranges(conn, [{"start": "13:00", "end": "14:00"}])
    assert core.is_blocked_now(conn, datetime(2026, 8, 10, 13, 30, tzinfo=core.TZ))
    assert not core.is_blocked_now(conn, datetime(2026, 8, 10, 12, 30, tzinfo=core.TZ))


def test_blocked_range_wraps_midnight():
    conn = make_conn()
    core.set_blocked_ranges(conn, [{"start": "22:00", "end": "07:00"}])
    assert core.is_blocked_now(conn, datetime(2026, 8, 10, 23, 0, tzinfo=core.TZ))
    assert core.is_blocked_now(conn, datetime(2026, 8, 10, 6, 0, tzinfo=core.TZ))
    assert not core.is_blocked_now(conn, datetime(2026, 8, 10, 12, 0, tzinfo=core.TZ))


def test_blocked_range_starts_soon_warns():
    conn = make_conn()
    core.set_blocked_ranges(conn, [{"start": "22:00", "end": "07:00"}])
    assert core.blocked_range_starts_soon(conn, 300, datetime(2026, 8, 10, 21, 57, tzinfo=core.TZ))
    assert not core.blocked_range_starts_soon(conn, 300, datetime(2026, 8, 10, 21, 30, tzinfo=core.TZ))


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} testów przeszło.")
