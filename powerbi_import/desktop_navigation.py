"""Bounded, document-scoped UI Automation tab selection (Windows, stdlib only).

No coordinates, keyboard shortcuts, translated labels, or process-wide window
searches are used. Unsupported providers fail closed. Tableau additionally
supports exact named sheets in its structurally qualified Window menu; arbitrary
ListItems and static menu commands are never navigation targets.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from powerbi_import import desktop_window
from powerbi_import.desktop_accessibility import (
    DESKTOP_DIALOG_OBSERVER_SCRIPT,
    is_powershell_failure,
    powershell_preflight,
    run_accessibility_script,
    select_native_tab,
)

_CAPTURE_SCRIPT = """
import json, sys
from powerbi_import import desktop_window as w
hwnd, pid, path = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
def frame():
    return next((v for v in w.windows_for_pid(pid) if v.hwnd == hwnd), None)
before = frame()
if w.window_process_id(hwnd) != pid:
    result = {"status": "error", "cause": "window_ownership_changed", "retryable": False}
elif w.dialog_windows(pid) or not w.is_responsive(hwnd):
    result = {"status": "inaccessible", "cause": "dialog_or_unresponsive_document", "retryable": False}
elif before is None:
    result = {"status": "error", "cause": "document_window_changed", "retryable": False}
else:
    captured = w.capture_window(hwnd, path)
    image = w.inspect_capture_png(path) if captured else None
    after = frame()
    if w.window_process_id(hwnd) != pid:
        result = {"status": "error", "cause": "window_ownership_changed", "retryable": False}
    elif w.dialog_windows(pid):
        result = {"status": "inaccessible", "cause": "owned_dialog", "retryable": False}
    elif before != after:
        result = {"status": "error", "cause": "document_window_changed", "retryable": False}
    elif (image and (image["width"], image["height"]) != (before.width, before.height)):
        result = {"status": "error", "cause": "capture_dimensions_changed", "retryable": False}
    elif not captured:
        result = {"status": "error", "cause": "capture_failed", "retryable": True}
    elif not image:
        result = {"status": "error", "cause": "capture_invalid", "retryable": True}
    elif image["blank"]:
        result = {"status": "error", "cause": "capture_blank", "retryable": True,
                  "screenshot_blank": True}
    else:
        result = {"status": "captured", "screenshot_blank": False, "image": image}
print(json.dumps(result))
"""


def _bounded_capture(hwnd: int, pid: int, shot: Path, timeout: float) -> dict:
    """PrintWindow can hang inside a provider; bound only our capture child."""
    deadline = time.monotonic() + timeout
    ready = desktop_window.wait_for_document_ready(hwnd, pid, timeout=timeout)
    if ready["status"] != "ready":
        return ready
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"status": "timed_out", "note": "No capture time left after native progress"}
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _CAPTURE_SCRIPT, str(hwnd), str(pid), str(shot)],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=remaining,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode:
            return {"status": "error", "cause": "capture_process_failed",
                    "note": "capture_process_failed", "retryable": False}
        result = json.loads(proc.stdout)
        if not isinstance(result, dict) or result.get("status") not in {
                "captured", "error", "inaccessible"}:
            raise ValueError
        return result
    except subprocess.TimeoutExpired:
        return {"status": "timed_out", "cause": "capture_timeout",
                "note": "Window capture child exceeded page deadline", "retryable": False}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return {"status": "error", "cause": "capture_protocol_failed",
                "note": "capture_protocol_failed", "retryable": False}


# The child is intentionally single-purpose and killed by subprocess.run on
# timeout. Applications are never terminated. Input is JSON, never interpolated
# into PowerShell source. Control types/patterns are locale independent.
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
try {
$q = [Console]::In.ReadToEnd() | ConvertFrom-Json
$script:discoveryIncomplete = $false
$script:actionAttempted = $false
$A = [System.Windows.Automation.AutomationElement]
$T = [System.Windows.Automation.ControlType]
$S = [System.Windows.Automation.SelectionItemPattern]
$root = $A::FromHandle([IntPtr][long]$q.hwnd)
if ($null -eq $root -or $root.Current.ProcessId -ne [int]$q.pid) {
    @{status='error'; cause='window_ownership_changed'; retryable=$false} |
        ConvertTo-Json -Compress
    exit
}
# Power BI also exposes ribbon tabs, field panes and custom-visual tabs. They
# must never masquerade as report pages (e.g. a report page named "Home").
# The executable and provider class are locale independent. Some WebView
# providers do not implement AutomationId conditions consistently, so inspect
# ancestry of semantic tabs rather than relying on a host FindFirst.
$processName = [Diagnostics.Process]::GetProcessById([int]$q.pid).ProcessName
$isPBI = $processName -eq 'PBIDesktop'
$isTableau = $processName -eq 'tableau'
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
function IsTableauSheetStrip($e) {
    if (-not $isTableau -or $e.Current.ClassName -ne 'SheetListWidget' -or
        $e.Current.AutomationId -ne 'SheetTabsWidget.SheetListWidget') { return $false }
    $p = $walker.GetParent($e)
    return ($null -ne $p -and $p.Current.ClassName -eq 'SheetTabsWidget' -and
            $p.Current.AutomationId -eq 'SheetTabsWidget')
}
function Nodes {
    $list = [Collections.Generic.List[System.Windows.Automation.AutomationElement]]::new()
    if ($root.Current.ControlType -eq $T::Tab) { $list.Add($root) }
    # Provider-side filtering crosses embedded browser/provider boundaries much
    # more reliably than walking every report visual through ControlViewWalker.
    # The outer Python timeout also bounds a hung FindAll provider call.
    $conditions = @(
        [System.Windows.Automation.PropertyCondition]::new($A::ControlTypeProperty, $T::Tab),
        [System.Windows.Automation.PropertyCondition]::new($A::ControlTypeProperty, $T::TabItem),
        [System.Windows.Automation.PropertyCondition]::new($A::NameProperty, [string]$q.name)
    )
    if ($isTableau) {
        $conditions += [System.Windows.Automation.PropertyCondition]::new(
            $A::ClassNameProperty, 'SheetListWidget')
    }
    $condition = [System.Windows.Automation.OrCondition]::new(
        [System.Windows.Automation.Condition[]]$conditions)
    $found = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    if ($found.Count -gt 6000) {
        $script:discoveryIncomplete = $true
        throw 'Incomplete page-tab discovery'
    }
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
$result = @{status='inaccessible'; note='No uniquely named selectable tab exposed by UI Automation (overflow may be unsupported by this provider)'; discovered=@(); selected=$null; application=$processName}
$nodes = Nodes
# Tableau Qt can expose the workbook strip as a List of unnamed ListItems.
# MSAA exposes these same entries as ROLE_SYSTEM_LISTITEM, not PAGETAB.
# SelectionItem.IsSelected on this control can change highlight WITHOUT
# activating the worksheet. Never infer names from order, screen position,
# the current worksheet widget, or a stale selection bit. Record the provider
# blocker instead of treating arbitrary ListItem selection as page evidence.
$sheetItems = @()
foreach ($container in $nodes) {
    if (-not (IsTableauSheetStrip $container)) { continue }
    $items = $container.FindAll([System.Windows.Automation.TreeScope]::Children,
        [System.Windows.Automation.PropertyCondition]::new(
            $A::ControlTypeProperty, $T::ListItem))
    foreach ($item in $items) {
        $sheetItems += @{name=$item.Current.Name; selected=(Selected $item);
            offscreen=$item.Current.IsOffscreen; enabled=$item.Current.IsEnabled;
            runtime_id=($item.GetRuntimeId() -join ':');
            control_type='ListItem'; container_id=$container.Current.AutomationId}
    }
}
if ($sheetItems.Count -gt 0) {
    $result.native_sheet_items = $sheetItems
    $result.discovered = $sheetItems
    $result.note = 'Native Tableau sheet strip exposes ListItems, not verifiably named/activated page tabs; no sheet-list action attempted'
    $result | ConvertTo-Json -Depth 6 -Compress
    exit
}
# Discover virtualized tabs by the locale-independent ItemContainer property.
foreach ($container in $nodes) {
    if (-not (IsContainer $container)) { continue }
    $ic = $null
    if ($container.TryGetCurrentPattern([System.Windows.Automation.ItemContainerPattern]::Pattern, [ref]$ic)) {
        $item = $ic.FindItemByProperty($null, $A::NameProperty, [string]$q.name)
        if ($null -ne $item) {
            $vp = $null
            if ($q.mode -eq 'select' -and $item.TryGetCurrentPattern([System.Windows.Automation.VirtualizedItemPattern]::Pattern, [ref]$vp)) {
                $script:actionAttempted = $true
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
            $script:actionAttempted = $true
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
                $script:actionAttempted = $true
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
} catch {
    @{status='inaccessible'; cause=$(if ($script:discoveryIncomplete) {
        'partial_provider_observation'} else {'ui_unreadable'});
      note='UI Automation page-tab observation incomplete'; retryable=$true;
      action_attempted=$script:actionAttempted} | ConvertTo-Json -Compress
}
"""


_TABLEAU_WINDOW_MENU_ID = "mainMenu.menu_Window"

