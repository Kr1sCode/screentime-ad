import functools
import secrets
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

import ad_lock
import core
import db

db.init_db()

AD_SYNC_INTERVAL = 30


def _ldap_cfg(conn):
    cfg = {
        "ldap_host": core.get_config(conn, "ldap_host", ""),
        "base_dn": core.get_config(conn, "ldap_base_dn", ""),
        "bind_dn": core.get_config(conn, "ldap_bind_dn", ""),
        "bind_password": core.get_config(conn, "ldap_bind_password", ""),
    }
    return cfg if all(cfg.values()) else None


def _ad_sync_loop():
    while True:
        try:
            conn = db.get_conn()
            if core.get_config(conn, "ad_lock_enabled", "0") == "1":
                target = core.get_config(conn, "target_username", "")
                cfg = _ldap_cfg(conn)
                if target and cfg:
                    locked = core.should_be_locked(conn)
                    if ad_lock.sync_account_disabled(cfg, target, locked):
                        print(f"[ad_lock] konto {target}: {'ZABLOKOWANE' if locked else 'odblokowane'}")
            conn.close()
        except Exception as e:
            print(f"[ad_lock] błąd synchronizacji: {e}", file=sys.stderr)
        time.sleep(AD_SYNC_INTERVAL)


threading.Thread(target=_ad_sync_loop, daemon=True).start()

SECRET_FILE = db.DATA_DIR / "flask_secret.key"
if not SECRET_FILE.exists():
    SECRET_FILE.write_text(secrets.token_hex(32))
    SECRET_FILE.chmod(0o600)

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = SECRET_FILE.read_text().strip()


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin"):
            return jsonify(error="unauthorized"), 401
        return view(*args, **kwargs)

    return wrapped


def agent_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        conn = db.get_conn()
        token = core.get_config(conn, "agent_token")
        conn.close()
        if request.headers.get("X-Agent-Token") != token:
            return jsonify(error="unauthorized"), 401
        return view(*args, **kwargs)

    return wrapped


# ---- static / pages ----

@app.get("/")
def index():
    if not session.get("admin"):
        return redirect("/login.html")
    return send_from_directory(app.static_folder, "index.html")


# ---- auth ----

@app.post("/api/login")
def login():
    data = request.get_json(force=True)
    conn = db.get_conn()
    row = conn.execute(
        "SELECT password_hash FROM admin WHERE username=?", (data.get("username", ""),)
    ).fetchone()
    conn.close()
    if not row or not check_password_hash(row["password_hash"], data.get("password", "")):
        return jsonify(error="złe dane logowania"), 401
    session["admin"] = True
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/change_password")
@login_required
def change_password():
    data = request.get_json(force=True)
    conn = db.get_conn()
    row = conn.execute("SELECT password_hash FROM admin WHERE username='admin'").fetchone()
    if not check_password_hash(row["password_hash"], data.get("current_password", "")):
        conn.close()
        return jsonify(error="obecne hasło nieprawidłowe"), 400
    new_password = data.get("new_password", "")
    if len(new_password) < 4:
        conn.close()
        return jsonify(error="nowe hasło zbyt krótkie"), 400
    conn.execute(
        "UPDATE admin SET password_hash=? WHERE username='admin'",
        (generate_password_hash(new_password),),
    )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


# ---- admin dashboard API ----

@app.get("/api/state")
@login_required
def state():
    conn = db.get_conn()
    date = core.today_str()
    weekday_limits = core.get_weekday_limits(conn)
    daily_limit_minutes = weekday_limits[core.WEEKDAY_KEYS[datetime.now(core.TZ).weekday()]]
    result = dict(
        date=date,
        target_username=core.get_config(conn, "target_username", ""),
        weekday_limits=weekday_limits,
        daily_limit_minutes=daily_limit_minutes,
        blocked_ranges=core.get_blocked_ranges(conn),
        blocked_now=core.is_blocked_now(conn),
        used_seconds=core.get_used_seconds(conn, date),
        bonus_seconds=core.get_bonus_seconds(conn, date),
        remaining_seconds=core.remaining_seconds(conn, date),
        machines=core.list_machines(conn),
        history=core.history(conn),
        agent_token=core.get_config(conn, "agent_token"),
        manual_locked=core.is_manually_locked(conn, date),
        idle_timeout_minutes=core.get_idle_timeout_minutes(conn),
        idle_action=core.get_idle_action(conn),
        dosing_segments=core.get_dosing_segments(conn),
        dosing_windows=core.dosing_windows_preview(conn, date),
        current_window_index=core.current_window_index(conn),
        window_remaining_seconds=core.remaining_breakdown(conn, date)["window_remaining"],
        ad_lock_enabled=core.get_config(conn, "ad_lock_enabled", "0") == "1",
        ldap_configured=_ldap_cfg(conn) is not None,
        ldap_host=core.get_config(conn, "ldap_host", ""),
        ldap_base_dn=core.get_config(conn, "ldap_base_dn", ""),
        ldap_bind_dn=core.get_config(conn, "ldap_bind_dn", ""),
    )
    conn.close()
    return jsonify(result)


