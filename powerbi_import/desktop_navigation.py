"""Bounded, document-scoped UI Automation tab selection (Windows, stdlib only).

No coordinates, keyboard shortcuts, translated labels, or process-wide window
searches are used. Unsupported providers fail closed. Only selection/scroll/
realization patterns on tab controls are allowed; no save/refresh/filter actions.
"""
from __future__ import annotations

import base64
import json
import math
import subprocess
import sys
import time
from pathlib import Path

from powerbi_import import desktop_window

_CAPTURE_SCRIPT = """
import json, sys
from powerbi_import import desktop_window as w
hwnd, pid, path = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
if w.window_process_id(hwnd) != pid:
    result = {"status": "error", "note": "Window ownership changed"}
elif w.dialog_windows(pid) or not w.is_responsive(hwnd):
    result = {"status": "inaccessible", "note": "Dialog or unresponsive document"}
else:
    captured = w.capture_window(hwnd, path)
    blank = w.is_blank(path) if captured else None
    result = {"status": "captured" if captured and blank is False else "error",
              "screenshot_blank": blank, "note": "Blank/failed window capture"}
print(json.dumps(result))
"""


def _bounded_capture(hwnd: int, pid: int, shot: Path, timeout: float) -> dict:
    """PrintWindow can hang inside a provider; bound only our capture child."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _CAPTURE_SCRIPT, str(hwnd), str(pid), str(shot)],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if proc.returncode:
            return {"status": "error", "note": proc.stderr[-2000:]}
        return json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        return {"status": "timed_out", "note": "Window capture child exceeded page deadline"}


# The child is intentionally single-purpose and killed by subprocess.run on
# timeout. Applications are never terminated. Input is JSON, never interpolated
# into PowerShell source. Control types/patterns are locale independent.
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$q = [Console]::In.ReadToEnd() | ConvertFrom-Json
$A = [System.Windows.Automation.AutomationElement]
$T = [System.Windows.Automation.ControlType]
$S = [System.Windows.Automation.SelectionItemPattern]
$root = $A::FromHandle([IntPtr][long]$q.hwnd)
if ($null -eq $root -or $root.Current.ProcessId -ne [int]$q.pid) {
    throw 'Window ownership changed'
}
# Power BI also exposes ribbon tabs, field panes and custom-visual tabs. They
# must never masquerade as report pages (e.g. a report page named "Home").
# The executable and provider class are locale independent. Some WebView
# providers do not implement AutomationId conditions consistently, so inspect
# ancestry of semantic tabs rather than relying on a host FindFirst.
$isPBI = [Diagnostics.Process]::GetProcessById([int]$q.pid).ProcessName -eq 'PBIDesktop'
$clock = [Diagnostics.Stopwatch]::StartNew()
$budget = [double]$q.seconds * 1000
$seen = @()
$walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
function IsContainer($e) {
    if ($e.Current.ControlType -ne $T::Tab) { return $false }
    if (-not $isPBI) { return $true }
    if ($e.Current.ClassName -ne 'carouselScrollPane') { return $false }
    $p = $walker.GetParent($e)
    for ($i = 0; $i -lt 16 -and $null -ne $p; $i++) {
        if ($p.Current.AutomationId -eq 'desktopExplorationHost') { return $true }
        $p = $walker.GetParent($p)
    }
    return $false
}
function Nodes {
    $list = [Collections.Generic.List[System.Windows.Automation.AutomationElement]]::new()
    if ($root.Current.ControlType -eq $T::Tab) { $list.Add($root) }
    # Provider-side filtering crosses embedded browser/provider boundaries much
    # more reliably than walking every report visual through ControlViewWalker.
    # The outer Python timeout also bounds a hung FindAll provider call.
    $conditions = [System.Windows.Automation.Condition[]]@(
        [System.Windows.Automation.PropertyCondition]::new($A::ControlTypeProperty, $T::Tab),
        [System.Windows.Automation.PropertyCondition]::new($A::ControlTypeProperty, $T::TabItem),
        [System.Windows.Automation.PropertyCondition]::new($A::NameProperty, [string]$q.name)
    )
    $condition = [System.Windows.Automation.OrCondition]::new($conditions)
    $found = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    for ($i = 0; $i -lt $found.Count -and $i -lt 6000; $i++) {
        $list.Add($found[$i])
    }
    return ,$list
}
function IsTab($e) {
    # SelectionItem on arbitrary lists can change a filter. Require a Tab
    # ancestor (or the semantic TabItem type), not merely a matching name.
    if ($e.Current.ControlType -eq $T::TabItem -and -not $isPBI) { return $true }
    $selection = $null
    if (-not $e.TryGetCurrentPattern($S::Pattern, [ref]$selection)) { return $false }
    $p = $walker.GetParent($e)
    for ($i = 0; $i -lt 8 -and $null -ne $p; $i++) {
        if (IsContainer $p) { return $true }
        $p = $walker.GetParent($p)
    }
    return $false
}
function Selected($e) {
    $p = $null
    if ($e.TryGetCurrentPattern($S::Pattern, [ref]$p)) {
        return [bool]$p.Current.IsSelected
    }
    return $false
}
$result = @{status='inaccessible'; note='No uniquely named selectable tab exposed by UI Automation (overflow may be unsupported by this provider)'; discovered=@(); selected=$null}
$nodes = Nodes
# Discover virtualized tabs by the locale-independent ItemContainer property.
foreach ($container in $nodes) {
    if (-not (IsContainer $container)) { continue }
    $ic = $null
    if ($container.TryGetCurrentPattern([System.Windows.Automation.ItemContainerPattern]::Pattern, [ref]$ic)) {
        $item = $ic.FindItemByProperty($null, $A::NameProperty, [string]$q.name)
        if ($null -ne $item) {
            $vp = $null
            if ($q.mode -eq 'select' -and $item.TryGetCurrentPattern([System.Windows.Automation.VirtualizedItemPattern]::Pattern, [ref]$vp)) {
                $vp.Realize()
            }
            $nodes.Add($item)
        }
    }
}
for ($pass = 0; $pass -lt 18 -and $clock.ElapsedMilliseconds -lt $budget; $pass++) {
    $tabs = @($nodes | Where-Object { IsTab $_ })
    $result.discovered = @($tabs | ForEach-Object {
        @{name=$_.Current.Name; automation_id=$_.Current.AutomationId; selected=(Selected $_); offscreen=$_.Current.IsOffscreen}
    })
    $matches = @($tabs | Where-Object {
        $_.Current.Name -ceq [string]$q.name -and $_.Current.IsEnabled
    })
    # Runtime IDs remove duplicate references from ItemContainer discovery.
    $unique = @{}
    foreach ($e in $matches) { $unique[($e.GetRuntimeId() -join ':')] = $e }
    $matches = @($unique.Values)
    if ($matches.Count -gt 1) {
        $result.status = 'ambiguous'; $result.note = 'Multiple exact-name tabs'; break
    }
    if ($matches.Count -eq 1) {
        $e = $matches[0]
        if ($q.mode -eq 'select') {
            $sp = $null
            if ($e.TryGetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern, [ref]$sp)) { $sp.ScrollIntoView() }
            $p = $null
            if ($e.TryGetCurrentPattern($S::Pattern, [ref]$p)) { $p.Select() }
            else {
                $result.note = 'Tab does not expose SelectionItemPattern; cannot verify selection'
                break
            }
        }
        if (Selected $e) {
            $result.status = 'verified'
            $result.note = 'Exact-name tab SelectionItem.IsSelected verified'
            $result.selected = @{name=$e.Current.Name; automation_id=$e.Current.AutomationId; runtime_id=($e.GetRuntimeId() -join ':')}
            break
        }
        if ($q.mode -eq 'verify') { $result.note = 'Requested tab is not selected'; break }
        Start-Sleep -Milliseconds 150
    } elseif ($q.mode -eq 'select') {
        # Overflow: scroll only semantic tab containers, never generic report
        # surfaces or arbitrary buttons (which could add/delete pages).
        $scrolled = $false
        foreach ($container in $nodes) {
            if (-not (IsContainer $container)) { continue }
            $p = $null
            if ($container.TryGetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern, [ref]$p) -and $p.Current.HorizontallyScrollable) {
                if ($pass -eq 0) { $p.SetScrollPercent(0, -1) }
                elseif ($p.Current.HorizontalScrollPercent -lt 100) {
                    $p.Scroll([System.Windows.Automation.ScrollAmount]::LargeIncrement, [System.Windows.Automation.ScrollAmount]::NoAmount)
                } else { continue }
                $scrolled = $true
            }
        }
        if (-not $scrolled) { break }
    } else { break }
    $nodes = Nodes
}
if ($clock.ElapsedMilliseconds -ge $budget -and $result.status -ne 'verified') {
    $result.status = 'timed_out'; $result.note = 'UI Automation deadline exceeded'
}
$result | ConvertTo-Json -Depth 6 -Compress
"""