# Separate from tab discovery: QAction runtime IDs are reassigned on rebuild.
# Only the named tail AFTER the last separator in this exact menu is invokable.
# Discovery expands/collapses qualified top-level menus, never invokes commands.
_TABLEAU_WINDOW_MENU_SCRIPT = r"""
$clock = [Diagnostics.Stopwatch]::StartNew()
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class TableauMenuWindow {
    delegate bool Callback(IntPtr h, IntPtr p);
    [DllImport("user32.dll")] static extern bool EnumWindows(Callback f, IntPtr p);
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr h, uint f);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern int GetWindowTextLengthW(IntPtr h);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    static extern int GetWindowTextW(IntPtr h, StringBuilder text, int length);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    static extern int GetClassNameW(IntPtr h, StringBuilder text, int length);
    [DllImport("user32.dll")] public static extern bool IsWindowEnabled(IntPtr h);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(
        IntPtr h, out uint p);
    public static bool Owned(long h, int p) {
        uint actual;
        return GetWindowThreadProcessId(new IntPtr(h), out actual) != 0 &&
               actual == (uint)p;
    }
    public static string PopupState(long h, int p) {
        IntPtr window = new IntPtr(h);
        if (!IsWindow(window)) return "gone";
        if (!Owned(h, p)) return "foreign";
        return IsWindowVisible(window) ? "visible" : "hidden";
    }
    public sealed class DialogObservation {
        public long hwnd, owner_hwnd;
        public int? pid_before, pid_after;
        public bool owned_before, owned_after, visible_before, visible_after;
        public string class_name = "", title = "";
        public bool metadata_complete, title_truncated;
    }
    public sealed class GuardObservation {
        public string basis = "win32", reason;
        public long elapsed_ms, document_hwnd;
        public int expected_pid;
        public int? document_pid_before, document_pid_after;
        public bool document_owned_before, document_owned_after;
        public bool document_enabled, document_enabled_after;
        public bool document_visible, document_visible_after;
        public bool dialog_check_performed, owned_dialog_present;
        public bool dialogs_complete, dialogs_truncated;
        public DialogObservation[] owned_dialogs = new DialogObservation[0];
    }
    static int? WindowPid(IntPtr h) {
        uint actual;
        return GetWindowThreadProcessId(h, out actual) != 0 && actual != 0
            ? (int?)actual : null;
    }
    public static GuardObservation ObserveGuard(long hwnd, int pid, bool dialogs) {
        IntPtr document = new IntPtr(hwnd);
        var result = new GuardObservation {
            document_hwnd = hwnd, expected_pid = pid,
            document_pid_before = WindowPid(document),
            document_enabled = IsWindowEnabled(document),
            document_visible = IsWindowVisible(document)
        };
        result.document_owned_before = result.document_pid_before == pid;
        if (dialogs && result.document_owned_before) {
            result.dialog_check_performed = true;
            var observations = new List<DialogObservation>();
            result.dialogs_complete = EnumWindows((window, unused) => {
                bool visible = IsWindowVisible(window);
                IntPtr owner = GetWindow(window, 4);
                int titleLength = GetWindowTextLengthW(window);
                int? before = WindowPid(window);
                // Keep the existing visible, titled, process-owned guard.
                if (!visible || owner == IntPtr.Zero || titleLength <= 0 || before != pid)
                    return true;
                result.owned_dialog_present = true;
                if (observations.Count == 16) {
                    result.dialogs_truncated = true;
                    return false;
                }
                var observation = new DialogObservation {
                    hwnd = window.ToInt64(), owner_hwnd = owner.ToInt64(),
                    pid_before = before, owned_before = true, visible_before = visible
                };
                var title = new StringBuilder(513);
                var className = new StringBuilder(257);
                int titleRead = GetWindowTextW(window, title, title.Capacity);
                int classRead = GetClassNameW(window, className, className.Capacity);
                observation.pid_after = WindowPid(window);
                observation.owned_after = observation.pid_after == pid;
                observation.visible_after = IsWindowVisible(window);
                observation.metadata_complete = observation.owned_after &&
                    titleRead > 0 && classRead > 0;
                observation.title_truncated = titleLength > 512 || titleRead >= 512;
                // Never expose text from a live foreign/reused HWND.
                if (observation.owned_after) {
                    observation.title = title.ToString();
                    observation.class_name = className.ToString();
                }
                observations.Add(observation);
                return true;
            }, IntPtr.Zero) && !result.dialogs_truncated;
            result.owned_dialogs = observations.ToArray();
        }
        result.document_pid_after = WindowPid(document);
        result.document_owned_after = result.document_pid_after == pid;
        result.document_enabled_after = IsWindowEnabled(document);
        result.document_visible_after = IsWindowVisible(document);
        return result;
    }
}
'@
""" + DESKTOP_DIALOG_OBSERVER_SCRIPT + r"""
$q = [Console]::In.ReadToEnd() | ConvertFrom-Json
$A = [Windows.Automation.AutomationElement]
$T = [Windows.Automation.ControlType]
# PowerShell names are case-insensitive: $E would be shadowed by sheet $e.
$expandPatternType = [Windows.Automation.ExpandCollapsePattern]
$walker = [Windows.Automation.TreeWalker]::RawViewWalker
$menuId = 'mainMenu.menu_Window'
$out = @{status='unsupported'; cause='tableau_window_menu_unsupported';
    provider='tableau_window_menu'; selected=$null; discovered=@();
    menu_snapshots=@(); menu_closed=$true; fresh_check=$false;
    action_attempted=$false; invoked=0; retryable=$false; menu_hint_used=$false}
$script:opened = $null
$script:openedPopup = $null
$script:progressDialogIdentities = $null
$script:popupNativeState = $null
$script:closeStartedAt = $null
$script:closePollCount = 0
$script:closedSince = $null
$script:collapseRequested = $false
$script:failure = 'tableau_window_menu_unreadable'
$script:failureStatus = 'unsupported'
$script:operation = 'process_check'
$script:topMenuName = $null
$script:guardStop = $false
function Exception-Type($exception) {
    if ($null -eq $exception) { return $null }
    $typeName = $exception.GetType().FullName
    if ($typeName.Length -le 160) { return $typeName }
    return 'System.Exception'
}
function Failure-Details($record) {
    # Public top-menu caption is bounded, never a sheet/request/exception body.
    return @{operation=$script:operation; guard_stop=$script:guardStop;
        top_menu_name=$script:topMenuName;
        exception_type=(Exception-Type $record.Exception);
        inner_exception_type=(Exception-Type $record.Exception.InnerException)}
}
function Stop-Menu($cause, $status='unsupported') {
    $script:failure = $cause; $script:failureStatus = $status
    $script:guardStop = $true
    throw 'Tableau menu observation stopped'
}
function Check-Time {
    if ($clock.Elapsed.TotalSeconds -ge [double]$q.seconds) {
        Stop-Menu 'tableau_window_menu_timeout' 'timed_out'
    }
}
function Pause-Menu {
    Check-Time
    $script:operation = 'menu_settle'
    $ms = [Math]::Min(100, ([double]$q.seconds-$clock.Elapsed.TotalSeconds)*1000)
    if ($ms -gt 0) { Start-Sleep -Milliseconds ([int]$ms) }
    Check-Time
}
function Retain-DocumentGuard($observation, $reason) {
    # Private native evidence: retain the first failed decision, not cleanup state.
    if (-not $out.ContainsKey('document_guard')) {
        $observation.reason = $reason
        $observation.elapsed_ms = [long]$clock.Elapsed.TotalMilliseconds
        $out.document_guard = $observation
    }
}
function Observe-DocumentMenuIdentity($proof, [bool]$anchor=$false) {
    if ($clock.Elapsed.TotalSeconds -ge [double]$q.seconds) {
        $proof.cause='guard_classification_deadline'; return
    }
    $proof.document_after=[TableauMenuWindow]::ObserveGuard(
        [long]$q.hwnd, [int]$q.pid, $false)
    $liveDocument=$A::FromHandle([IntPtr][long]$q.hwnd)
    $proof.document_runtime_id_after=$liveDocument.GetRuntimeId() -join ':'
    $proof.document_identity_after=(
        $proof.document_after.document_owned_before -and
        $proof.document_after.document_owned_after -and
        $liveDocument.Current.ProcessId -eq [int]$q.pid -and
        $proof.document_runtime_id_after -ceq $script:documentId)
    if ($clock.Elapsed.TotalSeconds -ge [double]$q.seconds) {
        $proof.cause='guard_classification_deadline'; return
    }
    if ($null -ne $top -and $null -ne $bar) {
        $topParent=$walker.GetParent($top.item)
        $proof.menu_bar_runtime_id=$bar.GetRuntimeId() -join ':'
        $proof.top_runtime_id=$top.item.GetRuntimeId() -join ':'
        $proof.menu_identity_after=(
            $proof.menu_bar_runtime_id -ceq $barId -and
            $proof.top_runtime_id -ceq $top.id -and $null -ne $topParent -and
            ($topParent.GetRuntimeId() -join ':') -ceq $barId -and
            $bar.Current.ProcessId -eq [int]$q.pid -and
            $bar.Current.AutomationId -ceq 'mainMenu' -and
            $bar.Current.ClassName -ceq 'QMenuBar' -and
            $bar.Current.ControlType -eq $T::MenuBar -and
            $top.item.Current.ProcessId -eq [int]$q.pid -and
            $top.item.Current.ClassName -ceq 'QAction' -and
            $top.item.Current.ControlType -eq $T::MenuItem)
        if ($anchor) {
            $p=$bar; $anchored=$false
            for ($i=0; $i -lt 16 -and $null -ne $p; $i++) {
                if ($clock.Elapsed.TotalSeconds -ge [double]$q.seconds) {
                    $proof.cause='guard_classification_deadline'; return
                }
                if (($p.GetRuntimeId() -join ':') -ceq $script:documentId) {
                    $anchored=$true; break
                }
                $p=$walker.GetParent($p)
            }
            $proof.menu_ancestry_verified=$anchored
            $proof.menu_identity_after=$proof.menu_identity_after -and $anchored
        }
    }
    if ($clock.Elapsed.TotalSeconds -ge [double]$q.seconds) {
        $proof.document_identity_after=$false; $proof.menu_identity_after=$false
        $proof.cause='guard_classification_deadline'
    }
}
function Observe-DocumentGuardClassification(
        $guard, [bool]$retain=$true, $approvedDialogs=$null) {
    if ($retain -and $out.ContainsKey('document_guard_classification')) {
        $script:currentGuardClassification=$out.document_guard_classification
        return
    }
    $started=$clock.Elapsed.TotalMilliseconds
    $proof=@{phase='document_guard_before_menu_cleanup'; basis='shared_strict_uia';
        progress_required=$true; wait_applied=$false; resume_authorized=$false;
        terminal_status='inaccessible'; terminal_cause='owned_dialog_or_disabled_document';
        guard_elapsed_ms=$guard.elapsed_ms; started_ms=[long]$started;
        complete=$false; dialogs=@(); document_runtime_id_at_guard=$script:documentId;
        document_identity_after=$false; menu_identity_after=$false}
    if (-not $retain) { $proof.phase='document_guard_wait' }
    if ($retain) { $out.document_guard_classification=$proof }
    $script:currentGuardClassification=$proof
    try {
        if (-not $guard.dialogs_complete -or $guard.dialogs_truncated -or
            $guard.owned_dialogs.Count -lt 1 -or
            -not $guard.document_owned_before -or -not $guard.document_owned_after) {
            $proof.cause='guard_dialog_inventory_unqualified'; return
        }
        foreach ($dialog in $guard.owned_dialogs) {
            $entry=@{hwnd=$dialog.hwnd; started_ms=[long]$clock.Elapsed.TotalMilliseconds;
                status='not_observed'; busy_conclusive=$false}
            $proof.dialogs+= $entry
            if (-not $dialog.metadata_complete -or $dialog.title_truncated -or
                -not $dialog.owned_before -or -not $dialog.owned_after -or
                -not $dialog.visible_before -or -not $dialog.visible_after -or
                $dialog.owner_hwnd -ne [long]$q.hwnd) {
                $entry.cause='guard_dialog_unqualified'; continue
            }
            $expected=$dialog; $postApprovedLayout=$false
            if ($null -ne $approvedDialogs) {
                $key=[long]$dialog.hwnd
                if (-not $approvedDialogs.ContainsKey($key)) {
                    $entry.cause='tableau_progress_dialog_inventory_changed'
                    $proof.cause=$entry.cause; continue
                }
                $expected=$approvedDialogs[$key]
                if ($expected.absence_observed) {
                    $entry.cause='tableau_progress_dialog_reappeared'
                    $proof.cause=$entry.cause; continue
                }
                $postApprovedLayout=($expected.progress_basis -ceq 'tableau_layout_timer')
            }
            $left=[double]$q.seconds-$clock.Elapsed.TotalSeconds
            if ($left -le 0) {
                $entry.status='timed_out'; $entry.cause='guard_classification_deadline'; break
            }
            # Immediate, read-only observation of THIS first failing dialog.
            # The shared function checks native/UIA ownership and exact identity.
            $entry.metadata=Read-DesktopDialogMetadata $dialog.hwnd ([int]$q.pid) $left (
                $expected) $null $false $postApprovedLayout
            $entry.status=$entry.metadata.status
            $entry.finished_ms=[long]$clock.Elapsed.TotalMilliseconds
            $entry.busy_conclusive=(
                $entry.metadata.strict_classification.status -ceq 'busy' -and
                $entry.metadata.strict_classification.complete -eq $true -and
                $clock.Elapsed.TotalSeconds -lt [double]$q.seconds)
        }
        if ($clock.Elapsed.TotalSeconds -ge [double]$q.seconds) {
            $proof.cause='guard_classification_deadline'; return
        }
        Observe-DocumentMenuIdentity $proof (-not $retain)
        $proof.complete=(
            $clock.Elapsed.TotalSeconds -lt [double]$q.seconds -and
            $proof.document_identity_after -and $proof.menu_identity_after -and
            $proof.dialogs.Count -eq $guard.owned_dialogs.Count -and
            @($proof.dialogs | Where-Object {
                $_.metadata.strict_classification.complete -ne $true }).Count -eq 0)
        if (-not $proof.complete -and -not $proof.cause) {
            $proof.cause='guard_classification_incomplete'
        }
    } catch {
        $proof.cause='guard_classification_failed'
        $proof.exception_type=Exception-Type $_.Exception
    } finally {
        if (-not $proof.complete) {
            foreach ($entry in $proof.dialogs) { $entry.busy_conclusive=$false }
        }
        $proof.finished_ms=[long]$clock.Elapsed.TotalMilliseconds
        $proof.elapsed_ms=[long]($clock.Elapsed.TotalMilliseconds-$started)
    }
}
function Record-DocumentProgress($wait, $observation) {
    $wait.observation_count++
    $wait.last_observation=$observation
    if ($wait.observations.Count -lt 8) {
        $wait.observations+= $observation
    } else { $wait.observations_truncated=$true }
}
function Observe-ProgressCaptionTransition($sample, $retained, $transition=$null) {
    # Pin at most ONE read-only follow-up before terminal cleanup. Its result
    # cannot authorize a new progress phase, waiting, or navigation.
    if ($out.ContainsKey('document_guard_caption_transition')) { return }
    $matches=@($sample.guard.owned_dialogs | Where-Object { $_.hwnd -eq $retained.hwnd })
    if ($matches.Count -ne 1 -or -not $sample.guard.dialogs_complete -or
        $sample.guard.dialogs_truncated -or -not $sample.guard.document_owned_before -or
        -not $sample.guard.document_owned_after) { return }
    $dialog=$matches[0]
    if (-not $dialog.metadata_complete -or $dialog.title_truncated -or
        -not $dialog.owned_before -or -not $dialog.owned_after -or
        -not $dialog.visible_before -or -not $dialog.visible_after -or
        $dialog.owner_hwnd -ne $retained.owner_hwnd -or
        $dialog.class_name -cne $retained.class_name) { return }
    if ($null -ne $transition) {
        if (-not $transition.caption_changed -or -not $transition.immutable_identity_complete -or
            $transition.uia_runtime_id_before -cne $retained.runtime_id -or
            $transition.uia_runtime_id_after -cne $retained.runtime_id -or
            -not $sample.classification.document_identity_after -or
            -not $sample.classification.menu_identity_after) { return }
        $classification=$transition.strict_classification
    } else {
        if ($dialog.title -ceq $retained.title) { return }
        $classification=Get-DesktopDialogClassification @{native_before=$dialog; texts=@()}
    }
    if ($classification.protected_text -or $classification.reason -eq 'interactive_controls') {
        return
    }
    $started=[long]$clock.Elapsed.TotalMilliseconds
    $proof=@{phase='document_progress_caption_transition_before_menu_cleanup';
        diagnostic_only=$true; previously_approved_progress=$true;
        wait_applied=$false; wait_authorized=$false; resume_authorized=$false;
        hwnd=$retained.hwnd; retained_dialog_identity=$retained;
        transition_guard=$sample.guard; transition_observation=$transition;
        started_ms=$started; deadline_ms=[long]([double]$q.seconds*1000);
        observation_count=0; complete=$false}
    $out.document_guard_caption_transition=$proof
    $script:operation='document_progress_caption_transition'
    try {
        $before=@{}
        Observe-DocumentMenuIdentity $before $true
        $proof.identity_before=$before
        if (-not $before.document_identity_after -or -not $before.menu_identity_after) {
            $proof.cause='caption_observation_identity_unverified'; return
        }
        $left=[double]$q.seconds-$clock.Elapsed.TotalSeconds
        if ($left -le 0) { $proof.cause='caption_observation_deadline'; return }
        $proof.observation_count=1
        $proof.fresh_observation=Read-DesktopDialogMetadata (
            [long]$retained.hwnd) ([int]$q.pid) $left $retained $null $true
        if ($clock.Elapsed.TotalSeconds -ge [double]$q.seconds) {
            $proof.cause='caption_observation_deadline'; return
        }
        $after=@{}
        Observe-DocumentMenuIdentity $after $true
        $proof.identity_after=$after
        $fresh=$proof.fresh_observation
        $proof.complete=($clock.Elapsed.TotalSeconds -lt [double]$q.seconds -and
            $after.document_identity_after -and $after.menu_identity_after -and
            $fresh.status -ceq 'observed' -and $fresh.immutable_identity_complete -and
            $fresh.strict_classification.complete -and
            -not $fresh.strict_classification.protected_text -and
            $fresh.strict_classification.reason -ne 'interactive_controls')
        if (-not $proof.complete) { $proof.cause='caption_observation_incomplete_or_protected' }
    } catch {
        $proof.cause='caption_observation_failed'
        $proof.exception_type=Exception-Type $_.Exception
    } finally {
        if ($clock.Elapsed.TotalSeconds -ge [double]$q.seconds) {
            $proof.complete=$false; $proof.cause='caption_observation_deadline'
        }
        $proof.finished_ms=[long]$clock.Elapsed.TotalMilliseconds
        $proof.elapsed_ms=$proof.finished_ms-$started
    }
}
function Wait-DocumentProgress($guard) {
    $first=$script:currentGuardClassification
    if ($out.ContainsKey('document_guard_wait') -and $first.cause -in @(
        'tableau_progress_dialog_reappeared', 'tableau_progress_dialog_inventory_changed')) {
        $prior=$out.document_guard_wait
        $prior.status='stopped'; $prior.resume_authorized=$false; $prior.cause=$first.cause
        $prior.finished_ms=[long]$clock.Elapsed.TotalMilliseconds
        $prior.elapsed_ms=$prior.finished_ms-$prior.started_ms; $prior.invoked_at_end=$out.invoked
        Stop-Menu $first.cause 'error'
    }
    # Waiting is post-invoke only, not permission to select/retry a sheet.
    if ($q.mode -cne 'select' -or $out.invoked -ne 1 -or
        -not $out.action_attempted -or -not $guard.document_visible -or
        -not $guard.document_visible_after -or -not $first.complete -or
        $first.dialogs.Count -lt 1 -or
        @($first.dialogs | Where-Object { -not $_.busy_conclusive }).Count -gt 0) {
        if ($out.ContainsKey('document_guard_wait')) {
            $prior=$out.document_guard_wait
            $prior.status='stopped'; $prior.resume_authorized=$false
            $prior.cause='dialog_not_conclusive_busy'
            $prior.finished_ms=[long]$clock.Elapsed.TotalMilliseconds
            $prior.elapsed_ms=$prior.finished_ms-$prior.started_ms
            $prior.invoked_at_end=$out.invoked
        }
        return $false
    }
    if ($out.ContainsKey('document_guard_wait')) {
        $wait=$out.document_guard_wait
        $wait.status='checking'; $wait.resume_authorized=$false
    } else {
        $wait=@{phase='document_guard_wait'; status='checking'; wait_applied=$false;
            resume_authorized=$false; invoked_at_start=$out.invoked;
            started_ms=[long]$clock.Elapsed.TotalMilliseconds;
            deadline_ms=[long]([double]$q.seconds*1000); guard_elapsed_ms=$guard.elapsed_ms;
            episode_count=0; observation_count=0; observations=@(); observations_truncated=$false}
        $out.document_guard_wait=$wait
        # Local to this one script/page budget; never a cross-page cache.
        $script:progressDialogIdentities=@{}
    }
    $wait.episode_count++
    $approved=$script:progressDialogIdentities
    try {
        Check-Time
        foreach ($entry in $first.dialogs) {
            $key=[long]$entry.hwnd; $native=$entry.metadata.native_before
            if ($approved.ContainsKey($key) -and $approved[$key].absence_observed) {
                Stop-Menu 'tableau_progress_dialog_reappeared' 'error'
            }
            if ($approved.ContainsKey($key) -and (
                $approved[$key].runtime_id -cne $entry.metadata.uia_runtime_id_before -or
                $approved[$key].class_name -cne $native.class_name -or
                $approved[$key].owner_hwnd -ne $native.owner_hwnd)) {
                Stop-Menu 'tableau_progress_dialog_identity_changed' 'error'
            }
            if (-not $approved.ContainsKey($key) -and $wait.episode_count -gt 1) {
                Stop-Menu 'tableau_progress_dialog_inventory_changed' 'error'
            }
            if (-not $approved.ContainsKey($key) -and $approved.Count -ge 64) {
                Stop-Menu 'tableau_progress_dialog_inventory_changed' 'error'
            }
            if (-not $approved.ContainsKey($key)) {
                $approved[$key]=@{hwnd=$key; owner_hwnd=$native.owner_hwnd;
                    owner_pid=[int]$q.pid; class_name=$native.class_name;
                    runtime_id=$entry.metadata.uia_runtime_id_before;
                    progress_basis=$entry.metadata.strict_classification.basis;
                    absence_observed=$false; title=$native.title}
                # title is pinned provenance only, never immutable identity.
            }
        }
        $identity=@{}
        Observe-DocumentMenuIdentity $identity $true
        Check-Time
        if (-not $identity.document_identity_after -or -not $identity.menu_identity_after) {
            Stop-Menu 'tableau_progress_document_or_menu_identity_changed' 'error'
        }
        $wait.wait_applied=$true; $wait.status='waiting'
        do {
            $script:operation='document_progress_wait'
            Check-Time
            $left=[double]$q.seconds-$clock.Elapsed.TotalSeconds
            $ms=[int][Math]::Min(100, $left*1000)
            if ($ms -gt 0) { Start-Sleep -Milliseconds $ms }
            Check-Time
            # Never carry the disabled state or a cached menu identity forward.
            Check-Document $false
            $script:operation='document_progress_wait'
            $identity=@{}
            Observe-DocumentMenuIdentity $identity $true
            Check-Time
            if (-not $identity.document_identity_after -or -not $identity.menu_identity_after) {
                Stop-Menu 'tableau_progress_document_or_menu_identity_changed' 'error'
            }
            $current=[TableauMenuWindow]::ObserveGuard(
                [long]$q.hwnd, [int]$q.pid, $true)
            $current.elapsed_ms=[long]$clock.Elapsed.TotalMilliseconds
            $sample=@{elapsed_ms=[long]$clock.Elapsed.TotalMilliseconds;
                guard=$current; identity_before=$identity; dialog_lifecycle=@()}
            Record-DocumentProgress $wait $sample
            Check-Time
            if (-not $current.document_owned_before -or -not $current.document_owned_after) {
                Stop-Menu 'window_ownership_changed' 'error'
            }
            if (-not $current.document_visible -or -not $current.document_visible_after -or
                -not $current.dialogs_complete -or $current.dialogs_truncated) {
                Stop-Menu 'document_guard_incomplete' 'error'
            }
            $present=@{}
            foreach ($dialog in $current.owned_dialogs) {
                $key=[long]$dialog.hwnd
                if (-not $approved.ContainsKey($key) -or
                    $dialog.owner_hwnd -ne $approved[$key].owner_hwnd -or
                    $dialog.class_name -cne $approved[$key].class_name) {
                    Stop-Menu 'tableau_progress_dialog_identity_changed' 'error'
                }
                if ($approved[$key].absence_observed) {
                    Stop-Menu 'tableau_progress_dialog_reappeared' 'error'
                }
                $present[$key]=$true
            }
            foreach ($key in $approved.Keys) {
                if ($present.ContainsKey($key)) { continue }
                Check-Time
                $state=[TableauMenuWindow]::PopupState($key, [int]$q.pid)
                $lifecycle=@{hwnd=$key; native_state=$state; absence_observed=$false}
                $sample.dialog_lifecycle+= $lifecycle
                Check-Time
                if ($state -eq 'foreign') { Stop-Menu 'window_ownership_changed' 'error' }
                if ($state -eq 'visible') {
                    Stop-Menu 'tableau_progress_dialog_inventory_changed' 'error'
                }
                if ($state -cnotin @('gone', 'hidden')) {
                    Stop-Menu 'tableau_progress_dialog_inventory_changed' 'error'
                }
                if ($state -eq 'hidden') {
                    # A live hidden handle is not a vanished window: requalify
                    # its retained native AND UIA identity before treating it absent.
                    $native=[NativeDesktopDialogWindow]::Read($key, [int]$q.pid)
                    Check-Time
                    $element=$A::FromHandle([IntPtr]$key)
                    $elementCurrent=$element.Current
                    $runtimeId=$element.GetRuntimeId() -join ':'
                    Check-Time
                    $nativeAfter=[NativeDesktopDialogWindow]::Read($key, [int]$q.pid)
                    Check-Time
                    $again=$A::FromHandle([IntPtr]$key)
                    $againCurrent=$again.Current
                    $lifecycle.native_before=$native; $lifecycle.native_after=$nativeAfter
                    $lifecycle.uia_runtime_id_before=$runtimeId
                    $lifecycle.uia_runtime_id_after=$again.GetRuntimeId() -join ':'
                    $nameBefore=[string]$elementCurrent.Name; $nameAfter=[string]$againCurrent.Name
                    $lifecycle.truncated=($nameBefore.Length -gt 500 -or $nameAfter.Length -gt 500)
                    $lifecycle.uia_root_name_before=$nameBefore.Substring(
                        0, [Math]::Min(500, $nameBefore.Length))
                    $lifecycle.uia_root_name_after=$nameAfter.Substring(
                        0, [Math]::Min(500, $nameAfter.Length))
                    Check-Time
                    if (-not $native.owned_before -or -not $native.owned_after -or
                        -not $nativeAfter.owned_before -or -not $nativeAfter.owned_after -or
                        $native.pid_before -ne [int]$q.pid -or $native.pid_after -ne [int]$q.pid -or
                        $nativeAfter.pid_before -ne [int]$q.pid -or
                        $nativeAfter.pid_after -ne [int]$q.pid -or
                        $native.hwnd -ne $key -or $nativeAfter.hwnd -ne $key -or
                        $native.visible -or $nativeAfter.visible -or
                        $native.truncated -or $nativeAfter.truncated -or $lifecycle.truncated -or
                        $native.class_name -cne $approved[$key].class_name -or
                        $nativeAfter.class_name -cne $approved[$key].class_name -or
                        $native.owner_hwnd -ne $approved[$key].owner_hwnd -or
                        $nativeAfter.owner_hwnd -ne $approved[$key].owner_hwnd -or
                        $elementCurrent.ProcessId -ne [int]$q.pid -or
                        $againCurrent.ProcessId -ne [int]$q.pid -or
                        [long]$elementCurrent.NativeWindowHandle -ne $key -or
                        [long]$againCurrent.NativeWindowHandle -ne $key -or
                        $runtimeId -cne $approved[$key].runtime_id -or
                        $lifecycle.uia_runtime_id_after -cne $approved[$key].runtime_id) {
                        Stop-Menu 'tableau_progress_dialog_identity_changed' 'error'
                    }
                    $attention=Get-DesktopDialogClassification @{
                        native_before=$native; native_after=$nativeAfter;
                        texts=@($lifecycle.uia_root_name_before, $lifecycle.uia_root_name_after)}
                    if ($attention.protected_text) {
                        Stop-Menu 'owned_dialog_or_disabled_document' 'inaccessible'
                    }
                    $lifecycle.immutable_identity_complete=$true
                }
                Check-Time
                # Observed absence ends this retained lifetime, including a
                # qualified hidden HWND. Qt's handle-derived runtime ID cannot
                # authorize a later appearance in this same page budget.
                if (-not $approved[$key].absence_observed) {
                    $approved[$key].absence_observed=$true
                    $approved[$key].absence_observed_at_ms=[long]$clock.Elapsed.TotalMilliseconds
                }
                $lifecycle.absence_observed=$true
            }
            if ($current.owned_dialog_present) {
                Observe-DocumentGuardClassification $current $false $approved
                $proof=$script:currentGuardClassification
                $sample.classification=$proof
                Check-Time
                if (-not $proof.complete) {
                    foreach ($entry in $proof.dialogs) {
                        if ($approved.ContainsKey([long]$entry.hwnd)) {
                            Observe-ProgressCaptionTransition $sample (
                                $approved[[long]$entry.hwnd]) $entry.metadata
                        }
                    }
                    Stop-Menu 'tableau_document_progress_unreadable' 'error'
                }
                foreach ($entry in $proof.dialogs) {
                    $retained=$approved[[long]$entry.hwnd]
                    if ($entry.metadata.uia_runtime_id_before -cne $retained.runtime_id -or
                        $entry.metadata.uia_runtime_id_after -cne $retained.runtime_id) {
                        Stop-Menu 'tableau_progress_dialog_identity_changed' 'error'
                    }
                    if (-not $entry.busy_conclusive) {
                        $wait.cause='dialog_not_conclusive_busy'
                        Stop-Menu 'owned_dialog_or_disabled_document' 'inaccessible'
                    }
                }
                Check-Time
                continue
            }
            $identity=@{}
            Observe-DocumentMenuIdentity $identity $true
            $sample.identity_after=$identity
            Check-Time
            if (-not $identity.document_identity_after -or -not $identity.menu_identity_after) {
                Stop-Menu 'tableau_progress_document_or_menu_identity_changed' 'error'
            }
            if (-not $current.document_enabled -or -not $current.document_enabled_after -or
                -not $identity.document_after.document_enabled -or
                -not $identity.document_after.document_enabled_after -or
                -not $identity.document_after.document_visible -or
                -not $identity.document_after.document_visible_after) {
                $wait.cause='document_disabled_without_progress'
                Stop-Menu 'owned_dialog_or_disabled_document' 'inaccessible'
            }
            Check-Time
            $wait.status='ready'; $wait.resume_authorized=$true
            # Readiness ONLY. The caller still rebuilds the exact Window menu
            # and proves unique fresh checked name + closure; no sheet Invoke here.
            return $true
        } while ($true)
    } catch {
        if ($clock.Elapsed.TotalSeconds -ge [double]$q.seconds -or
            $script:failureStatus -eq 'timed_out') {
            $wait.status='timed_out'; $wait.cause='tableau_document_progress_timeout'
            Stop-Menu 'tableau_document_progress_timeout' 'timed_out'
        }
        $wait.status='stopped'
        if (-not $wait.cause) { $wait.cause=$script:failure }
        if ($script:guardStop) { throw }
        $wait.cause='tableau_document_progress_unreadable'
        $wait.exception_type=Exception-Type $_.Exception
        Stop-Menu 'tableau_document_progress_unreadable' 'error'
    } finally {
        $wait.finished_ms=[long]$clock.Elapsed.TotalMilliseconds
        $wait.elapsed_ms=$wait.finished_ms-$wait.started_ms
        $wait.invoked_at_end=$out.invoked
    }
}
function Check-Document([bool]$dialogs=$true) {
    $script:operation = 'document_ownership'
    Check-Time
    $ownership = [TableauMenuWindow]::ObserveGuard(
        [long]$q.hwnd, [int]$q.pid, $false)
    if (-not $ownership.document_owned_before -or -not $ownership.document_owned_after) {
        Retain-DocumentGuard $ownership 'window_ownership_changed'
        Stop-Menu 'window_ownership_changed' 'error'
    }
    Check-Time
    $script:operation = 'document_identity'
    $current = $A::FromHandle([IntPtr][long]$q.hwnd)
    Check-Time
    if ($null -eq $current -or $current.Current.ProcessId -ne [int]$q.pid -or
        ($current.GetRuntimeId() -join ':') -cne $script:documentId) {
        Retain-DocumentGuard $ownership 'document_identity_changed'
        Stop-Menu 'document_identity_changed' 'error'
    }
    Check-Time
    $script:operation = 'document_dialogs'
    if ($dialogs) {
        $guard = [TableauMenuWindow]::ObserveGuard([long]$q.hwnd, [int]$q.pid, $true)
        $reason = $null
        if (-not $guard.document_owned_before -or -not $guard.document_owned_after) {
            $reason = 'window_ownership_changed'
        } elseif (-not $guard.document_enabled -or -not $guard.document_enabled_after) {
            $reason = $(if ($guard.owned_dialog_present) {
                'document_disabled_and_owned_dialog'
            } else { 'document_disabled' })
        } elseif ($guard.owned_dialog_present) {
            $reason = 'owned_dialog'
        } elseif (-not $guard.dialogs_complete) {
            $reason = 'native_guard_incomplete'
        }
        if ($null -ne $reason) { Retain-DocumentGuard $guard $reason }
        Check-Time
        if ($reason -eq 'window_ownership_changed') {
            Stop-Menu 'window_ownership_changed' 'error'
        }
        if ($reason -eq 'native_guard_incomplete') {
            Stop-Menu 'document_guard_incomplete' 'error'
        }
        if ($null -ne $reason) {
            if ($out.ContainsKey('document_guard_classification')) {
                $approved=$(if ($out.ContainsKey('document_guard_wait')) {
                    $script:progressDialogIdentities
                } else { $null })
                Observe-DocumentGuardClassification $guard $false $approved
                $out.last_document_guard_observation=@{guard=$guard;
                    classification=$script:currentGuardClassification}
            } else { Observe-DocumentGuardClassification $guard }
            if (Wait-DocumentProgress $guard) { Check-Time; return }
            # Unknown/protected/partial observations keep the original stop.
            Stop-Menu 'owned_dialog_or_disabled_document' 'inaccessible'
        }
    }
    Check-Time
}
function Find-Popup($top) {
    Check-Document $false
    $script:operation = 'top_menu_identity'
    Check-Time
    $barParent = $walker.GetParent($top.item)
    Check-Time
    if ($top.item.Current.ProcessId -ne [int]$q.pid -or
        $top.item.Current.ControlType -ne $T::MenuItem -or
        $top.item.Current.ClassName -cne 'QAction' -or
        ($top.item.GetRuntimeId() -join ':') -cne $top.id -or
        $null -eq $barParent -or
        ($barParent.GetRuntimeId() -join ':') -cne $barId -or
        $barParent.Current.AutomationId -cne 'mainMenu' -or
        $barParent.Current.ClassName -cne 'QMenuBar' -or
        $barParent.Current.ControlType -ne $T::MenuBar -or
        $barParent.Current.ProcessId -ne [int]$q.pid) {
        Stop-Menu 'tableau_menu_ancestry_changed' 'error'
    }
    Check-Time
    $retained = $script:openedPopup
    $script:popupNativeState = $null
    if ($null -ne $retained) {
        $script:operation = 'popup_native_identity'
        $script:popupNativeState = [TableauMenuWindow]::PopupState(
            [long]$retained.popup_hwnd, [int]$q.pid)
        if ($script:popupNativeState -eq 'foreign') {
            Stop-Menu 'tableau_menu_popup_ownership_or_type_changed' 'error'
        }
        Check-Time
        # Retained ID also finds a live type-changed/reparented element.
        $condition = [Windows.Automation.PropertyCondition]::new(
            $A::AutomationIdProperty, $retained.menu_automation_id)
    } else {
        # Discovery must observe EACH candidate's popup, not just Window's.
        $condition = [Windows.Automation.PropertyCondition]::new(
            $A::ControlTypeProperty, $T::Menu)
    }
    $script:operation = 'popup_find'
    $elements = $root.FindAll([Windows.Automation.TreeScope]::Descendants, $condition)
    Check-Time
    if ($elements.Count -gt 64) { Stop-Menu 'partial_provider_observation' }
    $found = @()
    foreach ($popup in $elements) {
        $script:operation = 'popup_ancestry'
        Check-Time
        $parent = $walker.GetParent($popup)
        Check-Time
        if ($null -eq $parent -or ($parent.GetRuntimeId() -join ':') -cne $top.id) {
            if ($null -ne $retained) {
                Stop-Menu 'tableau_menu_ancestry_changed' 'error'
            }
            continue
        }
        $script:operation = 'popup_properties'
        $c = $popup.Current
        Check-Time
        if ($popup.Current.ControlType -ne $T::Menu -or
            $popup.Current.ClassName -cne 'TableauMenu' -or
            [string]::IsNullOrWhiteSpace($c.AutomationId) -or
            $popup.Current.ProcessId -ne [int]$q.pid) {
            Stop-Menu 'tableau_menu_popup_ownership_or_type_changed' 'error'
        }
        $rawId = $popup.GetRuntimeId() -join ':'
        Check-Time
        if ($null -ne $retained -and (
            $c.AutomationId -cne $retained.menu_automation_id -or
            [long]$c.NativeWindowHandle -ne [long]$retained.popup_hwnd -or
            $rawId -cne $retained.popup_runtime_id)) {
            Stop-Menu 'tableau_menu_popup_identity_changed' 'error'
        }
        if ($c.NativeWindowHandle -le 0 -or -not $rawId) {
            Stop-Menu 'partial_provider_observation'
        }
        $script:operation = 'popup_native_identity'
        $nativeState = [TableauMenuWindow]::PopupState(
            [long]$popup.Current.NativeWindowHandle, [int]$q.pid)
        if ($nativeState -eq 'foreign') {
            Stop-Menu 'tableau_menu_popup_ownership_or_type_changed' 'error'
        }
        Check-Time
        if ($nativeState -eq 'visible' -and -not $c.IsOffscreen) {
            $found += $popup
        }
    }
    if ($found.Count -gt 1) { Stop-Menu 'tableau_window_menu_ambiguous' 'ambiguous' }
    if ($null -ne $retained) {
        $script:operation = 'popup_native_identity'
        $script:popupNativeState = [TableauMenuWindow]::PopupState(
            [long]$retained.popup_hwnd, [int]$q.pid)
        if ($script:popupNativeState -eq 'foreign') {
            Stop-Menu 'tableau_menu_popup_ownership_or_type_changed' 'error'
        }
        Check-Time
        if ($script:popupNativeState -ne 'visible') { return $null }
    }
    if ($found.Count -eq 0) { return $null }
    $popup = $found[0]
    # Retain an instance only AFTER our qualified top's Expand was requested.
    if ($null -eq $retained -and $null -ne $script:opened) {
        $script:openedPopup = @{
            menu_automation_id=$popup.Current.AutomationId;
            popup_hwnd=[long]$popup.Current.NativeWindowHandle;
            popup_runtime_id=($popup.GetRuntimeId() -join ':')}
        Check-Time
        $script:popupNativeState = [TableauMenuWindow]::PopupState(
            [long]$script:openedPopup.popup_hwnd, [int]$q.pid)
        if ($script:popupNativeState -eq 'foreign') {
            Stop-Menu 'tableau_menu_popup_ownership_or_type_changed' 'error'
        }
        Check-Time
        if ($script:popupNativeState -ne 'visible') { return $null }
    }
    Check-Document $false
    Check-Time
    return $popup
}
function Close-Menu {
    if ($null -eq $script:opened) { return }
    $script:operation = 'menu_close_verify'
    Check-Time
    Check-Document $false
    Check-Time
    $top = $script:opened
    if ($null -eq $script:openedPopup) {
        $null = Find-Popup $top
        if ($null -eq $script:openedPopup) {
            Stop-Menu 'tableau_menu_close_unverified' 'error'
        }
    }
    if ($null -eq $script:closeStartedAt) {
        $script:closeStartedAt = $clock.Elapsed.TotalSeconds
    }
    if (-not $script:collapseRequested) {
        # Requalify our retained instance before the only close action.
        $null = Find-Popup $top
        Check-Time
        $script:operation = 'menu_collapse'
        Check-Time
        $script:collapseRequested = $true
        $top.pattern.Collapse()
        Check-Time
    }
    do {
        $script:operation = 'menu_close_verify'
        Check-Time
        Check-Document $false
        Check-Time
        $script:operation = 'menu_close_verify'
        $state = $top.pattern.Current.ExpandCollapseState
        Check-Time
        $popupPresent = $null -ne (Find-Popup $top)
        $popupPresent = $popupPresent -or $script:popupNativeState -eq 'visible'
        $script:operation = 'menu_close_verify'
        Check-Time
        $now = $clock.Elapsed.TotalSeconds
        $closed = ($state -eq [Windows.Automation.ExpandCollapseState]::Collapsed -and
                   -not $popupPresent)
        if ($closed) {
            if ($null -eq $script:closedSince) { $script:closedSince = $now }
        } else {
            $script:closedSince = $null
        }
        $stableMs = $(if ($null -ne $script:closedSince) {
            ($now-$script:closedSince)*1000
        } else { 0.0 })
        $script:closePollCount++
        $out.last_menu_close_observation = @{
            poll_count=$script:closePollCount; expand_collapse_state=[string]$state;
            qualified_popup_present=$popupPresent;
            top_menu_name=$script:topMenuName;
            menu_automation_id=$script:openedPopup.menu_automation_id;
            popup_hwnd=$script:openedPopup.popup_hwnd;
            popup_runtime_id=$script:openedPopup.popup_runtime_id;
            popup_native_state=$script:popupNativeState;
            stable_closed_ms=[long]$stableMs;
            wait_ms=[long](($clock.Elapsed.TotalSeconds-$script:closeStartedAt)*1000);
            elapsed_ms=[long]$clock.Elapsed.TotalMilliseconds}
        # A first Collapsed sample can precede Qt's actual queued close.
        # Require repeated structural proof across the observed settling window.
        Check-Time
        if ($closed -and $stableMs -ge 250) {
            $script:opened = $null
            $script:openedPopup = $null
            $out.menu_closed = $true
            return
        }
        # Qt can complete Collapse and its UIA queries slowly while rendering.
        # Use only the shared script/page budget, including finally; no local cap.
        Check-Time
        $left = [double]$q.seconds - $clock.Elapsed.TotalSeconds
        $ms = [int][Math]::Min(50, $left * 1000)
        if ($ms -gt 0) { Start-Sleep -Milliseconds $ms }
        Check-Time
    } while ($true)
}
function Open-Menu($top) {
    Check-Time; Check-Document
    $script:topMenuName = $top.name
    $script:operation = 'top_menu_identity'
    $existing = Find-Popup $top
    if ($null -ne $script:opened -or $null -ne $existing -or
        $top.pattern.Current.ExpandCollapseState -ne
            [Windows.Automation.ExpandCollapseState]::Collapsed) {
        $out.menu_closed=$null
        Stop-Menu 'tableau_menu_already_open_or_changed'
    }
    Check-Time
    $script:opened = $top
    $script:openedPopup = $null
    $script:closeStartedAt = $null
    $script:closePollCount = 0
    $script:closedSince = $null
    $script:collapseRequested = $false
    $out.menu_closed = $false
    $script:operation = 'menu_expand'
    $top.pattern.Expand()
    Check-Time
    $polls = 0
    do {
        Check-Time
        $popup = Find-Popup $top
        $script:operation = 'menu_open_verify'
        $state = $top.pattern.Current.ExpandCollapseState
        Check-Time
        $polls++
        $out.last_menu_open_observation = @{
            top_menu_name=$script:topMenuName; top_runtime_id=$top.id;
            expand_collapse_state=[string]$state; poll_count=$polls;
            qualified_popup_visible=($null -ne $popup);
            popup_identity=$script:openedPopup;
            elapsed_ms=[long]$clock.Elapsed.TotalMilliseconds}
        if ($null -ne $popup -and
            $state -eq [Windows.Automation.ExpandCollapseState]::Expanded) {
            Check-Time
            return
        }
        # Wait for this candidate's ACTUAL native-visible popup before Collapse.
        Pause-Menu
    } while ($true)
}
function Read-Sheets($popup, $top) {
    Check-Time; Check-Document
    $script:operation = 'window_menu_identity'
    if ($popup.Current.AutomationId -cne $menuId -or
        $null -eq $script:openedPopup -or
        $script:openedPopup.menu_automation_id -cne $menuId) {
        Stop-Menu 'tableau_window_menu_identity_changed' 'error'
    }
    Check-Time
    $script:operation = 'sheet_children'
    $children = $popup.FindAll([Windows.Automation.TreeScope]::Children,
                              [Windows.Automation.Condition]::TrueCondition)
    if ($children.Count -gt 512) { Stop-Menu 'partial_provider_observation' }
    $lastSeparator = -1
    $script:operation = 'sheet_separator_scan'
    for ($i=0; $i -lt $children.Count; $i++) {
        if ($children[$i].Current.ControlType -eq $T::Separator) { $lastSeparator=$i }
    }
    $count = $children.Count-$lastSeparator-1
    if ($lastSeparator -lt 0 -or $count -lt 1 -or $count -gt 256) {
        Stop-Menu 'tableau_dynamic_sheet_section_unavailable'
    }
    $records = @(); $matches = @(); $checked = @()
    $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    for ($i=$lastSeparator+1; $i -lt $children.Count; $i++) {
        Check-Time
        $script:operation = 'sheet_properties'
        $e = $children[$i]; $c = $e.Current
        $parent = $walker.GetParent($e)
        $invoke = $null; $expand = $null; $toggle = $null; $state = $null
        if ($c.ControlType -ne $T::MenuItem -or $c.ClassName -cne 'QAction' -or
            $c.ProcessId -ne [int]$q.pid -or [string]::IsNullOrWhiteSpace($c.Name) -or
            -not $c.IsEnabled -or $c.IsOffscreen -or $null -eq $parent -or
            ($parent.GetRuntimeId() -join ':') -cne ($popup.GetRuntimeId() -join ':')) {
            Stop-Menu 'tableau_dynamic_sheet_section_incomplete'
        }
        $script:operation = 'sheet_invoke_pattern'
        if (-not $e.TryGetCurrentPattern(
                [Windows.Automation.InvokePattern]::Pattern, [ref]$invoke)) {
            Stop-Menu 'tableau_dynamic_sheet_section_incomplete'
        }
        $script:operation = 'sheet_expand_pattern'
        if ($e.TryGetCurrentPattern($expandPatternType::Pattern, [ref]$expand) -and
            $expand.Current.ExpandCollapseState -ne
                [Windows.Automation.ExpandCollapseState]::LeafNode) {
            Stop-Menu 'tableau_dynamic_sheet_is_submenu'
        }
        if (-not $names.Add($c.Name)) {
            Stop-Menu 'tableau_sheet_name_ambiguous' 'ambiguous'
        }
        $script:operation = 'sheet_toggle_pattern'
        if ($e.TryGetCurrentPattern([Windows.Automation.TogglePattern]::Pattern,
                                    [ref]$toggle)) {
            $state = [string]$toggle.Current.ToggleState
            if ($state -eq 'Indeterminate') { Stop-Menu 'tableau_checked_sheet_unavailable' }
        }
        $script:operation = 'sheet_runtime_id'
        $rawId = $e.GetRuntimeId() -join ':'
        if (-not $rawId) { Stop-Menu 'partial_provider_observation' }
        $records += @{name=$c.Name; selected=($state -eq 'On'); toggle=$state;
            enabled=$true; leaf=$true; control_type='MenuItem';
            provider='tableau_window_menu'; raw_item_runtime_id=$rawId}
        if ($state -eq 'On') { $checked += $c.Name }
        if ($c.Name -ceq [string]$q.name) {
            $matches += @{item=$e; pattern=$invoke; rawId=$rawId}
        }
    }
    $script:operation = 'sheet_snapshot'
    $snapshot=@{
        menu_automation_id=$menuId; popup_hwnd=$popup.Current.NativeWindowHandle;
        popup_native_pid=[int]$q.pid; popup_runtime_id=($popup.GetRuntimeId() -join ':');
        top_runtime_id=$top.id; menu_bar_runtime_id=$barId; ancestry_verified=$true;
        checked_name=$(if ($checked.Count -eq 1) {$checked[0]} else {$null});
        checked_names=$checked; records=$records}
    $out.discovered=$records; $out.last_menu_observation=$snapshot
    $script:operation = 'sheet_unique_check'
    if ($checked.Count -gt 1) { Stop-Menu 'tableau_checked_sheet_ambiguous' 'ambiguous' }
    if ($checked.Count -ne 1) { Stop-Menu 'tableau_checked_sheet_unavailable' }
    if ($matches.Count -ne 1) { Stop-Menu 'tableau_sheet_not_found' }
    return @{target=$matches[0]; snapshot=$snapshot}
}
try {
    Check-Time
    if ([Diagnostics.Process]::GetProcessById([int]$q.pid).ProcessName -ine 'tableau') {
        Stop-Menu 'tableau_process_required'
    }
    $hint = $q.menu_hint
    if ($null -ne $hint) {
        $script:operation = 'menu_hint_request'
        if ($q.mode -cne 'verify' -or $hint.provider -cne 'tableau_window_menu' -or
            $hint.name -cne [string]$q.name -or
            [long]$hint.document_hwnd -ne [long]$q.hwnd -or
            [int]$hint.owner_pid -ne [int]$q.pid -or
            $hint.menu_automation_id -cne $menuId -or
            [string]::IsNullOrWhiteSpace($hint.document_runtime_id) -or
            [string]::IsNullOrWhiteSpace($hint.menu_bar_runtime_id) -or
            [string]::IsNullOrWhiteSpace($hint.top_runtime_id)) {
            Stop-Menu 'invalid_tableau_menu_hint' 'error'
        }
    }
    $script:operation = 'document_ownership'
    if (-not [TableauMenuWindow]::Owned([long]$q.hwnd, [int]$q.pid)) {
        Stop-Menu 'window_ownership_changed' 'error'
    }
    $script:operation = 'document_identity'
    $root = $A::FromHandle([IntPtr][long]$q.hwnd)
    $script:documentId = $root.GetRuntimeId() -join ':'
    if ($null -ne $hint -and
        $script:documentId -cne $hint.document_runtime_id) {
        Stop-Menu 'tableau_menu_hint_changed' 'error'
    }
    Check-Document
    $script:operation = 'menu_bar_find'
    $bars = @($root.FindAll([Windows.Automation.TreeScope]::Descendants,
        [Windows.Automation.PropertyCondition]::new($A::AutomationIdProperty, 'mainMenu')) |
        Where-Object { $_.Current.ControlType -eq $T::MenuBar -and
            $_.Current.ClassName -ceq 'QMenuBar' -and
            $_.Current.ProcessId -eq [int]$q.pid -and -not $_.Current.IsOffscreen })
    Check-Time
    if ($null -ne $hint -and $bars.Count -ne 1) {
        Stop-Menu 'tableau_menu_hint_changed' 'error'
    }
    if ($bars.Count -gt 1) { Stop-Menu 'tableau_menu_bar_ambiguous' 'ambiguous' }
    if ($bars.Count -ne 1) { Stop-Menu 'tableau_menu_bar_unavailable' }
    $script:operation = 'menu_bar_ancestry'
    $bar = $bars[0]; $barId = $bar.GetRuntimeId() -join ':'
    if ($null -ne $hint -and $barId -cne $hint.menu_bar_runtime_id) {
        Stop-Menu 'tableau_menu_hint_changed' 'error'
    }
    $p = $bar; $anchored = $false
    for ($i=0; $i -lt 16 -and $null -ne $p; $i++) {
        if (($p.GetRuntimeId() -join ':') -ceq $script:documentId) {
            $anchored = $true; break
        }
        $p = $walker.GetParent($p)
    }
    if (-not $anchored) { Stop-Menu 'tableau_menu_ancestry_changed' 'error' }
    $script:operation = 'top_menu_children'
    $children = $bar.FindAll([Windows.Automation.TreeScope]::Children,
                            [Windows.Automation.Condition]::TrueCondition)
    if ($children.Count -gt 32) { Stop-Menu 'partial_provider_observation' }
    $tops = @()
    $hintMatches = 0
    foreach ($item in $children) {
        $script:operation = 'top_menu_properties'
        if ($null -ne $hint) {
            Check-Time
            if (($item.GetRuntimeId() -join ':') -cne $hint.top_runtime_id) { continue }
            $hintMatches++
        }
        $pattern = $null
        if ($item.Current.ControlType -ne $T::MenuItem -or
            $item.Current.ClassName -cne 'QAction' -or
            $item.Current.ProcessId -ne [int]$q.pid -or
            [string]::IsNullOrWhiteSpace($item.Current.Name) -or
            -not $item.Current.IsEnabled -or $item.Current.IsOffscreen) { continue }
        $script:operation = 'top_menu_expand_pattern'
        if (-not $item.TryGetCurrentPattern(
                $expandPatternType::Pattern, [ref]$pattern)) { continue }
        $script:operation = 'top_menu_expand_state'
        if ($pattern.Current.ExpandCollapseState -eq
            [Windows.Automation.ExpandCollapseState]::LeafNode) { continue }
        if ($pattern.Current.ExpandCollapseState -ne
            [Windows.Automation.ExpandCollapseState]::Collapsed) {
            $out.menu_closed=$false
            Stop-Menu 'tableau_menu_already_open_or_changed'
        }
        $tops += @{item=$item; pattern=$pattern; id=($item.GetRuntimeId() -join ':');
            name=$item.Current.Name.Substring(0, [Math]::Min(80, $item.Current.Name.Length))}
    }
    $top = $null; $popup = $null
    if ($null -ne $hint) {
        $script:operation = 'menu_hint_top'
        Check-Time
        if ($hintMatches -ne 1 -or $tops.Count -ne 1) {
            Stop-Menu 'tableau_menu_hint_changed' 'error'
        }
        # A hint is never selection evidence: requalify this exact top/bar/root.
        $top = $tops[0]
        $out.menu_hint_used=$true
    } else {
        foreach ($candidate in $tops) {
            Open-Menu $candidate
            $popup = Find-Popup $candidate
            if ($null -ne $popup -and $popup.Current.AutomationId -ceq $menuId) {
                $top=$candidate; break
            }
            Close-Menu
        }
    }
    if ($null -eq $top) { Stop-Menu 'tableau_window_menu_unavailable' }
    # Never use a preexisting/cached popup as the pre-action sheet inventory.
    Close-Menu
    Open-Menu $top
    $popup = Find-Popup $top
    if ($null -eq $popup) { Stop-Menu 'tableau_window_menu_unavailable' }
    $before = Read-Sheets $popup $top
    $out.menu_snapshots += $before.snapshot
    if ($q.mode -eq 'select' -and $before.snapshot.checked_name -cne [string]$q.name) {
        Check-Time; Check-Document
        $popup = Find-Popup $top
        if ($null -eq $popup) { Stop-Menu 'tableau_window_menu_unavailable' }
        $before = Read-Sheets $popup $top
        $out.menu_snapshots[0] = $before.snapshot
        $script:operation = 'sheet_revalidate'
        if ($before.target.item.Current.Name -cne [string]$q.name -or
            ($before.target.item.GetRuntimeId() -join ':') -cne $before.target.rawId) {
            Stop-Menu 'tableau_sheet_identity_changed' 'error'
        }
        $script:operation = 'sheet_invoke'
        if ($before.snapshot.checked_name -cne [string]$q.name) {
            $out.action_attempted=$true
            $before.target.pattern.Invoke()
            $out.invoked=1
        }
    }
    # Collapse is mandatory: an immediate Expand can expose Qt's old checks.
    Close-Menu
    $after = $null
    for ($pass=0; $pass -lt 8; $pass++) {
        Pause-Menu
        Open-Menu $top
        $popup = Find-Popup $top
        if ($null -eq $popup) { Stop-Menu 'tableau_window_menu_unavailable' }
        $current = Read-Sheets $popup $top
        $out.menu_snapshots += $current.snapshot
        Close-Menu
        if ($current.snapshot.checked_name -ceq [string]$q.name) {
            $after=$current; break
        }
        if ($q.mode -eq 'verify') { break }
    }
    if ($null -eq $after) { Stop-Menu 'tableau_sheet_not_checked' }
    Check-Time; Check-Document
    $script:operation = 'result_publish'
    $out.status='verified'; $out.Remove('cause')
    $out.fresh_check=$true
    $out.discovered=$after.snapshot.records
    $out.selected=@{provider='tableau_window_menu'; name=[string]$q.name;
        document_hwnd=[long]$q.hwnd; owner_pid=[int]$q.pid;
        document_runtime_id=$script:documentId; menu_automation_id=$menuId;
        checked_name=$after.snapshot.checked_name; unique_checked=$true;
        menu_closed=$true; raw_item_runtime_id=$after.target.rawId}
    $out.note='Exact dynamic sheet name and unique fresh checked state; menu closed'
} catch {
    $out.status=$script:failureStatus; $out.cause=$script:failure
    $out.note=$script:failure
    $out.failure_details = Failure-Details $_
} finally {
    if ($null -ne $script:opened) {
        $script:guardStop = $false
        try { Close-Menu } catch {
            if ($out.status -ne 'timed_out') { $out.status='error' }
            $out.cleanup_cause='tableau_menu_close_unverified'
            $out.menu_closed=$false
            $out.cleanup_failure_details = Failure-Details $_
        }
    }
}
$out | ConvertTo-Json -Depth 12 -Compress
"""


