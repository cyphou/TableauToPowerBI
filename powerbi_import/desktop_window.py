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
import math
import os
import re
import struct
import sys
import time
import zlib
from typing import (
    TYPE_CHECKING,
    Callable,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Tuple,
    Union,
)

from powerbi_import.desktop_accessibility import (
    DESKTOP_DIALOG_OBSERVER_SCRIPT,
    DIALOG_ATTENTION_PATTERN,
    DIALOG_BUSY_TEXT_PATTERN,
    DIALOG_BUSY_TITLE_PATTERN,
    NATIVE_ACCESSIBILITY_SCRIPT,
    is_powershell_failure,
    run_accessibility_script,
)

IS_WINDOWS = sys.platform == "win32"

# TYPE_CHECKING keeps these names bound for static analysis on every platform;
# at runtime the block still runs on Windows only.
if TYPE_CHECKING or IS_WINDOWS:                   # pragma: no cover - platform
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


# A progress window is a reason to keep waiting, never evidence of loading
# success and never permission to press Cancel. These are application messages,
# not document names. Unknown languages/dialogs remain manual-attention cases.
_OPENING_TITLE = re.compile(
    r"""^(?:Opening workbook|Ouverture du classeur)\s+['"“«].+['"”»]\s*(?:\.\.\.|…)$""",
    re.IGNORECASE)
_BUSY_TITLE = re.compile(
    DIALOG_BUSY_TITLE_PATTERN, re.IGNORECASE)
_ATTENTION_TEXT = re.compile(
    DIALOG_ATTENTION_PATTERN, re.IGNORECASE)
_BUSY_TEXT = re.compile(
    DIALOG_BUSY_TEXT_PATTERN, re.IGNORECASE)

_DIALOG_SCRIPT = NATIVE_ACCESSIBILITY_SCRIPT + DESKTOP_DIALOG_OBSERVER_SCRIPT + r"""
$clock = [Diagnostics.Stopwatch]::StartNew()
$q = [Console]::In.ReadToEnd() | ConvertFrom-Json
if (-not [NativeDesktopAccessibility]::Owned([long]$q.hwnd, [int]$q.pid)) {
    @{status='ownership_changed'} | ConvertTo-Json -Compress
    exit
}
$native = [NativeDesktopAccessibility]::Read([long]$q.hwnd, [int]$q.pid, $true)
$remaining = [double]$q.seconds - $clock.Elapsed.TotalSeconds
Read-DesktopDialogMetadata ([long]$q.hwnd) ([int]$q.pid) $remaining $null $native |
    ConvertTo-Json -Depth 7 -Compress
"""


def _dialog_metadata(hwnd: int, pid: int, timeout: float) -> dict:
    """Read-only accessibility snapshot in a killable, deadline-bound child."""
    return run_accessibility_script(
        _DIALOG_SCRIPT, {"hwnd": hwnd, "pid": pid, "seconds": timeout}, timeout)


def _classify_dialog_metadata(title: str, metadata: dict, *,
                              require_progress: bool = False) -> str:
    """busy|attention; cancellation alone is NOT a progress signal.

    ProgressBar and native IDCANCEL (2) are locale-independent. Text/title
    fallbacks explicitly support EN/FR only. Incomplete painting of a known
    transient title may delay opening, but can never permit a document capture.
    Strict observations require complete native/UIA progress structure: a bar
    or the observed Tableau layout/timer form, never title/cancellation alone.
    """
    if require_progress:
        # Consume the shared PS decision; never reproduce its criteria here.
        strict = metadata.get("strict_classification")
        return "busy" if (
            isinstance(strict, dict) and strict.get("basis") in {
                "shared_strict_uia", "tableau_layout_timer"}
            and strict.get("progress_required") is True and strict.get("complete") is True
            and strict.get("status") == "busy") else "attention"
    if (metadata.get("status") != "observed"
            or not metadata.get("native_complete", True)):
        return "attention"
    text = "\n".join([title] + [
        value for value in metadata.get("texts", []) if isinstance(value, str)])
    if _ATTENTION_TEXT.search(text):
        return "attention"
    buttons = metadata.get("buttons", [])
    cancel_only = all(
        isinstance(button, dict) and (
            button.get("control_id") == 2
            or str(button.get("name", "")).strip().casefold() in {"cancel", "annuler"})
        for button in buttons)
    if (metadata.get("interactive", 0) or metadata.get("truncated")
            or not cancel_only):
        return "attention"
    if _OPENING_TITLE.fullmatch(title.strip()) or _BUSY_TITLE.fullmatch(title.strip()):
        return "busy"
    if (metadata.get("status") == "observed"
            and metadata.get("native_complete", True) and (
            metadata.get("progress", 0) > 0
            or (buttons and _BUSY_TEXT.search(text)))):
        return "busy"
    return "attention"


