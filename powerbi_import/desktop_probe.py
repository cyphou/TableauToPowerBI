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
import math
import ntpath
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
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

REFRESH_LIMITATION = (
    "Refresh completion was not verified in Power BI Desktop. Aggregate row "
    "counts describe observed model data only and may reflect prior or partial "
    "loads; they do not prove an error-free refresh."
)

_TMDL_TABLE_RE = re.compile(r"^table\s+(.+?)\s*$", re.MULTILINE)
_TMDL_M_PARTITION_RE = re.compile(
    r"^[\t ]*partition[\t ]+(.+?)[\t ]*=[\t ]*m[\t ]*$", re.MULTILINE)
_PARTITION_TIME_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,7}))?(Z|[+-]\d{2}:\d{2})?")
_DATA_LOAD_KEYS = (
    "status", "tables_checked", "tables_nonempty", "tables_empty",
    "tables_failed", "total_rows",
)
_DESKTOP_PIDS_QUERY_FAILED = False
_OPEN_PROBE_PROCESSES: dict[int, subprocess.Popen] = {}

_ADOMD_VERIFY_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$result = [ordered]@{
    status = 'unavailable'; tables_checked = 0; tables_nonempty = 0
    tables_empty = 0; tables_failed = 0; total_rows = 0
}
$partitionMode = $false
$failureNote = 'invalid_request'
try {
    $requestJson = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String([Console]::In.ReadToEnd().Trim()))
    $request = ConvertFrom-Json -InputObject $requestJson
    $ownerPid = [int]$request.owner_pid
    $partitionMode = $request.operation -eq 'partitions'
    if ($partitionMode) {
        $result = [ordered]@{
            status = 'unavailable'; note = 'invalid_request'
            owner_pid = $ownerPid; engine_pid = 0; port = 0
            partitions = @(); unexpected = @()
        }
    }
    $failureNote = 'process_query_failed'
    # Project only binding fields; a full Win32_Process query fetches many
    # unrelated properties for every process on each observation.
    $processes = @(Get-CimInstance Win32_Process `
        -Property ProcessId,ParentProcessId,Name,ExecutablePath -ErrorAction Stop)
    $byId = @{}
    foreach ($process in $processes) { $byId[[int]$process.ProcessId] = $process }
    $failureNote = 'owner_unavailable'
    if (-not $byId.ContainsKey($ownerPid)) { throw 'owner unavailable' }
    if ($byId[$ownerPid].Name -ine 'PBIDesktop.exe') { throw 'owner not Desktop' }

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
    $failureNote = 'engine_unavailable_or_ambiguous'
    if ($engines.Count -ne 1) { throw 'engine unavailable or ambiguous' }

    $engine = $engines[0]
    if ($partitionMode) { $result.engine_pid = [int]$engine.ProcessId }
    $failureNote = 'port_query_failed'
    # Use the same TCP provider without loading the NetTCPIP cmdlet module.
    # MSFT_NetTCPConnection State 2 is Listen; retain exact PID/unique-port binding.
    $ports = @(Get-CimInstance -Namespace root/StandardCimv2 `
        -ClassName MSFT_NetTCPConnection `
        -Filter "State = 2 AND OwningProcess = $([int]$engine.ProcessId)" `
        -Property LocalPort -ErrorAction Stop |
        Select-Object -ExpandProperty LocalPort -Unique)
    $failureNote = 'port_unavailable_or_ambiguous'
    if ($ports.Count -ne 1) { throw 'engine port unavailable or ambiguous' }
    if ($partitionMode) { $result.port = [int]$ports[0] }
    $owner = $byId[$ownerPid]
    $failureNote = 'adomd_client_unavailable'
    $dllPath = Join-Path (Split-Path -Parent $owner.ExecutablePath) `
        'Microsoft.PowerBI.AdomdClient.dll'
    if (-not (Test-Path -LiteralPath $dllPath -PathType Leaf)) {
        throw 'ADOMD client unavailable'
    }
    Add-Type -Path $dllPath -ErrorAction Stop
    $failureNote = 'connection_failed'
    $connection = New-Object Microsoft.AnalysisServices.AdomdClient.AdomdConnection(
        "Data Source=localhost:$($ports[0]);Connect Timeout=10")
    try {
        $connection.Open()
        if ($partitionMode) {
            # Metadata only: never return expressions, source details, records,
            # or ErrorMessage text. Both operations share the exact owner binding.
            $failureNote = 'metadata_query_failed'
            $tableIds = @{}
            $command = $connection.CreateCommand()
            $command.CommandTimeout = 8
            $command.CommandText = 'SELECT [ID], [Name] FROM $SYSTEM.TMSCHEMA_TABLES'
            $reader = $null
            try {
                $reader = $command.ExecuteReader()
                while ($reader.Read()) {
                    $name = [string]$reader['Name']
                    if (@($request.tables) -ccontains $name) {
                        $id = [Convert]::ToString($reader['ID'],
                            [Globalization.CultureInfo]::InvariantCulture)
                        $tableIds[$id] = $name
                    }
                }
            }
            finally {
                if ($reader) { $reader.Dispose() }
                $command.Dispose()
            }
            $command = $connection.CreateCommand()
            $command.CommandTimeout = 8
            $command.CommandText = 'SELECT [ID], [TableID], [Name], [Type], [State], [RefreshedTime], [ErrorMessage] FROM $SYSTEM.TMSCHEMA_PARTITIONS'
            $reader = $null
            try {
                $reader = $command.ExecuteReader()
                while ($reader.Read()) {
                    $tableId = [Convert]::ToString($reader['TableID'],
                        [Globalization.CultureInfo]::InvariantCulture)
                    if (-not $tableIds.ContainsKey($tableId) -or
                        [int]$reader['Type'] -ne 4) { continue } # M partitions only
                    $table = $tableIds[$tableId]
                    $partition = [string]$reader['Name']
                    $partitionId = [Convert]::ToString($reader['ID'],
                        [Globalization.CultureInfo]::InvariantCulture)
                    $expected = @($request.partitions | Where-Object {
                        $_.table -ceq $table -and $_.partition -ceq $partition
                    })
                    if ($expected.Count -ne 1) {
                        # Do not include unrelated partition contents, but never
                        # hide added M partitions in an expected table.
                        $result.unexpected += [ordered]@{
                            table_id = $tableId; partition_id = $partitionId
                        }
                        continue
                    }
                    $stamp = $reader['RefreshedTime']
                    $refreshedTime = $null
                    if ($stamp -is [DateTime]) {
                        $refreshedTime = $stamp.ToString('o',
                            [Globalization.CultureInfo]::InvariantCulture)
                    }
                    $result.partitions += [ordered]@{
                        table = $table; partition = $partition
                        table_id = $tableId; partition_id = $partitionId
                        refreshed_time = $refreshedTime
                        state = [int]$reader['State']
                        has_error = -not [string]::IsNullOrEmpty(
                            [string]$reader['ErrorMessage'])
                    }
                }
            }
            finally {
                if ($reader) { $reader.Dispose() }
                $command.Dispose()
            }
            $result.status = 'observed'
            $result.note = 'metadata_observed'
        }
        else {
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
                catch { $result.tables_failed++ }
                finally {
                    if ($reader) { $reader.Dispose() }
                    $command.Dispose()
                }
            }
            if ($result.tables_failed -gt 0) { $result.status = 'query_failed' }
            elseif ($result.tables_checked -gt 0 -and
                    $result.tables_nonempty -eq $result.tables_checked) {
                $result.status = 'verified'
            }
            elseif ($result.tables_nonempty -gt 0) { $result.status = 'partial' }
            else { $result.status = 'empty' }
        }
    }
    finally { $connection.Dispose() }
}
catch {
    $result.status = 'unavailable'
    if ($partitionMode) {
        $result.note = $failureNote
        if ($failureNote -eq 'metadata_query_failed') { $result.status = 'query_failed' }
    }
}
[Console]::Out.WriteLine((ConvertTo-Json -InputObject $result -Depth 6 -Compress))
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
    screenshot_data_verified: Optional[bool] = None
    dialogs: List[str] = field(default_factory=list)
    data_load: dict = field(default_factory=lambda: {
        "status": "not_requested", "tables_checked": 0,
        "tables_nonempty": 0, "tables_empty": 0, "tables_failed": 0,
        "total_rows": 0,
    })
    refresh: dict = field(default_factory=lambda: {
        "status": "not_requested", "clicks": 0, "waited_s": 0.0,
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


def _report_definition_launch_path(pbip_path: str) -> str:
    """Resolve exactly one declared, project-contained report definition."""
    with open(pbip_path, encoding="utf-8-sig") as handle:
        manifest = json.load(handle)
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(artifacts, list):
        raise ValueError("PBIP artifacts must be a list containing one report")
    reports = [item["report"] for item in artifacts
               if isinstance(item, dict) and "report" in item]
    if len(reports) != 1:
        raise ValueError("PBIP must declare exactly one report artifact "
                         "(missing or ambiguous report)")
    path = reports[0].get("path") if isinstance(reports[0], dict) else None
    if not isinstance(path, str) or not path.strip():
        raise ValueError("PBIP report artifact path is missing or invalid")
    # Check Windows syntax even when validation runs on a non-Windows host.
    parts = path.replace("\\", "/").split("/")
    if (ntpath.isabs(path) or ntpath.splitdrive(path)[0]
            or ".." in parts or ":" in path or "\x00" in path):
        raise ValueError("PBIP report artifact path must be project-relative "
                         "without traversal")
    root = os.path.realpath(os.path.dirname(os.path.abspath(pbip_path)))
    definition = os.path.realpath(
        os.path.join(root, *parts, "definition.pbir"))
    if os.path.commonpath([root, definition]) != root:
        raise ValueError("PBIP report definition escapes the project directory")
    if not os.path.isfile(definition):
        raise ValueError("PBIP report artifact has no existing definition.pbir")
    return definition


def probe_desktop_open(pbip_path: str, *, settle: int = 20, timeout: float = 90,
                       close_after: bool = True,
                       screenshot_path: Optional[str] = None,
                       wait_for_window: bool = True,
                       verify_data: bool = False,
                       desktop_path: Optional[str] = None,
                       refresh: bool = False,
                       refresh_timeout: float = 300.0,
                       capture_unverified: bool = False,
                       launch_report_definition: bool = False) -> DesktopProbeReport:
    """Launch Power BI Desktop against ``pbip_path`` and watch for load failure.

    With *wait_for_window* the probe waits for the document window to carry the
    report name and answer a message rather than sleeping for a fixed period,
    and *screenshot_path* captures what actually rendered.

    *launch_report_definition* explicitly launches the single report artifact's
    ``definition.pbir`` with the project working directory, instead of the PBIP.
    This diagnostic mode does not verify PBIP launching; the report retains the
    original PBIP identity and window-title expectation. No fallback is attempted.

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
    launch_path = os.path.abspath(pbip_path)
    if launch_report_definition:
        try:
            launch_path = _report_definition_launch_path(pbip_path)
        except (OSError, ValueError, RecursionError) as exc:
            report.note = f"invalid report-definition diagnostic launch: {exc}"
            return report
        report.warnings.append(
            f"Diagnostic launch target: {launch_path} (definition.pbir), "
            f"not {os.path.abspath(pbip_path)}. This does not verify PBIP launching.")
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
    proc = None
    try:
        if launch_report_definition:
            proc = subprocess.Popen(
                [exe, launch_path], cwd=os.path.dirname(os.path.abspath(pbip_path)))
        else:
            proc = subprocess.Popen([exe, launch_path])
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
                                    screenshot_path, verify_data, pre_existing,
                                    refresh, refresh_timeout, capture_unverified)
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
            if refresh and report.refresh["status"] != "verified":
                report.note = REFRESH_LIMITATION
            elif (not refresh and report.data_load["status"]
                  not in ("not_requested", "verified")):
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
        elif proc is not None:
            _OPEN_PROBE_PROCESSES[proc.pid] = proc
    report.duration_s = round(time.time() - start, 1)
    return report


def close_probe_process(pid: int) -> dict:
    """Close only a live process started by this probe."""
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
                  screenshot_path, verify_data=False, pre_existing=None,
                  refresh=False, refresh_timeout: float = 300.0,
                  capture_unverified=False) -> bool:
    """Wait for the report window, then capture it. Returns True when crashed."""
    pre_existing = pre_existing or set()
    allowed_pids = set(desktop_pids()) - set(pre_existing)
    allowed_pids.add(proc.pid)
    if verify_data:
        report.data_load = _data_load_result("unavailable")
    expected = os.path.splitext(os.path.basename(pbip_path))[0]

    def launched_pids():
        return [pid for pid in desktop_pids() if pid not in pre_existing]

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

    owner_pid = desktop_window.window_process_id(window.hwnd)
    if refresh:
        report.refresh = refresh_report(window.hwnd, pbip_path, owner_pid,
                                        timeout=refresh_timeout)
        report.data_load = report.refresh["data_load"]
        # A refresh failure surfaces as an owned popup; keep its picture.
        _capture_dialogs(report, screenshot_path, allowed_pids)

    if verify_data and not refresh and report.data_load["status"] != "verified":
        report.data_load = verify_desktop_data(pbip_path, owner_pid)
    if verify_data:
        report.screenshot_data_verified = (
            report.refresh["status"] == "verified" if refresh
            else report.data_load["status"] == "verified")
        if not report.screenshot_data_verified:
            report.note = (
                REFRESH_LIMITATION if refresh
                and report.refresh["status"] != "verified"
                else DATA_LOAD_LIMITATION)
            if not capture_unverified:
                return False

    if screenshot_path:
        os.makedirs(os.path.dirname(os.path.abspath(screenshot_path)),
                    exist_ok=True)
        if refresh and report.refresh.get("clicks"):
            _wait_for_render(window.hwnd, screenshot_path)
            # Rendering can take long enough for a late refresh failure to surface.
            state = _refresh_ui_state(window.hwnd, owner_pid)
            report.refresh.setdefault("ui_state", {})["after_render"] = state
            if state not in {"clear", "completed"}:
                report.refresh["status"] = state
                report.screenshot_data_verified = False
                report.note = REFRESH_LIMITATION
        report.screenshot = desktop_window.capture_window(
            window.hwnd, screenshot_path)
        if report.screenshot:
            report.screenshot_blank = desktop_window.is_blank(report.screenshot)
    return False


# Ribbon Home > Refresh, then the relationships banner button, EN and FR UI.
_RIBBON_REFRESH_NAMES = ("Refresh", "Actualiser")
_BANNER_REFRESH_NAMES = ("Refresh now", "Actualiser maintenant")
_SCHEMA_REFRESH_NAMES = ("Schema and data", "Sch\u00e9ma et donn\u00e9es")
# Schema refresh leaves query changes pending until this banner is pressed.
_APPLY_CHANGES_NAMES = ("Apply changes", "Appliquer les modifications")
_PENDING_CHANGES_TEXT = (
    "There are pending changes in your queries that haven't been applied",
    "There are pending changes in your queries that haven’t been applied",
    "Des modifications sont en attente dans vos requêtes",
)

_UIA_MENU_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$out = [ordered]@{ opened = 0; invoked = 0 }
try {
    $request = ConvertFrom-Json -InputObject ([Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String([Console]::In.ReadToEnd().Trim())))
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $root = [Windows.Automation.AutomationElement]::FromHandle([IntPtr][long]$request.hwnd)
    $buttons = New-Object Windows.Automation.PropertyCondition(
        [Windows.Automation.AutomationElement]::ControlTypeProperty,
        [Windows.Automation.ControlType]::Button)
    $ribbon = @($request.ribbon)
    foreach ($e in $root.FindAll([Windows.Automation.TreeScope]::Descendants, $buttons)) {
        if ($e.Current.ClassName -notmatch 'SplitButton__menuButton') { continue }
        $label = ($e.Current.Name -split ' ')[0]
        if ($ribbon -notcontains $label -or -not $e.Current.IsEnabled) { continue }
        $pattern = $null
        if ($e.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
            $pattern.Invoke(); $out.opened++
        } elseif ($e.TryGetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$pattern)) {
            $pattern.Expand(); $out.opened++
        }
        break
    }
    if ($out.opened -gt 0) {
        $items = New-Object Windows.Automation.PropertyCondition(
            [Windows.Automation.AutomationElement]::ControlTypeProperty,
            [Windows.Automation.ControlType]::MenuItem)
        $names = @($request.names)
        for ($i = 0; $i -lt 30 -and $out.invoked -eq 0; $i++) {
            Start-Sleep -Milliseconds 500
            foreach ($m in $root.FindAll([Windows.Automation.TreeScope]::Descendants, $items)) {
                if ($names -notcontains $m.Current.Name) { continue }
                $pattern = $null
                if ($m.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
                    $pattern.Invoke(); $out.invoked++; break
                }
            }
        }
    }
} catch { }
[Console]::Out.WriteLine((ConvertTo-Json -InputObject $out -Compress))
'''

_UIA_INVOKE_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$out = [ordered]@{ found = 0; invoked = 0 }
try {
    $request = ConvertFrom-Json -InputObject ([Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String([Console]::In.ReadToEnd().Trim())))
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $root = [Windows.Automation.AutomationElement]::FromHandle([IntPtr][long]$request.hwnd)
    $cond = New-Object Windows.Automation.PropertyCondition(
        [Windows.Automation.AutomationElement]::ControlTypeProperty,
        [Windows.Automation.ControlType]::Button)
    $names = @($request.names)
    foreach ($e in $root.FindAll([Windows.Automation.TreeScope]::Descendants, $cond)) {
        if ($names -notcontains $e.Current.Name) { continue }
        $out.found++
        if ($out.invoked -gt 0 -or -not $e.Current.IsEnabled) { continue }
        $pattern = $null
        if ($e.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
            $pattern.Invoke(); $out.invoked++
        }
    }
} catch { }
[Console]::Out.WriteLine((ConvertTo-Json -InputObject $out -Compress))
'''

# Desktop's web banners ignore UIA Invoke, so these get a real mouse click.
_UIA_CLICK_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$out = [ordered]@{ found = 0; invoked = 0 }
try {
    $request = ConvertFrom-Json -InputObject ([Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String([Console]::In.ReadToEnd().Trim())))
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    Add-Type -Namespace TtPbi -Name Mouse -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, UIntPtr e);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
"@
    [TtPbi.Mouse]::SetProcessDPIAware() | Out-Null
    $hwnd = [IntPtr][long]$request.hwnd
    $root = [Windows.Automation.AutomationElement]::FromHandle($hwnd)
    $cond = New-Object Windows.Automation.PropertyCondition(
        [Windows.Automation.AutomationElement]::ControlTypeProperty,
        [Windows.Automation.ControlType]::Button)
    $names = @($request.names)
    foreach ($e in $root.FindAll([Windows.Automation.TreeScope]::Descendants, $cond)) {
        if ($names -notcontains $e.Current.Name -or -not $e.Current.IsEnabled) { continue }
        $out.found++
        $r = $e.Current.BoundingRectangle
        if ($r.Width -le 0 -or $r.Height -le 0) { continue }
        [TtPbi.Mouse]::SetForegroundWindow($hwnd) | Out-Null
        Start-Sleep -Milliseconds 300
        [TtPbi.Mouse]::SetCursorPos([int]($r.X + $r.Width / 2), [int]($r.Y + $r.Height / 2)) | Out-Null
        Start-Sleep -Milliseconds 200
        [TtPbi.Mouse]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
        [TtPbi.Mouse]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
        $out.invoked++
        break
    }
} catch { }
[Console]::Out.WriteLine((ConvertTo-Json -InputObject $out -Compress))
'''

_UIA_TEXT_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$out = [ordered]@{ found = 0 }
try {
    $request = ConvertFrom-Json -InputObject ([Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String([Console]::In.ReadToEnd().Trim())))
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $root = [Windows.Automation.AutomationElement]::FromHandle([IntPtr][long]$request.hwnd)
    $names = @($request.names)
    foreach ($e in $root.FindAll([Windows.Automation.TreeScope]::Descendants,
                                 [Windows.Automation.Condition]::TrueCondition)) {
        foreach ($name in $names) {
            if ($e.Current.Name -like "*$name*") { $out.found++; break }
        }
        if ($out.found -gt 0) { break }
    }
} catch { }
[Console]::Out.WriteLine((ConvertTo-Json -InputObject $out -Compress))
'''

# Read-only inspection: never invoke controls or inspect another process's UI.
# Return only states, not source paths, query text or other UI contents.
_UIA_REFRESH_STATE_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$request = ConvertFrom-Json -InputObject ([Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String([Console]::In.ReadToEnd().Trim())))
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [Windows.Automation.AutomationElement]::FromHandle([IntPtr][long]$request.hwnd)
if ($root.Current.ProcessId -ne [int]$request.owner_pid) { exit 1 }
# WebView2 provides Desktop's refresh dialog from a child process. Anchor the
# UI tree to the owned HWND, then admit only providers in its process tree.
$processes = @(Get-CimInstance Win32_Process `
    -Property ProcessId,ParentProcessId -ErrorAction Stop)