def _verified_tableau_menu(result: dict, hwnd: int, pid: int, name: str) -> bool:
    """Validate the menu provider without treating QAction IDs as page IDs."""
    selected = result.get("selected")
    if (result.get("status") != "verified"
            or result.get("provider") != "tableau_window_menu"
            or result.get("menu_closed") is not True or result.get("fresh_check") is not True
            or not isinstance(selected, dict)):
        return False
    if (selected.get("provider") != "tableau_window_menu"
            or selected.get("name") != name or selected.get("checked_name") != name
            or selected.get("menu_automation_id") != _TABLEAU_WINDOW_MENU_ID
            or type(selected.get("document_hwnd")) is not int
            or selected["document_hwnd"] != hwnd
            or type(selected.get("owner_pid")) is not int or selected["owner_pid"] != pid
            or selected.get("unique_checked") is not True
            or selected.get("menu_closed") is not True
            or not isinstance(selected.get("document_runtime_id"), str)
            or not selected["document_runtime_id"].strip()
            or not isinstance(selected.get("raw_item_runtime_id"), str)
            or not selected["raw_item_runtime_id"]):
        return False
    records, snapshots = result.get("discovered"), result.get("menu_snapshots")
    if (not isinstance(records, list) or not 1 <= len(records) <= 256
            or not isinstance(snapshots, list) or not 2 <= len(snapshots) <= 9):
        return False
    for snapshot in snapshots:
        if (not isinstance(snapshot, dict) or snapshot.get("ancestry_verified") is not True
                or snapshot.get("menu_automation_id") != _TABLEAU_WINDOW_MENU_ID
                or type(snapshot.get("popup_native_pid")) is not int
                or snapshot["popup_native_pid"] != pid
                or type(snapshot.get("popup_hwnd")) is not int or snapshot["popup_hwnd"] <= 0
                or any(not isinstance(snapshot.get(key), str) or not snapshot[key]
                       for key in ("popup_runtime_id", "top_runtime_id", "menu_bar_runtime_id"))):
            return False
    names = set()
    checked = []
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get("name"), str)
                or not record["name"].strip() or record["name"] in names
                or record.get("provider") != "tableau_window_menu"
                or record.get("control_type") != "MenuItem"
                or record.get("enabled") is not True or record.get("leaf") is not True
                or type(record.get("selected")) is not bool
                or record.get("toggle") not in (None, "On", "Off")
                or record["selected"] != (record.get("toggle") == "On")
                or not isinstance(record.get("raw_item_runtime_id"), str)
                or not record["raw_item_runtime_id"]):
            return False
        names.add(record["name"])
        if record["selected"]:
            if record["raw_item_runtime_id"] != selected["raw_item_runtime_id"]:
                return False
            checked.append(record["name"])
    snapshot = snapshots[-1]
    return (
        checked == [name] and snapshot.get("checked_name") == name
        and snapshot.get("records") == records)


