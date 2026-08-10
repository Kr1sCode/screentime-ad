"""screentime-ad agent (Windows). Jeden plik, tylko stdlib + ctypes (WTS API
z wtsapi32.dll) — bez pywin32, więc wystarczy zwykły python.exe na PATH.

Uruchamiany jako usługa (NSSM) na SYSTEM, więc widzi WSZYSTKIE sesje na
maszynie i działa niezależnie od tego kto jest akurat zalogowany. Co godzinę
sprawdza najnowszy commit na GitHubie dotykający agent_windows/ — jeśli inny
niż zainstalowany, podmienia pliki i restartuje się (os.execv/usługa wraca
przez NSSM Restart=always), więc numer wersji nie ma znaczenia.
"""
import ctypes
import io
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
from ctypes import wintypes
from pathlib import Path

REPO = "Kr1sCode/screentime-ad"
BRANCH = "main"
AGENT_SUBDIR = "agent_windows"
INSTALL_DIR = Path(__file__).resolve().parent
SHA_FILE = INSTALL_DIR / ".installed_sha"
CONFIG_PATH = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "screentime-ad" / "agent.json"

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
            tarfile.open(fileobj=io.BytesIO(data)).extractall(tmp)
            src = next(Path(tmp).glob(f"*/{AGENT_SUBDIR}"))
            for item in src.iterdir():
                shutil.copy2(item, INSTALL_DIR / item.name)
        SHA_FILE.write_text(sha)
        return True
    except Exception as e:
        print(f"aktualizacja nieudana: {e}", file=sys.stderr)
        return False


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
            if check_and_update():
                print("nowa wersja pobrana, restartuję...")
                os.execv(sys.executable, [sys.executable] + sys.argv)

        sessions = list_active_sessions()
        for s in sessions:
            accumulated[s["session_id"]] = accumulated.get(s["session_id"], 0) + POLL_INTERVAL

        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
            last_heartbeat = time.time()
            for s in sessions:
                sid = s["session_id"]
                delta = accumulated.pop(sid, 0)
                try:
                    resp = heartbeat(cfg, s["username"], delta)
                    last_remaining_cache[sid] = resp["remaining_seconds"]
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
                    show_banner(sid)
                    warned[sid] = True
                if resp.get("force_logout"):
                    logoff_session(sid)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
