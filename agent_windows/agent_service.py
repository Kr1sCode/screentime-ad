"""screentime-ad agent (Windows). Tylko stdlib + ctypes (WTS API z
wtsapi32.dll) — bez pywin32. Zamrożony przez PyInstaller, instalowany jako
usługa (NSSM) na SYSTEM, więc widzi WSZYSTKIE sesje na maszynie niezależnie
od tego kto jest akurat zalogowany.

Auto-update: co godzinę sprawdza najnowszy release na GitHubie, porównuje
tag z lokalnym VERSION obok exe. Jeśli nowszy — ściąga installer.exe z tego
release'a, weryfikuje jego sha256 względem SHA256SUMS.txt z tego samego
release'a, i uruchamia go w trybie cichym (Inno Setup sam zatrzyma usługę,
podmieni pliki, zainstaluje na nowo, wystartuje). Proces odpalający installer
MUSI być detached — Inno zabija tę usługę w trakcie instalacji, więc zwykłe
dziecko zginęłoby razem z rodzicem zanim instalacja się skończy.
"""
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

REPO = "Kr1sCode/screentime-ad"
INSTALL_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
VERSION_FILE = INSTALL_DIR / "version.txt"
DATA_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "screentime-ad"
CONFIG_PATH = DATA_DIR / "agent.json"
STATUS_PATH = DATA_DIR / "status.json"

POLL_INTERVAL = 15
HEARTBEAT_INTERVAL = 60
UPDATE_CHECK_INTERVAL = 3600
WARN_THRESHOLD_SECONDS = 300
WARN_TITLE = "screentime-ad"
WARN_TEXT = "Za niecałe 5 minut skończy Ci się czas.\nProszę zapisz pracę i zrób sobie przerwę!"

HOSTNAME = os.environ.get("COMPUTERNAME", "unknown")

# ---------- WTS API (ctypes) ----------

wtsapi32 = ctypes.WinDLL("wtsapi32")
WTS_CURRENT_SERVER_HANDLE = 0
WTS_ACTIVE = 0
WTS_USERNAME = 5


class WTS_SESSION_INFO(ctypes.Structure):
    _fields_ = [("SessionId", wintypes.DWORD),
                ("pWinStationName", wintypes.LPWSTR),
                ("State", ctypes.c_int)]


def _query_session_username(session_id: int) -> str:
    buf = ctypes.c_wchar_p()
    n = wintypes.DWORD()
    ok = wtsapi32.WTSQuerySessionInformationW(
        WTS_CURRENT_SERVER_HANDLE, session_id, WTS_USERNAME, ctypes.byref(buf), ctypes.byref(n)
    )
    if not ok or not buf.value:
        return ""
    val = buf.value
    wtsapi32.WTSFreeMemory(buf)
    return val


def _is_locked(session_id: int) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"SESSION eq {session_id}", "/FI", "IMAGENAME eq LogonUI.exe"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return "LogonUI.exe" in out
    except Exception:
        return False


def list_active_sessions():
    sessions_ptr = ctypes.POINTER(WTS_SESSION_INFO)()
    count = wintypes.DWORD()
    if not wtsapi32.WTSEnumerateSessionsW(WTS_CURRENT_SERVER_HANDLE, 0, 1, ctypes.byref(sessions_ptr), ctypes.byref(count)):
        return []
    result = []
    try:
        for i in range(count.value):
            si = sessions_ptr[i]
            if si.State != WTS_ACTIVE:
                continue
            username = _query_session_username(si.SessionId)
            if not username or _is_locked(si.SessionId):
                continue
            result.append({"session_id": si.SessionId, "username": username})
    finally:
        wtsapi32.WTSFreeMemory(sessions_ptr)
    return result


def show_banner(session_id: int) -> None:
    resp = wintypes.DWORD()
    wtsapi32.WTSSendMessageW(
        WTS_CURRENT_SERVER_HANDLE, session_id,
        WARN_TITLE, len(WARN_TITLE) * 2,
        WARN_TEXT, len(WARN_TEXT) * 2,
        0, 10, ctypes.byref(resp), False,
    )


def logoff_session(session_id: int) -> None:
    wtsapi32.WTSLogoffSession(WTS_CURRENT_SERVER_HANDLE, session_id, False)


# ---------- auto-update (GitHub Releases, hash-verified) ----------

def _installed_version() -> str:
    return VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else "0.0.0"


def _latest_release() -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def check_and_update() -> None:
    try:
        release = _latest_release()
        tag = release["tag_name"].lstrip("v")
        if tag == _installed_version():
            return
        assets = {a["name"]: a["browser_download_url"] for a in release["assets"]}
        installer_name = next(n for n in assets if n.endswith(".exe"))
        sums_url = assets.get("SHA256SUMS.txt")
        if not sums_url:
            raise RuntimeError("release bez SHA256SUMS.txt — nie ufam nieweryfikowalnemu instalatorowi")

        with urllib.request.urlopen(sums_url, timeout=15) as r:
            sums_text = r.read().decode()
        expected = next(
            line.split()[0] for line in sums_text.splitlines() if line.strip().endswith(installer_name)
        )

        tmp_dir = Path(tempfile.mkdtemp())
        installer_path = tmp_dir / installer_name
        with urllib.request.urlopen(assets[installer_name], timeout=60) as r:
            data = r.read()
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise RuntimeError(f"sha256 mismatch: pobrany={actual} oczekiwany={expected}")
        installer_path.write_bytes(data)

        print(f"aktualizacja {_installed_version()} -> {tag}, uruchamiam installer w tle...")
        # Detached: Inno Setup zatrzyma TĘ usługę w trakcie instalacji, więc
        # zwykłe subprocess.run() zginęłoby razem z rodzicem w połowie.
        subprocess.Popen(
            [str(installer_path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception as e:
        print(f"aktualizacja nieudana: {e}", file=sys.stderr)


# ---------- status dla ikony w trayu ----------

def write_status(sessions_status: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(json.dumps({
            "updated_at": time.time(),
            "sessions": sessions_status,
        }))
    except Exception:
        pass


# ---------- serwer ----------

def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def heartbeat(cfg: dict, username: str, delta: int) -> dict:
    body = json.dumps({
        "hostname": HOSTNAME, "os": "windows", "username": username, "active_seconds_delta": delta,
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
    last_heartbeat = 0.0
    last_update_check = time.time()

    while True:
        if time.time() - last_update_check >= UPDATE_CHECK_INTERVAL:
            last_update_check = time.time()
            check_and_update()

        sessions = list_active_sessions()
        for s in sessions:
            accumulated[s["session_id"]] = accumulated.get(s["session_id"], 0) + POLL_INTERVAL

        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
            last_heartbeat = time.time()
            status_by_user = {}
            for s in sessions:
                sid = s["session_id"]
                delta = accumulated.pop(sid, 0)
                connected = True
                try:
                    resp = heartbeat(cfg, s["username"], delta)
                    last_remaining_cache[sid] = resp["remaining_seconds"]
                except (urllib.error.URLError, OSError):
                    connected = False
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
                    show_banner(sid)
                    warned[sid] = True
                if resp.get("force_logout"):
                    logoff_session(sid)

                status_by_user[s["username"]] = {
                    "connected": connected,
                    "remaining_seconds": remaining,
                    "warn": bool(resp.get("warn_5min")),
                    "locked": bool(resp.get("force_logout")),
                }
            write_status(status_by_user)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
