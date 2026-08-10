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
        CREATE TABLE daily_bonus (date TEXT PRIMARY KEY, minutes INTEGER NOT NULL DEFAULT 0);
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
    core.add_bonus_minutes(conn, "2026-08-10", 15)
    assert core.remaining_seconds(conn, "2026-08-10") == 15 * 60


def test_add_bonus_minutes_is_additive_not_absolute():
    conn = make_conn()
    core.add_bonus_minutes(conn, "2026-08-10", 15)
    core.add_bonus_minutes(conn, "2026-08-10", 15)
    assert core.get_bonus_seconds(conn, "2026-08-10") == 30 * 60


def test_set_remaining_minutes_is_absolute_not_additive():
    conn = make_conn()
    core.add_used_seconds(conn, "2026-08-10", 60 * 60)  # zużyte 60 z 120
    core.set_remaining_minutes(conn, 65, "2026-08-10")
    assert core.remaining_seconds(conn, "2026-08-10") == 65 * 60
    # wywołane drugi raz z tą samą wartością nie dokłada kolejnych 65
    core.set_remaining_minutes(conn, 65, "2026-08-10")
    assert core.remaining_seconds(conn, "2026-08-10") == 65 * 60


def test_manual_lock_zeroes_remaining_and_is_reversible():
    conn = make_conn()
    core.set_manual_lock(conn, True, "2026-08-10")
    assert core.remaining_seconds(conn, "2026-08-10") == 0
    core.set_manual_lock(conn, False, "2026-08-10")
    assert core.remaining_seconds(conn, "2026-08-10") == 120 * 60


def test_manual_lock_is_scoped_to_its_day():
    conn = make_conn()
    core.set_manual_lock(conn, True, "2026-08-10")
    assert core.remaining_seconds(conn, "2026-08-11") == 120 * 60


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


def test_should_be_locked_when_quota_exhausted():
    conn = make_conn()
    core.add_used_seconds(conn, core.today_str(), 120 * 60)
    assert core.should_be_locked(conn)


def test_should_not_be_locked_with_quota_and_no_curfew():
    conn = make_conn()
    assert not core.should_be_locked(conn)


def test_weekday_limits_clamp_negative_to_zero():
    conn = make_conn()
    core.set_weekday_limits(conn, {"mon": -45, "tue": 120, "wed": 120, "thu": 120,
                                     "fri": 120, "sat": 120, "sun": 120})
    assert core.get_weekday_limits(conn)["mon"] == 0


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} testów przeszło.")