def _tableau_menu_hint_from_observation(observation: dict, hwnd: int, pid: int,
                                       name: str) -> dict | None:
    """Capture-local identities only; popup and QAction IDs must be rebuilt."""
    if not _verified_tableau_menu(observation, hwnd, pid, name):
        return None
    selected = observation["selected"]
    snapshot = observation["menu_snapshots"][-1]
    return {
        "provider": "tableau_window_menu", "name": name,
        "document_hwnd": hwnd, "owner_pid": pid,
        "document_runtime_id": selected["document_runtime_id"],
        "menu_automation_id": _TABLEAU_WINDOW_MENU_ID,
        "menu_bar_runtime_id": snapshot["menu_bar_runtime_id"],
        "top_runtime_id": snapshot["top_runtime_id"],
    }


def _valid_tableau_menu_hint(hint: dict, hwnd: int, pid: int, name: str) -> bool:
    return (
        isinstance(hint, dict) and hint.get("provider") == "tableau_window_menu"
        and hint.get("name") == name
        and hint.get("menu_automation_id") == _TABLEAU_WINDOW_MENU_ID
        and type(hint.get("document_hwnd")) is int and hint["document_hwnd"] == hwnd
        and type(hint.get("owner_pid")) is int and hint["owner_pid"] == pid
        and all(isinstance(hint.get(key), str) and hint[key].strip()
                for key in ("document_runtime_id", "menu_bar_runtime_id", "top_runtime_id")))