@app.post("/api/ldap_config")
@login_required
def set_ldap_config():
    data = request.get_json(force=True)
    conn = db.get_conn()
    for key in ("ldap_host", "ldap_base_dn", "ldap_bind_dn"):
        if key in data:
            core.set_config(conn, key, str(data[key]).strip())
    if data.get("ldap_bind_password"):
        core.set_config(conn, "ldap_bind_password", data["ldap_bind_password"])
    if "ad_lock_enabled" in data:
        core.set_config(conn, "ad_lock_enabled", "1" if data["ad_lock_enabled"] else "0")
    conn.close()
    return jsonify(ok=True)


@app.post("/api/ldap_test")
@login_required
def test_ldap_config():
    conn = db.get_conn()
    target = core.get_config(conn, "target_username", "")
    cfg = _ldap_cfg(conn)
    conn.close()
    if not target:
        return jsonify(error="najpierw ustaw konto AD (sAMAccountName) w Konfiguracji konta"), 400
    if not cfg:
        return jsonify(error="uzupełnij wszystkie pola LDAP"), 400
    try:
        changed = ad_lock.sync_account_disabled(cfg, target, False)
        return jsonify(ok=True, note="połączenie i uprawnienia OK" + (" (konto odblokowane)" if changed else ""))
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.post("/api/config")
@login_required
def set_config():
    data = request.get_json(force=True)
    conn = db.get_conn()
    if "target_username" in data:
        core.set_config(conn, "target_username", str(data["target_username"]).strip())
    if "weekday_limits" in data:
        core.set_weekday_limits(conn, data["weekday_limits"])
    if "blocked_ranges" in data:
        core.set_blocked_ranges(conn, data["blocked_ranges"])
    if "idle_timeout_minutes" in data or "idle_action" in data:
        core.set_idle_settings(
            conn,
            data.get("idle_timeout_minutes", core.get_idle_timeout_minutes(conn)),
            data.get("idle_action", core.get_idle_action(conn)),
        )
    if "dosing_segments" in data:
        core.set_dosing_segments(conn, data["dosing_segments"])
    conn.close()
    return jsonify(ok=True)


@app.post("/api/bonus")
@login_required
def bonus():
    data = request.get_json(force=True)
    conn = db.get_conn()
    core.add_bonus_minutes(conn, core.today_str(), int(data["minutes"]))
    conn.close()
    return jsonify(ok=True)


@app.post("/api/set_remaining")
@login_required
def set_remaining():
    data = request.get_json(force=True)
    conn = db.get_conn()
    core.set_remaining_minutes(conn, int(data["minutes"]))
    conn.close()
    return jsonify(ok=True)


@app.post("/api/lock_now")
@login_required
def lock_now():
    conn = db.get_conn()
    core.set_manual_lock(conn, True)
    conn.close()
    return jsonify(ok=True)


@app.post("/api/unlock_now")
@login_required
def unlock_now():
    conn = db.get_conn()
    core.set_manual_lock(conn, False)
    conn.close()
    return jsonify(ok=True)


# ---- agent API ----

@app.post("/api/heartbeat")
@agent_required
def heartbeat():
    data = request.get_json(force=True)
    conn = db.get_conn()
    target = core.get_config(conn, "target_username", "")
    hostname = data.get("hostname", "unknown")
    username = data.get("username", "")
    delta = int(data.get("active_seconds_delta", 0))
    os_name = data.get("os", "")

    is_target = bool(target) and username.lower() == target.lower()
    blocked = core.is_blocked_now(conn) if is_target else False
    date = core.today_str()
    if is_target and not blocked:
        core.add_used_seconds(conn, date, delta)
        core.add_window_used_seconds(conn, date, core.current_window_index(conn), delta)

    core.touch_machine(conn, hostname, os_name, delta if is_target else 0)

    breakdown = core.remaining_breakdown(conn, date) if is_target else None
    remaining = breakdown["remaining"] if is_target else 999_999
    force = is_target and (blocked or remaining <= 0)

    # Gdy limit tnie dawkowanie (nie brak puli na dziś), agent pokazuje
    # inny komunikat — "okno się zamyka, wznowienie o HH:MM" zamiast
    # zwykłego "koniec czasu na dziś" — w dwóch etapach (5 i 1 min).
    window_reset_soon = bool(is_target and breakdown["window_limited"] and breakdown["window_reset_minutes"] is not None)
    window_warn_1min = window_reset_soon and remaining <= 60
    window_warn_5min = window_reset_soon and 60 < remaining <= 300
    window_reset_at = None
    if window_reset_soon:
        m = breakdown["window_reset_minutes"]
        window_reset_at = f"{m // 60:02d}:{m % 60:02d}"

    warn = is_target and not window_reset_soon and (remaining <= 300 or core.blocked_range_starts_soon(conn))
    idle_timeout_minutes = core.get_idle_timeout_minutes(conn) if is_target else 0
    idle_action = core.get_idle_action(conn) if is_target else "none"
    conn.close()
    return jsonify(
        remaining_seconds=remaining, warn_5min=warn, force_logout=force,
        idle_timeout_minutes=idle_timeout_minutes, idle_action=idle_action,
        window_warn_5min=window_warn_5min, window_warn_1min=window_warn_1min,
        window_reset_at=window_reset_at,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