def dialog_state(dialog: WindowInfo, pid: int, *, timeout: float = 3.0,
                 require_progress: bool = False) -> dict:
    """Read-only classification; strict mode never treats title-only as BUSY."""
    try:
        timeout = float(timeout)
        valid = math.isfinite(timeout) and timeout > 0
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not IS_WINDOWS or not valid:
        return {"status": "attention", "basis": "inspection_unavailable"}
    actual_pid = window_process_id(dialog.hwnd)
    if actual_pid is not None and actual_pid != pid:
        return {"status": "attention", "basis": "ownership_changed",
                "cause": "window_ownership_changed", "retryable": False,
                "owner_pid_before": actual_pid, "explicit_attention": True}
    if _ATTENTION_TEXT.search(dialog.title):
        return {"status": "attention", "basis": "attention_title",
                "explicit_attention": True}
    if actual_pid is None:
        return {"status": "vanished", "basis": "ownership_recheck",
                "cause": "dialog_disappeared", "retryable": True,
                "owner_pid_before": None, "explicit_attention": False}
    metadata = _dialog_metadata(dialog.hwnd, pid, min(timeout, 3.0))
    if is_powershell_failure(metadata):
        return {**metadata, "basis": "native_helper_failure", "metadata": metadata}
    texts = "\n".join(str(text) for text in metadata.get("texts", []))
    after_pid = window_process_id(dialog.hwnd)
    ownership = {"owner_pid_before": actual_pid, "owner_pid_after": after_pid}
    if metadata.get("status") == "ownership_changed" or after_pid != pid:
        # A popup destroyed while the child starts/reads is not a live ownership
        # violation. Never discard an observed error/auth/trust message, though.
        if after_pid is None and not _ATTENTION_TEXT.search(texts):
            return {"status": "vanished", "basis": "ownership_recheck",
                    "cause": "dialog_disappeared", "retryable": True,
                    "metadata": metadata, "explicit_attention": False, **ownership}
        # Any still-live handle (even back in the expected PID) is ambiguous or
        # reused after a failed ownership check. It cannot authorize readiness.
        return {"status": "attention", "basis": "ownership_changed",
                "cause": "window_ownership_changed", "retryable": False,
                "metadata": metadata, "explicit_attention": True, **ownership}
    if require_progress and any(
            not isinstance(metadata.get(key), dict)
            or metadata[key].get("hwnd") != dialog.hwnd
            or metadata[key].get("title") != dialog.title
            or metadata[key].get("class_name") != dialog.class_name
            for key in ("native_before", "native_after")):
        return {"status": "attention", "basis": "dialog_identity_recheck",
                "cause": "dialog_identity_unverified", "retryable": False,
                "metadata": metadata, "explicit_attention": True, **ownership}
    classification = _classify_dialog_metadata(
        dialog.title, metadata, require_progress=require_progress)
    strict = metadata.get("strict_classification")
    partial = (metadata.get("status") != "observed"
               or not metadata.get("native_complete", True)
               or (require_progress and (
                   not isinstance(strict, dict) or strict.get("complete") is not True)))
    # A recognized opening/layout title can precede its painted accessibility
    # children. This is WAIT-ONLY, explicitly partial evidence, never readiness.
    # Actual helper timeouts/tooling errors were returned above, not retried.
    if (not require_progress and partial
            and metadata.get("status") in {"observed", "unavailable", "timed_out"}
            and not metadata.get("truncated")
            and not metadata.get("interactive", 0)
            and not _ATTENTION_TEXT.search(texts)
            and (_OPENING_TITLE.fullmatch(dialog.title.strip())
                 or _BUSY_TITLE.fullmatch(dialog.title.strip()))
            and all(isinstance(button, dict) and (
                button.get("control_id") == 2
                or str(button.get("name", "")).strip().casefold() in {"cancel", "annuler"})
                for button in metadata.get("buttons", []))):
        classification = "busy"
    return {"status": classification,
            "basis": "native_progress_metadata", "metadata": metadata,
            **ownership,
            "cause": ("partial_provider_observation"
                      if partial or metadata.get("truncated") else None),
            "explicit_attention": bool(_ATTENTION_TEXT.search(texts))}


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


