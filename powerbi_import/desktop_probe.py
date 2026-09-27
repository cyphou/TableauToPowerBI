"""Best-effort Power BI Desktop open self-check (v44).

Complements the static :mod:`openability` preflight with an *actual* launch of
Power BI Desktop against a generated ``.pbip``. Power BI Desktop is a Windows GUI
application that cannot be driven or inspected headlessly, so this is a **best-effort
smoke test**, NOT an authoritative check — the static preflight remains the source
of truth for "will it open?".

What it does:
    1. Locate ``PBIDesktop.exe`` (PATH, Program Files, or pbi-tools).
    2. Launch it with the ``.pbip`` and note the launch time.
    3. Watch for an early crash (process exits quickly / non-zero) and for
       FrownDump crash dumps and error entries in the Desktop trace logs.
    4. Optionally terminate the process after a settle window.

Verdicts (DesktopProbeReport.status):
    unavailable   Desktop not installed → cannot probe (not a failure)
    opened        process stayed alive past the settle window, no crash/error signal
    crashed       process exited early / crash dump / error trace after launch
    timed_out     never reached a stable state within the timeout
    error         probe itself failed (bad path, launch error)

Public API:
    probe_desktop_open(pbip_path, *, settle=20, timeout=90, close_after=True)
    find_pbi_desktop() -> Optional[str]
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from powerbi_import import desktop_window

# Power BI Desktop trace + crash-dump locations (Windows).
_TRACE_GLOBS = (
    r"Microsoft\Power BI Desktop\Traces\*.log",
    r"Microsoft\Power BI Desktop Store App\Traces\*.log",
)
_FROWN_GLOBS = (
    r"Microsoft\Power BI Desktop\FrownDump*",
    r"Microsoft\Power BI Desktop Store App\FrownDump*",
)
_ERROR_TOKENS = ("error", "exception", "failed to load", "corrupt", "invalid")


#: What an ``opened`` verdict actually covers. Measured against deliberately
#: broken projects: a corrupted report.json, a deleted SemanticModel and
#: invalid DAX all still report ``opened``, because Desktop surfaces content
#: errors in a dialog and keeps running. The probe observes the process, not
#: the document.
VERIFIED_SCOPE = "process_survival"

#: Said plainly wherever the verdict is reported, so it cannot be read as
#: proof that the project loaded correctly.
OPENED_LIMITATION = (
    "'opened' means the Desktop process launched and survived the settle "
    "window without crashing. Desktop reports content errors in a dialog "
    "while staying alive, so this does not prove the model or report loaded. "
    "Static validation remains the authoritative content check."
)

#: Raised scope when the probe waited for the document window instead of a
#: fixed sleep: the window appeared, carried the report name and answered a
#: message, so the model finished loading.
WINDOW_VERIFIED_SCOPE = "window_loaded"

WINDOW_LIMITATION = (
    "'opened' means the report window appeared, carried the report name and "
    "became responsive, and a screenshot was captured. Power BI draws most "
    "content errors inside that window, so read the screenshot: an empty "
    "dialog list does not prove every visual rendered."
)


@dataclass
class DesktopProbeReport:
    pbip_path: str
    status: str = "error"          # unavailable|opened|crashed|timed_out|error
    executable: Optional[str] = None
    pid: Optional[int] = None
    duration_s: float = 0.0
    signals: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    note: str = ""
    window_title: Optional[str] = None
    screenshot: Optional[str] = None
    screenshot_blank: Optional[bool] = None
    dialog_screenshot: Optional[str] = None
    dialogs: List[str] = field(default_factory=list)

    @property
    def opened(self) -> bool:
        return self.status == "opened"

    @property
    def window_loaded(self) -> bool:
        return bool(self.window_title)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["opened"] = self.opened
        d["window_loaded"] = self.window_loaded
        d["verified"] = (WINDOW_VERIFIED_SCOPE if self.window_loaded
                          else VERIFIED_SCOPE)
        if self.opened:
            d["limitation"] = (WINDOW_LIMITATION if self.window_loaded
                               else OPENED_LIMITATION)
        return d


def find_pbi_desktop() -> Optional[str]:
    """Return the path to PBIDesktop.exe, or None if not installed."""
    for name in ("PBIDesktop", "PBIDesktop.exe"):
        found = shutil.which(name)
        if found:
            return found
    for base in (os.environ.get("ProgramFiles", ""),
                 os.environ.get("ProgramW6432", ""),
                 os.environ.get("ProgramFiles(x86)", "")):
        if not base:
            continue
        cand = os.path.join(base, "Microsoft Power BI Desktop", "bin",
                            "PBIDesktop.exe")
        if os.path.isfile(cand):
            return cand
    return None


def _recent_matches(globs, since: float) -> List[str]:
    """Files matching any glob under LOCALAPPDATA modified after ``since``."""
    out = []
    base = os.environ.get("LOCALAPPDATA", "")
    if not base:
        return out
    for pat in globs:
        for fp in glob.glob(os.path.join(base, pat)):
            try:
                if os.path.getmtime(fp) >= since - 1:
                    out.append(fp)
            except OSError:
                continue
    return out


def _scan_trace_errors(since: float) -> List[str]:
    signals = []
    for fp in _recent_matches(_TRACE_GLOBS, since):
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read().lower()
        except OSError:
            continue
        if any(tok in text for tok in _ERROR_TOKENS):
            signals.append(f"error tokens in trace {os.path.basename(fp)}")
    return signals


def probe_desktop_open(pbip_path: str, *, settle: int = 20, timeout: int = 90,
                       close_after: bool = True,
                       screenshot_path: Optional[str] = None,
                       wait_for_window: bool = True) -> DesktopProbeReport:
    """Launch Power BI Desktop against ``pbip_path`` and watch for load failure.

    With *wait_for_window* the probe waits for the document window to carry the
    report name and answer a message rather than sleeping for a fixed period,
    and *screenshot_path* captures what actually rendered.

    Best-effort: returns a DesktopProbeReport; never raises.
    """
    report = DesktopProbeReport(pbip_path=pbip_path)
    if not pbip_path or not os.path.isfile(pbip_path):
        report.status = "error"
        report.note = f".pbip not found: {pbip_path}"
        return report
    exe = find_pbi_desktop()
    if not exe:
        report.status = "unavailable"
        report.note = ("Power BI Desktop not found. Install it or open the .pbip "
                       "manually; the static --verify-open preflight is the "
                       "authoritative check.")
        return report
    report.executable = exe
    pre_existing = set(desktop_pids())
    if pre_existing and wait_for_window:
        report.warnings.append(
            f"{len(pre_existing)} Power BI Desktop instance(s) already running; "
            "close them for an unambiguous probe")

    start = time.time()
    try:
        proc = subprocess.Popen([exe, os.path.abspath(pbip_path)])
    except (OSError, ValueError) as exc:
        report.status = "error"
        report.note = f"failed to launch Desktop: {exc}"
        return report
    report.pid = proc.pid

    deadline = start + timeout
    settle_until = start + settle
    crashed = False
    try:
        if wait_for_window and desktop_window.IS_WINDOWS:
            crashed = _watch_window(report, proc, pbip_path, start, timeout,
                                    screenshot_path)
        else:
            crashed = _watch_process(report, proc, start, deadline, settle_until)

        report.signals.extend(_scan_trace_errors(start))
        alive = proc.poll() is None

        if crashed or report.signals:
            report.status = "crashed"
        elif alive:
            report.status = "opened"
            report.note = (WINDOW_LIMITATION if report.window_loaded
                           else "Desktop launched and stayed alive past the "
                                "settle window with no crash/error signal. "
                                + OPENED_LIMITATION)
        else:
            report.status = "timed_out"
    finally:
        if close_after:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                except OSError:
                    pass
            # The file may have been handed to a second instance we spawned.
            # Only close instances that did not exist before the launch.
            for pid in set(desktop_pids()) - pre_existing - {proc.pid}:
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True, timeout=15)
                except (OSError, subprocess.SubprocessError):
                    pass
    report.duration_s = round(time.time() - start, 1)
    return report


def _watch_process(report, proc, start, deadline, settle_until) -> bool:
    """Original behaviour: watch the process only. Returns True when crashed."""
    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            # Exited before settling → almost certainly a load failure.
            if time.time() < settle_until:
                report.signals.append(f"process exited early rc={rc}")
                return True
            break
        if _recent_matches(_FROWN_GLOBS, start):
            report.signals.append("FrownDump crash dump created")
            return True
        if time.time() >= settle_until:
            break
        time.sleep(1.0)
    return False


def desktop_pids() -> List[int]:
    """PIDs of every running PBIDesktop.exe (Windows; empty elsewhere)."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq PBIDesktop.exe", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=15).stdout
    except (OSError, TypeError, AttributeError, subprocess.SubprocessError):
        # A test double for Popen can break subprocess.run; never raise here.
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def _capture_dialogs(report, screenshot_path) -> None:
    """Record every open popup and screenshot the first one."""
    dialogs = [w for pid in desktop_pids()
               for w in desktop_window.dialog_windows(pid)]
    if not dialogs:
        return
    report.dialogs = [w.title for w in dialogs]
    if not screenshot_path:
        return
    base, ext = os.path.splitext(screenshot_path)
    os.makedirs(os.path.dirname(os.path.abspath(screenshot_path)),
                exist_ok=True)
    report.dialog_screenshot = desktop_window.capture_window(
        dialogs[0].hwnd, base + ".dialog" + (ext or ".png"))


