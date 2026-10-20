"""Optional, stdlib-only Tableau window diagnostic. No migration integration.

Only the PID started here is inspected. If Tableau hands the document to an
existing instance, this diagnostic fails rather than borrowing that window.
The direct probe does not close its process; callers may defer cleanup through
the PID-owned cleanup helper after any additional capture work.
"""

from __future__ import annotations

import os
import math
from pathlib import Path
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from powerbi_import import desktop_window

CAPTURE_LIMITATION = (
    "A responsive document-titled window and nonblank full-window screenshot "
    "are diagnostic evidence only, not proof of loaded data or visual fidelity. "
    "Application chrome differs; review manually. The direct probe leaves the "
    "application open; comparison runs close only their launched PIDs after capture. "
    "Existing-instance handoff is not supported."
)

_OPEN_PROBE_PROCESSES: dict[int, subprocess.Popen] = {}


@dataclass
class TableauProbeReport:
    workbook_path: str
    status: str = "error"  # captured|unavailable|error|timed_out
    executable: Optional[str] = None
    pid: Optional[int] = None
    window_title: Optional[str] = None
    screenshot: Optional[str] = None
    screenshot_blank: Optional[bool] = None
    dialog_screenshot: Optional[str] = None
    duration_s: float = 0.0
    signals: list[str] = field(default_factory=list)
    note: str = ""
    limitation: str = CAPTURE_LIMITATION

    def to_dict(self) -> dict:
        return asdict(self)


def find_tableau_desktop(preferred_path: Optional[str] = None) -> Optional[str]:
    """Discover tableau.exe: explicit override, TABLEAU_DESKTOP_PATH, PATH, PF.

    An invalid override is not silently replaced by another installation.
    Program Files candidates use numeric version order (newest first), with a
    deterministic path tie-breaker.
    """
    preferred = preferred_path or os.environ.get("TABLEAU_DESKTOP_PATH")

    def valid(value):
        candidate = Path(os.path.expandvars(os.path.expanduser(str(value))))
        if candidate.is_file() and candidate.name.casefold() == "tableau.exe":
            return str(candidate.resolve())
        return None

    try:
        if preferred:
            return valid(preferred)
        on_path = shutil.which("tableau.exe")
        if on_path and valid(on_path):
            return valid(on_path)
        candidates = set()
        for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            base = os.environ.get(key)
            if base:
                candidates.update(Path(base).glob("Tableau/Tableau*/bin/tableau.exe"))
        def version_key(path):
            folder = path.parent.parent.name
            match = re.search(r"Tableau\s+(\d+(?:\.\d+)*)", folder, re.IGNORECASE)
            version = tuple(int(n) for n in match.group(1).split(".")) if match else ()
            return version, str(path).casefold()

        for candidate in sorted(candidates, key=version_key, reverse=True):
            found = valid(candidate)
            if found:
                return found
    except (OSError, ValueError):
        pass
    return None


def document_title_matches(title: str, workbook_stem: str) -> bool:
    """Conservative document match; 'Sales2' must not match 'Sales'.

    Supports document-only titles and Tableau's 'document - Tableau' or
    'Tableau - document' titles, including an unsaved-change asterisk.
    """
    wanted = workbook_stem.casefold()
    text = title.strip().casefold()
    if not wanted:
        return False
    documents = (wanted, wanted + ".twb", wanted + ".twbx")
    def normalize(value):
        # Tableau's bundled examples and locked workbooks append this marker;
        # it is application state, not part of the document name. Never strip
        # arbitrary bracket text (which may be a real workbook name).
        text = value.strip().rstrip("*").strip()
        if text.endswith(" [read-only]") and text not in documents:
            text = text[:-len(" [read-only]")].rstrip()
        return text
    if normalize(text) in documents:
        return True
    brands = ("tableau", "power bi desktop", "microsoft power bi desktop")
    for separator in (" - ", " — ", " – "):
        # Split at the application boundary, not at every dash in the workbook
        # name (e.g. 'Sales - East - Tableau').
        document, boundary, application = text.rpartition(separator)
        if (boundary and normalize(document) in documents
                and application.strip().startswith(brands)):
            return True
        application, boundary, document = text.partition(separator)
        if (boundary and application.strip().startswith(brands)
                and normalize(document) in documents):
            return True
    return False


