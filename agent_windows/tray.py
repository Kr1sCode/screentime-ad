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
import subprocess
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
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WM_TIMER = 0x0113
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
IDI_APPLICATION, IDI_WARNING, IDI_ERROR = 32512, 32515, 32513
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
DT_CENTER, DT_VCENTER, DT_SINGLELINE = 0x1, 0x4, 0x20
ICON_SIZE = 16
# ARGB (alpha w najwyższym bajcie), kolejność kolorów w pamięci to BGRA.
BG_COLOR = {"ok": 0xFF2563EB, "warn": 0xFFF59E0B, "crit": 0xFFDC2626, "off": 0xFF6B7280}


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


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
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
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
user32.CreateIconIndirect.restype = wintypes.HICON
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT]
# Te same 32-bit-int-truncates-wskaźnik pułapki dotyczą też uchwytów GDI
# użytych w make_icon() — bez jawnych restype HDC/HBITMAP/HFONT obcinają się
# na x64 dokładnie tak samo jak HICON/HWND wyżej.
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.DestroyIcon.argtypes = [wintypes.HICON]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
gdi32.CreateFontW.restype = wintypes.HFONT
gdi32.CreateFontW.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
]
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wintypes.BOOL
kernel32.GetTickCount.restype = wintypes.DWORD

nid = NOTIFYICONDATA()


