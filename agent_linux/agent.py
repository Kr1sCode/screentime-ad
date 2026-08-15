#!/usr/bin/env python3
"""screentime-ad agent (Linux/systemd, np. CachyOS + sssd/AD).

Jeden plik, zero zależności spoza stdlib (poza binarką `zenity` na baner i
`loginctl` z systemd). Co godzinę sprawdza najnowszy commit na GitHubie
dotykający agent_linux/ — jeśli inny niż zainstalowany, podmienia pliki i
sam się restartuje (os.execv), więc wersja nie ma znaczenia, liczy się SHA.
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = "Kr1sCode/screentime-ad"
BRANCH = "main"
AGENT_SUBDIR = "agent_linux"
INSTALL_DIR = Path(__file__).resolve().parent
SHA_FILE = INSTALL_DIR / ".installed_sha"
CONFIG_PATH = Path("/etc/screentime-ad/agent.conf")

POLL_INTERVAL = 15
HEARTBEAT_INTERVAL = 60
UPDATE_CHECK_INTERVAL = 3600
WARN_THRESHOLD_SECONDS = 300
WARN_TEXT = "Za niecałe 5 minut skończy Ci się czas.\nProszę zapisz pracę i zrób sobie przerwę!"

HOSTNAME = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()


# ---------- auto-update ----------

def _latest_sha() -> str:
    url = f"https://api.github.com/repos/{REPO}/commits?sha={BRANCH}&path={AGENT_SUBDIR}&per_page=1"
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.loads(r.read())
    return data[0]["sha"]


def check_and_update() -> bool:
    try:
        sha = _latest_sha()
    except Exception:
        return False
    installed = SHA_FILE.read_text().strip() if SHA_FILE.exists() else ""
    if sha == installed:
        return False
    try:
        tarball_url = f"https://github.com/{REPO}/archive/{sha}.tar.gz"
        with urllib.request.urlopen(tarball_url, timeout=30) as r:
            data = r.read()
        with tempfile.TemporaryDirectory() as tmp:
            tarfile.open(fileobj=__import__("io").BytesIO(data)).extractall(tmp)
            src = next(Path(tmp).glob(f"*/{AGENT_SUBDIR}"))
            for item in src.iterdir():
                shutil.copy2(item, INSTALL_DIR / item.name)
        SHA_FILE.write_text(sha)
        return True
    except Exception as e:
        print(f"aktualizacja nieudana: {e}", file=sys.stderr)
        return False


# ---------- sesje (loginctl) ----------

def list_active_sessions():
    out = subprocess.run(["loginctl", "list-sessions", "--no-legend"], capture_output=True, text=True).stdout
    result = []
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        sid = parts[0]
        props = subprocess.run(
            ["loginctl", "show-session", sid, "-p", "Name", "-p", "State", "-p", "LockedHint", "-p", "Leader"],
            capture_output=True, text=True,
        ).stdout
        d = dict(l.split("=", 1) for l in props.strip().splitlines() if "=" in l)
        if d.get("State") == "active" and d.get("LockedHint") == "no":
            result.append({"session_id": sid, "username": d.get("Name", ""), "leader_pid": d.get("Leader", "")})
    return result


def terminate_session(session_id: str) -> None:
    subprocess.run(["loginctl", "terminate-session", session_id])


# ponytail: bezczynność opiera się na IdleHint z systemd-logind, który nie
# każde środowisko (np. gołe i3/sway bez idle daemona) w ogóle ustawia —
# gdy go brak, po prostu nic nie wymuszamy zamiast zgadywać. Upgrade: własny
# fallback przez X11 XScreenSaverQueryInfo, jeśli się okaże że to za mało.
def session_idle_seconds(session_id: str) -> float | None:
    props = subprocess.run(
        ["loginctl", "show-session", session_id, "-p", "IdleHint", "-p", "IdleSinceHintMonotonic"],
        capture_output=True, text=True,
    ).stdout
    d = dict(l.split("=", 1) for l in props.strip().splitlines() if "=" in l)
    if d.get("IdleHint") != "yes":
        return 0.0
    try:
        since_us = int(d.get("IdleSinceHintMonotonic", "0"))
    except ValueError:
        return None
    if since_us <= 0:
        return None
    return max(0.0, time.monotonic() - since_us / 1_000_000)


def _session_env(leader_pid: str) -> dict:
    envs = {}
    try:
        raw = Path(f"/proc/{leader_pid}/environ").read_bytes()
        for kv in raw.split(b"\0"):
            if b"=" in kv:
                k, v = kv.split(b"=", 1)
                envs[k.decode(errors="ignore")] = v.decode(errors="ignore")
    except Exception:
        pass
    return envs


def show_banner(session) -> None:
    envs = _session_env(session["leader_pid"])
    env = os.environ.copy()
    env["DISPLAY"] = envs.get("DISPLAY", ":0")
    if envs.get("WAYLAND_DISPLAY"):
        env["WAYLAND_DISPLAY"] = envs["WAYLAND_DISPLAY"]
    if envs.get("DBUS_SESSION_BUS_ADDRESS"):
        env["DBUS_SESSION_BUS_ADDRESS"] = envs["DBUS_SESSION_BUS_ADDRESS"]
    try:
        subprocess.Popen(
            ["runuser", "-u", session["username"], "--",
             "zenity", "--warning", "--title=screentime-ad",
             f"--text={WARN_TEXT}", "--timeout=10", "--width=420"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"nie udało się pokazać banera: {e}", file=sys.stderr)


# ---------- serwer ----------

def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def heartbeat(cfg: dict, username: str, delta: int) -> dict:
    body = json.dumps({
        "hostname": HOSTNAME, "os": "linux", "username": username, "active_seconds_delta": delta,
    }).encode()
    req = urllib.request.Request(
        cfg["server_url"].rstrip("/") + "/api/heartbeat", data=body,
        headers={"Content-Type": "application/json", "X-Agent-Token": cfg["token"]}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def main() -> None:
    cfg = load_config()
    warned = {}
    accumulated = {}
    last_remaining_cache = {}
    idle_settings = {}
    idle_triggered = {}
    last_heartbeat = 0.0
    last_update_check = time.time()

    while True:
        if time.time() - last_update_check >= UPDATE_CHECK_INTERVAL:
            last_update_check = time.time()
            if check_and_update():
                print("nowa wersja pobrana, restartuję...")
                os.execv(sys.executable, [sys.executable] + sys.argv)

        sessions = list_active_sessions()
        for s in sessions:
            sid = s["session_id"]
            accumulated[sid] = accumulated.get(sid, 0) + POLL_INTERVAL

            timeout_min, action = idle_settings.get(sid, (0, "none"))
            if timeout_min and action != "none":
                idle_sec = session_idle_seconds(sid)
                if idle_sec is None:
                    pass  # logind nie raportuje idle w tym środowisku
                elif idle_sec < 60:
                    idle_triggered[sid] = False
                elif idle_sec >= timeout_min * 60 and not idle_triggered.get(sid):
                    idle_triggered[sid] = True
                    print(f"[idle] {s['username']}: bezczynność {int(idle_sec)}s >= {timeout_min}min, akcja: {action}")
                    if action == "shutdown":
                        subprocess.run(["systemctl", "poweroff"])
                    elif action == "sleep":
                        subprocess.run(["systemctl", "suspend"])

        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
            last_heartbeat = time.time()
            for s in sessions:
                sid = s["session_id"]
                delta = accumulated.pop(sid, 0)
                try:
                    resp = heartbeat(cfg, s["username"], delta)
                    last_remaining_cache[sid] = resp["remaining_seconds"]
                    idle_settings[sid] = (resp.get("idle_timeout_minutes", 0), resp.get("idle_action", "none"))
                except (urllib.error.URLError, OSError):
                    prev = last_remaining_cache.get(sid)
                    remaining = max(0, prev - delta) if prev is not None else None
                    last_remaining_cache[sid] = remaining
                    resp = {
                        "remaining_seconds": remaining,
                        "warn_5min": remaining is not None and remaining <= WARN_THRESHOLD_SECONDS,
                        "force_logout": remaining is not None and remaining <= 0,
                    }

                remaining = resp.get("remaining_seconds")
                if remaining is not None and remaining > WARN_THRESHOLD_SECONDS:
                    warned[sid] = False
                if resp.get("warn_5min") and not warned.get(sid):
                    show_banner(s)
                    warned[sid] = True
                if resp.get("force_logout"):
                    terminate_session(sid)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
