"""Self-check for app/core.py quota math. Run: python3 tests/test_core.py"""
import sqlite3
import sys
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


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} testów przeszło.")
