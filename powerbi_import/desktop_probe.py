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

import base64
import glob
import json
import os
import re
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

DATA_LOAD_LIMITATION = (
    "Data-load verification could not prove usable rows. Pending Power Query "
    "queries, unapplied changes, or unavailable source data may require user "
    "action in Power BI Desktop."
)

_TMDL_TABLE_RE = re.compile(r"^table\s+(.+?)\s*$", re.MULTILINE)
_TMDL_M_PARTITION_RE = re.compile(
    r"^\s*partition\s+.+?\s*=\s*m\s*$", re.MULTILINE)
_DATA_LOAD_KEYS = (
    "status", "tables_checked", "tables_nonempty", "tables_empty",
    "tables_failed", "total_rows",
)
_DESKTOP_PIDS_QUERY_FAILED = False

_ADOMD_VERIFY_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$result = [ordered]@{
    status = 'unavailable'; tables_checked = 0; tables_nonempty = 0
    tables_empty = 0; tables_failed = 0; total_rows = 0
}
try {
    $requestJson = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String([Console]::In.ReadToEnd().Trim()))
    $request = ConvertFrom-Json -InputObject $requestJson
    $ownerPid = [int]$request.owner_pid
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $byId = @{}
    foreach ($process in $processes) { $byId[[int]$process.ProcessId] = $process }
    if (-not $byId.ContainsKey($ownerPid)) { throw 'owner unavailable' }

    $descendants = New-Object 'System.Collections.Generic.HashSet[int]'
    $pending = New-Object System.Collections.Queue
    $null = $descendants.Add($ownerPid)
    $pending.Enqueue($ownerPid)
    while ($pending.Count -gt 0) {
        $parent = [int]$pending.Dequeue()
        foreach ($process in $processes) {
            if ([int]$process.ParentProcessId -eq $parent) {
                $child = [int]$process.ProcessId
                if ($descendants.Add($child)) { $pending.Enqueue($child) }
            }
        }
    }
    $engines = @($processes | Where-Object {
        $_.Name -ieq 'msmdsrv.exe' -and $descendants.Contains([int]$_.ProcessId)
    })
    if ($engines.Count -ne 1) { throw 'engine unavailable or ambiguous' }

    $engine = $engines[0]
    $ports = @(Get-NetTCPConnection -State Listen `
        -OwningProcess ([int]$engine.ProcessId) -ErrorAction Stop |
        Select-Object -ExpandProperty LocalPort -Unique)
    if ($ports.Count -ne 1) { throw 'engine port unavailable or ambiguous' }
    $owner = $byId[$ownerPid]
    $dllPath = Join-Path (Split-Path -Parent $owner.ExecutablePath) `
        'Microsoft.PowerBI.AdomdClient.dll'
    if (-not (Test-Path -LiteralPath $dllPath -PathType Leaf)) {
        throw 'ADOMD client unavailable'
    }
    Add-Type -Path $dllPath -ErrorAction Stop
    $connection = New-Object Microsoft.AnalysisServices.AdomdClient.AdomdConnection(
        "Data Source=localhost:$($ports[0]);Connect Timeout=10")
    try {
        $connection.Open()
        foreach ($table in $request.tables) {
            $result.tables_checked++
            $escaped = ([string]$table).Replace("'", "''")
            $command = $connection.CreateCommand()
            $command.CommandTimeout = 8
            $command.CommandText = "EVALUATE ROW(`"Rows`", COUNTROWS('$escaped'))"
            $reader = $null
            try {
                $reader = $command.ExecuteReader()
                if (-not $reader.Read()) { throw 'query returned no aggregate' }
                $rows = [long]$reader.GetValue(0)
                if ($rows -gt 0) { $result.tables_nonempty++ }
                else { $result.tables_empty++ }
                $result.total_rows += $rows
            }
            catch {
                $result.tables_failed++
            }
            finally {
                if ($reader) { $reader.Dispose() }
                $command.Dispose()
            }
        }
        if ($result.tables_failed -gt 0) { $result.status = 'query_failed' }
        elseif ($result.total_rows -gt 0) { $result.status = 'verified' }
        else { $result.status = 'empty' }
    }
    finally { $connection.Dispose() }
}
catch { $result.status = 'unavailable' }
[Console]::Out.WriteLine((ConvertTo-Json -InputObject $result -Compress))
'''


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
    data_load: dict = field(default_factory=lambda: {
        "status": "not_requested", "tables_checked": 0,
        "tables_nonempty": 0, "tables_empty": 0, "tables_failed": 0,
        "total_rows": 0,
    })

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


def find_pbi_desktop(preferred_path: Optional[str] = None) -> Optional[str]:
    """Return a validated PBIDesktop.exe path, or None if unavailable.

    ``preferred_path`` and ``POWERBI_DESKTOP_PATH`` support client machines
    with a custom or centrally managed installation location.
    """
    preferred = preferred_path or os.environ.get("POWERBI_DESKTOP_PATH", "")
    if preferred:
        candidate = os.path.abspath(os.path.expandvars(os.path.expanduser(preferred)))
        if os.path.isfile(candidate) and os.path.basename(candidate).lower() == "pbidesktop.exe":
            return candidate
        return None
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
                text = fh.read(1024 * 1024).lower()
        except OSError:
            continue
        if any(tok in text for tok in _ERROR_TOKENS):
            signals.append(f"error tokens in trace {os.path.basename(fp)}")
    return signals


def probe_desktop_open(pbip_path: str, *, settle: int = 20, timeout: int = 90,
                       close_after: bool = True,
                       screenshot_path: Optional[str] = None,
                       wait_for_window: bool = True,
                       verify_data: bool = False,
                       desktop_path: Optional[str] = None) -> DesktopProbeReport:
    """Launch Power BI Desktop against ``pbip_path`` and watch for load failure.

    With *wait_for_window* the probe waits for the document window to carry the
    report name and answer a message rather than sleeping for a fixed period,
    and *screenshot_path* captures what actually rendered.

    Best-effort: returns a DesktopProbeReport; never raises.
    """
    report = DesktopProbeReport(pbip_path=pbip_path)
    if screenshot_path and not _safe_output_path(screenshot_path):
        report.status = "error"
        report.note = "screenshot path contains a symbolic-link component"
        return report
    if not pbip_path or not os.path.isfile(pbip_path):
        report.status = "error"
        report.note = f".pbip not found: {pbip_path}"
        return report
    exe = find_pbi_desktop(desktop_path)
    if not exe:
        report.status = "unavailable"
        report.note = ("Power BI Desktop executable not found or invalid. Set "
                       "POWERBI_DESKTOP_PATH or --powerbi-desktop-path, install it, "
                       "or open the .pbip "
                       "manually; the static --verify-open preflight is the "
                       "authoritative check.")
        return report
    report.executable = exe
    pre_existing = set(desktop_pids())
    if os.name == "nt" and wait_for_window and _DESKTOP_PIDS_QUERY_FAILED:
        report.status = "error"
        report.note = "cannot enumerate existing Power BI Desktop processes safely"
        return report
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
                                    screenshot_path, verify_data, pre_existing)
        else:
            crashed = _watch_process(report, proc, start, deadline, settle_until)
            if verify_data:
                report.data_load = _data_load_result("unavailable")

        report.signals.extend(_scan_trace_errors(start))
        alive = proc.poll() is None

        if crashed or report.signals:
            report.status = "crashed"
        elif alive:
            report.status = "opened"
            if report.data_load["status"] not in ("not_requested", "verified"):
                report.note = DATA_LOAD_LIMITATION
            else:
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
    global _DESKTOP_PIDS_QUERY_FAILED
    _DESKTOP_PIDS_QUERY_FAILED = False
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq PBIDesktop.exe", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=15).stdout
    except (OSError, TypeError, AttributeError, subprocess.SubprocessError):
        _DESKTOP_PIDS_QUERY_FAILED = True
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def _capture_dialogs(report, screenshot_path, allowed_pids) -> None:
    """Record every open popup and screenshot the first one."""
    dialogs = [w for pid in allowed_pids
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


def _safe_output_path(path: str) -> bool:
    """Return false when an existing path component is a symbolic link."""
    current = os.path.abspath(path)
    while current and current != os.path.dirname(current):
        if os.path.lexists(current) and os.path.islink(current):
            return False
        current = os.path.dirname(current)
    return True


def _capture_any(report, screenshot_path, allowed_pids) -> None:
    """Screenshot only a window owned by a process launched by this probe."""
    if not screenshot_path:
        return
    best = None
    for pid in allowed_pids:
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
                  screenshot_path, verify_data=False, pre_existing=None) -> bool:
    """Wait for the report window, then capture it. Returns True when crashed."""
    pre_existing = pre_existing or set()
    allowed_pids = set(desktop_pids()) - set(pre_existing)
    allowed_pids.add(proc.pid)
    if verify_data:
        report.data_load = _data_load_result("unavailable")
    expected = os.path.splitext(os.path.basename(pbip_path))[0]
    launched_pids = lambda: [pid for pid in desktop_pids()
                             if pid not in pre_existing]
    window, signals = desktop_window.wait_for_window(
        launched_pids, expected, timeout=timeout)

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
        _capture_dialogs(report, screenshot_path, allowed_pids)
        _capture_any(report, screenshot_path, allowed_pids)
        return True

    if _recent_matches(_FROWN_GLOBS, start):
        report.signals.append("FrownDump crash dump created")
        return True

    report.window_title = window.title
    _capture_dialogs(report, screenshot_path, allowed_pids)
    for title in report.dialogs:
        report.signals.append(f"error dialog: {title!r}")

    if verify_data:
        owner_pid = desktop_window.window_process_id(window.hwnd)
        report.data_load = verify_desktop_data(pbip_path, owner_pid)
        if report.data_load["status"] != "verified":
            report.note = DATA_LOAD_LIMITATION
            return False

    if screenshot_path:
        os.makedirs(os.path.dirname(os.path.abspath(screenshot_path)),
                    exist_ok=True)
        report.screenshot = desktop_window.capture_window(
            window.hwnd, screenshot_path)
        if report.screenshot:
            report.screenshot_blank = desktop_window.is_blank(report.screenshot)
    return False


def _data_load_result(status="unavailable", *, checked=0, nonempty=0,
                      empty=0, failed=0, rows=0):
    return {
        "status": status,
        "tables_checked": checked,
        "tables_nonempty": nonempty,
        "tables_empty": empty,
        "tables_failed": failed,
        "total_rows": rows,
    }


def _m_backed_tables(pbip_path: str):
    """Return local TMDL table names with M partitions, or None if ambiguous."""
    project_dir = os.path.dirname(os.path.abspath(pbip_path))
    model_dirs = glob.glob(os.path.join(project_dir, "*.SemanticModel"))
    if len(model_dirs) != 1:
        return None
    tables_dir = os.path.join(model_dirs[0], "definition", "tables")
    table_names = []
    for path in glob.glob(os.path.join(tables_dir, "*.tmdl")):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeError):
            return None
        table_match = _TMDL_TABLE_RE.search(text)
        if not table_match or not _TMDL_M_PARTITION_RE.search(text):
            continue
        name = table_match.group(1).strip()
        if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
            name = name[1:-1].replace("''", "'")
        if name:
            table_names.append(name)
    return table_names


def verify_desktop_data(pbip_path: str, owner_pid: Optional[int], *,
                        timeout: int = 60) -> dict:
    """Verify M-backed tables through the Analysis Services engine owned by HWND.

    Only aggregate counts/status are returned. Query details and exceptions are
    deliberately kept out of stdout, warnings, and the report.
    """
    tables = _m_backed_tables(pbip_path)
    if tables is None or not owner_pid or not desktop_window.IS_WINDOWS:
        return _data_load_result("unavailable")
    if not tables:
        return _data_load_result("no_m_tables")

    request = json.dumps({"owner_pid": int(owner_pid), "tables": tables},
                         ensure_ascii=True).encode("utf-8")
    encoded = base64.b64encode(request).decode("ascii")
    try:
        bounded_timeout = max(5, min(int(timeout), 120))
    except (TypeError, ValueError):
        return _data_load_result("unavailable")
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             _ADOMD_VERIFY_SCRIPT],
            input=encoded, capture_output=True, text=True,
            timeout=bounded_timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return _data_load_result("unavailable")
    if completed.returncode != 0:
        return _data_load_result("unavailable")
    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        if not isinstance(result, dict):
            raise ValueError
        values = {key: result[key] for key in _DATA_LOAD_KEYS}
        if values["status"] not in {
                "verified", "empty", "query_failed", "unavailable"}:
            raise ValueError
        if any(not isinstance(values[key], int) or values[key] < 0
               for key in _DATA_LOAD_KEYS[1:]):
            raise ValueError
        counts_complete = (
            values["tables_checked"] == len(tables)
            and values["tables_nonempty"] + values["tables_empty"]
            + values["tables_failed"] == len(tables)
        )
        if values["status"] == "verified" and (
                not counts_complete or values["tables_failed"]
                or values["total_rows"] <= 0):
            raise ValueError
        if values["status"] == "empty" and (
                not counts_complete or values["tables_failed"]
                or values["total_rows"] != 0):
            raise ValueError
        if values["status"] == "query_failed" and not values["tables_failed"]:
            raise ValueError
        return values
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return _data_load_result("unavailable")


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
