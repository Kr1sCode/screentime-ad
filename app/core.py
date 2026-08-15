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


IDLE_ACTIONS = ("none", "sleep", "shutdown")


def get_idle_timeout_minutes(conn: sqlite3.Connection) -> int:
    return int(get_config(conn, "idle_timeout_minutes", "0") or 0)


def get_idle_action(conn: sqlite3.Connection) -> str:
    action = get_config(conn, "idle_action", "none")
    return action if action in IDLE_ACTIONS else "none"


def set_idle_settings(conn: sqlite3.Connection, timeout_minutes: int, action: str) -> None:
    set_config(conn, "idle_timeout_minutes", str(max(0, int(timeout_minutes))))
    set_config(conn, "idle_action", action if action in IDLE_ACTIONS else "none")


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


# ---- dawkowanie: dzienny limit rozłożony na równe okna czasu, żeby
# dziecko nie wykorzystało całej puli od razu (jak limity sesji Claude Code) ----

MAX_DOSING_SEGMENTS = 12


def get_dosing_segments(conn: sqlite3.Connection) -> int:
    return int(get_config(conn, "dosing_segments", "0") or 0)


def set_dosing_segments(conn: sqlite3.Connection, segments: int) -> None:
    set_config(conn, "dosing_segments", str(max(0, min(MAX_DOSING_SEGMENTS, int(segments)))))