def window_process_id(hwnd: int) -> Optional[int]:
    """Return the owning process ID for a window, or None when unavailable."""
    if not IS_WINDOWS or not hwnd:
        return None
    try:
        owner_pid = wintypes.DWORD()
        if not _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid)):
            return None
        return int(owner_pid.value) or None
    except (OSError, ValueError, ctypes.ArgumentError):
        return None


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


def is_responsive(hwnd: int, timeout_ms: float = 2000) -> bool:
    """Check message pumping; fractional milliseconds are truncated for Win32."""
    if not IS_WINDOWS:
        return False
    try:
        result = ctypes.c_size_t()
        ok = _user32.SendMessageTimeoutW(
            hwnd, _WM_NULL, 0, 0, _SMTO_ABORTIFHUNG, max(1, int(timeout_ms)),
            ctypes.byref(result))
        return bool(ok)
    except (OSError, ValueError, TypeError, ctypes.ArgumentError):
        return False


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


def wait_for_window(
        pids: Union[int, Iterable[int], Callable[[], Iterable[int]]],
        title_fragment: str = "", *, timeout: float = 120,
        stable_for: float = 3.0,
        poll: float = 1.0) -> Tuple[Optional[WindowInfo], List[str]]:
    """Wait until one of *pids* shows a responsive report window.

    Returns ``(window, signals)``. Recognized transient progress consumes the
    SAME finite deadline and resets stability. Unknown/actionable dialogs stop
    the wait; none are dismissed. Signals are observations, not crash verdicts.
    """
    signals: List[str] = []
    if not IS_WINDOWS:
        return None, ["window inspection is Windows-only"]
    try:
        timeout, stable_for, poll = map(float, (timeout, stable_for, poll))
        valid = (all(math.isfinite(v) for v in (timeout, stable_for, poll))
                 and timeout > 0 and stable_for >= 0 and poll > 0)
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        return None, ["invalid window wait timeout/stability/poll"]
    if isinstance(pids, int):
        pids = [pids]
    elif not callable(pids):
        pids = tuple(pids)
    deadline = time.monotonic() + timeout
    matched_since: Optional[float] = None
    matched_identity = None
    seen_titles = set()

    while time.monotonic() < deadline:
        live = list(pids() if callable(pids) else pids)
        busy = False
        for pid in live:
            for dialog in dialog_windows(pid):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                state = dialog_state(dialog, pid, timeout=remaining)
                if is_powershell_failure(state):
                    signals.append(f"native helper failure: {state['cause']}")
                    return None, signals
                if state.get("cause") == "partial_provider_observation":
                    signal = f"partial provider observation: {dialog.title!r}"
                    if signal not in signals:
                        signals.append(signal)
                signal = (f"busy dialog: {dialog.title!r}" if state["status"] == "busy"
                          else f"error dialog: {dialog.title!r}")
                if state["status"] != "busy":
                    if (not state.get("explicit_attention")
                            and window_process_id(dialog.hwnd) is None):
                        signal = f"transient dialog disappeared: {dialog.title!r}"
                        if signal not in signals:
                            signals.append(signal)
                        busy = True
                        continue
                    if signal not in signals:
                        signals.append(signal)
                    return None, signals
                if signal not in signals:
                    signals.append(signal)
                busy = True
        if busy:
            matched_since = matched_identity = None
            time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
            continue

        win = report_window(live, title_fragment)
        if win is not None and win.title:
            seen_titles.add(win.title)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if win is not None and is_responsive(win.hwnd, min(2000, remaining * 1000)):
            now = time.monotonic()
            identity = (win.hwnd, win.title)
            if matched_since is None or matched_identity != identity:
                matched_since = now
                matched_identity = identity
            if now < deadline and now - matched_since >= stable_for:
                return win, signals
        else:
            matched_since = matched_identity = None
            for pid in live:
                other = main_window(pid)
                if other is not None and other.title:
                    seen_titles.add(other.title)
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))

    if not seen_titles:
        signals.append("no document window appeared before the timeout")
    else:
        signals.append(
            f"no stable responsive window titled {title_fragment!r} before the timeout "
            f"(saw: {', '.join(sorted(seen_titles)[:3])})")
    return None, signals


