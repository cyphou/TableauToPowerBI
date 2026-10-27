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
import ntpath
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
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
    r"^\s*partition\s+.+?\s*=\s*m\s*$", re.MULTILINE)
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
        elseif ($result.tables_checked -gt 0 -and
                $result.tables_nonempty -eq $result.tables_checked) {
            $result.status = 'verified'
        }
        elseif ($result.tables_nonempty -gt 0) { $result.status = 'partial' }
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
                       refresh_timeout: int = 300,
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
                  refresh=False, refresh_timeout=300,
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
$processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
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
    r"(?:DataSource|Expression|DataFormat)\.Error\b|"
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


def _refresh_ui_state(hwnd: int, owner_pid: Optional[int]) -> str:
    """Inspect the report and only its process-owned visible popup windows."""
    if not owner_pid:
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
        })
        states.append(observation.get("status", "unavailable"))
    for state in ("refresh_error", "pending_changes", "busy", "unavailable"):
        if state in states:
            return state
    if all(state in {"clear", "completed"} for state in states):
        return "completed" if "completed" in states else "clear"
    return "unavailable"


def _run_uia(script: str, payload: dict) -> dict:
    if not desktop_window.IS_WINDOWS:
        return {}
    request = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             script],
            input=request, capture_output=True, text=True, timeout=120)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return {}


def _invoke_button(hwnd: int, names) -> int:
    """Invoke the first enabled button named in *names*. Returns clicks made."""
    result = _run_uia(_UIA_INVOKE_SCRIPT, {"hwnd": int(hwnd), "names": list(names)})
    return int(result.get("invoked", 0) or 0)


def _button_present(hwnd: int, names) -> bool:
    """Return whether an enabled button is still visible without invoking it."""
    result = _run_uia(_UIA_INVOKE_SCRIPT, {"hwnd": int(hwnd), "names": list(names)})
    return bool(result.get("found", 0))


def _text_present(hwnd: int, names) -> bool:
    result = _run_uia(_UIA_TEXT_SCRIPT, {"hwnd": int(hwnd), "names": list(names)})
    return bool(result.get("found", 0))


def _click_button(hwnd: int, names) -> int:
    """Mouse-click the first enabled button named in *names*. Returns clicks made."""
    result = _run_uia(_UIA_CLICK_SCRIPT, {"hwnd": int(hwnd), "names": list(names)})
    return int(result.get("invoked", 0) or 0)


def _invoke_schema_refresh(hwnd: int) -> int:
    """Home > Refresh arrow > Schema and data. Returns clicks made."""
    result = _run_uia(_UIA_MENU_SCRIPT, {
        "hwnd": int(hwnd), "ribbon": list(_RIBBON_REFRESH_NAMES),
        "names": list(_SCHEMA_REFRESH_NAMES)})
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
                   timeout: int = 300, poll: float = 10.0) -> dict:
    """Refresh, observing owned UI completion separately from aggregate rows.

    Counts may reflect a previous or partial load. They never override a visible
    error, pending changes or busy UI. Empty tables are not refresh failures.
    """
    result = {"status": "not_found", "mode": "none", "clicks": 0,
              "waited_s": 0.0, "data_load": _data_load_result("unavailable")}
    started = time.time()
    initial_state = _refresh_ui_state(hwnd, owner_pid)
    result["ui_state"] = {"initial": initial_state, "observed_progress": False}
    if initial_state == "refresh_error":
        result["status"] = initial_state
        result["waited_s"] = round(time.time() - started, 1)
        return result
    for _attempt in range(3):
        result["clicks"] = _invoke_schema_refresh(hwnd)
        if result["clicks"]:
            break
        time.sleep(5)
    if result["clicks"]:
        result["mode"] = "schema_and_data"
    else:
        result["clicks"] = _invoke_button(hwnd, _RIBBON_REFRESH_NAMES)
        if result["clicks"]:
            result["mode"] = "data"
    if not result["clicks"]:
        return result
    result["status"] = "clicked"
    banner_done = False
    applied = False
    apply_attempted_at = None
    observed_progress = False
    while time.time() - started < timeout:
        time.sleep(poll)
        before = _refresh_ui_state(hwnd, owner_pid)
        observed_progress = observed_progress or before == "busy"
        result["ui_state"].update(
            before=before, observed_progress=observed_progress)
        if before == "refresh_error":
            result["status"] = before
            break
        if not applied:
            applied_clicks = _click_button(hwnd, _APPLY_CHANGES_NAMES)
            if not applied_clicks:
                applied_clicks = _invoke_button(hwnd, _APPLY_CHANGES_NAMES)
            if applied_clicks:
                result["clicks"] += applied_clicks
                result["applied_changes"] = True
                applied = True
                apply_attempted_at = time.time()
        elif (result["data_load"].get("status") != "verified"
              and apply_attempted_at and time.time() - apply_attempted_at >= 10):
            # Desktop occasionally leaves the banner visible after a click.
            # Retry without treating its presence as a successful refresh.
            retry_clicks = _click_button(hwnd, _APPLY_CHANGES_NAMES)
            if retry_clicks:
                result["clicks"] += retry_clicks
                apply_attempted_at = time.time()
        data = verify_desktop_data(pbip_path, owner_pid)
        result["data_load"] = data
        after = _refresh_ui_state(hwnd, owner_pid)
        observed_progress = observed_progress or after == "busy"
        result["ui_state"].update(
            after=after, observed_progress=observed_progress)
        if after == "refresh_error":
            result["status"] = after
            break
        result["status"] = (
            "waiting" if after in {"clear", "completed"} else after)
        if after == "unavailable":
            break
        counts_complete = (
            data["status"] in {"verified", "partial", "empty"}
            and data["tables_checked"] > 0
            and data["tables_failed"] == 0
            and data["tables_nonempty"] + data["tables_empty"]
            == data["tables_checked"])
        # Existing rows (including intentionally empty tables) say nothing
        # about whether this request actually ran. Require a UI lifecycle or
        # explicit load completion, not just the absence of a visible error.
        if (before in {"clear", "completed"}
                and after in {"clear", "completed"} and counts_complete
                and (observed_progress or (
                    initial_state not in {"completed", "unavailable"}
                    and "completed" in {before, after}))):
            result["status"] = "verified"
            break
        if not banner_done and time.time() - started >= timeout / 3:
            result["clicks"] += _invoke_button(hwnd, _BANNER_REFRESH_NAMES)
            banner_done = True
    result["waited_s"] = round(time.time() - started, 1)
    return result


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