def _split_evenly(total: int, n: int) -> list[int]:
    """Dzieli `total` na `n` całkowitych części, resztę z zaokrąglenia
    dokładając po jednej jednostce do pierwszych części."""
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def _blocked_intervals_minutes(ranges: list) -> list[tuple[int, int]]:
    """Zakresy blokady jako (start,end) w minutach 0..1440 — zakresy
    zawijające północ są rozbijane na dwa kawałki."""
    out = []
    for r in ranges:
        s, e = _parse_hhmm(r["start"]), _parse_hhmm(r["end"])
        if s == e:
            continue
        if s < e:
            out.append((s, e))
        else:
            out.append((s, 1440))
            out.append((0, e))
    return out


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    ivs = sorted(intervals)
    merged = [ivs[0]]
    for s, e in ivs[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def available_intervals_minutes(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """Dostępne (niezablokowane godzinami blokady) przedziały doby, w
    minutach 0..1440, posortowane."""
    blocked = _merge_intervals(_blocked_intervals_minutes(get_blocked_ranges(conn)))
    avail = []
    cursor = 0
    for s, e in blocked:
        if s > cursor:
            avail.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < 1440:
        avail.append((cursor, 1440))
    return avail


def compute_dosing_windows(conn: sqlite3.Connection, segments: int) -> list[list[tuple[int, int]]]:
    """Dzieli dostępny (niezablokowany) czas doby na `segments` równych
    (co do długości) okien czasowych. Każde okno to lista przedziałów minut
    — więcej niż jeden, gdy okno "przeskakuje" przez godziny blokady."""
    if segments <= 0:
        return []
    avail = available_intervals_minutes(conn)
    total = sum(e - s for s, e in avail)
    if total <= 0:
        return [[] for _ in range(segments)]
    sizes = _split_evenly(total, segments)

    windows: list[list[tuple[int, int]]] = [[] for _ in range(segments)]
    win_idx = 0
    win_left = sizes[0]
    for s, e in avail:
        pos = s
        while pos < e:
            while win_left == 0 and win_idx < segments - 1:
                win_idx += 1
                win_left = sizes[win_idx]
            take = min(e - pos, win_left) if win_left > 0 else 0
            if take <= 0:
                break
            windows[win_idx].append((pos, pos + take))
            pos += take
            win_left -= take
    return windows


def get_window_budget_seconds(conn: sqlite3.Connection, date: str, window_index: int) -> int:
    segments = get_dosing_segments(conn)
    if segments <= 0 or window_index is None:
        return 0
    base_limit = get_daily_limit_seconds(conn, date)
    return _split_evenly(base_limit, segments)[window_index]


def get_window_used_seconds(conn: sqlite3.Connection, date: str, window_index: int) -> int:
    if window_index is None:
        return 0
    row = conn.execute(
        "SELECT seconds_used FROM window_usage WHERE date=? AND window_index=?", (date, window_index)
    ).fetchone()
    return row["seconds_used"] if row else 0


def add_window_used_seconds(conn: sqlite3.Connection, date: str, window_index: int | None, delta: int) -> None:
    if delta <= 0 or window_index is None:
        return
    conn.execute(
        "INSERT INTO window_usage (date, window_index, seconds_used) VALUES (?, ?, ?) "
        "ON CONFLICT(date, window_index) DO UPDATE SET seconds_used = seconds_used + excluded.seconds_used",
        (date, window_index, delta),
    )
    conn.commit()


def current_window_index(conn: sqlite3.Connection, now: datetime | None = None) -> int | None:
    segments = get_dosing_segments(conn)
    if segments <= 0:
        return None
    now = now or datetime.now(TZ)
    now_min = now.hour * 60 + now.minute
    for idx, ranges in enumerate(compute_dosing_windows(conn, segments)):
        for s, e in ranges:
            if s <= now_min < e:
                return idx
    return None


def next_window_reset_minutes(conn: sqlite3.Connection, now: datetime | None = None) -> int | None:
    """Minuta doby (0..1440), w której zaczyna się najbliższe KOLEJNE okno
    dawkowania po `now` — None gdy dawkowanie wyłączone albo to już
    ostatnie okno na dziś."""
    segments = get_dosing_segments(conn)
    if segments <= 0:
        return None
    now = now or datetime.now(TZ)
    now_min = now.hour * 60 + now.minute
    starts = sorted(s for ranges in compute_dosing_windows(conn, segments) for s, _ in ranges if s > now_min)
    return starts[0] if starts else None


def dosing_windows_preview(conn: sqlite3.Connection, date: str | None = None) -> list[dict]:
    segments = get_dosing_segments(conn)
    if segments <= 0:
        return []
    date = date or today_str()
    budgets = _split_evenly(get_daily_limit_seconds(conn, date), segments)

    def fmt(m):
        return f"{m // 60:02d}:{m % 60:02d}"

    return [
        {
            "index": i,
            "ranges": [f"{fmt(s)}–{fmt(e)}" for s, e in ranges],
            "budget_minutes": budgets[i] // 60,
        }
        for i, ranges in enumerate(compute_dosing_windows(conn, segments))
    ]


def remaining_breakdown(conn: sqlite3.Connection, date: str | None = None) -> dict:
    """Rozbicie pozostałego czasu na całodniowy limit i (opcjonalnie) limit
    bieżącego okna dawkowania — potrzebne do heartbeatu, żeby agent wiedział
    CZY i KIEDY pokazać ostrzeżenie "okno się zamyka, wznowienie o HH:MM"
    zamiast zwykłego "koniec czasu na dziś"."""
    date = date or today_str()
    empty = {"remaining": 0, "daily_remaining": 0, "window_remaining": None,
              "window_limited": False, "window_reset_minutes": None}
    if is_manually_locked(conn, date):
        return empty

    limit = get_daily_limit_seconds(conn, date) + get_bonus_seconds(conn, date)
    used = get_used_seconds(conn, date)
    daily_remaining = max(0, limit - used)

    segments = get_dosing_segments(conn) if date == today_str() else 0
    if segments <= 0:
        return {"remaining": daily_remaining, "daily_remaining": daily_remaining,
                "window_remaining": None, "window_limited": False, "window_reset_minutes": None}

    window_idx = current_window_index(conn)
    if window_idx is None:
        return {"remaining": daily_remaining, "daily_remaining": daily_remaining,
                "window_remaining": None, "window_limited": False,
                "window_reset_minutes": next_window_reset_minutes(conn)}

    window_budget = get_window_budget_seconds(conn, date, window_idx)
    window_used = get_window_used_seconds(conn, date, window_idx)
    window_remaining = max(0, window_budget - window_used) + get_bonus_seconds(conn, date)
    effective = min(daily_remaining, window_remaining)
    return {
        "remaining": effective,
        "daily_remaining": daily_remaining,
        "window_remaining": window_remaining,
        "window_limited": window_remaining < daily_remaining,
        "window_reset_minutes": next_window_reset_minutes(conn),
    }


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
    return remaining_breakdown(conn, date)["remaining"]


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