def _log(msg: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def make_icon(kind: str, minutes: int | None = None):
    """Ikona 16x16 z tłem wg statusu i liczbą pozostałych minut narysowaną
    GDI (DIBSection 32bpp) zamiast systemowej ikonki — dzięki temu widać
    licznik bez najeżdżania myszką na tray. Maska AND zostaje wyzerowana
    (w całości nieprzezroczysta), bo kolor+alfa daje już pełną kontrolę."""
    if minutes is None:
        ids = {"ok": IDI_APPLICATION, "warn": IDI_WARNING, "crit": IDI_ERROR, "off": IDI_APPLICATION}
        return user32.LoadIconW(None, ctypes.cast(ids.get(kind, IDI_APPLICATION), wintypes.LPCWSTR))

    text = str(min(minutes, 99)) if minutes >= 0 else "0"
    size = ICON_SIZE
    hdc_screen = user32.GetDC(None)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = size
    bmi.bmiHeader.biHeight = -size  # top-down, żeby nie odwracać tekstu
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB

    bits_ptr = ctypes.c_void_p()
    hbm_color = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits_ptr), None, 0)
    old_bm = gdi32.SelectObject(hdc, hbm_color)

    pixels = ctypes.cast(bits_ptr, ctypes.POINTER(ctypes.c_uint32 * (size * size))).contents
    bg = BG_COLOR.get(kind, BG_COLOR["off"])
    for i in range(size * size):
        pixels[i] = bg

    gdi32.SetBkMode(hdc, 1)  # TRANSPARENT — GDI nie dotyka alfy przy zwykłym rysowaniu tekstu
    gdi32.SetTextColor(hdc, 0x00FFFFFF)  # biały (0x00BBGGRR)
    font = gdi32.CreateFontW(-11, 0, 0, 0, 700, 0, 0, 0, 0, 0, 0, 0, 0, "Segoe UI")
    old_font = gdi32.SelectObject(hdc, font)
    rect = wintypes.RECT(0, 0, size, size)
    user32.DrawTextW(hdc, text, -1, ctypes.byref(rect), DT_CENTER | DT_VCENTER | DT_SINGLELINE)
    gdi32.SelectObject(hdc, old_font)
    gdi32.DeleteObject(font)
    gdi32.SelectObject(hdc, old_bm)

    mask_bmi = BITMAPINFO()
    mask_bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    mask_bmi.bmiHeader.biWidth = size
    mask_bmi.bmiHeader.biHeight = -size
    mask_bmi.bmiHeader.biPlanes = 1
    mask_bmi.bmiHeader.biBitCount = 1
    mask_bmi.bmiHeader.biCompression = 0
    mask_bits = ctypes.c_void_p()
    hbm_mask = gdi32.CreateDIBSection(hdc, ctypes.byref(mask_bmi), 0, ctypes.byref(mask_bits), None, 0)
    mask_row_bytes = ((size + 31) // 32) * 4
    ctypes.memset(mask_bits, 0, mask_row_bytes * size)  # 0 = nieprzezroczyste w masce AND

    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(None, hdc_screen)

    ii = ICONINFO(fIcon=True, xHotspot=0, yHotspot=0, hbmMask=hbm_mask, hbmColor=hbm_color)
    hicon = user32.CreateIconIndirect(ctypes.byref(ii))
    gdi32.DeleteObject(hbm_color)
    gdi32.DeleteObject(hbm_mask)
    return hicon


def get_idle_seconds() -> float:
    """Sekundy od ostatniego ruchu myszy/klawiatury W TEJ sesji. Tray działa
    per-user (Scheduled Task at logon), więc w przeciwieństwie do usługi
    (SYSTEM, bez dostępu do pulpitu) widzi realny input użytkownika."""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    user32.GetLastInputInfo(ctypes.byref(lii))
    return max(0, kernel32.GetTickCount() - lii.dwTime) / 1000.0


_idle_triggered = False


def check_idle_enforcement() -> None:
    """Wymusza uśpienie/wyłączenie po skonfigurowanym czasie bezczynności
    (idle_timeout_minutes/idle_action, dopisywane do status.json przez
    usługę na podstawie odpowiedzi /api/heartbeat)."""
    global _idle_triggered
    username = os.environ.get("USERNAME", "")
    try:
        data = json.loads(STATUS_PATH.read_text())
        me = data.get("sessions", {}).get(username, {})
        timeout_min = me.get("idle_timeout_minutes", 0)
        action = me.get("idle_action", "none")
        if not timeout_min or action == "none":
            _idle_triggered = False
            return
        idle_seconds = get_idle_seconds()
        if idle_seconds < 60:
            _idle_triggered = False
            return
        if idle_seconds >= timeout_min * 60 and not _idle_triggered:
            _idle_triggered = True
            _log(f"bezczynność {int(idle_seconds)}s >= {timeout_min}min, akcja: {action}")
            if action == "shutdown":
                subprocess.run(["shutdown", "/s", "/f", "/t", "0"])
            elif action == "sleep":
                subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
    except FileNotFoundError:
        pass
    except Exception as e:
        _log(f"check_idle_enforcement error: {e}")


def read_status():
    """Zwraca (kind, text, minuty_pozostałe_lub_None — None gdy nie ma sensu
    rysować licznika, np. brak połączenia albo usługa nieaktywna)."""
    username = os.environ.get("USERNAME", "")
    try:
        data = json.loads(STATUS_PATH.read_text())
        if time.time() - data.get("updated_at", 0) > STALE_AFTER_SECONDS:
            return "off", "screentime-ad: usługa nie odpowiada", None
        me = data.get("sessions", {}).get(username)
        if not me:
            return "off", "screentime-ad: to konto nie jest śledzone", None
        if not me.get("connected"):
            return "warn", "screentime-ad: brak połączenia z serwerem", None
        remaining = me.get("remaining_seconds")
        if remaining is None:
            return "off", "screentime-ad: brak danych", None
        mins = remaining // 60
        if me.get("locked"):
            return "crit", "screentime-ad: ZABLOKOWANE", 0
        if me.get("warn"):
            return "warn", f"screentime-ad: kończy się czas ({mins} min)", mins
        return "ok", f"screentime-ad: pozostało {mins} min", mins
    except FileNotFoundError:
        return "off", "screentime-ad: usługa jeszcze nie wystartowała", None
    except Exception as e:
        _log(f"read_status error: {e}")
        return "off", "screentime-ad: błąd odczytu statusu", None


def update_tray() -> None:
    kind, text, minutes = read_status()
    old_icon = nid.hIcon
    nid.hIcon = make_icon(kind, minutes)
    nid.szTip = text[:127]
    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
    if old_icon:
        user32.DestroyIcon(old_icon)


def wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_TIMER:
        update_tray()
        check_idle_enforcement()
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