def select_tableau_window_menu(hwnd: int, pid: int, name: str, *, timeout: float = 30,
                               verify_only: bool = False,
                               _tableau_menu_hint: dict | None = None) -> dict:
    """Bounded named-sheet fallback; only our qualified menus may be collapsed.

    Verification expands/collapses menus to refresh Qt's checked state, but
    never invokes a sheet. No fallback to static commands or unnamed ListItems.
    A private capture-local hint skips discovery, never native qualification.
    """
    provider = "tableau_window_menu"
    try:
        timeout = float(timeout)
        if (not math.isfinite(timeout) or timeout <= 0 or type(hwnd) is not int
                or hwnd <= 0 or type(pid) is not int or pid <= 0
                or not isinstance(name, str) or not name):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return {"status": "error", "cause": "invalid_tableau_menu_request",
                "provider": provider, "retryable": False}
    if _tableau_menu_hint is not None and (
            not verify_only or not _valid_tableau_menu_hint(_tableau_menu_hint, hwnd, pid, name)):
        return {"status": "error", "cause": "invalid_tableau_menu_hint",
                "provider": provider, "retryable": False}
    deadline = time.monotonic() + timeout
    tooling = powershell_preflight()
    if tooling["status"] != "ready":
        return {**tooling, "provider": provider}
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"status": "timed_out", "cause": "tableau_window_menu_timeout",
                "provider": provider, "retryable": False}
    ready = desktop_window.wait_for_document_ready(hwnd, pid, timeout=remaining)
    if ready.get("status") != "ready":
        return {**ready, "provider": provider}
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"status": "timed_out", "cause": "tableau_window_menu_timeout",
                "provider": provider, "retryable": False}
    # Leave part of the one budget for finally/Collapse and child shutdown.
    query: dict = {
        "hwnd": hwnd, "pid": pid, "name": name,
        "mode": "verify" if verify_only else "select",
        "seconds": remaining - min(2.0, remaining / 3),
    }
    if _tableau_menu_hint is not None:
        query["menu_hint"] = dict(_tableau_menu_hint)
    result = run_accessibility_script(_TABLEAU_WINDOW_MENU_SCRIPT, query, remaining)
    if is_powershell_failure(result):
        return {**result, "provider": provider, "menu_closed": None}
    if desktop_window.window_process_id(hwnd) != pid:
        return {"status": "error", "cause": "window_ownership_changed",
                "provider": provider, "retryable": False, "menu_observation": result}
    if (not isinstance(result, dict) or result.get("provider") != provider
            or result.get("status") not in {
                "verified", "unsupported", "ambiguous", "timed_out", "inaccessible", "error"}
            or (result["status"] == "verified"
                and not _verified_tableau_menu(result, hwnd, pid, name))):
        return {"status": "error", "cause": "tableau_menu_result_invalid",
                "provider": provider, "retryable": False}
    if (result["status"] == "verified" and _tableau_menu_hint is not None
            and (result.get("menu_hint_used") is not True
                 or _tableau_menu_hint_from_observation(result, hwnd, pid, name)
                 != _tableau_menu_hint
                 or any(snapshot["menu_bar_runtime_id"]
                        != _tableau_menu_hint["menu_bar_runtime_id"]
                        or snapshot["top_runtime_id"] != _tableau_menu_hint["top_runtime_id"]
                        for snapshot in result["menu_snapshots"]))):
        return {"status": "error", "cause": "tableau_menu_hint_changed",
                "provider": provider, "retryable": False, "menu_observation": result}
    if result["status"] == "verified" and desktop_window.dialog_windows(pid):
        return {"status": "inaccessible", "cause": "owned_dialog",
                "provider": provider, "retryable": False, "menu_observation": result}
    if time.monotonic() >= deadline:
        return {"status": "timed_out", "cause": "tableau_window_menu_timeout",
                "provider": provider, "retryable": False, "menu_observation": result}
    result["readiness"] = ready
    return result


