"""screentime-ad tray — ikona w zasobniku systemowym z podpowiedzią (hover)
pokazującą status połączenia i pozostały czas. Tylko stdlib + ctypes (Shell_
NotifyIcon), bez pystray/Pillow, żeby build nie potrzebował dodatkowych
zależności. Odpalane per-user (Scheduled Task "at logon", NIE usługa —
usługi w nowoczesnym Windows nie mają dostępu do pulpitu/traya).

Czyta {ProgramData}\\screentime-ad\\status.json, który usługa
(agent_service.py) dopisuje po każdym heartbeat. Nic nie łączy się z
serwerem samodzielnie — tray tylko pokazuje to, co usługa już wie.

To najmniej "przetestowane w boju" miejsce w projekcie (ctypes Win32 GUI,
nie da się zweryfikować bez prawdziwego Windows) — jeśli coś nie działa,
sprawdź %ProgramData%\\screentime-ad\\tray.log.
"""
import ctypes
import json
import os
import time
import traceback
from ctypes import wintypes
from pathlib import Path

DATA_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "screentime-ad"
STATUS_PATH = DATA_DIR / "status.json"
LOG_PATH = DATA_DIR / "tray.log"
STALE_AFTER_SECONDS = 180

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_TIMER = 0x0113
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
IDI_APPLICATION, IDI_WARNING, IDI_ERROR = 32512, 32515, 32513
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

# Bez jawnych restype/argtypes ctypes zakłada 32-bit int zwrotny — na x64
# obcina wskaźniki (HWND/HICON) i wszystko się wysypuje. Ustawić jawnie.
user32.LoadIconW.restype = wintypes.HICON
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.CreateWindowExW.restype = wintypes.HWND
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.DefWindowProcW.restype = ctypes.c_longlong
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

nid = NOTIFYICONDATA()


def _log(msg: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def make_icon(kind: str):
    ids = {"ok": IDI_APPLICATION, "warn": IDI_WARNING, "crit": IDI_ERROR, "off": IDI_APPLICATION}
    return user32.LoadIconW(None, ctypes.cast(ids.get(kind, IDI_APPLICATION), wintypes.LPCWSTR))


def read_status():
    username = os.environ.get("USERNAME", "")
    try:
        data = json.loads(STATUS_PATH.read_text())
        if time.time() - data.get("updated_at", 0) > STALE_AFTER_SECONDS:
            return "off", "screentime-ad: usługa nie odpowiada"
        me = data.get("sessions", {}).get(username)
        if not me:
            return "off", "screentime-ad: to konto nie jest śledzone"
        if not me.get("connected"):
            return "warn", "screentime-ad: brak połączenia z serwerem"
        remaining = me.get("remaining_seconds")
        if remaining is None:
            return "off", "screentime-ad: brak danych"
        mins = remaining // 60
        if me.get("locked"):
            return "crit", "screentime-ad: ZABLOKOWANE"
        if me.get("warn"):
            return "warn", f"screentime-ad: kończy się czas ({mins} min)"
        return "ok", f"screentime-ad: pozostało {mins} min"
    except FileNotFoundError:
        return "off", "screentime-ad: usługa jeszcze nie wystartowała"
    except Exception as e:
        _log(f"read_status error: {e}")
        return "off", "screentime-ad: błąd odczytu statusu"


def update_tray() -> None:
    kind, text = read_status()
    nid.hIcon = make_icon(kind)
    nid.szTip = text[:127]
    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))


def wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_TIMER:
        update_tray()
        return 0
    if msg in (WM_CLOSE, WM_DESTROY):
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def main() -> None:
    hinst = kernel32.GetModuleHandleW(None)
    wndproc_ref = WNDPROC(wndproc)

    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(wndproc_ref, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = "ScreentimeAdTrayWnd"
    user32.RegisterClassW(ctypes.byref(wc))

    hwnd = user32.CreateWindowExW(
        0, "ScreentimeAdTrayWnd", "screentime-ad tray", 0, 0, 0, 0, 0, None, None, hinst, None
    )

    nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE
    nid.uCallbackMessage = WM_TRAYICON
    nid.hIcon = make_icon("off")
    nid.szTip = "screentime-ad"
    shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
    update_tray()

    user32.SetTimer(hwnd, 1, 5000, None)

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _log("crash:\n" + traceback.format_exc())