def select_document_tab(hwnd: int, pid: int, name: str, *, timeout: float = 30,
                        verify_only: bool = False) -> dict:
    """Select one exact-name tab and return observable selection evidence.

    A caller must obtain hwnd/pid from its own launched document. No fallback
    to other windows, coordinates or keyboard input. Safe to call in headless CI.
    """
    if not desktop_window.IS_WINDOWS:
        return {"status": "unavailable", "note": "UI Automation requires Windows"}
    if (not hwnd or not pid or not isinstance(name, str) or not name
            or not math.isfinite(timeout) or timeout <= 0):
        return {"status": "error", "note": "Invalid tab selection arguments"}
    if desktop_window.window_process_id(hwnd) != pid:
        return {"status": "error", "note": "Window is not owned by the launched PID"}
    query = {"hwnd": hwnd, "pid": pid, "name": name,
             "seconds": max(0.1, timeout - 0.5),
             "mode": "verify" if verify_only else "select"}
    try:
        encoded = base64.b64encode(_SCRIPT.encode("utf-16le")).decode("ascii")
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            input=json.dumps(query, ensure_ascii=True), text=True,
            encoding="utf-8", errors="replace", capture_output=True,
            timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
        if proc.returncode:
            return {"status": "error", "note": proc.stderr[-3000:]}
        result = json.loads(proc.stdout.lstrip("\ufeff").strip())
        if not isinstance(result, dict) or result.get("status") not in {
                "verified", "inaccessible", "ambiguous", "timed_out"}:
            raise ValueError("Invalid UI Automation response")
        return result
    except subprocess.TimeoutExpired:
        return {"status": "timed_out", "note": "UI Automation child exceeded deadline"}
    except Exception as exc:
        return {"status": "error", "note": f"{type(exc).__name__}: {exc}"}