def select_document_tab(hwnd: int, pid: int, name: str, *, timeout: float = 30,
                        verify_only: bool = False,
                        _tableau_menu_hint: dict | None = None) -> dict:
    """Select one exact-name page and return provider-specific identity evidence.

    A caller must obtain hwnd/pid from its own launched document. No fallback
    to other windows, coordinates or keyboard input. Safe to call in headless CI.
    Supported providers are named tabs and the qualified Tableau Window menu.
    """
    if not desktop_window.IS_WINDOWS:
        return {"status": "unavailable", "note": "UI Automation requires Windows"}
    try:
        timeout = float(timeout)
        valid = math.isfinite(timeout) and timeout > 0
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not hwnd or not pid or not isinstance(name, str) or not name or not valid:
        return {"status": "error", "note": "Invalid tab selection arguments"}
    if _tableau_menu_hint is not None:
        # Only capture of a previously verified menu page takes this route.
        # An invalid/reassigned hint is terminal, never generic rediscovery.
        return select_tableau_window_menu(
            hwnd, pid, name, timeout=timeout, verify_only=verify_only,
            _tableau_menu_hint=_tableau_menu_hint)
    tooling = powershell_preflight()
    if tooling["status"] != "ready":
        return tooling
    if desktop_window.window_process_id(hwnd) != pid:
        return {"status": "error", "note": "Window is not owned by the launched PID"}
    deadline = time.monotonic() + timeout
    try:
        ready = desktop_window.wait_for_document_ready(hwnd, pid, timeout=timeout)
        if ready["status"] != "ready":
            return ready
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "timed_out", "note": "No tab time left after native progress"}
        query = {"hwnd": hwnd, "pid": pid, "name": name,
                 "seconds": remaining, "mode": "verify" if verify_only else "select"}
        result = run_accessibility_script(_SCRIPT, query, remaining)
        if is_powershell_failure(result) or result.get("status") == "error":
            return result
        if not isinstance(result, dict) or result.get("status") not in {
                "verified", "inaccessible", "ambiguous", "timed_out"}:
            raise ValueError("Invalid UI Automation response")
        # Standard Win32 controls may have only generic UIA Pane proxies while
        # MSAA still exposes real PAGETAB roles. Do not bypass UIA ambiguity,
        # Power BI's host qualification, or Tableau's proven unnamed-list block.
        if (result["status"] == "inaccessible" and not result.get("native_sheet_items")
                and not result.get("action_attempted")
                and result.get("cause") != "partial_provider_observation"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"status": "timed_out", "note": "No native tab observation time left"}
            native = select_native_tab(
                hwnd, pid, name, timeout=remaining, verify_only=verify_only)
            if (is_powershell_failure(native)
                    or native.get("status") in {"error", "ownership_changed"}):
                return {**native, "uia_observation": result}
            if native.get("status") in {"verified", "ambiguous", "timed_out"}:
                native["uia_observation"] = result
                result = native
            else:
                result["native_observation"] = native
        if (result["status"] == "inaccessible"
                and str(result.get("application", "")).casefold() == "tableau"
                and not result.get("action_attempted")
                and result.get("cause") != "partial_provider_observation"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"status": "timed_out", "note": "No named menu navigation time left"}
            menu = select_tableau_window_menu(
                hwnd, pid, name, timeout=remaining, verify_only=verify_only)
            result = {**menu, "uia_observation": result}
        if desktop_window.window_process_id(hwnd) != pid:
            raise ValueError("Document window ownership changed during tab observation")
        if result["status"] == "verified":
            selected = result.get("selected")
            if result.get("provider") == "tableau_window_menu":
                if not _verified_tableau_menu(result, hwnd, pid, name):
                    raise ValueError("Named menu response did not verify the exact active sheet")
            elif (not isinstance(selected, dict) or selected.get("name") != name
                  or not selected.get("runtime_id")):
                raise ValueError("Native response did not identify the exact selected tab")
        result["readiness"] = ready
        return result
    except subprocess.TimeoutExpired:
        return {"status": "timed_out", "note": "UI Automation child exceeded deadline"}
    except Exception as exc:
        return {"status": "error", "note": f"{type(exc).__name__}: {exc}"}