def wait_for_document_ready(hwnd: int, pid: int, *, timeout: float,
                            stable_for: float = 0.2, poll: float = 0.1) -> dict:
    """Bounded readiness for an EXACT owned handle, not a substitute document."""
    if not IS_WINDOWS:
        return {"status": "unavailable", "note": "Window inspection requires Windows"}
    try:
        timeout, stable_for, poll = map(float, (timeout, stable_for, poll))
        valid = (all(math.isfinite(v) for v in (timeout, stable_for, poll))
                 and timeout > 0 and stable_for >= 0 and poll > 0)
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        return {"status": "error", "note": "Invalid document wait budget"}
    deadline = time.monotonic() + timeout
    stable_since = None
    signals = []
    while time.monotonic() < deadline:
        if window_process_id(hwnd) != pid:
            return {"status": "error", "note": "Document window ownership changed"}
        dialogs = dialog_windows(pid)
        for dialog in dialogs:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            state = dialog_state(dialog, pid, timeout=remaining)
            if dialog.title not in signals:
                signals.append(dialog.title)
            if is_powershell_failure(state):
                return {**state, "signals": signals}
            if state.get("cause") == "partial_provider_observation":
                signal = f"partial provider observation: {dialog.title!r}"
                if signal not in signals:
                    signals.append(signal)
            if state["status"] != "busy":
                if (not state.get("explicit_attention")
                        and window_process_id(dialog.hwnd) is None):
                    signal = f"transient dialog disappeared: {dialog.title!r}"
                    if signal not in signals:
                        signals.append(signal)
                    continue
                return {"status": "inaccessible", "note": "Owned dialog requires manual attention",
                        "signals": signals, "dialog": state}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not dialogs and is_responsive(hwnd, min(2000, remaining * 1000)):
            now = time.monotonic()
            if stable_since is None:
                stable_since = now
            if now < deadline and now - stable_since >= stable_for:
                return {"status": "ready", "signals": signals}
        else:
            stable_since = None
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
    return {"status": "timed_out", "note": "Document remained busy or unresponsive before deadline",
            "signals": signals}


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
    if os.path.islink(path):
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

        if (not _user32.PrintWindow(hwnd, mem_dc, _PW_RENDERFULLCONTENT)
                and not _gdi32.BitBlt(mem_dc, 0, 0, width, height,
                                      window_dc, 0, 0, _SRCCOPY)):
            return None

        buf = ctypes.string_at(bits, width * height * 4)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags | no_follow, 0o600)
        with os.fdopen(fd, "wb") as handle:
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


def inspect_capture_png(png_path: str, sample_stride: int = 97) -> Optional[dict]:
    """Validate a complete native-writer PNG; uniform pixels are not data proof.

    Check chunk boundaries/CRCs, the complete deflate stream, IEND, dimensions
    and our RGB/filter-0 format. A partial write must not become page evidence.
    """
    try:
        with open(png_path, "rb") as handle:
            data = handle.read()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        offset, width, height = 8, 0, 0
        compressed = bytearray()
        ended = False
        while offset < len(data):
            size, tag = struct.unpack(">I4s", data[offset:offset + 8])
            end = offset + size + 12
            if end > len(data):
                return None
            payload = data[offset + 8:end - 4]
            crc = struct.unpack(">I", data[end - 4:end])[0]
            if binascii.crc32(tag + payload) & 0xFFFFFFFF != crc:
                return None
            if offset == 8:
                if tag != b"IHDR" or size != 13:
                    return None
                width, height, depth, colour, compression, filtering, interlace = (
                    struct.unpack(">IIBBBBB", payload))
                if (width <= 0 or height <= 0 or width * height > 100_000_000
                        or (depth, colour, compression, filtering, interlace)
                        != (8, 2, 0, 0, 0)):
                    return None
            elif tag == b"IDAT":
                compressed.extend(payload)
            elif tag == b"IEND":
                if size or end != len(data):
                    return None
                ended = True
            else:
                return None
            offset = end
        if not ended or not compressed:
            return None
        row_len = 1 + width * 3
        decoder = zlib.decompressobj()
        raw = decoder.decompress(bytes(compressed), row_len * height + 1)
        if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
                or len(raw) != row_len * height
                or any(raw[y * row_len] != 0 for y in range(height))):
            return None
        step = min(max(1, int(sample_stride)), max(1, width * height // 256))
    except (OSError, ValueError, TypeError, OverflowError, struct.error, zlib.error):
        return None
    total = width * height
    seen = set()
    for index in range(0, total, step):
        row, col = divmod(index, width)
        start = row * row_len + 1 + col * 3
        seen.add(raw[start:start + 3])
        if len(seen) > 1:
            return {"width": width, "height": height, "blank": False}
    return {"width": width, "height": height, "blank": True}


def is_blank(png_path: str, sample_stride: int = 97) -> Optional[bool]:
    """Uniform capture -> unknown/failed capture, never proof of an empty report."""
    info = inspect_capture_png(png_path, sample_stride)
    return info["blank"] if info else None
