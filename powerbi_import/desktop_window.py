"""Win32 window inspection and capture for the Power BI Desktop probe.

The probe used to watch the *process* only, which is why a deliberately broken
project still reported ``opened``: Desktop shows content errors in a dialog and
keeps running. Looking at the window gives two stronger signals -- whether the
report window actually appeared and became responsive, and what it looks like.

Windows-only and stdlib-only (ctypes + zlib). Every function degrades to a
safe empty result on another platform or when an API call fails, so a capture
problem can never break a migration.
"""

from __future__ import annotations

import binascii
import struct
import sys
import time
import zlib
from typing import List, NamedTuple, Optional, Tuple

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:                                    # pragma: no cover - platform
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    _WNDENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [
        wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.argtypes = [
        wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowRect.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetWindow.restype = wintypes.HWND
    _user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    _user32.PrintWindow.argtypes = [
        wintypes.HWND, wintypes.HDC, wintypes.UINT]
    _user32.GetWindowDC.argtypes = [wintypes.HWND]
    _user32.GetWindowDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _gdi32.BitBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]

    class _BITMAPINFOHEADER(ctypes.Structure):
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

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                    ("bmiColors", wintypes.DWORD * 3)]

    _gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    _gdi32.CreateDIBSection.restype = wintypes.HBITMAP

# PrintWindow flag that captures DirectComposition/WPF content. Power BI
# Desktop is WPF, so without it the capture comes back blank.
_PW_RENDERFULLCONTENT = 0x00000002
_SRCCOPY = 0x00CC0020
_GW_OWNER = 4
_WM_NULL = 0
_SMTO_ABORTIFHUNG = 0x0002


class WindowInfo(NamedTuple):
    hwnd: int
    title: str
    class_name: str
    width: int
    height: int
    owned: bool


#: Per-monitor v2, the context that makes GetWindowRect report real pixels.
_DPI_PER_MONITOR_V2 = -4
_DPI_PER_MONITOR = 2

_dpi_ready = False


def _dpi_calls():
    """The ways to claim DPI awareness, newest API first."""
    if not IS_WINDOWS:
        return []
    calls = []
    if hasattr(_user32, "SetProcessDpiAwarenessContext"):
        calls.append(lambda: _user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(_DPI_PER_MONITOR_V2)))
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
    except OSError:
        shcore = None
    if shcore is not None and hasattr(shcore, "SetProcessDpiAwareness"):
        calls.append(
            lambda: shcore.SetProcessDpiAwareness(_DPI_PER_MONITOR) == 0)
    if hasattr(_user32, "SetProcessDPIAware"):
        calls.append(_user32.SetProcessDPIAware)
    return calls


def _ensure_dpi_aware() -> bool:
    """Ask Windows for physical pixels before measuring or capturing.

    A DPI-unaware process is handed GetWindowRect in *logical* coordinates,
    so on a scaled display the rect is smaller than the window really is.
    The bitmap gets sized from that rect while PrintWindow renders at the
    window's true size, and the capture comes back cropped along the bottom
    and right edges -- which on an error dialog cuts off the buttons.

    Awareness is a one-shot, process-wide setting: if the host already chose
    one, ours is refused and there is nothing to do about it. So this runs at
    most once and never raises.
    """
    global _dpi_ready
    if _dpi_ready:
        return True
    _dpi_ready = True
    for call in _dpi_calls():
        try:
            if call():
                return True
        except (OSError, AttributeError, ValueError):
            continue
    return False


def _text_of(hwnd) -> str:                        # pragma: no cover - platform
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class_of(hwnd) -> str:                       # pragma: no cover - platform
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def windows_for_pid(pid: int) -> List[WindowInfo]:
    """Every visible top-level window belonging to ``pid``."""
    if not IS_WINDOWS or not pid:
        return []
    _ensure_dpi_aware()
    found: List[WindowInfo] = []

    def _callback(hwnd, _lparam):                 # pragma: no cover - platform
        owner_pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value != pid or not _user32.IsWindowVisible(hwnd):
            return True
        rect = wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        found.append(WindowInfo(
            hwnd=int(hwnd),
            title=_text_of(hwnd),
            class_name=_class_of(hwnd),
            width=rect.right - rect.left,
            height=rect.bottom - rect.top,
            owned=bool(_user32.GetWindow(hwnd, _GW_OWNER)),
        ))
        return True

    try:                                          # pragma: no cover - platform
        _user32.EnumWindows(_WNDENUMPROC(_callback), 0)
    except OSError:
        return []
    return found


def is_responsive(hwnd: int, timeout_ms: int = 2000) -> bool:
    """True when the window pumps messages, i.e. is not busy loading."""
    if not IS_WINDOWS:
        return False
    result = ctypes.c_size_t()
    ok = _user32.SendMessageTimeoutW(
        hwnd, _WM_NULL, 0, 0, _SMTO_ABORTIFHUNG, timeout_ms,
        ctypes.byref(result))
    return bool(ok)


def main_window(pid: int, min_width: int = 400,
                min_height: int = 300) -> Optional[WindowInfo]:
    """Largest unowned titled window of the process, i.e. the document window."""
    candidates = [w for w in windows_for_pid(pid)
                  if not w.owned and w.title
                  and w.width >= min_width and w.height >= min_height]
    if not candidates:
        return None
    return max(candidates, key=lambda w: w.width * w.height)