def capture_document_tab(hwnd: int, pid: int, name: str, screenshot_path: str,
                         *, timeout: float = 30) -> dict:
    """Retry transient capture failures under ONE monotonic page deadline.

    Every attempt has a fresh private destination. Only a complete, nonuniform
    image bracketed by the same exact page identity and owned HWND/PID can be
    published. This is capture evidence, not proof of empty reports, loaded
    data or visual fidelity.
    """
    started = time.monotonic()
    result = {"status": "error", "screenshot": None, "screenshot_blank": None,
              "note": "", "attempt_count": 0, "capture_attempts": [], "capture_errors": []}
    output_created = False
    shot: Path | None = None
    phase = "arguments"

    def fail(observation, *, cause=None):
        result.update(status=observation.get("status", "error"),
                      note=observation.get("note", observation.get("cause", "")),
                      failure_phase=phase)
        for key in ("cause", "retryable"):
            if key in observation:
                result[key] = observation[key]
        if cause:
            result.update(status="error", cause=cause, note=cause, retryable=False)
        if "cause" not in result:
            result.update(cause="selection_not_verified", retryable=False)
        return result

    def same_tab(before, after):
        a, b = before.get("selected"), after.get("selected")
        if (before.get("provider") == "tableau_window_menu"
                or after.get("provider") == "tableau_window_menu"):
            return (
                _verified_tableau_menu(before, hwnd, pid, name)
                and _verified_tableau_menu(after, hwnd, pid, name)
                and _tableau_menu_hint_from_observation(before, hwnd, pid, name)
                == _tableau_menu_hint_from_observation(after, hwnd, pid, name)
                and all(a.get(key) == b.get(key) for key in (
                    "provider", "name", "document_hwnd", "owner_pid",
                    "document_runtime_id", "menu_automation_id", "checked_name",
                    "unique_checked", "menu_closed")))
        return (
            before.get("status") == after.get("status") == "verified"
            and isinstance(a, dict) and isinstance(b, dict)
            and a.get("name") == b.get("name") == name
            and bool(a.get("runtime_id"))
            and all(a.get(key) == b.get(key)
                    for key in ("runtime_id", "automation_id", "provider")))

    try:
        shot = Path(screenshot_path).absolute()
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Page timeout must be positive and finite")
        deadline = started + timeout

        def remaining():
            return max(0.0, deadline - time.monotonic())

        def expired():
            result.update(status="timed_out", cause="capture_timeout",
                          note="Page capture deadline exhausted", retryable=False,
                          failure_phase=phase)
            return result

        if shot.suffix.lower() != ".png":
            raise ValueError("Screenshot destination must have .png extension")
        if shot.exists() or shot.is_symlink():
            raise ValueError("Screenshot destination already exists")
        if any(p.is_symlink() or p.is_junction() for p in shot.parents):
            raise ValueError("Screenshot path contains a link or junction")
        if remaining() <= 0:
            return expired()
        phase = "selection"
        evidence = select_document_tab(hwnd, pid, name, timeout=remaining())
        result["selection"] = evidence
        if evidence["status"] != "verified":
            return fail(evidence)
        if not same_tab(evidence, evidence):
            return fail(evidence, cause="selection_evidence_invalid")
        # No global cache or cross-page reuse. Runtime hints only reduce actions;
        # every native verify requalifies root/bar/top and the fresh Window popup.
        menu_hint = _tableau_menu_hint_from_observation(evidence, hwnd, pid, name)
        verify_options = {"_tableau_menu_hint": menu_hint} if menu_hint is not None else {}
        # Allow paint to settle without interpreting responsiveness as loaded data.
        time.sleep(min(0.5, remaining()))
        shot.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".desktop-capture-", dir=shot.parent) as temp:
            while remaining() > 0:
                phase = "pre_capture_selection"
                if desktop_window.window_process_id(hwnd) != pid:
                    return fail({}, cause="window_ownership_changed")
                pre = select_document_tab(
                    hwnd, pid, name, timeout=remaining(), verify_only=True, **verify_options)
                # Preserve a failed PRE query too; initial selection stays intact.
                result["pre_capture_selection"] = pre
                if pre.get("status") != "verified":
                    return fail(pre)
                if not same_tab(evidence, pre):
                    return fail(pre, cause="tab_selection_changed")
                if remaining() <= 0:
                    return expired()
                index = result["attempt_count"] + 1
                candidate = Path(temp) / f"attempt-{index}.png"
                attempt = {"attempt": index, "pre_capture_selection": pre}
                result["capture_attempts"].append(attempt)
                result["attempt_count"] = index
                phase = "capture"
                capture = _bounded_capture(hwnd, pid, candidate, remaining())
                attempt["capture"] = capture
                attempt["elapsed_s"] = round(time.monotonic() - started, 3)
                result["screenshot_blank"] = capture.get("screenshot_blank")
                if capture.get("status") != "captured":
                    result["capture_errors"].append({"attempt": index, **capture})
                if remaining() <= 0:
                    return expired()
                if desktop_window.window_process_id(hwnd) != pid:
                    return fail({}, cause="window_ownership_changed")
                # Tooling errors/dialogs are terminal; never mask them with a
                # successful tab query, nor retry a changed window or selection.
                transient = capture.get("retryable") is True and capture.get("cause") in {
                    "capture_blank", "capture_failed", "capture_invalid"}
                if capture.get("status") != "captured" and not transient:
                    return fail(capture)
                phase = "post_capture_selection"
                post = select_document_tab(
                    hwnd, pid, name, timeout=remaining(), verify_only=True, **verify_options)
                attempt["post_capture_selection"] = post
                result["post_capture_selection"] = post
                if post.get("status") != "verified":
                    return fail(post)
                if not same_tab(pre, post):
                    return fail(post, cause="tab_selection_changed")
                if remaining() <= 0:
                    return expired()
                if capture.get("status") == "captured":
                    phase = "capture_validation"
                    image = (desktop_window.inspect_capture_png(str(candidate))
                             if candidate.is_file() and not candidate.is_symlink() else None)
                    if not image or image["blank"]:
                        capture = {"status": "error", "retryable": True,
                                   "cause": "capture_blank" if image else "capture_invalid",
                                   "screenshot_blank": image["blank"] if image else None}
                        attempt["capture"] = capture
                        result["screenshot_blank"] = capture["screenshot_blank"]
                        result["capture_errors"].append({"attempt": index, **capture})
                    elif capture.get("image", image) != image:
                        return fail({}, cause="capture_dimensions_changed")
                    else:
                        if desktop_window.window_process_id(hwnd) != pid:
                            return fail({}, cause="window_ownership_changed")
                        if remaining() <= 0:
                            return expired()
                        phase = "capture_publish"
                        # Exclusive creation never overwrites stale evidence;
                        # only a complete validated image is exposed as accepted.
                        # No hard-link/filesystem feature is required.
                        image_bytes = candidate.read_bytes()
                        if remaining() <= 0:
                            return expired()
                        with shot.open("xb") as handle:
                            output_created = True
                            handle.write(image_bytes)
                        if remaining() <= 0:
                            return expired()
                        result.update(
                            status="captured", screenshot=str(shot), screenshot_blank=False,
                            note="Verified exact page identity; complete nonblank full-window capture, manual review only")
                        return result
                candidate.unlink(missing_ok=True)
                time.sleep(min(0.25, remaining()))
        return expired()
    except Exception as exc:
        result.update(status="error", cause="capture_operation_failed", retryable=False,
                      failure_phase=phase,
                      note=(str(exc) if isinstance(exc, ValueError)
                            else f"{type(exc).__name__}: capture operation failed"))
        return result
    finally:
        if output_created and shot is not None and result["status"] != "captured":
            try:
                shot.unlink(missing_ok=True)
            except OSError:
                result["cleanup_error"] = "rejected_capture_cleanup_failed"
            result["screenshot"] = None
        result["duration_s"] = round(time.monotonic() - started, 3)