def probe_tableau_open(workbook_path: str, *, screenshot_path: str,
                       desktop_path: Optional[str] = None,
                       timeout: float = 90, stable_for: float = 3,
                       poll: float = 0.5) -> TableauProbeReport:
    """Launch a .twb/.twbx without a shell, wait on its PID, capture nonblank PNG.

    Returns explicit failures, never infers document loading from survival.
    Destination must be new, so a previous screenshot cannot become evidence.
    Never terminates processes. Only window inspection/capture uses Win32.
    """
    report = TableauProbeReport(workbook_path=str(workbook_path))
    started = time.monotonic()
    proc = None
    try:
        source = Path(workbook_path).resolve()
        shot = Path(screenshot_path).absolute()
        if (not all(math.isfinite(v) for v in (timeout, stable_for, poll))
                or timeout <= 0 or stable_for < 0 or poll <= 0):
            raise ValueError("timeout/poll must be positive; stable_for nonnegative")
        if not source.is_file() or source.suffix.lower() not in (".twb", ".twbx"):
            raise ValueError("source must be an existing .twb or .twbx workbook")
        if shot.suffix.lower() != ".png":
            raise ValueError("screenshot destination must have .png extension")
        if shot.exists() or shot.is_symlink():
            raise ValueError("screenshot destination must not already exist")
        if any(parent.is_symlink() or parent.is_junction()
               for parent in shot.parents):
            raise ValueError("screenshot path contains a link or junction")
        if not desktop_window.IS_WINDOWS:
            report.status = "unavailable"
            report.note = "Tableau window capture requires an interactive Windows desktop"
            return report
        report.executable = find_tableau_desktop(desktop_path)
        if not report.executable:
            report.status = "unavailable"
            report.note = ("Tableau Desktop not found or invalid override; set "
                           "TABLEAU_DESKTOP_PATH or --tableau-desktop-path")
            return report
        shot.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([report.executable, str(source)], shell=False)
        report.pid = proc.pid
        deadline = time.monotonic() + timeout
        matched_since = None
        previous_hwnd = None
        while time.monotonic() < deadline:
            dialogs = desktop_window.dialog_windows(proc.pid)
            if dialogs:
                # Opening a workbook creates a transient owned progress window.
                # Wait for it to disappear, but never dismiss a real error dialog.
                loading = all(re.fullmatch(
                    r"Opening workbook '.+'\.\.\.", w.title) for w in dialogs)
                for dialog in dialogs:
                    if dialog.title not in report.signals:
                        report.signals.append(dialog.title)
                if loading:
                    matched_since = None
                    previous_hwnd = None
                    if proc.poll() is not None:
                        report.note = "Launcher exited while opening workbook"
                        return report
                    time.sleep(min(poll, max(0, deadline - time.monotonic())))
                    continue
                report.note = "Owned dialog requires manual attention"
                dialog_shot = shot.with_name(shot.stem + "-dialog.png")
                if dialog_shot.exists() or dialog_shot.is_symlink():
                    raise ValueError("dialog screenshot destination must not already exist")
                if (desktop_window.capture_window(dialogs[0].hwnd, str(dialog_shot))
                        and dialog_shot.is_file()
                        and desktop_window.is_blank(str(dialog_shot)) is False):
                    report.dialog_screenshot = str(dialog_shot)
                return report
            matches = [w for w in desktop_window.windows_for_pid(proc.pid)
                       if not w.owned and w.width >= 400 and w.height >= 300
                       and document_title_matches(w.title, source.stem)]
            if len(matches) > 1:
                report.note = "Ambiguous document windows in launched PID"
                return report
            win = matches[0] if matches else None
            if win and desktop_window.is_responsive(win.hwnd):
                if previous_hwnd != win.hwnd:
                    matched_since = time.monotonic()
                    previous_hwnd = win.hwnd
                if time.monotonic() - matched_since >= stable_for:
                    report.window_title = win.title
                    captured = desktop_window.capture_window(win.hwnd, str(shot))
                    report.screenshot_blank = (
                        desktop_window.is_blank(str(shot)) if captured else None)
                    if captured and shot.is_file() and report.screenshot_blank is False:
                        report.screenshot = str(shot)
                        report.status = "captured"
                    else:
                        report.note = "Screenshot unavailable, blank, or unreadable"
                    return report
            else:
                matched_since = None
                previous_hwnd = None
            if proc.poll() is not None:
                report.note = (f"Launcher exited rc={proc.returncode}; no responsive "
                               "document window in its PID (possible existing-instance handoff)")
                return report
            time.sleep(min(poll, max(0, deadline - time.monotonic())))
        report.status = "timed_out"
        report.note = "No stable responsive document-specific window before timeout"
    except Exception as exc:
        report.status = "error"
        report.note = f"{type(exc).__name__}: {exc}"
    finally:
        if proc is not None:
            _OPEN_PROBE_PROCESSES[proc.pid] = proc
        report.duration_s = round(time.monotonic() - started, 2)
    return report


def close_probe_process(pid: int) -> dict:
    """Close only a process launched and registered by this probe."""
    proc = _OPEN_PROBE_PROCESSES.pop(pid, None)
    if proc is None:
        return {"status": "not_owned", "note": "No probe-owned process is registered"}
    try:
        if proc.poll() is not None:
            return {"status": "already_exited", "note": "Probe process already exited"}
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        return {"status": "closed", "note": "Probe-owned process terminated"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "note": f"{type(exc).__name__}: {exc}"}