def capture_document_tab(hwnd: int, pid: int, name: str, screenshot_path: str,
                         *, timeout: float = 30) -> dict:
    """Fresh capture only after selection, and re-check selection after capture."""
    started = time.monotonic()
    shot = Path(screenshot_path).absolute()
    result = {"status": "error", "screenshot": None, "note": ""}
    try:
        if shot.exists() or shot.is_symlink():
            raise ValueError("Screenshot destination already exists")
        if any(p.is_symlink() or p.is_junction() for p in shot.parents):
            raise ValueError("Screenshot path contains a link or junction")
        evidence = select_document_tab(hwnd, pid, name, timeout=timeout)
        result["selection"] = evidence
        if evidence["status"] != "verified":
            result.update(status=evidence["status"], note=evidence.get("note", ""))
            return result
        # Allow paint to settle without interpreting responsiveness as loaded data.
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 1:
            result.update(status="timed_out", note="No capture time left")
            return result
        time.sleep(min(0.5, remaining / 4))
        shot.parent.mkdir(parents=True, exist_ok=True)
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            result.update(status="timed_out", note="No window capture time left")
            return result
        capture = _bounded_capture(hwnd, pid, shot, remaining)
        captured = capture["status"] == "captured"
        blank = capture.get("screenshot_blank")
        result["screenshot_blank"] = blank
        if not captured:
            result.update(status=capture["status"], note=capture.get("note", ""))
            return result
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            result.update(status="timed_out", note="Capture deadline exceeded")
            return result
        verified = select_document_tab(hwnd, pid, name, timeout=remaining, verify_only=True)
        result["post_capture_selection"] = verified
        if captured and shot.is_file() and blank is False and verified["status"] == "verified":
            result.update(status="captured", screenshot=str(shot),
                          note="Verified selected tab; nonblank full-window capture, manual review only")
        else:
            result.update(note="Blank/failed capture or selected tab changed during capture")
        return result
    except Exception as exc:
        result.update(note=f"{type(exc).__name__}: {exc}")
        return result
    finally:
        result["duration_s"] = round(time.monotonic() - started, 3)
