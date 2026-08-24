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


@dataclass
class DesktopProbeReport:
    pbip_path: str
    status: str = "error"          # unavailable|opened|crashed|timed_out|error
    executable: Optional[str] = None
    pid: Optional[int] = None
    duration_s: float = 0.0
    signals: List[str] = field(default_factory=list)
    note: str = ""

    @property
    def opened(self) -> bool:
        return self.status == "opened"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["opened"] = self.opened
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
                       close_after: bool = True) -> DesktopProbeReport:
    """Launch Power BI Desktop against ``pbip_path`` and watch for load failure.

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
        while time.time() < deadline:
            rc = proc.poll()
            if rc is not None:
                # Exited before settling → almost certainly a load failure.
                if time.time() < settle_until:
                    crashed = True
                    report.signals.append(f"process exited early rc={rc}")
                break
            if _recent_matches(_FROWN_GLOBS, start):
                crashed = True
                report.signals.append("FrownDump crash dump created")
                break
            if time.time() >= settle_until:
                break
            time.sleep(1.0)

        report.signals.extend(_scan_trace_errors(start))
        alive = proc.poll() is None

        if crashed or (report.signals and not alive):
            report.status = "crashed"
        elif alive:
            report.status = "opened"
            report.note = ("Best-effort: Desktop launched and stayed alive past the "
                           "settle window with no crash/error signal.")
        else:
            report.status = "timed_out"
    finally:
        if close_after and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except OSError:
                pass
    report.duration_s = round(time.time() - start, 1)
    return report