def _capture_any(report, screenshot_path) -> None:
    """Screenshot the largest Power BI window, whatever it is showing."""
    if not screenshot_path:
        return
    best = None
    for pid in desktop_pids():
        win = desktop_window.main_window(pid)
        if win and (best is None or win.width * win.height >
                    best.width * best.height):
            best = win
    if best is None:
        return
    os.makedirs(os.path.dirname(os.path.abspath(screenshot_path)),
                exist_ok=True)
    # Deliberately not window_title: that field means "the report loaded".
    report.warnings.append(f"captured window titled {best.title!r}")
    report.screenshot = desktop_window.capture_window(best.hwnd,
                                                      screenshot_path)
    if report.screenshot:
        report.screenshot_blank = desktop_window.is_blank(report.screenshot)


def _watch_window(report, proc, pbip_path, start, timeout,
                  screenshot_path) -> bool:
    """Wait for the report window, then capture it. Returns True when crashed."""
    expected = os.path.splitext(os.path.basename(pbip_path))[0]
    window, signals = desktop_window.wait_for_window(
        desktop_pids, expected, timeout=timeout)

    if window is None:
        # Power BI hands the file to an existing instance and the launcher
        # exits, so an exited process is only a crash when no window appeared.
        if proc.poll() is not None:
            report.signals.append(
                f"process exited and no report window appeared "
                f"rc={proc.returncode}")
        report.signals.extend(signals)
        if _recent_matches(_FROWN_GLOBS, start):
            report.signals.append("FrownDump crash dump created")
        # An error popup is what makes a project unusable, so keep its own
        # picture: it carries the message, and it is a separate window that a
        # capture of the main window would not show.
        _capture_dialogs(report, screenshot_path)
        _capture_any(report, screenshot_path)
        return True

    if _recent_matches(_FROWN_GLOBS, start):
        report.signals.append("FrownDump crash dump created")
        return True

    report.window_title = window.title
    _capture_dialogs(report, screenshot_path)
    for title in report.dialogs:
        report.signals.append(f"error dialog: {title!r}")

    if screenshot_path:
        os.makedirs(os.path.dirname(os.path.abspath(screenshot_path)),
                    exist_ok=True)
        report.screenshot = desktop_window.capture_window(
            window.hwnd, screenshot_path)
        if report.screenshot:
            report.screenshot_blank = desktop_window.is_blank(report.screenshot)
    return False


def probe_desktop_reopen(pbip_path: str, *, settle: int = 20, timeout: int = 90,
                         close_after: bool = True) -> DesktopProbeReport:
    """Open the same project twice and report a successful second launch.

    This verifies repeated loading only; it does not claim that Desktop saved
    or persisted changes between launches.
    """
    first = probe_desktop_open(pbip_path, settle=settle, timeout=timeout,
                               close_after=close_after)
    if first.status != "opened":
        first.note = "Initial Desktop open did not reach a healthy state."
        return first
    second = probe_desktop_open(pbip_path, settle=settle, timeout=timeout,
                                close_after=close_after)
    second.signals = [f"initial: {signal}" for signal in first.signals] + second.signals
    if second.status == "opened":
        second.status = "reopened"
        second.note = "Two consecutive Desktop launches completed without observed load errors."
    return second