def report_window(pids, title_fragment: str, min_width: int = 400,
                  min_height: int = 300) -> Optional[WindowInfo]:
    """Document window carrying *title_fragment* across any of *pids*.

    Power BI hands a file to an already-running instance and the launcher
    process then exits, so the window often belongs to a different process
    than the one we started.
    """
    wanted = (title_fragment or "").lower()
    best = None
    for pid in pids:
        for win in windows_for_pid(pid):
            if win.owned or not win.title:
                continue
            if win.width < min_width or win.height < min_height:
                continue
            if wanted and wanted not in win.title.lower():
                continue
            if best is None or win.width * win.height > best.width * best.height:
                best = win
    return best


def dialog_windows(pid: int) -> List[WindowInfo]:
    """Owned popup windows, which is how a modal error surfaces in Win32.

    Power BI renders most of its errors inside the WPF document window, so an
    empty list here does NOT mean the report is clean.
    """
    return [w for w in windows_for_pid(pid) if w.owned and w.title]


def wait_for_window(pids, title_fragment: str = "", *, timeout: float = 120,
                    stable_for: float = 3.0,
                    poll: float = 1.0) -> Tuple[Optional[WindowInfo], List[str]]:
    """Wait until one of *pids* shows a responsive report window.

    Returns ``(window, signals)``. An error dialog ends the wait immediately:
    a popup is what makes a project unusable, and waiting out the timeout only
    delays the verdict. Waiting for the title to match and for the window to
    answer a message is a real load signal, unlike a fixed sleep.
    """
    signals: List[str] = []
    if not IS_WINDOWS:
        return None, ["window inspection is Windows-only"]
    if isinstance(pids, int):
        pids = [pids]
    deadline = time.time() + timeout
    matched_since: Optional[float] = None
    seen_titles = set()

    while time.time() < deadline:
        live = list(pids() if callable(pids) else pids)

        for dialog in (d for pid in live for d in dialog_windows(pid)):
            signals.append(f"error dialog: {dialog.title!r}")
            return None, signals

        win = report_window(live, title_fragment)
        if win is not None and is_responsive(win.hwnd):
            matched_since = matched_since or time.time()
            if time.time() - matched_since >= stable_for:
                return win, signals
        else:
            matched_since = None
            for pid in live:
                other = main_window(pid)
                if other is not None and other.title:
                    seen_titles.add(other.title)
        time.sleep(poll)

    if not seen_titles:
        signals.append("no Power BI window appeared before the timeout")
    else:
        signals.append(
            f"no window titled {title_fragment!r} before the timeout "
            f"(saw: {', '.join(sorted(seen_titles)[:3])})")
    return None, signals


def _png_bytes(width: int, height: int, bgra: bytes) -> bytes:
    """Encode a top-down BGRA buffer as PNG (stdlib only)."""
    rgb = bytearray(width * height * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]

    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)                              # filter type None
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", binascii.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def capture_window(hwnd: int, path: str) -> Optional[str]:
    """Write a PNG of the window. Returns the path, or None when unavailable."""
    if not IS_WINDOWS:
        return None
    _ensure_dpi_aware()
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None

    window_dc = mem_dc = bitmap = None
    try:                                          # pragma: no cover - platform
        window_dc = _user32.GetWindowDC(hwnd)
        if not window_dc:
            return None
        mem_dc = _gdi32.CreateCompatibleDC(window_dc)
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height          # negative = top-down
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        bitmap = _gdi32.CreateDIBSection(
            mem_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        if not bitmap:
            return None
        _gdi32.SelectObject(mem_dc, bitmap)

        if not _user32.PrintWindow(hwnd, mem_dc, _PW_RENDERFULLCONTENT):
            _gdi32.BitBlt(mem_dc, 0, 0, width, height,
                          window_dc, 0, 0, _SRCCOPY)

        buf = ctypes.string_at(bits, width * height * 4)
        with open(path, "wb") as handle:
            handle.write(_png_bytes(width, height, buf))
        return path
    except (OSError, ValueError, ctypes.ArgumentError):
        return None
    finally:                                      # pragma: no cover - platform
        if bitmap:
            _gdi32.DeleteObject(bitmap)
        if mem_dc:
            _gdi32.DeleteDC(mem_dc)
        if window_dc:
            _user32.ReleaseDC(hwnd, window_dc)


def is_blank(png_path: str, sample_stride: int = 97) -> Optional[bool]:
    """True when the capture holds a single colour, i.e. it caught nothing.

    A blank capture means the screenshot failed, not that the report is empty,
    so callers should report it as an unknown rather than a pass.

    Samples across the whole image. Reading only the first few thousand bytes
    would cover barely half the top row -- uniform window chrome on any real
    capture, so every screenshot read as blank.
    """
    try:
        with open(png_path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    idat = data.find(b"IDAT")
    if idat < 0 or len(data) < 24:
        return None
    width, height = struct.unpack(">II", data[16:24])
    if width <= 0 or height <= 0:
        return None
    size = struct.unpack(">I", data[idat - 4:idat])[0]
    try:
        raw = zlib.decompress(data[idat + 4:idat + 4 + size])
    except zlib.error:
        return None

    row_len = 1 + width * 3                    # our writer always filters 0
    if len(raw) < row_len * height:
        return None
    total = width * height
    step = min(sample_stride, max(1, total // 256))
    seen = set()
    for index in range(0, total, step):
        row, col = divmod(index, width)
        start = row * row_len + 1 + col * 3
        seen.add(raw[start:start + 3])
        if len(seen) > 1:
            return False
    return True