$providers = New-Object 'System.Collections.Generic.HashSet[int]'
$pending = New-Object System.Collections.Queue
$null = $providers.Add([int]$request.owner_pid)
$pending.Enqueue([int]$request.owner_pid)
while ($pending.Count -gt 0) {
    $parent = [int]$pending.Dequeue()
    foreach ($process in $processes) {
        if ([int]$process.ParentProcessId -ne $parent) { continue }
        $child = [int]$process.ProcessId
        if ($providers.Add($child)) { $pending.Enqueue($child) }
    }
}
$out = [ordered]@{ status = 'clear' }
$completed = $false
$elements = @($root) + @($root.FindAll(
    [Windows.Automation.TreeScope]::Descendants,
    [Windows.Automation.Condition]::TrueCondition))
foreach ($e in $elements) {
    if ($e.Current.ProcessId -ne [int]$request.owner_pid -and
        -not $providers.Contains([int]$e.Current.ProcessId)) { continue }
    if ($e.Current.IsOffscreen) { continue }
    $r = $e.Current.BoundingRectangle
    if ($r.Width -le 0 -or $r.Height -le 0) { continue }
    $name = $e.Current.Name
    if ($name -match $request.error_pattern) { $out.status = 'refresh_error'; break }
    if ($name -match $request.completed_pattern) { $completed = $true }
    if ($name -match $request.pending_pattern) {
        $out.status = 'pending_changes'
    } elseif ($out.status -eq 'clear' -and (
        $name -match $request.busy_pattern -or
        $e.Current.ControlType -eq [Windows.Automation.ControlType]::ProgressBar -or
        ($request.refresh_dialog -and $e.Current.IsEnabled -and
         $e.Current.ControlType -eq [Windows.Automation.ControlType]::Button -and
         $name -match '^(Cancel|Annuler)$'))) {
        $out.status = 'busy'
    }
    if ($out.status -eq 'clear' -and $completed) { $out.status = 'completed' }
}
[Console]::Out.WriteLine((ConvertTo-Json -InputObject $out -Compress))
'''

# Nonzero counts only: "0 errors" and benign no-data/incomplete banners are
# not evidence of a failed refresh. Match summaries as well as individual rows.
_REFRESH_ERROR_PATTERN = (
    r"(?i)(?:contained errors|queries? (?:had|has|have) errors|"
    r"(?<![\d.,])\d*[1-9]\d*\s+quer(?:y|ies)\s+(?:is|are)\s+"
    r"blocked by the following errors?\b|"
    r"(?<![\d.,])\d*[1-9]\d*\s+requêtes?\s+(?:est|sont)\s+"
    r"bloquées?\s+par (?:les erreurs suivantes|l['’]erreur suivante)\b|"
    r"requêtes?.*(?:contenaient|contenait|contiennent|contient|comportent|comporte"
    r"|ont rencontré).*erreurs?|"
    r"(?<![\d.,])\d*[1-9]\d*(?:[.,\s\u00a0\u202f]\d{3})*\s+"
    r"(?:errors?|erreurs?)\b|"
    r"(?:DataSource|Expression|DataFormat|HeaderMatch|HeaderSchema)\.Error\b|"
    # Desktop can display only Error.Record's canonical Message, not its Reason.
    # Match the entire UI name, permitting only surrounding UI whitespace.
    r"^\s*(?:Declared source column names must be nonblank and unique|"
    r"Source headers must uniquely match the declared source column names|"
    r"Source header matches must be one-to-one)\.\s*$|"
    r"(?:OLE DB (?:or|ou) ODBC (?:error|erreur)|"
    r"an error occurred while (?:loading|refreshing)|"
    r"erreur (?:lors|au cours) (?:du|de l['’])\s*(?:chargement|actualisation))|"
    r"(?:refresh|load|actualisation|chargement) (?:has failed|failed|a échoué)|"
    r"(?:failed to load|échec (?:du|de l['’])\s*(?:chargement|actualisation)))"
)
_REFRESH_BUSY_PATTERN = (
    r"(?i)^(?:(?:Loading|Refreshing|Evaluating|Connecting|Waiting for)"
    r"(?:\b|[.…])|(?:Chargement|Actualisation|Évaluation|Connexion)"
    r"(?:[.…]|$|\s+(?:en cours|des données))|En attente de\b)"
)
_REFRESH_COMPLETED_PATTERN = (
    r"(?i)(?:\b\d[\d,.\s\u00a0\u202f]*\s+(?:rows? loaded|lignes? chargées?)\b|"
    r"^(?:Refresh completed|Actualisation terminée)[.!]?$)"
)


def _positive_timeout(timeout: float) -> float:
    """Keep fractional budgets; zero, nonfinite and invalid values cannot run."""
    seconds = float(timeout)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("timeout must be positive and finite")
    return seconds


def _refresh_ui_state(hwnd: int, owner_pid: Optional[int], *,
                      timeout: float = 30.0) -> str:
    """Inspect the report and only its process-owned visible popup windows."""
    if not owner_pid:
        return "unavailable"
    try:
        # A read-only snapshot must not inherit the two-minute UI action
        # allowance and monopolize the refresh budget on a blocked provider.
        deadline = time.monotonic() + min(_positive_timeout(timeout), 30.0)
    except (TypeError, ValueError, OverflowError):
        return "unavailable"
    roots = {int(hwnd): False}
    for window in desktop_window.dialog_windows(owner_pid):
        roots[window.hwnd] = window.title.strip().casefold() in {
            "refresh", "actualiser", "actualisation",
        }
    states = []
    for handle, refresh_dialog in roots.items():
        observation = _run_uia(_UIA_REFRESH_STATE_SCRIPT, {
            "hwnd": handle, "owner_pid": int(owner_pid),
            "refresh_dialog": refresh_dialog,
            "error_pattern": _REFRESH_ERROR_PATTERN,
            "pending_pattern": "(?i)" + "|".join(
                re.escape(text) for text in _PENDING_CHANGES_TEXT),
            "busy_pattern": _REFRESH_BUSY_PATTERN,
            "completed_pattern": _REFRESH_COMPLETED_PATTERN,
        }, timeout=max(0.0, deadline - time.monotonic()))
        states.append(observation.get("status", "unavailable"))
    for state in ("refresh_error", "pending_changes", "busy", "unavailable"):
        if state in states:
            return state
    if all(state in {"clear", "completed"} for state in states):
        return "completed" if "completed" in states else "clear"
    return "unavailable"


def _run_uia(script: str, payload: dict, *, timeout: float = 120.0) -> dict:
    if not desktop_window.IS_WINDOWS:
        return {}
    request = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    try:
        bounded_timeout = min(_positive_timeout(timeout), 120.0)
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             script],
            input=request, capture_output=True, text=True, timeout=bounded_timeout)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        return result if isinstance(result, dict) else {}
    except (OSError, TypeError, ValueError, OverflowError, IndexError,
            subprocess.SubprocessError):
        return {}


def _invoke_button(hwnd: int, names, *, timeout: float = 120.0) -> int:
    """Invoke the first enabled button named in *names*. Returns clicks made."""
    result = _run_uia(
        _UIA_INVOKE_SCRIPT, {"hwnd": int(hwnd), "names": list(names)}, timeout=timeout)
    return int(result.get("invoked", 0) or 0)


def _button_present(hwnd: int, names) -> bool:
    """Return whether an enabled button is still visible without invoking it."""
    result = _run_uia(_UIA_INVOKE_SCRIPT, {"hwnd": int(hwnd), "names": list(names)})
    return bool(result.get("found", 0))


def _text_present(hwnd: int, names) -> bool:
    result = _run_uia(_UIA_TEXT_SCRIPT, {"hwnd": int(hwnd), "names": list(names)})
    return bool(result.get("found", 0))


def _click_button(hwnd: int, names, *, timeout: float = 120.0) -> int:
    """Mouse-click the first enabled button named in *names*. Returns clicks made."""
    result = _run_uia(
        _UIA_CLICK_SCRIPT, {"hwnd": int(hwnd), "names": list(names)}, timeout=timeout)
    return int(result.get("invoked", 0) or 0)


def _invoke_schema_refresh(hwnd: int, *, timeout: float = 120.0) -> int:
    """Home > Refresh arrow > Schema and data. Returns clicks made."""
    result = _run_uia(_UIA_MENU_SCRIPT, {
        "hwnd": int(hwnd), "ribbon": list(_RIBBON_REFRESH_NAMES),
        "names": list(_SCHEMA_REFRESH_NAMES)}, timeout=timeout)
    return int(result.get("invoked", 0) or 0)


def _wait_for_render(hwnd: int, screenshot_path: str, *, timeout: float = 60.0,
                     poll: float = 5.0) -> None:
    """Wait until two consecutive captures match, i.e. visuals stopped redrawing."""
    previous = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        shot = desktop_window.capture_window(hwnd, screenshot_path)
        if not shot:
            return
        try:
            with open(shot, "rb") as handle:
                current = handle.read()
        except OSError:
            return
        if current == previous:
            return
        previous = current


def refresh_report(hwnd: int, pbip_path: str, owner_pid: Optional[int], *,
                   timeout: float = 300.0, poll: float = 10.0) -> dict:
    """Refresh, observing owned UI or physical partition freshness, not just rows.

    Counts may reflect a previous or partial load. They never override a visible
    error, pending changes or busy UI. Empty tables are not refresh failures.
    All refresh observations, UI requests and sleeps share one monotonic budget;
    a positive fractional timeout is honored without a minimum timeout floor.
    """
    unobserved = _partition_observation_result("not_observed", "refresh_not_requested")
    result = {"status": "not_found", "mode": "none", "clicks": 0,
              "waited_s": 0.0, "data_load": _data_load_result("unavailable"),
              "completion_basis": "none",
              "partition_freshness": _compare_partition_freshness(
                  unobserved, unobserved)}
    started = time.monotonic()
    try:
        timeout = _positive_timeout(timeout)
        poll = float(poll)
        if not math.isfinite(poll) or poll < 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        result.update(status="error", note="invalid_refresh_timeout_or_poll")
        return result
    deadline = started + timeout

    def remaining():
        return max(0.0, deadline - time.monotonic())

    def finish():
        now = time.monotonic()
        result["waited_s"] = round(max(0.0, now - started), 1)
        if now >= deadline:
            result.update(status="timed_out", note="refresh_timeout",
                          completion_basis="none")
        return result

    if remaining() <= 0:
        return finish()
    initial_state = _refresh_ui_state(hwnd, owner_pid, timeout=remaining())
    result["ui_state"] = {
        "initial": initial_state, "readiness": initial_state,
        "readiness_checks": 1, "observed_progress": False,
    }
    ready_state = initial_state
    # Startup UI providers can be transiently unavailable or still loading.
    # Do not request refresh until a safe pre-request baseline can be attempted.
    # Readiness progress predates this request and is not refresh completion.
    while ready_state not in {"clear", "completed"}:
        result["status"] = ready_state
        if ready_state == "refresh_error" or remaining() <= 0:
            return finish()
        time.sleep(min(poll, 1.0, remaining()))
        if remaining() <= 0:
            return finish()
        ready_state = _refresh_ui_state(hwnd, owner_pid, timeout=remaining())
        result["ui_state"]["readiness"] = ready_state
        result["ui_state"]["readiness_checks"] += 1
    if remaining() <= 0:
        return finish()
    # A baseline taken after invoking Refresh could already contain the new
    # timestamps. Never infer freshness without a safe pre-request observation.
    baseline = observe_desktop_partitions(
        pbip_path, owner_pid, timeout=min(30.0, remaining()))
    result["partition_freshness"] = _compare_partition_freshness(
        baseline, unobserved)
    for _attempt in range(3):
        if remaining() <= 0:
            return finish()
        result["clicks"] = _invoke_schema_refresh(hwnd, timeout=remaining())
        if result["clicks"] or remaining() <= 0:
            break
        time.sleep(min(5.0, remaining()))
    if result["clicks"]:
        result["mode"] = "schema_and_data"
    elif remaining() > 0:
        result["clicks"] = _invoke_button(
            hwnd, _RIBBON_REFRESH_NAMES, timeout=remaining())
        if result["clicks"]:
            result["mode"] = "data"
    if not result["clicks"] or remaining() <= 0:
        return finish()
    result["status"] = "clicked"
    banner_done = False
    applied = False
    apply_attempted_at = None
    observed_progress = False
    while remaining() > 0:
        time.sleep(min(poll, remaining()))
        if remaining() <= 0:
            break
        before = _refresh_ui_state(hwnd, owner_pid, timeout=remaining())
        observed_progress = observed_progress or before == "busy"
        result["ui_state"].update(
            before=before, observed_progress=observed_progress)
        if before == "refresh_error":
            result["status"] = before
            break
        if remaining() <= 0:
            break
        if not applied:
            applied_clicks = _click_button(
                hwnd, _APPLY_CHANGES_NAMES, timeout=remaining())
            if not applied_clicks and remaining() > 0:
                applied_clicks = _invoke_button(
                    hwnd, _APPLY_CHANGES_NAMES, timeout=remaining())
            if applied_clicks:
                result["clicks"] += applied_clicks
                result["applied_changes"] = True
                applied = True
                apply_attempted_at = time.monotonic()
        elif (result["data_load"].get("status") != "verified"
              and apply_attempted_at is not None
              and time.monotonic() - apply_attempted_at >= 10):
            # Desktop occasionally leaves the banner visible after a click.
            # Retry without treating its presence as a successful refresh.
            retry_clicks = _click_button(
                hwnd, _APPLY_CHANGES_NAMES, timeout=remaining())
            if retry_clicks:
                result["clicks"] += retry_clicks
                apply_attempted_at = time.monotonic()
        if remaining() <= 0:
            break
        data = verify_desktop_data(
            pbip_path, owner_pid, timeout=min(60.0, remaining()))
        result["data_load"] = data
        if remaining() <= 0:
            break
        partitions = (
            observe_desktop_partitions(
                pbip_path, owner_pid, timeout=min(30.0, remaining()))
            if before in {"clear", "completed"}
            else _partition_observation_result("not_observed", "ui_not_clear"))
        freshness = _compare_partition_freshness(baseline, partitions)
        result["partition_freshness"] = freshness
        if remaining() <= 0:
            break
        after = _refresh_ui_state(hwnd, owner_pid, timeout=remaining())
        observed_progress = observed_progress or after == "busy"
        result["ui_state"].update(
            after=after, observed_progress=observed_progress)
        if after == "refresh_error":
            result["status"] = after
            break
        result["status"] = (
            "waiting" if after in {"clear", "completed"} else after)
        if after == "unavailable" or remaining() <= 0:
            break
        if (before in {"clear", "completed"} and after in {"clear", "completed"}
                and (data["status"] == "no_m_tables"
                     or freshness["status"] == "no_m_tables")):
            result["status"] = "no_m_tables"
            break
        observed_tables = {
            item["table"] for snapshot in (baseline, partitions)
            for item in snapshot["partitions"]
        }
        counts_complete = (
            data["status"] in {"verified", "partial", "empty"}
            and data["tables_checked"] > 0
            and data["tables_failed"] == 0
            and data["tables_nonempty"] + data["tables_empty"]
            == data["tables_checked"]
            and (not observed_tables or data["tables_checked"] == len(observed_tables)))
        ui_completed = observed_progress or (
            ready_state == "clear"
            and "completed" in {before, after})
        # A healthy but unchanged snapshot is not freshness. It can still
        # accompany an independently observed UI lifecycle. If the read-only
        # observer is unavailable, preserve that existing UI verification path;
        # explicit metadata failures/mismatches must never become "unavailable".
        partitions_safe = freshness["status"] in {
            "advanced", "unchanged", "unavailable",
        }
        if (before in {"clear", "completed"}
                and after in {"clear", "completed"} and counts_complete
                and partitions_safe
                and (ui_completed or freshness["status"] == "advanced")
                and remaining() > 0):
            result["status"] = "verified"
            result["completion_basis"] = (
                "ui_lifecycle" if ui_completed else "partition_freshness")
            break
        if (not banner_done and remaining() > 0
                and time.monotonic() - started >= timeout / 3):
            result["clicks"] += _invoke_button(
                hwnd, _BANNER_REFRESH_NAMES, timeout=remaining())
            banner_done = True
    return finish()


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


def _m_backed_partitions(pbip_path: str):
    """Return declared M table/partition names, or None if ambiguous."""
    project_dir = os.path.dirname(os.path.abspath(pbip_path))
    model_dirs = glob.glob(os.path.join(project_dir, "*.SemanticModel"))
    if len(model_dirs) != 1:
        return None
    tables_dir = os.path.join(model_dirs[0], "definition", "tables")
    partitions = []
    seen = set()
    for path in sorted(glob.glob(os.path.join(tables_dir, "*.tmdl"))):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeError):
            return None
        partition_matches = list(_TMDL_M_PARTITION_RE.finditer(text))
        if not partition_matches:
            continue
        table_matches = _TMDL_TABLE_RE.findall(text)
        if len(table_matches) != 1:
            return None

        def unquote(name):
            name = name.strip()
            return (name[1:-1].replace("''", "'")
                    if len(name) >= 2 and name[0] == name[-1] == "'" else name)

        table = unquote(table_matches[0])
        for match in partition_matches:
            partition = unquote(match.group(1))
            key = (table.casefold(), partition.casefold())
            if not table or not partition or key in seen:
                return None
            seen.add(key)
            partitions.append({"table": table, "partition": partition})
    return partitions


def _m_backed_tables(pbip_path: str):
    """Return local TMDL table names with M partitions, or None if ambiguous."""
    partitions = _m_backed_partitions(pbip_path)
    if partitions is None:
        return None
    return list(dict.fromkeys(partition["table"] for partition in partitions))


def _partition_observation_result(status="unavailable", note="not_observed", *,
                                  expected=0):
    return {
        "status": status, "note": note, "expected_partitions": expected,
        "partitions": [], "missing": [], "unexpected": [],
    }


def observe_desktop_partitions(pbip_path: str, owner_pid: Optional[int], *,
                               timeout: float = 30.0) -> dict:
    """Read only expected M partition metadata from the HWND owner's engine.

    Shares the aggregate verifier's unique descendant-engine/port and local
    ADOMD resolution. IDs are decimal strings; RefreshedTime is a round-trip
    .NET timestamp, never a float. Diagnostics are fixed codes, not exceptions
    or SQL errors. A failed metadata query is not observer unavailability.
    Timeout is positive finite seconds (including fractions), capped at 60.
    """
    try:
        deadline = time.monotonic() + min(_positive_timeout(timeout), 60.0)
    except (TypeError, ValueError, OverflowError):
        return _partition_observation_result(note="invalid_observer_request")
    try:
        expected = _m_backed_partitions(pbip_path)
    except (OSError, TypeError, ValueError):
        expected = None
    if expected is None:
        return _partition_observation_result(note="expected_partitions_unavailable")
    if not expected:
        return _partition_observation_result("no_m_tables", "no_m_tables")
    observation = _partition_observation_result(
        note="owner_unavailable", expected=len(expected))
    if type(owner_pid) is not int or owner_pid <= 0:
        return observation
    if not desktop_window.IS_WINDOWS:
        observation["note"] = "windows_unavailable"
        return observation
    request = {
        "operation": "partitions", "owner_pid": owner_pid,
        "tables": list(dict.fromkeys(item["table"] for item in expected)),
        "partitions": expected,
    }
    encoded = base64.b64encode(json.dumps(request).encode("utf-8")).decode("ascii")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        observation["note"] = "observer_timeout"
        return observation
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             _ADOMD_VERIFY_SCRIPT],
            input=encoded, capture_output=True, text=True, timeout=remaining)
    except subprocess.TimeoutExpired:
        observation["note"] = "observer_timeout"
        return observation
    except (OSError, ValueError, subprocess.SubprocessError):
        observation["note"] = "observer_process_failed"
        return observation
    if time.monotonic() >= deadline:
        observation["note"] = "observer_timeout"
        return observation
    if completed.returncode != 0:
        observation["note"] = "observer_process_failed"
        return observation
    try:
        raw = json.loads(completed.stdout.strip().splitlines()[-1])
        status = raw["status"]
        allowed_notes = {
            "invalid_request", "process_query_failed", "owner_unavailable",
            "engine_unavailable_or_ambiguous", "port_query_failed",
            "port_unavailable_or_ambiguous", "adomd_client_unavailable",
            "connection_failed", "metadata_query_failed", "metadata_observed",
        }
        if (status not in {"observed", "unavailable", "query_failed"}
                or raw["note"] not in allowed_notes
                or (status == "observed") != (raw["note"] == "metadata_observed")
                or (status == "query_failed") != (raw["note"] == "metadata_query_failed")
                or type(raw["owner_pid"]) is not int
                or raw["owner_pid"] != owner_pid):
            raise ValueError
        for key in ("engine_pid", "port"):
            if type(raw[key]) is not int or raw[key] < 0:
                raise ValueError
        if (raw["port"] > 65535 or (status != "unavailable"
                and (not raw["engine_pid"] or not raw["port"]))):
            raise ValueError

        def identity(item):
            ids = {key: str(item[key]) for key in ("table_id", "partition_id")}
            if any(not re.fullmatch(r"[1-9]\d{0,19}", value)
                   for value in ids.values()):
                raise ValueError
            return ids

        if not isinstance(raw["partitions"], list) or not isinstance(raw["unexpected"], list):
            raise ValueError
        pairs = {(item["table"], item["partition"]) for item in expected}
        partitions = []
        for item in raw["partitions"]:
            name = (item["table"], item["partition"])
            if (name not in pairs or type(item["state"]) is not int
                    or type(item["has_error"]) is not bool
                    or not (item["refreshed_time"] is None
                            or isinstance(item["refreshed_time"], str))):
                raise ValueError
            partitions.append({
                "table": name[0], "partition": name[1], **identity(item),
                "refreshed_time": item["refreshed_time"],
                "state": item["state"], "has_error": item["has_error"],
            })
        unexpected = [identity(item) for item in raw["unexpected"]]
        if status == "unavailable" and (partitions or unexpected):
            raise ValueError
        found = {(item["table"], item["partition"]) for item in partitions}
        ids = {(item["table_id"], item["partition_id"]) for item in partitions}
        observation.update(
            status=status, note=raw["note"], owner_pid=owner_pid,
            engine_pid=raw["engine_pid"], port=raw["port"],
            partitions=partitions, unexpected=unexpected)
        if status == "observed":
            observation["missing"] = [
                item for item in expected if (item["table"], item["partition"]) not in found]
            if (observation["missing"] or unexpected or len(ids) != len(partitions)
                    or len(found) != len(partitions)):
                observation.update(status="mismatch", note="expected_partition_mismatch")
        return observation
    except (IndexError, KeyError, TypeError, ValueError):
        return _partition_observation_result(
            "invalid", "observer_result_invalid", expected=len(expected))


def _refreshed_time_key(value):
    """Parse ISO/.NET time into exact 100 ns ticks and clock-kind, without floats."""
    match = _PARTITION_TIME_RE.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("invalid partition timestamp")
    year, month, day, hour, minute, second = map(int, match.groups()[:6])
    date = datetime(year, month, day, hour, minute, second)
    ticks = (((date.toordinal() - 1) * 86400 + hour * 3600 + minute * 60 + second)
             * 10_000_000 + int((match[7] or "").ljust(7, "0")))
    zone = match[8]
    if zone and zone != "Z":
        hours, minutes = int(zone[1:3]), int(zone[4:6])
        if hours > 23 or minutes > 59:
            raise ValueError("invalid partition timestamp zone")
        ticks -= (1 if zone[0] == "+" else -1) * (hours * 60 + minutes) * 600_000_000
    return zone is not None, ticks


def _compare_partition_freshness(before: dict, after: dict) -> dict:
    """Compare every expected identity; never discard a failed/stale partition."""
    result = {
        "status": "unavailable", "before": before, "after": after,
        "changed": [], "unchanged": [], "regressed": [],
        "missing": [], "added": [], "baseline_failed": [], "failed": [],
        "invalid_timestamps": [],
    }

    def key(item):
        return item["table_id"], item["partition_id"]

    def identities(keys):
        return [{"table_id": table, "partition_id": partition}
                for table, partition in sorted(keys)]

    # Unprocessed/new (State4) or previously failed partitions can be repaired
    # by this refresh. Keep their baseline health, but gate on terminal health.
    # Their IDs and parseable timestamps still participate in every comparison;
    # a real .NET MinValue timestamp is valid, an absent/invalid one is not.
    for failed_key, snapshot in (("baseline_failed", before), ("failed", after)):
        result[failed_key] = identities({
            key(item) for item in snapshot["partitions"]
            if item["state"] != 1 or item["has_error"]})
    statuses = {before["status"], after["status"]}
    comparable = statuses <= {"observed", "mismatch"}
    duplicate_or_renamed = False
    old = {}
    new = {}
    if comparable:
        old = {key(item): item for item in before["partitions"]}
        new = {key(item): item for item in after["partitions"]}
        result["missing"] = identities(old.keys() - new.keys())
        result["added"] = identities(new.keys() - old.keys())
        duplicate_or_renamed = (
            len(old) != len(before["partitions"])
            or len(new) != len(after["partitions"]))
        for identity in sorted(old.keys() & new.keys()):
            previous, current = old[identity], new[identity]
            duplicate_or_renamed |= any(
                previous[name] != current[name] for name in ("table", "partition"))
            try:
                old_kind, old_time = _refreshed_time_key(previous["refreshed_time"])
                new_kind, new_time = _refreshed_time_key(current["refreshed_time"])
                if old_kind != new_kind:
                    raise ValueError
                category = ("changed" if new_time > old_time else
                            "unchanged" if new_time == old_time else "regressed")
            except ValueError:
                category = "invalid_timestamps"
            result[category].extend(identities({identity}))
    if "no_m_tables" in statuses:
        result["status"] = "no_m_tables"
    elif "query_failed" in statuses:
        result["status"] = "query_failed"
    elif result["failed"]:
        result["status"] = "partition_failed"
    elif "invalid" in statuses:
        result["status"] = "invalid"
    elif ("mismatch" in statuses or result["missing"] or result["added"]
          or duplicate_or_renamed):
        result["status"] = "mismatch"
    elif statuses == {"not_observed"}:
        result["status"] = "not_observed"
    elif comparable:
        if (not old or len(old) != before["expected_partitions"]
                or len(new) != after["expected_partitions"]):
            result["status"] = "mismatch"
        elif any(before[name] != after[name] for name in ("owner_pid", "engine_pid", "port")):
            result["status"] = "binding_changed"
        elif result["invalid_timestamps"]:
            result["status"] = "invalid_timestamp"
        elif result["regressed"]:
            result["status"] = "regressed"
        elif result["unchanged"]:
            result["status"] = "unchanged"
        elif (before["expected_partitions"] == after["expected_partitions"]
              == len(result["changed"]) > 0):
            result["status"] = "advanced"
        else:
            result["status"] = "mismatch"
    return result


def verify_desktop_data(pbip_path: str, owner_pid: Optional[int], *,
                        timeout: float = 60.0) -> dict:
    """Verify M-backed tables through the Analysis Services engine owned by HWND.

    Only aggregate counts/status are returned. Query details and exceptions are
    deliberately kept out of stdout, warnings, and the report.
    Positive fractional seconds are supported without a timeout floor.
    """
    try:
        deadline = time.monotonic() + min(_positive_timeout(timeout), 120.0)
    except (TypeError, ValueError, OverflowError):
        return _data_load_result("unavailable")
    tables = _m_backed_tables(pbip_path)
    if tables is None or not owner_pid or not desktop_window.IS_WINDOWS:
        return _data_load_result("unavailable")
    if not tables:
        return _data_load_result("no_m_tables")

    request = json.dumps({"owner_pid": int(owner_pid), "tables": tables},
                         ensure_ascii=True).encode("utf-8")
    encoded = base64.b64encode(request).decode("ascii")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _data_load_result("unavailable")
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             _ADOMD_VERIFY_SCRIPT],
            input=encoded, capture_output=True, text=True,
            timeout=remaining)
    except (OSError, ValueError, subprocess.SubprocessError):
        return _data_load_result("unavailable")
    if time.monotonic() >= deadline:
        return _data_load_result("unavailable")
    if completed.returncode != 0:
        return _data_load_result("unavailable")
    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        if not isinstance(result, dict):
            raise ValueError
        values = {key: result[key] for key in _DATA_LOAD_KEYS}
        if values["status"] not in {
                "verified", "partial", "empty", "query_failed", "unavailable"}:
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
                or values["tables_nonempty"] != len(tables)
                or values["total_rows"] <= 0):
            raise ValueError
        if values["status"] == "partial" and (
                not counts_complete or values["tables_failed"]
                or not values["tables_nonempty"] or not values["tables_empty"]):
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
