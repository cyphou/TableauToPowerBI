"""Focused synthetic tab identity/fallback regressions; no native app launches."""

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from powerbi_import import desktop_accessibility as accessibility
from powerbi_import import desktop_navigation as navigation
from powerbi_import import desktop_window
from tests.desktop_dialog_fixtures import (
    CAPTION_TRANSITION_TITLE,
    LAYOUT_TEXTS,
    inert_dialog_observer_script,
    inert_tableau_menu_lifecycle_script,
    layout_caption_transition_case,
    layout_dialog_case,
    strict_dialog_metadata,
)
from tests.pixel_comparison_fixtures import tiny_png

NATIVE_PNG = tiny_png(((10, 20, 30, 40, 50, 60),), channels=3)


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("Negative sleep")
        self.sleeps.append(seconds)
        self.now += seconds


def _verified(name="Synthetic Page", runtime_id="11:msaa:root:0:1"):
    return {
        "status": "verified", "provider": "msaa",
        "discovery_phase": "after_selection",
        "selected": {"name": name, "runtime_id": runtime_id,
                     "automation_id": "", "provider": "msaa"},
        "discovered": [{
            "name": name, "selected": True, "offscreen": False,
            "runtime_id": runtime_id, "control_type": "PAGETAB",
            "provider": "msaa",
        }],
    }


def _menu_verified(name="Synthetic Page", *, build: int | str = 1, hwnd=11, pid=42):
    """Rebuilt QAction IDs are raw observations, never a stable sheet ID."""
    names = [name, "Embedded View", "Hidden View", "Tooltip View",
             "Detail View", "Second Detail"]

    def snapshot(generation):
        records = [{
            "name": label, "selected": label == name,
            "toggle": "On" if label == name else None,
            "enabled": True, "leaf": True, "control_type": "MenuItem",
            "provider": "tableau_window_menu",
            "raw_item_runtime_id": f"menu:build-{generation}:item-{index}",
        } for index, label in enumerate(names)]
        return {
            "menu_automation_id": "mainMenu.menu_Window", "popup_hwnd": 31,
            "popup_native_pid": pid, "popup_runtime_id": f"popup:{generation}",
            "top_runtime_id": "top:window", "menu_bar_runtime_id": "bar:main",
            "ancestry_verified": True, "checked_name": name,
            "checked_names": [name], "records": records,
        }

    before, after = snapshot(f"{build}-before"), snapshot(f"{build}-after")
    return {
        "status": "verified", "provider": "tableau_window_menu",
        "menu_closed": True, "fresh_check": True,
        "action_attempted": False, "invoked": 0,
        "selected": {
            "provider": "tableau_window_menu", "name": name,
            "document_hwnd": hwnd, "owner_pid": pid,
            "document_runtime_id": "document:synthetic:11",
            "menu_automation_id": "mainMenu.menu_Window",
            "checked_name": name, "unique_checked": True, "menu_closed": True,
            "raw_item_runtime_id": after["records"][0]["raw_item_runtime_id"],
        },
        "discovered": copy.deepcopy(after["records"]),
        "menu_snapshots": [before, after],
    }


def _menu_target(
        observation: object, path: tuple[str | int, ...]) -> dict[str, object]:
    """Resolve only actual object/list segments before mutating test evidence."""
    target: object = observation
    for part in path:
        if isinstance(part, str):
            if not isinstance(target, dict):
                raise AssertionError("Synthetic menu path must index an object")
            target = target[part]
        else:
            if not isinstance(target, list):
                raise AssertionError("Synthetic menu path must index a list")
            target = target[part]
    if not isinstance(target, dict):
        raise AssertionError("Synthetic menu field target must be an object")
    return target


def _menu_hint(name="Synthetic Page"):
    """Only document/bar/top identities survive a fresh menu rebuild."""
    return {
        "provider": "tableau_window_menu", "name": name,
        "document_hwnd": 11, "owner_pid": 42,
        "document_runtime_id": "document:synthetic:11",
        "menu_automation_id": "mainMenu.menu_Window",
        "menu_bar_runtime_id": "bar:main", "top_runtime_id": "top:window",
    }


def _document_guard(*, reason="document_disabled", enabled=False, visible=True, dialogs=()):
    return {
        "basis": "win32", "reason": reason, "elapsed_ms": 125,
        "document_hwnd": 11, "expected_pid": 42,
        "document_pid_before": 42, "document_pid_after": 42,
        "document_owned_before": True, "document_owned_after": True,
        "document_enabled": enabled, "document_enabled_after": enabled,
        "document_visible": visible, "document_visible_after": visible,
        "dialog_check_performed": True, "owned_dialog_present": bool(dialogs),
        "dialogs_complete": True, "dialogs_truncated": False,
        "owned_dialogs": copy.deepcopy(list(dialogs)),
    }


def _owned_guard_dialog():
    return {
        "hwnd": 31, "owner_hwnd": 11, "pid_before": 42, "pid_after": 42,
        "owned_before": True, "owned_after": True,
        "visible_before": True, "visible_after": True,
        "class_name": "SyntheticModal", "title": 'Synthetic Recovery "Étude"',
        "metadata_complete": True, "title_truncated": False,
    }


class TestTableauWindowMenuScript(unittest.TestCase):
    """Inspect production guards without loading UIA or opening an application."""

    def test_exact_document_menu_requires_both_uia_ancestry_and_native_pid(self):
        source = navigation._TABLEAU_WINDOW_MENU_SCRIPT
        popup = source.split("function Find-Popup($top) {", 1)[1].split(
            "function Close-Menu {", 1)[0]
        for guard in (
                "$popup.Current.ControlType -ne $T::Menu",
                "$popup.Current.ClassName -cne 'TableauMenu'",
                "$c.AutomationId -cne $retained.menu_automation_id",
                "$popup.Current.ProcessId -ne [int]$q.pid",
                "[long]$popup.Current.NativeWindowHandle, [int]$q.pid",
                "($parent.GetRuntimeId() -join ':') -cne $top.id",
                "($barParent.GetRuntimeId() -join ':') -cne $barId",
                "$barParent.Current.AutomationId -cne 'mainMenu'",
                "$barParent.Current.ClassName -cne 'QMenuBar'"):
            self.assertIn(guard, popup)
        # GW_OWNER may be zero: it cannot substitute for the UIA parent chain.
        self.assertNotIn("GetWindow(", popup)
        self.assertIn("ProcessName -ine 'tableau'", source)
        self.assertIn("($current.GetRuntimeId() -join ':') -cne $script:documentId",
                      source)
        self.assertIn("if (-not $anchored)", source)
        self.assertIn("Stop-Menu 'tableau_menu_ancestry_changed' 'error'", source)
        # Generic candidate popups must close safely during discovery, but only
        # the qualified Window menu can expose invokable sheet commands.
        self.assertIn("$A::ControlTypeProperty, $T::Menu", popup)
        self.assertIn("$A::AutomationIdProperty, $retained.menu_automation_id", popup)
        read = source.split("function Read-Sheets($popup, $top) {", 1)[1].split(
            "\ntry {", 1)[0]
        self.assertIn("$popup.Current.AutomationId -cne $menuId", read)
        self.assertIn("$script:openedPopup.menu_automation_id -cne $menuId", read)
        self.assertIn("Stop-Menu 'tableau_window_menu_identity_changed' 'error'", read)

    def test_static_command_name_collision_cannot_escape_the_last_separator_tail(self):
        source = navigation._TABLEAU_WINDOW_MENU_SCRIPT
        read = source.split("function Read-Sheets($popup, $top) {", 1)[1].split(
            "\ntry {", 1)[0]
        self.assertIn("$lastSeparator = -1", read)
        self.assertRegex(
            read, r"for \(\$i=0; \$i -lt \$children.Count; \$i\+\+\) \{\s*"
            r"if \(\$children\[\$i\].Current.ControlType -eq \$T::Separator\) "
            r"\{ \$lastSeparator=\$i \}\s*\}")
        self.assertIn(
            "for ($i=$lastSeparator+1; $i -lt $children.Count; $i++)", read)
        self.assertIn("$c.Name -ceq [string]$q.name", read)
        self.assertEqual(read.count("$matches +="), 1)
        self.assertIn(
            "if ($matches.Count -ne 1) { Stop-Menu 'tableau_sheet_not_found' }", read)
        self.assertEqual(source.count(".Invoke()"), 1)
        self.assertIn("$before.target.pattern.Invoke()", source)
        for unsafe in ("SendKeys", "SetCursorPos", "mouse_event", ".Toggle()",
                       ".Select()", ".SetFocus()"):
            self.assertNotIn(unsafe, source)

    def test_missing_duplicate_incomplete_and_truncated_sections_fail_closed(self):
        source = navigation._TABLEAU_WINDOW_MENU_SCRIPT
        for guard in (
                "$children.Count -gt 512", "$children.Count -gt 32",
                "$lastSeparator -lt 0 -or $count -lt 1 -or $count -gt 256",
                "$c.ControlType -ne $T::MenuItem", "$c.ClassName -cne 'QAction'",
                "$c.ProcessId -ne [int]$q.pid", "-not $c.IsEnabled -or $c.IsOffscreen",
                "[Windows.Automation.InvokePattern]::Pattern",
                "[Windows.Automation.ExpandCollapseState]::LeafNode",
                "[StringComparer]::Ordinal", "if (-not $names.Add($c.Name))",
                "if ($state -eq 'Indeterminate')",
                "if ($checked.Count -gt 1)", "if ($checked.Count -ne 1)"):
            self.assertIn(guard, source)
        self.assertIn("Stop-Menu 'partial_provider_observation'", source)
        self.assertIn("Stop-Menu 'tableau_sheet_name_ambiguous' 'ambiguous'", source)
        self.assertIn("Stop-Menu 'tableau_checked_sheet_ambiguous' 'ambiguous'", source)

    def test_fresh_reopen_and_verified_collapse_are_required_not_cached_checks(self):
        source = navigation._TABLEAU_WINDOW_MENU_SCRIPT
        self.assertRegex(
            source, r"Close-Menu\s+Open-Menu \$top\s+"
            r"\$popup = Find-Popup \$top\s+"
            r"if \(\$null -eq \$popup\) \{[^\n]+\}\s+"
            r"\$before = Read-Sheets \$popup \$top")
        self.assertRegex(
            source, r"\$current = Read-Sheets \$popup \$top\s+"
            r"\$out.menu_snapshots \+= \$current.snapshot\s+Close-Menu\s+"
            r"if \(\$current.snapshot.checked_name -ceq \[string\]\$q.name\)")
        close = source.split("function Close-Menu {", 1)[1].split(
            "function Open-Menu($top) {", 1)[0]
        self.assertIn("$top.pattern.Collapse()", close)
        self.assertIn("[Windows.Automation.ExpandCollapseState]::Collapsed", close)
        self.assertRegex(close, r"\$popupPresent = \$null -ne \(Find-Popup \$top\)")
        self.assertRegex(
            close, r"\$closed = \(\$state -eq "
            r"\[Windows.Automation.ExpandCollapseState\]::Collapsed -and\s+"
            r"-not \$popupPresent\)")
        self.assertIn("if ($closed -and $stableMs -ge 250", close)
        self.assertIn("Stop-Menu 'tableau_menu_close_unverified' 'error'", close)
        self.assertIn("Stop-Menu 'tableau_menu_already_open_or_changed'", source)
        self.assertIn("$out.menu_closed=$false", source)
        self.assertIn("$out.fresh_check=$true", source)

    def test_verify_and_fresh_already_active_target_never_invoke_a_sheet(self):
        source = navigation._TABLEAU_WINDOW_MENU_SCRIPT
        action = source.split(
            "if ($q.mode -eq 'select' -and "
            "$before.snapshot.checked_name -cne [string]$q.name) {", 1)[1].split(
                "# Collapse is mandatory:", 1)[0]
        self.assertRegex(
            action, r"if \(\$before.snapshot.checked_name -cne \[string\]\$q.name\) "
            r"\{\s*\$out.action_attempted=\$true\s+"
            r"\$before.target.pattern.Invoke\(\)")
        self.assertIn(
            "($before.target.item.GetRuntimeId() -join ':') -cne $before.target.rawId",
            action)
        self.assertIn("if ($q.mode -eq 'verify') { break }", source)
        self.assertIn("checked_name=$after.snapshot.checked_name", source)

    def test_hint_requalifies_fresh_root_bar_top_and_popup_without_discovery_fallback(self):
        source = navigation._TABLEAU_WINDOW_MENU_SCRIPT
        request = source.split("    $hint = $q.menu_hint", 1)[1].split(
            "    # Never use a preexisting/cached popup", 1)[0]
        for guard in (
                "$q.mode -cne 'verify'", "$hint.provider -cne 'tableau_window_menu'",
                "$hint.name -cne [string]$q.name",
                "[long]$hint.document_hwnd -ne [long]$q.hwnd",
                "[int]$hint.owner_pid -ne [int]$q.pid",
                "$hint.menu_automation_id -cne $menuId",
                "Stop-Menu 'invalid_tableau_menu_hint' 'error'",
                "$root = $A::FromHandle([IntPtr][long]$q.hwnd)",
                "$script:documentId -cne $hint.document_runtime_id",
                "$null -ne $hint -and $bars.Count -ne 1",
                "$barId -cne $hint.menu_bar_runtime_id",
                "if (-not $anchored)",
                "($item.GetRuntimeId() -join ':') -cne $hint.top_runtime_id",
                "$hintMatches -ne 1 -or $tops.Count -ne 1",
                "Stop-Menu 'tableau_menu_hint_changed' 'error'"):
            self.assertIn(guard, request)
        for key in ("document_runtime_id", "menu_bar_runtime_id", "top_runtime_id"):
            self.assertIn(f"[string]::IsNullOrWhiteSpace($hint.{key})", request)
        for guard in (
                "$_.Current.ControlType -eq $T::MenuBar",
                "$_.Current.ClassName -ceq 'QMenuBar'",
                "$_.Current.ProcessId -eq [int]$q.pid",
                "$item.Current.ControlType -ne $T::MenuItem",
                "$item.Current.ClassName -cne 'QAction'",
                "$item.Current.ProcessId -ne [int]$q.pid",
                "-not $item.Current.IsEnabled -or $item.Current.IsOffscreen"):
            self.assertIn(guard, request)
        hinted_top, discovery = request.split(
            "$script:operation = 'menu_hint_top'", 1)[1].split("    } else {", 1)
        self.assertIn("$top = $tops[0]", hinted_top)
        self.assertIn("$out.menu_hint_used=$true", hinted_top)
        self.assertNotIn("Open-Menu", hinted_top)
        self.assertIn("foreach ($candidate in $tops)", discovery)
        self.assertIn("Open-Menu $candidate", discovery)
        # Hints skip only candidate discovery, never fresh native qualification,
        # protected-dialog checks, Window-popup identity or sheet checked state.
        self.assertIn("Check-Time; Check-Document", source)
        self.assertIn("Stop-Menu 'owned_dialog_or_disabled_document' 'inaccessible'", source)
        self.assertIn("$script:openedPopup = $null", source.split(
            "function Open-Menu($top) {", 1)[1].split("function Read-Sheets", 1)[0])
        read = source.split("function Read-Sheets($popup, $top) {", 1)[1].split(
            "\ntry {", 1)[0]
        self.assertIn("$popup.Current.AutomationId -cne $menuId", read)
        self.assertNotIn("$hint", read)


@unittest.skipUnless(desktop_window.IS_WINDOWS, "Isolated Windows PowerShell functions")
class TestTableauMenuAsyncClosePowerShell(unittest.TestCase):
    """Run unchanged production functions, with no UIA/Win32 implementations.

    All Windows.Automation types here are inert test types. Only a short-lived
    PowerShell child runs; no applications, windows, COM providers or P/Invoke.
    """

    @classmethod
    def setUpClass(cls):
        if accessibility.powershell_preflight()["status"] != "ready":
            raise unittest.SkipTest("Windows PowerShell not installed")
        source = navigation._TABLEAU_WINDOW_MENU_SCRIPT
        functions = []
        for declaration, next_declaration in (
                ("function Exception-Type($exception) {", "function Failure-Details("),
                ("function Failure-Details($record) {", "function Stop-Menu("),
                ("function Stop-Menu($cause, $status='unsupported') {", "function Check-Time {"),
                ("function Check-Time {", "function Pause-Menu {"),
                ("function Find-Popup($top) {", "function Close-Menu {"),
                ("function Close-Menu {", "function Open-Menu($top) {")):
            functions.append(declaration + source.split(declaration, 1)[1].split(
                next_declaration, 1)[0])
        handler_start = "} catch {\n    $out.status=$script:failureStatus;"
        handlers = handler_start + source.split(handler_start, 1)[1].split(
            "$out | ConvertTo-Json", 1)[0]
        script = r"""
Add-Type -TypeDefinition @'
namespace Windows.Automation {
    public enum ExpandCollapseState { Collapsed, Expanded }
    public enum TreeScope { Descendants }
    public class PropertyCondition {
        public PropertyCondition(object property, object value) { }
    }
}
public static class SyntheticProperties {
    public static string AutomationIdProperty = "AutomationId";
    public static string ControlTypeProperty = "ControlType";
}
public static class SyntheticControls {
    public static string Menu = "Menu";
    public static string MenuItem = "MenuItem";
    public static string MenuBar = "MenuBar";
}
public static class TableauMenuWindow {
    public static int NativePid = 42;
    public static double Now = 0;
    public static double PopupMs = 0;
    public static bool Reappears = false;
    public static bool Owned(long hwnd, int pid) { return NativePid == pid; }
    public static string PopupState(long hwnd, int pid) {
        if (NativePid != pid) return "foreign";
        if (Reappears && Now >= 100 && Now < 200) return "visible";
        return Now >= PopupMs ? "hidden" : "visible";
    }
}
'@
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$A = [SyntheticProperties]; $T = [SyntheticControls]
$menuId = 'mainMenu.menu_Window'; $barId = 'bar:main'
function Node($identity, $current) {
    $node = [pscustomobject]@{Identity=$identity; Current=$current}
    $node | Add-Member ScriptMethod GetRuntimeId { return @($this.Identity) }
    return $node
}
function Check-Document([bool]$dialogs=$true) { $script:documentChecks++ }
function Start-Sleep([int]$Milliseconds) {
    if ($Milliseconds -le 0) { throw 'Synthetic invalid sleep' }
    [void]$script:sleeps.Add($Milliseconds)
    $clock.Elapsed.TotalMilliseconds += $Milliseconds
    $clock.Elapsed.TotalSeconds = $clock.Elapsed.TotalMilliseconds / 1000.0
    [TableauMenuWindow]::Now = $clock.Elapsed.TotalMilliseconds
    if ($case.failure -eq 'native_owner' -and
            $clock.Elapsed.TotalMilliseconds -ge 100) {
        [TableauMenuWindow]::NativePid=99
    }
    if ($clock.Elapsed.TotalMilliseconds -ge $case.state_ms) {
        $pattern.Current.ExpandCollapseState = [Windows.Automation.ExpandCollapseState]::Collapsed
    }
    if ($script:sleeps.Count -gt 100) { throw 'Synthetic runaway wait' }
}
""" + "\n".join(functions) + r"""
$results = @()
foreach ($case in $request.cases) {
    $q = @{hwnd=11; pid=42; seconds=[double]$case.budget}
    $clock = [pscustomobject]@{Elapsed=[pscustomobject]@{
        TotalSeconds=0.0; TotalMilliseconds=0.0}}
    $script:openedPopup=$null; $script:popupNativeState=$null
    $script:closeStartedAt=$null
    $script:closePollCount=0; $script:closedSince=$null; $script:collapseRequested=$false
    $script:collapseCalls=0; $script:popupCalls=0; $script:documentChecks=0
    $script:sleeps=[Collections.Generic.List[int]]::new()
    $script:operation='process_check'; $script:guardStop=$false
    $script:topMenuName=$case.top_name
    $script:failure='tableau_window_menu_unreadable'; $script:failureStatus='unsupported'
    $out=@{status='unverified'; menu_closed=$false; action_attempted=$false; invoked=0}
    [TableauMenuWindow]::NativePid=42
    [TableauMenuWindow]::Now=0
    [TableauMenuWindow]::PopupMs=$case.popup_ms
    [TableauMenuWindow]::Reappears=($case.failure -eq 'reappears')
    $bar = Node 'bar:main' @{
        AutomationId='mainMenu'; ClassName='QMenuBar'; ControlType='MenuBar'; ProcessId=42}
    $topItem = Node 'top:window' @{
        ControlType='MenuItem'; ClassName='QAction'; ProcessId=42}
    $foreign = Node 'top:other' @{}
    $popup = Node 'popup:synthetic' ([pscustomobject]@{
        ControlType='Menu'; ClassName='TableauMenu'; AutomationId=$case.menu_id;
        ProcessId=42; NativeWindowHandle=31; IsOffscreen=$false})
    $pattern = [pscustomobject]@{Current=[pscustomobject]@{
        ExpandCollapseState=[Windows.Automation.ExpandCollapseState]::Expanded}}
    if ($case.state_ms -eq 0) {
        $pattern.Current.ExpandCollapseState=[Windows.Automation.ExpandCollapseState]::Collapsed
    }
    $pattern | Add-Member ScriptMethod Collapse {
        $script:collapseCalls++
        if ($case.failure -eq 'collapse_exception') {
            throw [InvalidOperationException]::new('Synthetic private collapse body')
        }
    }
    $root = [pscustomobject]@{}
    $root | Add-Member ScriptMethod FindAll {
        param($scope, $condition)
        $script:popupCalls++
        $elapsed = $clock.Elapsed.TotalMilliseconds
        if ($elapsed -ge 100) {
            if ($case.failure -eq 'popup_exception') {
                throw [InvalidOperationException]::new('Synthetic private provider body')
            }
            if ($case.failure -eq 'native_owner') { [TableauMenuWindow]::NativePid=99 }
            if ($case.failure -eq 'type_changed') { $popup.Current.ClassName='OtherMenu' }
        }
        if ($case.failure -eq 'reappears' -and $elapsed -ge 100 -and $elapsed -lt 200) {
            return $popup
        }
        if ($elapsed -ge $case.popup_ms) { return @() }
        return $popup
    }
    $walker = [pscustomobject]@{}
    $walker | Add-Member ScriptMethod GetParent {
        param($element)
        if ([object]::ReferenceEquals($element, $popup)) {
            if ($case.failure -eq 'ancestry_changed' -and
                    $clock.Elapsed.TotalMilliseconds -ge 100) { return $foreign }
            return $topItem
        }
        return $bar
    }
    $script:opened=@{pattern=$pattern; item=$topItem; id='top:window'}
    # Open-Menu had already observed and retained this qualified actual popup.
    $script:openedPopup=@{
        menu_automation_id=$case.menu_id; popup_hwnd=31; popup_runtime_id='popup:synthetic'}
    try {
        Close-Menu
        $out.status='closed'
""" + handlers + r"""
    $results += @{case=$case.name; result=$out; collapse_calls=$script:collapseCalls;
        popup_calls=$script:popupCalls; document_checks=$script:documentChecks;
        elapsed_ms=$clock.Elapsed.TotalMilliseconds; sleeps=@($script:sleeps);
        opened=($null -ne $script:opened)}
}
$script:operation='menu_close_verify'; $script:guardStop=$false
$script:topMenuName='Window'
$inner=[ArgumentException]::new('Synthetic private inner body')
$outer=[InvalidOperationException]::new('Synthetic private outer body', $inner)
$details=Failure-Details ([pscustomobject]@{Exception=$outer})
@{cases=$results; details=$details} | ConvertTo-Json -Depth 9 -Compress
"""
        cases = [
            {"name": "popup_async", "state_ms": 0, "popup_ms": 250},
            {"name": "candidate_async", "state_ms": 0, "popup_ms": 250,
             "menu_id": "mainMenu.menu_File", "top_name": "File"},
            {"name": "state_async", "state_ms": 250, "popup_ms": 100},
            {"name": "already_absent", "state_ms": 0, "popup_ms": 0},
            {"name": "reappears", "failure": "reappears", "state_ms": 0, "popup_ms": 0},
            {"name": "popup_lingers", "state_ms": 0, "popup_ms": 9000},
            {"name": "page_deadline", "state_ms": 0, "popup_ms": 9000, "budget": .18},
            {"name": "short_stability", "state_ms": 0, "popup_ms": 0, "budget": .18},
            {"name": "state_lingers", "state_ms": 9000, "popup_ms": 0},
            *[{"name": failure, "failure": failure, "state_ms": 0, "popup_ms": 9000}
              for failure in ("native_owner", "type_changed", "ancestry_changed",
                              "collapse_exception", "popup_exception")],
        ]
        cls.observation = accessibility.run_powershell_json(
            script, {"cases": [{"budget": 2, "failure": "", "top_name": "Window",
                               "menu_id": "mainMenu.menu_Window", **case} for case in cases]},
            timeout=30)
        if not isinstance(cls.observation.get("cases"), list):
            raise AssertionError(
                "Isolated production functions failed: "
                + cls.observation.get("cause", "invalid_harness_result"))
        cls.results = {case["case"]: case for case in cls.observation["cases"]}

    def test_collapsed_state_waits_for_popup_and_popup_absence_waits_for_collapsed_state(self):
        for scenario, duration in (("popup_async", 500), ("state_async", 500),
                                   ("already_absent", 250), ("candidate_async", 500)):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                result = case["result"]
                self.assertEqual(result["status"], "closed")
                self.assertIs(result["menu_closed"], True)
                self.assertIs(case["opened"], False)
                self.assertEqual(case["collapse_calls"], 1)
                self.assertEqual(case["elapsed_ms"], duration)
                self.assertEqual(case["sleeps"], [50] * (duration // 50))
                self.assertGreaterEqual(case["document_checks"], 6)
                self.assertEqual(result["last_menu_close_observation"], {
                    "poll_count": duration // 50 + 1, "expand_collapse_state": "Collapsed",
                    "qualified_popup_present": False, "stable_closed_ms": 250,
                    "top_menu_name": "File" if scenario == "candidate_async" else "Window",
                    "menu_automation_id": ("mainMenu.menu_File" if scenario == "candidate_async"
                                           else "mainMenu.menu_Window"),
                    "popup_hwnd": 31, "popup_runtime_id": "popup:synthetic",
                    "popup_native_state": "hidden",
                    "wait_ms": duration, "elapsed_ms": duration})
                self.assertEqual(result["invoked"], 0)
                self.assertIs(result["action_attempted"], False)

    def test_reappearing_popup_resets_stability_without_requesting_another_collapse(self):
        case = self.results["reappears"]
        result = case["result"]
        self.assertEqual(result["status"], "closed")
        self.assertIs(result["menu_closed"], True)
        self.assertEqual(case["collapse_calls"], 1)
        self.assertEqual(case["elapsed_ms"], 450)
        self.assertEqual(result["last_menu_close_observation"]["stable_closed_ms"], 250)
        self.assertEqual(case["sleeps"], [50] * 9)

    def test_lingering_popup_or_expanded_state_exhausts_shared_deadline_including_cleanup(self):
        for scenario, deadline in (
                ("popup_lingers", 2000), ("state_lingers", 2000),
                ("page_deadline", 180), ("short_stability", 180)):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                result = case["result"]
                self.assertEqual(result["status"], "timed_out")
                self.assertEqual(result["cause"], "tableau_window_menu_timeout")
                self.assertEqual(result["cleanup_cause"], "tableau_menu_close_unverified")
                self.assertIs(result["menu_closed"], False)
                self.assertEqual(case["collapse_calls"], 1)
                self.assertEqual(case["elapsed_ms"], deadline)
                self.assertEqual(sum(case["sleeps"]), deadline)
                self.assertTrue(all(0 < delay <= 50 for delay in case["sleeps"]))
                self.assertEqual(result["failure_details"]["operation"], "menu_close_verify")
                self.assertEqual(result["cleanup_failure_details"]["operation"],
                                 "menu_close_verify")
                self.assertIs(result["failure_details"]["guard_stop"], True)
                self.assertIs(result["cleanup_failure_details"]["guard_stop"], True)
                observation = result["last_menu_close_observation"]
                # At expiry no new provider query is permitted. The final raw
                # observation is the last in-budget sample, not a late success.
                self.assertEqual(observation["wait_ms"], deadline - case["sleeps"][-1])
                self.assertEqual(observation["elapsed_ms"], observation["wait_ms"])
                self.assertEqual(observation["qualified_popup_present"],
                                 scenario in ("popup_lingers", "page_deadline"))

    def test_foreign_popup_type_and_ancestry_changes_are_terminal_during_settle(self):
        for scenario, cause, operation in (
                ("native_owner", "tableau_menu_popup_ownership_or_type_changed",
                 "popup_native_identity"),
                ("type_changed", "tableau_menu_popup_ownership_or_type_changed",
                 "popup_properties"),
                ("ancestry_changed", "tableau_menu_ancestry_changed", "popup_ancestry")):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                result = case["result"]
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["cause"], cause)
                self.assertEqual(result["failure_details"]["operation"], operation)
                self.assertIs(result["failure_details"]["guard_stop"], True)
                self.assertIs(result["menu_closed"], False)
                self.assertEqual(case["collapse_calls"], 1)
                self.assertEqual(case["elapsed_ms"], 100)
                self.assertEqual(case["sleeps"], [50, 50])

    def test_provider_exception_keeps_original_operation_separate_from_cleanup_failure(self):
        for scenario, operation, cleanup_guard in (
                ("collapse_exception", "menu_collapse", True),
                ("popup_exception", "popup_find", False)):
            with self.subTest(scenario=scenario):
                result = self.results[scenario]["result"]
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["cause"], "tableau_window_menu_unreadable")
                self.assertEqual(result["cleanup_cause"], "tableau_menu_close_unverified")
                self.assertEqual(result["failure_details"]["operation"], operation)
                self.assertIs(result["failure_details"]["guard_stop"], False)
                self.assertIs(result["cleanup_failure_details"]["guard_stop"], cleanup_guard)
                self.assertIs(result["menu_closed"], False)
                self.assertEqual(self.results[scenario]["collapse_calls"], 1)

    def test_failure_metadata_contains_fixed_operation_and_types_not_exception_bodies(self):
        self.assertEqual(self.observation["details"], {
            "operation": "menu_close_verify", "guard_stop": False,
            "top_menu_name": "Window",
            "exception_type": "System.InvalidOperationException",
            "inner_exception_type": "System.ArgumentException"})
        for case in self.results.values():
            for key in ("failure_details", "cleanup_failure_details"):
                details = case["result"].get(key)
                if details is None:
                    continue
                self.assertEqual(set(details), {
                    "operation", "guard_stop", "top_menu_name",
                    "exception_type", "inner_exception_type"})
                self.assertEqual(details["top_menu_name"], "Window")
                self.assertIsInstance(details["exception_type"], str)
                self.assertLessEqual(len(details["exception_type"]), 160)
        serialized = json.dumps(self.observation)
        for forbidden in ("Synthetic private", "Message", "StackTrace",
                          "Tableau menu observation stopped"):
            self.assertNotIn(forbidden, serialized)


@unittest.skipUnless(desktop_window.IS_WINDOWS, "Isolated Windows PowerShell functions")
class TestTableauDocumentGuardPowerShell(unittest.TestCase):
    """Execute production guard decisions/observer with only in-memory windows."""

    @classmethod
    def setUpClass(cls):
        if accessibility.powershell_preflight()["status"] != "ready":
            raise unittest.SkipTest("Windows PowerShell not installed")
        source = navigation._TABLEAU_WINDOW_MENU_SCRIPT
        native = source.split("Add-Type @'\n", 1)[1].split("\n'@", 1)[0]
        declaration = "    public sealed class DialogObservation {"
        native_guard = declaration + native.split(declaration, 1)[1]
        # Keep ObserveGuard, WindowPid and the observation types unchanged.
        # Replace only Win32 APIs, never load user32/UIA or inspect real windows.
        native_fakes = r"""
using System;
using System.Collections.Generic;
using System.Text;
public sealed class SyntheticGuardWindow {
    public long Hwnd, OwnerHwnd;
    public int Pid, AfterPid, PidReads;
    public bool Enabled, Visible, MetadataForeign, MissingClass;
    public string Title, ClassName;
}
public sealed class SyntheticGuardCurrent { public int ProcessId = 42; }
public sealed class SyntheticGuardDocument {
    public SyntheticGuardCurrent Current = new SyntheticGuardCurrent();
    public string Identity;
    public string[] GetRuntimeId() { return new[] { Identity }; }
}
public static class SyntheticGuardAutomation {
    public static SyntheticGuardDocument Document;
    public static SyntheticGuardDocument FromHandle(IntPtr hwnd) { return Document; }
}
public static class TableauMenuWindow {
    delegate bool Callback(IntPtr hwnd, IntPtr unused);
    public static List<SyntheticGuardWindow> Windows = new List<SyntheticGuardWindow>();
    public static bool EnumerationComplete;
    public static int EnumCalls, Enumerated;
    static SyntheticGuardWindow Window(IntPtr hwnd) {
        return Windows.Find(window => window.Hwnd == hwnd.ToInt64());
    }
    static bool EnumWindows(Callback callback, IntPtr unused) {
        EnumCalls++;
        foreach (var window in Windows) {
            Enumerated++;
            if (!callback(new IntPtr(window.Hwnd), unused)) return false;
        }
        return EnumerationComplete;
    }
    static IntPtr GetWindow(IntPtr hwnd, uint flag) {
        if (flag != 4) throw new InvalidOperationException("Synthetic invalid owner flag");
        return new IntPtr(Window(hwnd).OwnerHwnd);
    }
    static bool IsWindowEnabled(IntPtr hwnd) { return Window(hwnd).Enabled; }
    static bool IsWindowVisible(IntPtr hwnd) { return Window(hwnd).Visible; }
    static int GetWindowTextLengthW(IntPtr hwnd) { return Window(hwnd).Title.Length; }
    static int Copy(string value, StringBuilder buffer, int length) {
        string text = value.Substring(0, Math.Min(value.Length, length - 1));
        buffer.Append(text);
        return text.Length;
    }
    static int GetWindowTextW(IntPtr hwnd, StringBuilder buffer, int length) {
        return Copy(Window(hwnd).Title, buffer, length);
    }
    static int GetClassNameW(IntPtr hwnd, StringBuilder buffer, int length) {
        var window = Window(hwnd);
        int read = window.MissingClass ? 0 : Copy(window.ClassName, buffer, length);
        if (window.MetadataForeign) window.Pid = 99;
        return read;
    }
    static uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid) {
        var window = Window(hwnd);
        window.PidReads++;
        pid = (uint)(window.AfterPid != 0 && window.PidReads > 1
            ? window.AfterPid : window.Pid);
        return pid == 0 ? 0u : 1u;
    }
"""
        functions = []
        for declaration, next_declaration in (
                ("function Exception-Type($exception) {", "function Failure-Details("),
                ("function Failure-Details($record) {", "function Stop-Menu("),
                ("function Stop-Menu($cause, $status='unsupported') {", "function Check-Time {"),
                ("function Check-Time {", "function Pause-Menu {"),
                ("function Retain-DocumentGuard($observation, $reason) {", "function Check-Document("),
                ("function Check-Document([bool]$dialogs=$true) {", "function Find-Popup(")):
            functions.append(declaration + source.split(declaration, 1)[1].split(
                next_declaration, 1)[0])
        handler_start = "} catch {\n    $out.status=$script:failureStatus;"
        handlers = handler_start + source.split(handler_start, 1)[1].split(
            "$out | ConvertTo-Json", 1)[0]
        script = inert_dialog_observer_script() + "\nAdd-Type -TypeDefinition @'\n" + native_fakes + native_guard + r"""
'@
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$A = [SyntheticGuardAutomation]
function Window($record) {
    $window = [SyntheticGuardWindow]::new()
    $window.Hwnd=$record.hwnd; $window.OwnerHwnd=$record.owner_hwnd
    $window.Pid=$record.pid; $window.AfterPid=$record.after_pid
    $window.Enabled=$record.enabled; $window.Visible=$record.visible
    $window.Title=$record.title; $window.ClassName=$record.class_name
    $window.MetadataForeign=$record.metadata_foreign; $window.MissingClass=$record.missing_class
    [TableauMenuWindow]::Windows.Add($window)
    return $window
}
function Start-Sleep { throw 'Document guards must not wait or retry' }
function Close-Menu {
    $script:cleanupCalls++
    $script:firstGuardJson = $out.document_guard | ConvertTo-Json -Depth 6 -Compress
    $script:firstClassificationJson = $out.document_guard_classification |
        ConvertTo-Json -Depth 8 -Compress
    $clock.Elapsed.TotalSeconds=.375; $clock.Elapsed.TotalMilliseconds=375
    $document.Pid=99; $document.AfterPid=0
    $document.Enabled=$true; $document.Visible=$true
    Observe-DocumentGuardClassification $out.document_guard
    # Real menu cleanup rechecks native document ownership with dialogs=false.
    # A later, different failed decision must not replace the first guard.
    Check-Document $false
}
""" + "\n".join(functions) + r"""
$results = @()
foreach ($case in $request.cases) {
    [TableauMenuWindow]::Windows.Clear()
    [TableauMenuWindow]::EnumCalls=0; [TableauMenuWindow]::Enumerated=0
    [TableauMenuWindow]::EnumerationComplete=$case.complete
    $document = Window $case.document
    foreach ($record in $case.dialogs) { $null = Window $record }
    $uia = [SyntheticGuardDocument]::new(); $uia.Identity=$case.identity
    [SyntheticGuardAutomation]::Document=$uia
    $q=@{hwnd=11; pid=42; mode='verify'; seconds=$case.budget}
    $clock=[pscustomobject]@{Elapsed=[pscustomobject]@{
        TotalSeconds=.125; TotalMilliseconds=125}}
    $script:documentId='document:synthetic:11'
    $script:failure='tableau_window_menu_unreadable'; $script:failureStatus='unsupported'
    $script:operation='process_check'; $script:guardStop=$false; $script:topMenuName='Window'
    $script:opened=$(if ($case.cleanup) { @{} } else { $null })
    $script:cleanupCalls=0; $script:firstGuardJson=$null
    $script:firstClassificationJson=$null
    $T=[Windows.Automation.ControlType]; $barId='bar:main'
    $bar=Synthetic-DialogNode $barId @{
        ProcessId=42; AutomationId='mainMenu'; ClassName='QMenuBar'; ControlType=$T::MenuBar}
    $topItem=Synthetic-DialogNode 'top:window' @{
        ProcessId=42; ClassName='QAction'; ControlType=$T::MenuItem}
    $top=@{item=$topItem; id='top:window'}
    $walker=[pscustomobject]@{}
    $walker | Add-Member ScriptMethod GetParent { param($item) return $bar }
    if ($case.menu_missing) { $top=$null; $bar=$null }
    $expected=$(if ($case.dialogs.Count -gt 0) { $case.dialogs[0] } else { $null })
    Initialize-SyntheticDialog $case.dialog_case $expected
    [Windows.Automation.AutomationElement]::ReadHook=[Action]{
        if ([Windows.Automation.AutomationElement]::Reads -eq 2) {
            if ($case.classification_end_ms) {
                $clock.Elapsed.TotalMilliseconds=$case.classification_end_ms
                $clock.Elapsed.TotalSeconds=$case.classification_end_ms / 1000.0
            }
            if ($case.document_changed) { $uia.Identity='document:changed' }
            if ($case.menu_changed) { $bar.Identity='bar:changed' }
        }
    }
    $out=@{status='unverified'; provider='tableau_window_menu'; selected=$null;
        action_attempted=($case.invoked -ne 0); invoked=$case.invoked;
        menu_closed=$true; retryable=$false}
    try {
        Check-Document
        $out.status='allowed'
""" + handlers + r"""
    $results += @{case=$case.name; result=$out; cleanup_calls=$script:cleanupCalls;
        first_guard_json=$script:firstGuardJson; elapsed_ms=$clock.Elapsed.TotalMilliseconds;
        first_classification_json=$script:firstClassificationJson;
        dialog_native_reads=[NativeDesktopDialogWindow]::Reads;
        dialog_uia_reads=[Windows.Automation.AutomationElement]::Reads;
        dialog_hwnd=[NativeDesktopDialogWindow]::LastHwnd;
        dialog_pid=[NativeDesktopDialogWindow]::LastPid;
        enum_calls=[TableauMenuWindow]::EnumCalls; enumerated=[TableauMenuWindow]::Enumerated}
}
@{cases=$results} | ConvertTo-Json -Depth 9 -Compress
"""
        if any(unsafe in script for unsafe in ("DllImport", "user32.dll", "UIAutomationClient")):
            raise AssertionError("Isolated guard harness contains native dependencies")

        def window(hwnd=31, **options):
            return {
                "hwnd": hwnd, "owner_hwnd": 11, "pid": 42, "after_pid": 0,
                "enabled": True, "visible": True, "class_name": "SyntheticModal",
                "title": 'Synthetic Recovery "Étude"',
                "metadata_foreign": False, "missing_class": False, **options,
            }

        document = window(11, owner_hwnd=0, class_name="SyntheticDocument",
                          title="Synthetic Document")
        rejected = [
            window(32, pid=99, title="Synthetic foreign dialog"),
            window(33, visible=False, title="Synthetic hidden dialog"),
            window(34, title=""),
            window(35, owner_hwnd=0, title="Synthetic unowned window"),
        ]
        cases = [
            {"name": "healthy"}, {"name": "filtered", "dialogs": rejected},
            {"name": "disabled", "document": dict(document, enabled=False, visible=False)},
            {"name": "modal", "dialogs": [window(), *rejected]},
            {"name": "disabled_modal", "document": dict(document, enabled=False),
             "dialogs": [window()]},
            {"name": "foreign_document", "document": dict(document, pid=99)},
            {"name": "reassigned_document", "document": dict(document, after_pid=99)},
            {"name": "changed_identity", "identity": "document:other"},
            {"name": "incomplete", "complete": False},
            {"name": "long_metadata", "dialogs": [
                window(title="Synthetic " + "x" * 520, class_name="Synthetic" + "C" * 260)]},
            {"name": "overflow", "dialogs": [
                window(31 + index, title=f"Synthetic Modal {index}") for index in range(17)]},
            {"name": "foreign_after_metadata", "dialogs": [window(metadata_foreign=True)]},
            {"name": "missing_metadata", "dialogs": [window(missing_class=True)]},
            {"name": "disabled_cleanup", "cleanup": True,
             "document": dict(document, enabled=False, visible=False)},
            {"name": "modal_cleanup", "cleanup": True, "invoked": 1, "dialogs": [window()]},
            *[{"name": name, "invoked": 1, "dialogs": [window(title="Processing Request")],
               **options} for name, options in (
                   ("live_busy", {}),
                   ("live_busy_cleanup", {"cleanup": True}),
                   ("live_busy_deadline", {"budget": .5, "classification_end_ms": 500}),
                   ("live_document_changed", {"document_changed": True}),
                   ("live_menu_changed", {"menu_changed": True}),
                   ("live_without_menu", {"menu_missing": True}),
                   ("live_title_only", {"dialog_case": {"nodes": []}}),
                   ("live_cancel_only", {"dialog_case": {"nodes": [{
                       "type": "Button", "name": "Cancel", "hwnd": 32,
                       "identity": "button:synthetic:2"}]}}),
               )],
        ]
        observation = accessibility.run_powershell_json(script, {
            "cases": [{"document": document, "dialogs": [], "complete": True, "cleanup": False,
                       "identity": "document:synthetic:11", "invoked": 0, "budget": 2,
                       **case} for case in cases],
        }, timeout=30)
        if not isinstance(observation.get("cases"), list):
            raise AssertionError(
                "Isolated production guard failed: " + observation.get("cause", "invalid_harness_result"))
        cls.results = {case["case"]: case for case in observation["cases"]}

    def test_disabled_no_dialog_and_owned_modal_have_distinct_first_failed_decisions(self):
        for scenario, expected in (
                ("disabled", _document_guard(visible=False)),
                ("modal", _document_guard(reason="owned_dialog", enabled=True,
                                          dialogs=[_owned_guard_dialog()])),
                ("disabled_modal", _document_guard(reason="document_disabled_and_owned_dialog",
                                                   dialogs=[_owned_guard_dialog()]))):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                result = case["result"]
                self.assertEqual((result["status"], result["cause"]),
                                 ("inaccessible", "owned_dialog_or_disabled_document"))
                self.assertEqual(result["document_guard"], expected)
                self.assertEqual(result["failure_details"]["operation"], "document_dialogs")
                self.assertIs(result["failure_details"]["guard_stop"], True)
                self.assertEqual(case["enum_calls"], 1)
                self.assertEqual(case["elapsed_ms"], 125)
                self.assertEqual(case["cleanup_calls"], 0)
                self.assertEqual(result["invoked"], 0)

    def test_ownership_identity_and_incomplete_inventory_remain_terminal_not_dialog_success(self):
        for scenario, reason, operation, pids in (
                ("foreign_document", "window_ownership_changed", "document_ownership", (99, 99)),
                ("reassigned_document", "window_ownership_changed", "document_ownership", (42, 99)),
                ("changed_identity", "document_identity_changed", "document_identity", (42, 42)),
                ("incomplete", "native_guard_incomplete", "document_dialogs", (42, 42))):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                result = case["result"]
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["cause"],
                                 "document_guard_incomplete" if scenario == "incomplete" else reason)
                guard = result["document_guard"]
                self.assertEqual(guard["reason"], reason)
                self.assertEqual((guard["document_pid_before"], guard["document_pid_after"]), pids)
                self.assertEqual((guard["document_owned_before"], guard["document_owned_after"]),
                                 tuple(pid == 42 for pid in pids))
                self.assertEqual(guard["owned_dialogs"], [])
                self.assertIs(guard["dialogs_complete"], False)
                self.assertEqual(result["failure_details"]["operation"], operation)
                self.assertEqual(case["enum_calls"], int(scenario == "incomplete"))
                self.assertEqual(case["elapsed_ms"], 125)
        for scenario in ("healthy", "filtered"):
            with self.subTest(scenario=scenario):
                self.assertEqual(self.results[scenario]["result"]["status"], "allowed")
                self.assertNotIn("document_guard", self.results[scenario]["result"])
                self.assertEqual(self.results[scenario]["enum_calls"], 1)

    def test_dialog_metadata_is_exact_owned_bounded_and_marks_partial_inventory(self):
        modal = self.results["modal"]["result"]["document_guard"]
        self.assertEqual(modal["owned_dialogs"], [_owned_guard_dialog()])
        bounded = self.results["long_metadata"]["result"]["document_guard"]
        record = bounded["owned_dialogs"][0]
        self.assertEqual(record["title"], ("Synthetic " + "x" * 520)[:512])
        self.assertEqual(record["class_name"], ("Synthetic" + "C" * 260)[:256])
        self.assertIs(record["title_truncated"], True)
        self.assertIs(record["metadata_complete"], True)
        self.assertIs(bounded["dialogs_complete"], True)
        overflow = self.results["overflow"]["result"]
        self.assertEqual((overflow["status"], overflow["cause"]),
                         ("inaccessible", "owned_dialog_or_disabled_document"))
        guard = overflow["document_guard"]
        self.assertIs(guard["dialogs_truncated"], True)
        self.assertIs(guard["dialogs_complete"], False)
        self.assertIs(guard["owned_dialog_present"], True)
        self.assertEqual([record["hwnd"] for record in guard["owned_dialogs"]], list(range(31, 47)))
        self.assertEqual(self.results["overflow"]["enumerated"], 18)
        self.assertNotIn("Synthetic Modal 16", json.dumps(guard))

    def test_reused_foreign_handle_redacts_text_and_missing_metadata_is_not_complete(self):
        for scenario in ("foreign_after_metadata", "missing_metadata"):
            with self.subTest(scenario=scenario):
                result = self.results[scenario]["result"]
                self.assertEqual((result["status"], result["cause"]),
                                 ("inaccessible", "owned_dialog_or_disabled_document"))
                guard = result["document_guard"]
                self.assertIs(guard["dialogs_complete"], True)
                record = guard["owned_dialogs"][0]
                self.assertEqual(record["hwnd"], 31)
                self.assertEqual(record["owner_hwnd"], 11)
                self.assertEqual(record["pid_before"], 42)
                self.assertIs(record["owned_before"], True)
                self.assertIs(record["metadata_complete"], False)
                self.assertEqual(record["class_name"], "")
                if scenario == "foreign_after_metadata":
                    self.assertEqual(record["pid_after"], 99)
                    self.assertIs(record["owned_after"], False)
                    self.assertEqual(record["title"], "")
                    self.assertNotIn('Synthetic Recovery "Étude"', json.dumps(guard, ensure_ascii=False))
                else:
                    self.assertEqual(record["pid_after"], 42)
                    self.assertIs(record["owned_after"], True)
                    self.assertEqual(record["title"], _owned_guard_dialog()["title"])

    def test_cleanup_cannot_replace_first_guard_with_later_ownership_enabled_or_visible_state(self):
        for scenario, expected in (
                ("disabled_cleanup", _document_guard(visible=False)),
                ("modal_cleanup", _document_guard(reason="owned_dialog", enabled=True,
                                                  dialogs=[_owned_guard_dialog()]))):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                result = case["result"]
                self.assertEqual(result["document_guard"], expected)
                self.assertEqual(result["document_guard"], json.loads(case["first_guard_json"]))
                self.assertEqual((result["status"], result["cause"]),
                                 ("error", "owned_dialog_or_disabled_document"))
                self.assertEqual(result["cleanup_cause"], "tableau_menu_close_unverified")
                self.assertEqual(result["failure_details"]["operation"], "document_dialogs")
                self.assertEqual(result["cleanup_failure_details"]["operation"], "document_ownership")
                self.assertIs(result["menu_closed"], False)
                self.assertEqual(case["cleanup_calls"], 1)
                self.assertEqual(case["enum_calls"], 1)
                self.assertEqual(case["elapsed_ms"], 375)
                self.assertEqual(result["invoked"], int(scenario == "modal_cleanup"))

    def test_live_classification_is_before_cleanup_pinned_and_never_resumes_or_reinvokes(self):
        for scenario, busy in (
                ("live_busy", True), ("live_busy_cleanup", True),
                ("live_title_only", False), ("live_cancel_only", False), ("modal_cleanup", False)):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                result = case["result"]
                proof = result["document_guard_classification"]
                self.assertEqual(proof["phase"], "document_guard_before_menu_cleanup")
                self.assertEqual(proof["basis"], "shared_strict_uia")
                self.assertIs(proof["progress_required"], True)
                self.assertIs(proof["wait_applied"], False)
                self.assertIs(proof["resume_authorized"], False)
                self.assertEqual(proof["terminal_status"], "inaccessible")
                self.assertEqual(proof["terminal_cause"], "owned_dialog_or_disabled_document")
                self.assertEqual((proof["guard_elapsed_ms"], proof["started_ms"]), (125, 125))
                self.assertIs(proof["complete"], True)
                self.assertIs(proof["document_identity_after"], True)
                self.assertIs(proof["menu_identity_after"], True)
                entry = proof["dialogs"][0]
                self.assertIs(entry["busy_conclusive"], busy)
                self.assertEqual(entry["metadata"]["strict_classification"]["status"],
                                 "busy" if busy else "attention")
                self.assertEqual((case["dialog_hwnd"], case["dialog_pid"]), (31, 42))
                self.assertEqual((case["dialog_native_reads"], case["dialog_uia_reads"]), (2, 2))
                self.assertEqual(result["cause"], "owned_dialog_or_disabled_document")
                self.assertEqual(result["invoked"], 1)
                self.assertIs(result["retryable"], False)
                if scenario.endswith("cleanup"):
                    self.assertEqual(proof, json.loads(case["first_classification_json"]))
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(case["cleanup_calls"], 1)
                else:
                    self.assertEqual(result["status"], "inaccessible")
        proof = self.results["live_busy_cleanup"]["result"]["document_guard_classification"]
        self.assertEqual(proof["dialogs"][0]["metadata"]["progress_controls"],
                         strict_dialog_metadata()["progress_controls"])
        self.assertEqual(proof["finished_ms"], 125)

    def test_partial_identity_changed_or_expired_classification_cannot_replace_original_stop(self):
        for scenario, cause in (
                ("disabled", "guard_dialog_inventory_unqualified"),
                ("overflow", "guard_dialog_inventory_unqualified"),
                ("missing_metadata", "guard_classification_incomplete"),
                ("live_document_changed", "guard_classification_incomplete"),
                ("live_menu_changed", "guard_classification_incomplete"),
                ("live_without_menu", "guard_classification_incomplete"),
                ("live_busy_deadline", "guard_classification_deadline")):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                result = case["result"]
                proof = result["document_guard_classification"]
                self.assertEqual((result["status"], result["cause"]),
                                 ("inaccessible", "owned_dialog_or_disabled_document"))
                self.assertEqual(proof["cause"], cause)
                self.assertIs(proof["complete"], False)
                self.assertIs(proof["wait_applied"], False)
                self.assertIs(proof["resume_authorized"], False)
                self.assertTrue(all(entry["busy_conclusive"] is False for entry in proof["dialogs"]))
                self.assertEqual(case["elapsed_ms"], 500 if scenario == "live_busy_deadline" else 125)
                if scenario == "live_busy_deadline":
                    self.assertEqual(proof["elapsed_ms"], 375)
                    self.assertEqual(proof["guard_elapsed_ms"], 125)
                    self.assertNotIn("document_after", proof)
                    self.assertEqual(result["invoked"], 1)
                if scenario in ("disabled", "overflow", "missing_metadata"):
                    self.assertEqual(case["dialog_native_reads"], 0)


@unittest.skipUnless(desktop_window.IS_WINDOWS, "Isolated Windows PowerShell functions")
class TestTableauLayoutWaitPowerShell(unittest.TestCase):
    """Execute the real menu action/guard/fresh-check flow with inert providers."""

    @classmethod
    def setUpClass(cls):
        if accessibility.powershell_preflight()["status"] != "ready":
            raise unittest.SkipTest("Windows PowerShell not installed")
        source = navigation._TABLEAU_WINDOW_MENU_SCRIPT
        start = "function Exception-Type($exception) {"
        main_gate = "\ntry {\n    Check-Time\n    if ([Diagnostics.Process]::GetProcessById"
        functions = start + source.split(start, 1)[1].split(main_gate, 1)[0]
        setup = source.split("$q = [Console]::In.ReadToEnd() | ConvertFrom-Json\n", 1)[1].split(
            start, 1)[0]
        # All menu, guard, wait, classification and publish code is unchanged.
        # The native process-name gate alone is omitted: no real process exists.
        main = "try {\n    Check-Time\n    $hint = $q.menu_hint" + source.split(
            "    $hint = $q.menu_hint", 1)[1].split("$out | ConvertTo-Json", 1)[0]
        script = inert_tableau_menu_lifecycle_script() + "\n" + functions + r"""
$request=[Console]::In.ReadToEnd() | ConvertFrom-Json
$results=@()
foreach ($case in $request.cases) {
    $q=@{hwnd=11; pid=42; name='Synthetic Page'; mode='select'; seconds=$case.budget}
    $clock=[pscustomobject]@{Elapsed=[pscustomobject]@{
        TotalSeconds=0.0; TotalMilliseconds=0.0}}
    Initialize-SyntheticMenu $case
""" + setup + "\n" + main + r"""
    $results+=@{case=$case.name; budget=$case.budget; result=$out; events=@($script:menuEvents);
        sleeps=@($script:menuSleeps); elapsed_ms=$clock.Elapsed.TotalMilliseconds;
        invoke_calls=$script:invokeCalls; expand_calls=$script:expandCalls;
        collapse_calls=$script:collapseCalls; dialog_native_reads=[NativeDesktopDialogWindow]::Reads}
}
@{cases=$results} | ConvertTo-Json -Depth 12 -Compress
"""
        layout = layout_dialog_case()
        protected = {**layout, "native_before": {"title": "File Recovery"}}
        unknown = copy.deepcopy(layout)
        unknown["nodes"][0]["name"] = "Computing Synthetic Content"
        unqualified_body = [*layout_caption_transition_case()["nodes"], {
            "type": "ListItem", "name": "", "hwnd": 0, "identity": "list:synthetic:1"}]
        cases = [
            {"name": "resolved"},
            {"name": "gone", "absence_state": "gone"},
            {"name": "hidden_reappeared", "reappear_after_absence": True},
            {"name": "gone_reappeared", "absence_state": "gone", "reappear_after_absence": True},
            {"name": "hidden_reused", "hidden_reused": True},
            {"name": "persistent", "persistent_busy": True},
            {"name": "short_deadline", "persistent_busy": True, "budget": .8},
            {"name": "partial", "dialog_case": {**layout, "native_before": {"complete": False}}},
            {"name": "protected", "dialog_case": protected},
            {"name": "unknown", "dialog_case": unknown},
            {"name": "foreign", "dialog_case": {**layout, "native_after": {
                "pid_after": 99, "owned_after": False, "complete": False}}},
            {"name": "reused", "dialog_case": {**layout, "after_identity": "dialog:reused"}},
            {"name": "disabled_no_dialog", "disabled_no_dialog": True},
            {"name": "disabled_after_wait", "disabled_after_wait": True},
            {"name": "document_changed", "persistent_busy": True, "change_during_wait": "document"},
            {"name": "menu_changed", "persistent_busy": True, "change_during_wait": "menu"},
            {"name": "dialog_reused", "persistent_busy": True, "change_during_wait": "dialog"},
            {"name": "dialog_foreign_after_wait", "dialog_foreign_after_wait": True},
            *[{"name": name, "persistent_busy": True, "caption_transition_read": 10,
               "transition_read_ms": 37, "transition_title": CAPTION_TRANSITION_TITLE,
               "dialog_case": {"nodes": layout_caption_transition_case()["nodes"]}, **options}
              for name, options in (
                  ("caption_transition", {}),
                  ("caption_resolved", {"persistent_busy": False, "resolve_after_ms": 1000}),
                  ("caption_capture", {"persistent_busy": False, "resolve_after_ms": 1000,
                                       "budget": 3}),
                  ("caption_class_changed", {"transition_native": {"class_name": "OtherModal"}}),
                  ("caption_foreign", {"transition_native": {
                      "pid_after": 99, "owned_after": False, "complete": False}}),
                  ("caption_protected", {"transition_title": "Authentication required"}),
                  ("caption_fresh_expired", {"caption_fresh_expire": True}),
                  ("caption_diagnostic", {"transition_nodes": unqualified_body}),
                  ("caption_diagnostic_expired", {"transition_nodes": unqualified_body,
                                                  "caption_fresh_expire": True}),
              )],
            # Same synthetic IDs on another page start a new approval lifetime.
            {"name": "page_after_retired"},
        ]
        cls.requests = [{"budget": 2, "resolve_after_ms": 500, "dialog_case": layout, **case}
                        for case in cases]
        cls.script = script
        cls.original_source = source
        observation = accessibility.run_powershell_json(script, {"cases": cls.requests}, timeout=30)
        if not isinstance(observation.get("cases"), list):
            raise AssertionError(
                "Isolated menu lifecycle failed: " + observation.get("cause", "invalid_harness_result"))
        cls.results = {case["case"]: case for case in observation["cases"]}

    def assert_verified_layout_lifecycle(self, case, *, expected_texts=LAYOUT_TEXTS):
        result = case["result"]
        self.assertEqual(result["status"], "verified")
        self.assertTrue(navigation._verified_tableau_menu(result, 11, 42, "Synthetic Page"))
        self.assertEqual(case["invoke_calls"], 1)
        self.assertEqual(result["invoked"], 1)
        self.assertIs(result["action_attempted"], True)
        guard = result["document_guard"]
        self.assertEqual(guard["reason"], "document_disabled_and_owned_dialog")
        self.assertIs(guard["document_enabled"], False)
        self.assertEqual(guard["owned_dialogs"][0]["title"], "Processing Request")
        first = result["document_guard_classification"]
        self.assertEqual(first["phase"], "document_guard_before_menu_cleanup")
        self.assertIs(first["wait_applied"], False)
        self.assertIs(first["resume_authorized"], False)
        self.assertEqual(first["guard_elapsed_ms"], guard["elapsed_ms"])
        self.assertEqual(first["dialogs"][0]["metadata"]["texts"], expected_texts)
        self.assertEqual(first["dialogs"][0]["metadata"]["progress_controls"], [])
        self.assertEqual(first["dialogs"][0]["metadata"]["strict_classification"]["basis"],
                         "tableau_layout_timer")
        wait = result["document_guard_wait"]
        self.assertEqual(wait["status"], "ready")
        self.assertIs(wait["wait_applied"], True)
        self.assertIs(wait["resume_authorized"], True)
        self.assertEqual((wait["invoked_at_start"], wait["invoked_at_end"]), (1, 1))
        self.assertEqual(wait["guard_elapsed_ms"], guard["elapsed_ms"])
        self.assertEqual(wait["deadline_ms"], round(case["budget"] * 1000))
        self.assertIs(wait["last_observation"]["guard"]["document_enabled"], True)
        self.assertEqual(wait["last_observation"]["guard"]["owned_dialogs"], [])
        self.assertTrue(any(event["event"] == "busy_resolved" for event in case["events"]))
        sheets = [event for event in case["events"] if event["event"] == "sheets"]
        self.assertGreater(sheets[-1]["build"], sheets[0]["build"])
        self.assertGreaterEqual(sheets[-1]["elapsed_ms"], wait["finished_ms"])
        self.assertIs(sheets[-1]["busy"], False)
        self.assertIs(result["menu_closed"], True)
        self.assertIs(result["fresh_check"], True)
        self.assertLess(case["elapsed_ms"], wait["deadline_ms"])
        self.assertEqual(case["expand_calls"], case["collapse_calls"])

    def test_complete_layout_wait_keeps_one_invoke_first_guard_and_requires_fresh_checked_menu(self):
        self.assert_verified_layout_lifecycle(self.results["resolved"])

    def test_complete_known_body_caption_race_resumes_only_after_absence_and_fresh_checked_menu(self):
        case = self.results["caption_resolved"]
        self.assert_verified_layout_lifecycle(case, expected_texts=[
            "Processing Request", "Computing View Layout", "Elapsed time 00:05", "Cancel"])
        result = case["result"]
        self.assertNotIn("document_guard_caption_transition", result)
        first = result["document_guard_classification"]["dialogs"][0]["metadata"]
        self.assertEqual(first["native_before"]["title"], "Processing Request")
        self.assertEqual(first["native_after"]["title"], "Processing Request")
        self.assertEqual(first["strict_classification"]["reason"],
                         "complete_owned_layout_timer_structure")
        wait = result["document_guard_wait"]
        raw = wait["observations"][3]["classification"]["dialogs"][0]["metadata"]
        self.assertEqual(raw["status"], "observed")
        self.assertIs(raw["post_approved_layout_observation"], True)
        self.assertIs(raw["caption_changed"], True)
        self.assertIs(raw["immutable_identity_complete"], True)
        self.assertEqual(raw["identity_change_fields"], [])
        self.assertEqual(raw["native_before"]["title"], "Processing Request")
        self.assertEqual(raw["native_after"]["title"], CAPTION_TRANSITION_TITLE)
        self.assertEqual(raw["body_texts"], [
            "Computing View Layout", "Elapsed time 00:05", "Cancel"])
        self.assertEqual(raw["progress_controls"], [])
        self.assertIs(raw["layout_body_controls_complete"], True)
        self.assertEqual(raw["strict_classification"]["reason"], "complete_owned_layout_timer_body")
        self.assertEqual(raw["strict_classification"]["status"], "busy")
        for key in ("uia_runtime_id_before", "uia_runtime_id_after"):
            self.assertEqual(raw[key], first[key])
        for key in ("native_complete", "native_ownership_complete", "uia_complete",
                    "uia_owned_before", "uia_owned_after", "uia_visible_before", "uia_visible_after"):
            self.assertIs(raw[key], True)
        self.assertEqual(wait["deadline_ms"], 2000)
        self.assertEqual(wait["observations"][3]["elapsed_ms"], 1000)
        self.assertEqual(wait["observations"][3]["classification"]["dialogs"][0]["finished_ms"], 1037)
        absence = wait["last_observation"]["dialog_lifecycle"][0]
        self.assertEqual(absence["native_state"], "hidden")
        self.assertIs(absence["absence_observed"], True)
        self.assertIs(absence["native_before"]["visible"], False)
        self.assertIs(absence["native_after"]["visible"], False)

    def test_disabling_only_post_approved_path_reproduces_old_caption_identity_stop_in_memory(self):
        enabled = self.results["caption_resolved"]
        texts = ["Processing Request", "Computing View Layout", "Elapsed time 00:05", "Cancel"]
        self.assert_verified_layout_lifecycle(enabled, expected_texts=texts)
        producer_switch = "$postApprovedLayout=($expected.progress_basis -ceq 'tableau_layout_timer')"
        self.assertEqual(self.script.count(producer_switch), 1)
        disabled_script = self.script.replace(producer_switch, "$postApprovedLayout=$false", 1)
        request = next(case for case in self.requests if case["name"] == "caption_resolved")
        observation = accessibility.run_powershell_json(
            disabled_script, {"cases": [request]}, timeout=10)
        self.assertIsInstance(observation.get("cases"), list)
        self.assertEqual(len(observation["cases"]), 1)
        disabled = observation["cases"][0]
        # Apply the same permanent positive assertion to the one-path mutation.
        with self.assertRaisesRegex(AssertionError, "'error' != 'verified'"):
            self.assert_verified_layout_lifecycle(disabled, expected_texts=texts)
        result = disabled["result"]
        self.assertEqual((result["status"], result["cause"]),
                         ("error", "tableau_document_progress_unreadable"))
        self.assertEqual(disabled["invoke_calls"], 1)
        self.assertEqual(result["invoked"], 1)
        self.assertEqual(result["document_guard"], enabled["result"]["document_guard"])
        first_disabled = copy.deepcopy(result["document_guard_classification"])
        first_enabled = copy.deepcopy(enabled["result"]["document_guard_classification"])
        # Only the shared observer's real stopwatch duration is nondeterministic;
        # retain exact fake page-clock timing and every original evidence field.
        first_disabled["dialogs"][0]["metadata"].pop("elapsed_ms")
        first_enabled["dialogs"][0]["metadata"].pop("elapsed_ms")
        self.assertEqual(first_disabled, first_enabled)
        wait = result["document_guard_wait"]
        self.assertEqual(wait["deadline_ms"], enabled["result"]["document_guard_wait"]["deadline_ms"])
        self.assertEqual((wait["invoked_at_start"], wait["invoked_at_end"]), (1, 1))
        self.assertEqual(wait["observation_count"], 4)
        self.assertIs(wait["resume_authorized"], False)
        raw = wait["last_observation"]["classification"]["dialogs"][0]["metadata"]
        self.assertEqual(raw["status"], "identity_changed")
        self.assertIs(raw["caption_changed"], True)
        self.assertIs(raw["immutable_identity_complete"], True)
        self.assertIs(raw["post_approved_layout_observation"], False)
        self.assertEqual(raw["identity_change_fields"], [])
        self.assertEqual(raw["native_before"]["title"], "Processing Request")
        self.assertEqual(raw["native_after"]["title"], CAPTION_TRANSITION_TITLE)
        self.assertEqual(raw["body_texts"], [
            "Computing View Layout", "Elapsed time 00:05", "Cancel"])
        self.assertIsNone(result["selected"])
        self.assertIs(result["fresh_check"], False)
        self.assertEqual(len(result["menu_snapshots"]), 1)
        self.assertFalse(any(event["event"] == "busy_resolved" for event in disabled["events"]))
        self.assertLess(disabled["elapsed_ms"], wait["deadline_ms"])
        self.assertEqual(navigation._TABLEAU_WINDOW_MENU_SCRIPT, self.original_source)

    def test_caption_repair_producer_selection_retains_provenance_through_pre_post_capture(self):
        case = self.results["caption_capture"]
        texts = ["Processing Request", "Computing View Layout", "Elapsed time 00:05", "Cancel"]
        self.assert_verified_layout_lifecycle(case, expected_texts=texts)
        initial = case["result"]
        original = copy.deepcopy(initial)
        pre, post = _menu_verified(build="pre"), _menu_verified(build="post")
        clock = _Clock()
        calls = []

        def select(hwnd, pid, name, **kwargs):
            self.assertEqual((hwnd, pid, name), (11, 42, "Synthetic Page"))
            calls.append(kwargs)
            if len(calls) == 1:
                self.assertEqual(kwargs, {"timeout": 3})
                clock.now += case["elapsed_ms"] / 1000
                return initial
            self.assertIs(kwargs["verify_only"], True)
            self.assertEqual(kwargs["_tableau_menu_hint"], _menu_hint())
            self.assertAlmostEqual(kwargs["timeout"], 3 - clock.now)
            clock.now += .25
            return pre if len(calls) == 2 else post

        def capture(hwnd, pid, path, timeout):
            self.assertEqual((hwnd, pid), (11, 42))
            self.assertEqual(len(calls), 2)
            self.assertIs(pre["fresh_check"], True)
            self.assertIs(pre["menu_closed"], True)
            self.assertAlmostEqual(timeout, 3 - clock.now)
            Path(path).write_bytes(NATIVE_PNG)
            clock.now += .05
            return {"status": "captured", "screenshot_blank": False}

        with tempfile.TemporaryDirectory() as directory:
            shot = Path(directory, "page.png")
            with mock.patch.object(navigation, "select_document_tab", side_effect=select), \
                    mock.patch.object(navigation, "_bounded_capture", side_effect=capture), \
                    mock.patch.object(navigation.desktop_window, "window_process_id", return_value=42), \
                    mock.patch.object(navigation.time, "monotonic", side_effect=clock.monotonic), \
                    mock.patch.object(navigation.time, "sleep", side_effect=clock.sleep):
                result = navigation.capture_document_tab(
                    11, 42, "Synthetic Page", str(shot), timeout=3)
            self.assertEqual(result["status"], "captured")
            self.assertIs(result["selection"], initial)
            self.assertEqual(result["selection"], original)
            self.assertEqual(result["selection"]["invoked"], 1)
            self.assertEqual(result["selection"]["document_guard_wait"]["deadline_ms"], 3000)
            self.assertIs(result["pre_capture_selection"], pre)
            self.assertIs(result["post_capture_selection"], post)
            self.assertEqual(result["attempt_count"], 1)
            self.assertEqual(len(result["capture_attempts"]), 1)
            self.assertEqual(result["capture_errors"], [])
            self.assertEqual(shot.read_bytes(), NATIVE_PNG)
            self.assertEqual(list(shot.parent.iterdir()), [shot])
        self.assertEqual(len(calls), 3)
        self.assertLess(calls[-1]["timeout"], calls[1]["timeout"])
        self.assertLess(clock.now, 3)
        self.assertEqual(clock.sleeps, [.5])  # Existing bounded paint settle, not a retry.

    def test_gone_or_qualified_hidden_absence_retires_lifetime_and_matching_reappearance_fails(self):
        self.assert_verified_layout_lifecycle(self.results["gone"])
        gone = self.results["gone"]["result"]["document_guard_wait"]["last_observation"]
        self.assertEqual(gone["dialog_lifecycle"][0]["native_state"], "gone")
        self.assertIs(gone["dialog_lifecycle"][0]["absence_observed"], True)
        for scenario in ("hidden_reappeared", "gone_reappeared"):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                result = case["result"]
                self.assertEqual((result["status"], result["cause"]),
                                 ("error", "tableau_progress_dialog_reappeared"))
                self.assertEqual(case["invoke_calls"], 1)
                self.assertEqual(result["invoked"], 1)
                self.assertIsNone(result["selected"])
                self.assertIs(result["fresh_check"], False)
                self.assertEqual(len(result["menu_snapshots"]), 1)
                wait = result["document_guard_wait"]
                self.assertEqual(wait["status"], "stopped")
                self.assertIs(wait["resume_authorized"], False)
                retired = next(sample["dialog_lifecycle"][0] for sample in wait["observations"]
                               if sample.get("dialog_lifecycle"))
                self.assertIs(retired["absence_observed"], True)
                self.assertEqual(retired["native_state"],
                                 "gone" if scenario == "gone_reappeared" else "hidden")
                self.assertEqual(result["last_document_guard_observation"]["classification"][
                    "dialogs"][0]["cause"],
                                 "tableau_progress_dialog_reappeared")
                self.assertTrue(any(event["event"] == "dialog_reappeared" for event in case["events"]))
                self.assertLess(case["elapsed_ms"], wait["deadline_ms"])
        self.assert_verified_layout_lifecycle(self.results["page_after_retired"])

    def test_persistent_known_layout_uses_original_page_deadline_and_bounded_observations(self):
        for scenario in ("persistent", "short_deadline", "caption_transition"):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                result = case["result"]
                self.assertEqual((result["status"], result["cause"]),
                                 ("timed_out", "tableau_document_progress_timeout"))
                self.assertEqual(case["invoke_calls"], 1)
                self.assertEqual(result["invoked"], 1)
                self.assertIsNone(result["selected"])
                self.assertIs(result["fresh_check"], False)
                self.assertEqual(len(result["menu_snapshots"]), 1)
                wait = result["document_guard_wait"]
                deadline = round(case["budget"] * 1000)
                self.assertEqual(wait["deadline_ms"], deadline)
                self.assertEqual(case["elapsed_ms"], deadline)
                self.assertEqual(sum(case["sleeps"]) + (37 if scenario == "caption_transition" else 0),
                                 deadline)
                self.assertEqual(wait["status"], "timed_out")
                self.assertEqual(wait["cause"], "tableau_document_progress_timeout")
                self.assertIs(wait["wait_applied"], True)
                self.assertIs(wait["resume_authorized"], False)
                self.assertEqual((wait["invoked_at_start"], wait["invoked_at_end"]), (1, 1))
                self.assertEqual(wait["finished_ms"], deadline)
                self.assertLess(wait["last_observation"]["elapsed_ms"], deadline)
                self.assertLessEqual(len(wait["observations"]), 8)
                self.assertIs(result["document_guard"]["document_enabled"], False)
                self.assertIs(result["document_guard_classification"]["resume_authorized"], False)
                if scenario != "short_deadline":
                    self.assertGreater(wait["observation_count"], 8)
                    self.assertIs(wait["observations_truncated"], True)
                if scenario == "caption_transition":
                    self.assertNotIn("document_guard_caption_transition", result)
                    raw = wait["last_observation"]["classification"]["dialogs"][0]["metadata"]
                    self.assertEqual(raw["strict_classification"]["reason"],
                                     "complete_owned_layout_timer_body")
                    self.assertEqual(raw["native_after"]["title"], CAPTION_TRANSITION_TITLE)
                    self.assertIs(raw["immutable_identity_complete"], True)

    def test_partial_protected_unknown_foreign_reused_or_disabled_no_dialog_remain_terminal(self):
        for scenario in ("partial", "protected", "unknown", "foreign", "reused",
                         "disabled_no_dialog"):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                result = case["result"]
                self.assertEqual((result["status"], result["cause"]),
                                 ("inaccessible", "owned_dialog_or_disabled_document"))
                self.assertEqual(case["invoke_calls"], 1)
                self.assertEqual(result["invoked"], 1)
                self.assertIs(result["retryable"], False)
                self.assertIsNone(result["selected"])
                self.assertEqual(len(result["menu_snapshots"]), 1)
                self.assertIs(result["fresh_check"], False)
                self.assertIs(result["document_guard_classification"]["resume_authorized"], False)
                self.assertNotIn("document_guard_wait", result)

    def test_identity_change_or_disabled_without_progress_during_wait_never_authorizes_resume(self):
        for scenario, status, cause in (
                ("disabled_after_wait", "inaccessible", "owned_dialog_or_disabled_document"),
                ("document_changed", "error", "document_identity_changed"),
                ("menu_changed", "error", "tableau_progress_document_or_menu_identity_changed"),
                ("dialog_reused", "error", "tableau_document_progress_unreadable"),
                ("hidden_reused", "error", "tableau_progress_dialog_identity_changed"),
                ("dialog_foreign_after_wait", "error", "window_ownership_changed")):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                result = case["result"]
                self.assertEqual((result["status"], result["cause"]), (status, cause))
                self.assertEqual(case["invoke_calls"], 1)
                self.assertEqual(result["invoked"], 1)
                self.assertIsNone(result["selected"])
                self.assertEqual(len(result["menu_snapshots"]), 1)
                self.assertIs(result["document_guard"]["document_enabled"], False)
                wait = result["document_guard_wait"]
                self.assertEqual(wait["status"], "stopped")
                self.assertIs(wait["wait_applied"], True)
                self.assertIs(wait["resume_authorized"], False)
                self.assertEqual((wait["invoked_at_start"], wait["invoked_at_end"]), (1, 1))
                self.assertLess(case["elapsed_ms"], wait["deadline_ms"])

    def test_unqualified_body_caption_follow_up_stays_diagnostic_and_pins_raw_fourth_sample(self):
        case = self.results["caption_diagnostic"]
        result = case["result"]
        self.assertEqual((result["status"], result["cause"]),
                         ("error", "tableau_document_progress_unreadable"))
        self.assertEqual(case["invoke_calls"], 1)
        self.assertEqual(result["invoked"], 1)
        self.assertIsNone(result["selected"])
        self.assertEqual(len(result["menu_snapshots"]), 1)
        self.assertIs(result["fresh_check"], False)
        first_guard = result["document_guard"]
        self.assertEqual(first_guard["owned_dialogs"][0]["title"], "Processing Request")
        self.assertEqual(first_guard["reason"], "document_disabled_and_owned_dialog")
        first = result["document_guard_classification"]
        self.assertEqual(first["dialogs"][0]["metadata"]["strict_classification"]["status"], "busy")
        self.assertIs(first["wait_applied"], False)
        self.assertIs(first["resume_authorized"], False)
        wait = result["document_guard_wait"]
        self.assertEqual(wait["status"], "stopped")
        self.assertIs(wait["resume_authorized"], False)
        self.assertEqual(wait["observation_count"], 4)
        raw = wait["last_observation"]["classification"]["dialogs"][0]["metadata"]
        self.assertEqual(raw["status"], "observed")
        self.assertEqual(raw["strict_classification"]["status"], "attention")
        self.assertIs(raw["strict_classification"]["complete"], False)
        self.assertIs(raw["layout_body_controls_complete"], False)
        self.assertEqual(raw["unexpected_controls"],
                         [{"control_type": "ControlType.ListItem", "name": "", "hwnd": 0}])
        self.assertIs(raw["caption_changed"], True)
        self.assertIs(raw["immutable_identity_complete"], True)
        self.assertEqual(raw["identity_change_fields"], [])
        self.assertEqual(raw["native_before"]["title"], "Processing Request")
        self.assertEqual(raw["native_after"]["title"], CAPTION_TRANSITION_TITLE)
        self.assertEqual(raw["texts"], [
            "Processing Request", "Computing View Layout", "Elapsed time 00:05", "Cancel"])
        proof = result["document_guard_caption_transition"]
        self.assertEqual(proof["phase"], "document_progress_caption_transition_before_menu_cleanup")
        self.assertIs(proof["diagnostic_only"], True)
        self.assertEqual(proof["transition_observation"], raw)
        self.assertEqual(proof["transition_guard"], wait["last_observation"]["guard"])
        self.assertEqual(proof["retained_dialog_identity"]["title"], "Processing Request")
        self.assertEqual(proof["observation_count"], 1)
        self.assertIs(proof["complete"], True)  # Diagnostic completeness, NOT page readiness.
        for key in ("wait_applied", "wait_authorized", "resume_authorized"):
            self.assertIs(proof[key], False)
        fresh = proof["fresh_observation"]
        self.assertEqual(fresh["status"], "observed")
        self.assertEqual(fresh["native_before"]["title"], CAPTION_TRANSITION_TITLE)
        self.assertEqual(fresh["native_after"]["title"], CAPTION_TRANSITION_TITLE)
        self.assertEqual(fresh["strict_classification"]["status"], "attention")
        self.assertIs(fresh["diagnostic_only"], True)
        self.assertIs(fresh["resume_authorized"], False)
        self.assertIs(fresh["wait_authorized"], False)
        self.assertEqual(proof["deadline_ms"], wait["deadline_ms"])
        self.assertLess(proof["finished_ms"], proof["deadline_ms"])
        self.assertGreater(proof["started_ms"], first_guard["elapsed_ms"])
        self.assertEqual((wait["invoked_at_start"], wait["invoked_at_end"]), (1, 1))
        self.assertEqual(case["dialog_native_reads"], 12)
        self.assertTrue(all(event["event"] != "busy_resolved" for event in case["events"]))

    def test_caption_diagnostic_keeps_true_identity_and_protected_transitions_terminal(self):
        for scenario in ("caption_class_changed", "caption_foreign", "caption_protected"):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                result = case["result"]
                self.assertEqual((result["status"], result["cause"]),
                                 ("inaccessible", "owned_dialog_or_disabled_document")
                                 if scenario == "caption_protected"
                                 else ("error", "tableau_document_progress_unreadable"))
                self.assertEqual(case["invoke_calls"], 1)
                self.assertEqual(result["invoked"], 1)
                self.assertIsNone(result["selected"])
                self.assertEqual(result["document_guard"]["owned_dialogs"][0]["title"],
                                 "Processing Request")
                self.assertIs(result["document_guard_wait"]["resume_authorized"], False)
                raw = result["document_guard_wait"]["last_observation"]["classification"][
                    "dialogs"][0]["metadata"]
                self.assertNotIn("document_guard_caption_transition", result)
                self.assertEqual(case["dialog_native_reads"], 10)
                if scenario == "caption_protected":
                    self.assertIs(raw["strict_classification"]["protected_text"], True)
                    self.assertEqual(raw["strict_classification"]["reason"], "protected_text")
                    self.assertIs(raw["immutable_identity_complete"], True)
                else:
                    self.assertIs(raw["immutable_identity_complete"], False)

    def test_caption_follow_up_cannot_extend_shared_deadline_or_shift_capture_attempt_zero(self):
        expired_case = self.results["caption_diagnostic_expired"]
        expired = expired_case["result"]
        self.assertEqual((expired["status"], expired["cause"]),
                         ("timed_out", "tableau_document_progress_timeout"))
        proof = expired["document_guard_caption_transition"]
        self.assertEqual(proof["cause"], "caption_observation_deadline")
        self.assertIs(proof["complete"], False)
        self.assertEqual(proof["observation_count"], 1)
        self.assertEqual(proof["finished_ms"], proof["deadline_ms"])
        self.assertEqual(expired_case["elapsed_ms"], proof["deadline_ms"])
        self.assertEqual(expired_case["invoke_calls"], 1)
        self.assertIs(expired["document_guard_wait"]["resume_authorized"], False)
        self.assertEqual(proof["transition_observation"]["status"], "observed")
        self.assertIs(proof["transition_observation"]["layout_body_controls_complete"], False)
        # A regular approved-body read expiring mid-observation is bounded too;
        # it must not gain diagnostic authorization or a renewed page budget.
        regular = self.results["caption_fresh_expired"]
        self.assertEqual((regular["result"]["status"], regular["result"]["cause"]),
                         ("timed_out", "tableau_document_progress_timeout"))
        self.assertNotIn("document_guard_caption_transition", regular["result"])
        self.assertEqual(regular["elapsed_ms"],
                         regular["result"]["document_guard_wait"]["deadline_ms"])
        self.assertEqual(regular["invoke_calls"], 1)
        self.assertIs(regular["result"]["document_guard_wait"]["resume_authorized"], False)
        for scenario, status, cause in (
                ("caption_diagnostic", "error", "tableau_document_progress_unreadable"),
                ("caption_transition", "timed_out", "tableau_document_progress_timeout"),
                ("caption_protected", "inaccessible", "owned_dialog_or_disabled_document")):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                failure = self.results[scenario]["result"]
                original = copy.deepcopy(failure)
                shot = Path(directory, "page.png")
                with mock.patch.object(navigation, "select_document_tab", return_value=failure) as select, \
                        mock.patch.object(navigation, "_bounded_capture") as capture, \
                        mock.patch.object(navigation.time, "monotonic", return_value=0), \
                        mock.patch.object(navigation.time, "sleep") as sleep:
                    result = navigation.capture_document_tab(
                        11, 42, "Synthetic Page", str(shot), timeout=2)
                self.assertEqual((result["status"], result["cause"]), (status, cause))
                self.assertEqual(result["failure_phase"], "selection")
                self.assertEqual(result["selection"], original)
                self.assertIs(result["selection"], failure)
                self.assertEqual(result["selection"]["invoked"], 1)
                self.assertEqual(result["attempt_count"], 0)
                self.assertEqual(result["capture_attempts"], [])
                self.assertEqual(result["capture_errors"], [])
                self.assertIsNone(result["screenshot"])
                self.assertIsNone(result["screenshot_blank"])
                self.assertEqual(list(shot.parent.iterdir()), [])
                select.assert_called_once_with(11, 42, "Synthetic Page", timeout=2)
                capture.assert_not_called()
                sleep.assert_not_called()


class TestTableauWindowMenuEvidence(unittest.TestCase):
    def valid(self, response):
        return navigation._verified_tableau_menu(response, 11, 42, "Synthetic Page")

    def test_one_requested_target_among_six_names_with_rebuilt_item_ids_is_valid(self):
        response = _menu_verified()
        self.assertTrue(self.valid(response))
        first, last = response["menu_snapshots"]
        self.assertEqual(len(response["discovered"]), 6)
        self.assertNotEqual(first["records"][0]["raw_item_runtime_id"],
                            last["records"][0]["raw_item_runtime_id"])
        self.assertNotIn("runtime_id", response["selected"])
        self.assertEqual(response["invoked"], 0)

    def test_capture_hint_is_a_fresh_stable_identity_not_popup_or_sheet_evidence(self):
        hints = [navigation._tableau_menu_hint_from_observation(
            _menu_verified(build=build), 11, 42, "Synthetic Page") for build in (1, 2)]
        self.assertEqual(hints, [_menu_hint(), _menu_hint()])
        self.assertIsNot(hints[0], hints[1])
        first_hint = hints[0]
        if first_hint is None:
            self.fail("Valid menu observation must produce a capture hint")
        first_hint.clear()
        self.assertEqual(navigation._tableau_menu_hint_from_observation(
            _menu_verified(build=3), 11, 42, "Synthetic Page"), _menu_hint())
        for observation in (
                _verified(),
                dict(_menu_verified(), fresh_check=False),
                dict(_menu_verified(), menu_closed=False),
                dict(_menu_verified(), status="error")):
            with self.subTest(provider=observation["provider"], status=observation["status"],
                              fresh_check=observation.get("fresh_check"),
                              menu_closed=observation.get("menu_closed")):
                self.assertIsNone(navigation._tableau_menu_hint_from_observation(
                    observation, 11, 42, "Synthetic Page"))

    def test_exact_typed_document_and_menu_identity_is_required(self):
        for key, value in (
                ("provider", "uia"), ("name", "synthetic page"),
                ("checked_name", "Other View"), ("menu_automation_id", "mainMenu.other"),
                ("document_hwnd", 12), ("document_hwnd", 11.0),
                ("document_hwnd", "11"), ("document_hwnd", True),
                ("owner_pid", 99), ("owner_pid", 42.0),
                ("owner_pid", "42"), ("owner_pid", True),
                ("document_runtime_id", ""), ("document_runtime_id", " "),
                ("document_runtime_id", None),
                ("raw_item_runtime_id", "")):
            with self.subTest(key=key, value=value):
                response = _menu_verified()
                response["selected"][key] = value
                self.assertFalse(self.valid(response))

    def test_uncollapsed_unfresh_or_unqualified_provider_is_not_verified(self):
        cases = [
            ((), "menu_closed", False), ((), "menu_closed", 1),
            ((), "fresh_check", False), ((), "fresh_check", 1),
            (("selected",), "menu_closed", False),
            (("selected",), "unique_checked", False),
            (("menu_snapshots", 0), "ancestry_verified", False),
            (("menu_snapshots", 0), "popup_native_pid", 99),
            (("menu_snapshots", -1), "ancestry_verified", False),
            (("menu_snapshots", -1), "ancestry_verified", 1),
            (("menu_snapshots", -1), "popup_native_pid", 99),
            (("menu_snapshots", -1), "popup_native_pid", 42.0),
            (("menu_snapshots", -1), "popup_native_pid", "42"),
            (("menu_snapshots", -1), "popup_hwnd", 31.0),
            (("menu_snapshots", -1), "top_runtime_id", ""),
            (("menu_snapshots", -1), "checked_name", "Other View"),
        ]
        for path, key, value in cases:
            with self.subTest(path=path, key=key, value=value):
                response = _menu_verified()
                target = response
                for part in path:
                    target = target[part]
                target[key] = value
                self.assertFalse(self.valid(response))

    def test_empty_duplicate_incomplete_or_truncated_records_fail_closed(self):
        for mutation in ("empty", "duplicate", "overflow", "disabled", "submenu",
                         "not_menuitem", "unnamed", "missing_raw_id", "partial_snapshot",
                         "malformed_toggle", "untyped_selected"):
            with self.subTest(mutation=mutation):
                response = _menu_verified()
                records = response["discovered"]
                if mutation == "empty":
                    records.clear()
                elif mutation == "duplicate":
                    records.append(copy.deepcopy(records[0]))
                elif mutation == "overflow":
                    records[:] = [dict(records[0], name=f"View {i}") for i in range(257)]
                elif mutation == "partial_snapshot":
                    response["menu_snapshots"] = response["menu_snapshots"][:1]
                else:
                    key, value = {
                        "disabled": ("enabled", False), "submenu": ("leaf", False),
                        "not_menuitem": ("control_type", "ListItem"),
                        "unnamed": ("name", ""), "missing_raw_id": ("raw_item_runtime_id", ""),
                        "malformed_toggle": ("toggle", []), "untyped_selected": ("selected", 1),
                    }[mutation]
                    records[0][key] = value
                response["menu_snapshots"][-1]["records"] = copy.deepcopy(records)
                self.assertFalse(self.valid(response))

    def test_no_checked_target_two_on_or_stale_checked_target_never_passes(self):
        for state in ("none_on", "two_on", "other_on", "indeterminate"):
            with self.subTest(state=state):
                response = _menu_verified()
                records = response["discovered"]
                if state in ("none_on", "other_on"):
                    records[0].update(selected=False, toggle="Off")
                if state in ("two_on", "other_on"):
                    records[1].update(selected=True, toggle="On")
                if state == "indeterminate":
                    records[0]["toggle"] = "Indeterminate"
                response["menu_snapshots"][-1]["records"] = copy.deepcopy(records)
                self.assertFalse(self.valid(response))


class TestTableauWindowMenuRequests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.enterContext(mock.patch.object(desktop_window, "IS_WINDOWS", True))
        self.enterContext(mock.patch.object(
            navigation.time, "monotonic", side_effect=self.clock.monotonic))
        self.preflight = self.enterContext(mock.patch.object(
            navigation, "powershell_preflight", return_value={"status": "ready"}))
        self.ready = self.enterContext(mock.patch.object(
            desktop_window, "wait_for_document_ready", return_value={"status": "ready"}))
        self.owner = self.enterContext(mock.patch.object(
            desktop_window, "window_process_id", return_value=42))
        self.dialogs = self.enterContext(mock.patch.object(
            desktop_window, "dialog_windows", return_value=[]))
        self.child = self.enterContext(mock.patch.object(
            navigation, "run_accessibility_script", return_value=_menu_verified()))
        self.enterContext(mock.patch.object(
            navigation.subprocess, "Popen",
            side_effect=AssertionError("Menu regressions must not launch processes")))

    def select(self, **options):
        return navigation.select_tableau_window_menu(
            11, 42, "Synthetic Page", timeout=.9, **options)

    def test_exact_quoted_name_select_and_verify_share_one_fractional_budget(self):
        name = 'Synthetic "Étude"'

        def preflight():
            self.clock.now += .1
            return {"status": "ready"}

        def ready(*args, **kwargs):
            self.assertAlmostEqual(kwargs["timeout"], .8)
            self.clock.now += .2
            return {"status": "ready"}

        self.preflight.side_effect = preflight
        self.ready.side_effect = ready
        for verify_only, mode in ((False, "select"), (True, "verify")):
            with self.subTest(mode=mode):
                self.clock.now = 0
                self.child.return_value = _menu_verified(name)
                result = navigation.select_tableau_window_menu(
                    11, 42, name, timeout=.9, verify_only=verify_only)
                self.assertEqual(result["status"], "verified")
                script, payload, timeout = self.child.call_args.args
                self.assertEqual(script, navigation._TABLEAU_WINDOW_MENU_SCRIPT)
                self.assertNotIn(name, script)
                self.assertEqual({k: payload[k] for k in ("hwnd", "pid", "name", "mode")},
                                 {"hwnd": 11, "pid": 42, "name": name, "mode": mode})
                self.assertAlmostEqual(timeout, .6)
                self.assertAlmostEqual(payload["seconds"], .4)
                self.assertEqual(result["selected"]["name"], name)

    def test_invalid_hwnd_pid_name_and_budget_never_query_the_provider(self):
        cases = [
            (True, 42, "View", 1), (11.0, 42, "View", 1), ("11", 42, "View", 1),
            (11, True, "View", 1), (11, 42.0, "View", 1), (11, "42", "View", 1),
            (0, 42, "View", 1), (11, -1, "View", 1), (11, 42, "", 1),
            (11, 42, None, 1), (11, 42, "View", 0), (11, 42, "View", float("inf")),
        ]
        for hwnd, pid, name, timeout in cases:
            with self.subTest(hwnd=hwnd, pid=pid, name=name, timeout=timeout):
                result = navigation.select_tableau_window_menu(
                    hwnd, pid, name, timeout=timeout)
                self.assertEqual(result["cause"], "invalid_tableau_menu_request")
        self.preflight.assert_not_called()
        self.ready.assert_not_called()
        self.child.assert_not_called()

    def test_capture_hint_is_copied_into_verify_payload_with_fresh_rebuilt_item_ids(self):
        hint = _menu_hint()
        response = dict(_menu_verified(build=9), menu_hint_used=True)
        self.child.return_value = response
        result = self.select(verify_only=True, _tableau_menu_hint=hint)
        self.assertEqual(result["status"], "verified")
        self.assertIs(result, response)
        script, payload, timeout = self.child.call_args.args
        self.assertEqual(script, navigation._TABLEAU_WINDOW_MENU_SCRIPT)
        self.assertEqual(payload["mode"], "verify")
        self.assertEqual(payload["menu_hint"], _menu_hint())
        self.assertIsNot(payload["menu_hint"], hint)
        self.assertEqual(hint, _menu_hint())
        self.assertAlmostEqual(timeout, .9)
        self.assertAlmostEqual(payload["seconds"], .6)
        first, last = result["menu_snapshots"]
        self.assertNotEqual(first["popup_runtime_id"], last["popup_runtime_id"])
        self.assertNotEqual(first["records"][0]["raw_item_runtime_id"],
                            last["records"][0]["raw_item_runtime_id"])
        self.child.assert_called_once()

    def test_invalid_capture_hints_and_nonverify_mode_stop_before_tooling_or_actions(self):
        cases = [(False, _menu_hint()), (True, []), (True, {})]
        for key, value in (
                ("provider", "msaa"), ("name", "synthetic page"),
                ("menu_automation_id", "mainMenu.menu_File"),
                ("document_hwnd", 12), ("document_hwnd", "11"), ("document_hwnd", 11.0),
                ("document_hwnd", True), ("owner_pid", 99), ("owner_pid", "42"),
                ("owner_pid", 42.0), ("owner_pid", True),
                ("document_runtime_id", None), ("document_runtime_id", ""),
                ("menu_bar_runtime_id", " "), ("top_runtime_id", 1)):
            cases.append((True, dict(_menu_hint(), **{key: value})))
        for verify_only, hint in cases:
            with self.subTest(verify_only=verify_only, hint=hint):
                result = self.select(verify_only=verify_only, _tableau_menu_hint=hint)
                self.assertEqual(result, {
                    "status": "error", "cause": "invalid_tableau_menu_hint",
                    "provider": "tableau_window_menu", "retryable": False})
        self.preflight.assert_not_called()
        self.ready.assert_not_called()
        self.child.assert_not_called()

    def test_hinted_provider_must_attest_use_and_match_every_root_bar_top_snapshot(self):
        for path, key, value in (
                ((), "menu_hint_used", None), ((), "menu_hint_used", False),
                ((), "menu_hint_used", 1),
                (("selected",), "document_runtime_id", "document:other"),
                (("menu_snapshots", 0), "menu_bar_runtime_id", "bar:other"),
                (("menu_snapshots", 0), "top_runtime_id", "top:other"),
                (("menu_snapshots", -1), "menu_bar_runtime_id", "bar:other"),
                (("menu_snapshots", -1), "top_runtime_id", "top:other")):
            with self.subTest(path=path, key=key, value=value):
                self.child.reset_mock()
                response = dict(_menu_verified(build=9), menu_hint_used=True)
                target = _menu_target(response, path)
                target[key] = value
                self.child.return_value = response
                result = self.select(verify_only=True, _tableau_menu_hint=_menu_hint())
                self.assertEqual((result["status"], result["cause"]),
                                 ("error", "tableau_menu_hint_changed"))
                self.assertIs(result["retryable"], False)
                self.assertIs(result["menu_observation"], response)
                self.child.assert_called_once()

    def test_hint_never_bypasses_file_recovery_readiness_or_late_owned_dialog(self):
        recovery = {"status": "inaccessible", "cause": "owned_dialog",
                    "note": "File Recovery requires manual attention", "retryable": False}
        self.ready.return_value = recovery
        result = self.select(verify_only=True, _tableau_menu_hint=_menu_hint())
        self.assertEqual(result, {**recovery, "provider": "tableau_window_menu"})
        self.child.assert_not_called()
        self.ready.return_value = {"status": "ready"}
        self.child.return_value = dict(_menu_verified(), menu_hint_used=True)
        self.dialogs.return_value = [desktop_window.WindowInfo(
            31, "File Recovery", "SyntheticDialog", 400, 300, True)]
        result = self.select(verify_only=True, _tableau_menu_hint=_menu_hint())
        self.assertEqual((result["status"], result["cause"]), ("inaccessible", "owned_dialog"))
        self.assertIs(result["retryable"], False)
        self.assertIs(result["menu_observation"], self.child.return_value)
        self.child.assert_called_once()

    def test_missing_tooling_and_busy_document_never_run_a_menu_child(self):
        failure = {"status": "tooling_unavailable", "cause": "powershell_not_found",
                   "note": "powershell_not_found", "retryable": False}
        self.preflight.return_value = failure
        self.assertEqual(self.select(), {**failure, "provider": "tableau_window_menu"})
        self.ready.assert_not_called()
        self.child.assert_not_called()
        self.preflight.return_value = {"status": "ready"}
        self.ready.return_value = {"status": "timed_out", "note": "Synthetic busy"}
        self.assertEqual(self.select()["status"], "timed_out")
        self.child.assert_not_called()

    def test_expired_preflight_readiness_or_provider_never_restarts_the_deadline(self):
        for stage in ("preflight", "readiness", "provider"):
            with self.subTest(stage=stage):
                self.clock.now = 0
                for observed in (self.preflight, self.ready, self.child):
                    observed.reset_mock(side_effect=True)
                target = {"preflight": self.preflight, "readiness": self.ready,
                          "provider": self.child}[stage]

                def expire(*args, response=target.return_value, **kwargs):
                    self.clock.now = .9
                    return response

                target.side_effect = expire
                result = self.select()
                self.assertEqual(result["status"], "timed_out")
                self.assertEqual(result["cause"], "tableau_window_menu_timeout")
                self.assertAlmostEqual(self.clock.now, .9)
                if stage != "provider":
                    self.child.assert_not_called()
                else:
                    self.child.assert_called_once()

    def test_tooling_and_terminal_menu_failures_are_preserved_without_retry(self):
        cases = [
            ("error", "powershell_malformed_json"),
            ("timed_out", "powershell_timeout"),
            ("unsupported", "tableau_sheet_not_found"),
            ("unsupported", "tableau_dynamic_sheet_section_unavailable"),
            ("unsupported", "tableau_dynamic_sheet_section_incomplete"),
            ("unsupported", "tableau_menu_already_open_or_changed"),
            ("unsupported", "partial_provider_observation"),
            ("unsupported", "tableau_sheet_not_checked"),
            ("ambiguous", "tableau_sheet_name_ambiguous"),
            ("ambiguous", "tableau_checked_sheet_ambiguous"),
            ("error", "tableau_menu_ancestry_changed"),
            ("error", "tableau_menu_popup_ownership_or_type_changed"),
            ("error", "tableau_menu_close_unverified"),
        ]
        for status, cause in cases:
            with self.subTest(cause=cause):
                self.child.reset_mock()
                response = {"status": status, "cause": cause, "retryable": False,
                            "provider": "tableau_window_menu",
                            "menu_closed": cause != "tableau_menu_close_unverified"}
                self.child.return_value = response
                result = self.select()
                self.assertEqual((result["status"], result["cause"]), (status, cause))
                self.assertIs(result["retryable"], False)
                if cause.startswith("powershell_"):
                    self.assertIsNone(result["menu_closed"])
                else:
                    self.assertEqual(result["menu_closed"], response["menu_closed"])
                self.child.assert_called_once()

    def test_late_foreign_owner_or_owned_dialog_rejects_verified_menu_observation(self):
        self.owner.return_value = 99
        result = self.select()
        self.assertEqual(result["cause"], "window_ownership_changed")
        self.assertEqual(result["menu_observation"], self.child.return_value)
        self.owner.return_value = 42
        self.dialogs.return_value = [desktop_window.WindowInfo(
            31, "Authentication required", "Synthetic", 400, 300, True)]
        result = self.select()
        self.assertEqual(result["status"], "inaccessible")
        self.assertEqual(result["cause"], "owned_dialog")
        self.assertEqual(result["menu_observation"], self.child.return_value)


class TestNativeTabFallback(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(desktop_window, "IS_WINDOWS", True))
        self.owner = self.enterContext(mock.patch.object(
            desktop_window, "window_process_id", return_value=42))
        self.enterContext(mock.patch.object(
            navigation.time, "monotonic", return_value=0))
        self.preflight = self.enterContext(mock.patch.object(
            navigation, "powershell_preflight", return_value={"status": "ready"}))
        self.ready = self.enterContext(mock.patch.object(
            desktop_window, "wait_for_document_ready",
            return_value={"status": "ready"}))
        self.child = self.enterContext(mock.patch.object(
            navigation, "run_accessibility_script", return_value={
                "status": "inaccessible", "discovered": [], "selected": None}))
        self.native = self.enterContext(mock.patch.object(
            navigation, "select_native_tab", return_value=_verified()))
        self.menu = self.enterContext(mock.patch.object(
            navigation, "select_tableau_window_menu", return_value=_menu_verified()))

    def select(self, **kwargs):
        return navigation.select_document_tab(
            11, 42, "Synthetic Page", timeout=.5, **kwargs)

    def test_unique_native_role_evidence_is_preserved_in_select_and_verify_modes(self):
        for verify_only in (False, True):
            with self.subTest(verify_only=verify_only):
                result = self.select(verify_only=verify_only)
                self.assertEqual(result["status"], "verified")
                self.assertEqual(result["provider"], "msaa")
                self.assertEqual(result["selected"], _verified()["selected"])
                self.assertEqual(result["discovered"], _verified()["discovered"])
                self.assertEqual(result["uia_observation"]["status"], "inaccessible")
                self.native.assert_called_with(
                    11, 42, "Synthetic Page", timeout=.5,
                    verify_only=verify_only)
        self.assertTrue(all(call.args == (11,)
                            for call in self.owner.call_args_list))

    def test_duplicate_native_page_tabs_remain_ambiguous(self):
        self.native.return_value = {
            "status": "ambiguous", "provider": "msaa", "selected": None,
            "discovered": [
                _verified()["discovered"][0],
                _verified(runtime_id="11:msaa:root:0:2")["discovered"][0],
            ]}
        result = self.select()
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["selected"])
        self.assertEqual(len(result["discovered"]), 2)

    def test_uia_ambiguity_timeout_and_unnamed_list_items_are_not_bypassed(self):
        cases = [
            {"status": "ambiguous", "selected": None},
            {"status": "timed_out", "selected": None},
            {"status": "inaccessible", "selected": None,
             "native_sheet_items": [{
                 "name": "", "selected": True, "control_type": "ListItem",
                 "runtime_id": "11:uia:list:1"}]},
        ]
        for response in cases:
            with self.subTest(response=response):
                self.native.reset_mock()
                self.child.return_value = response
                result = self.select()
                self.assertEqual(result["status"], response["status"])
                self.assertIsNone(result["selected"])
                self.native.assert_not_called()

    def test_generic_listitem_or_unqualified_ribbon_is_not_page_evidence(self):
        for role, name in (("ListItem", "Synthetic Page"), ("TabItem", "Home")):
            with self.subTest(role=role):
                self.child.return_value = {
                    "status": "inaccessible", "selected": None,
                    "discovered": [{"name": name, "control_type": role,
                                    "selected": True, "runtime_id": "unqualified"}]}
                self.native.return_value = {
                    "status": "inaccessible", "selected": None,
                    "note": "No native page tab"}
                result = self.select()
                self.assertEqual(result["status"], "inaccessible")
                self.assertIsNone(result["selected"])
                self.assertNotEqual(result.get("provider"), "msaa")

    def test_claimed_verification_requires_exact_name_and_nonempty_runtime_identity(self):
        for selected in (None, {}, {"name": "Synthetic Page"},
                         {"name": "synthetic page", "runtime_id": "native:1"},
                         {"name": "Synthetic Page ", "runtime_id": "native:1"},
                         {"name": "Synthetic Page", "runtime_id": ""}):
            with self.subTest(selected=selected):
                self.native.return_value = {
                    "status": "verified", "provider": "msaa", "selected": selected}
                result = self.select()
                self.assertEqual(result["status"], "error")
                self.assertIn("exact selected tab", result["note"])

    def test_window_owner_change_rejects_otherwise_verified_native_tab(self):
        self.owner.side_effect = [42, 99]
        result = self.select()
        self.assertEqual(result["status"], "error")
        self.assertIn("ownership changed", result["note"])

    def test_busy_or_attention_document_never_selects_a_tab(self):
        for status in ("timed_out", "inaccessible", "error"):
            with self.subTest(status=status):
                self.ready.return_value = {
                    "status": status, "note": "Synthetic readiness blocker"}
                result = self.select()
                self.assertEqual(result, self.ready.return_value)
        self.child.assert_not_called()
        self.native.assert_not_called()

    def test_tool_failure_at_preflight_or_observation_never_falls_back_to_msaa(self):
        failures = [
            {"status": "tooling_unavailable", "cause": "powershell_not_found"},
            {"status": "error", "cause": "powershell_nonzero_exit"},
            {"status": "timed_out", "cause": "powershell_timeout"},
        ]
        for failure in failures:
            with self.subTest(stage="preflight", failure=failure):
                self.child.reset_mock()
                self.preflight.return_value = failure
                self.assertEqual(self.select(), failure)
                self.child.assert_not_called()
            with self.subTest(stage="observe", failure=failure):
                self.preflight.return_value = {"status": "ready"}
                self.child.return_value = failure
                self.assertEqual(self.select(), failure)
        self.native.assert_not_called()

    def test_partial_provider_tree_or_attempted_selection_cannot_use_another_provider(self):
        for metadata in ({"cause": "partial_provider_observation"},
                         {"action_attempted": True}):
            with self.subTest(metadata=metadata):
                self.child.return_value = {"status": "inaccessible", **metadata}
                result = self.select()
                self.assertEqual(result["status"], "inaccessible")
                self.native.assert_not_called()

    def test_tableau_unnamed_strip_uses_only_the_separate_qualified_named_menu(self):
        response = {
            "status": "inaccessible", "application": "tableau", "selected": None,
            "native_sheet_items": [{
                "name": "", "selected": True, "control_type": "ListItem",
                "runtime_id": "strip:unnamed:1"}]}
        self.child.return_value = response
        for verify_only in (False, True):
            with self.subTest(verify_only=verify_only):
                result = self.select(verify_only=verify_only)
                self.assertEqual(result["status"], "verified")
                self.assertEqual(result["provider"], "tableau_window_menu")
                self.assertEqual(result["uia_observation"], response)
                self.assertEqual(result["selected"]["name"], "Synthetic Page")
                self.menu.assert_called_with(
                    11, 42, "Synthetic Page", timeout=.5, verify_only=verify_only)
        self.native.assert_not_called()

    def test_menu_cannot_bypass_ambiguity_timeout_action_partial_or_unknown_application(self):
        base = {
            "status": "inaccessible", "application": "tableau", "selected": None,
            "native_sheet_items": [{"name": "", "control_type": "ListItem"}]}
        for overrides in (
                {"status": "ambiguous"}, {"status": "timed_out"},
                {"status": "error", "cause": "powershell_malformed_json"},
                {"action_attempted": True}, {"cause": "partial_provider_observation"},
                {"application": "PBIDesktop"}, {"application": ""}):
            with self.subTest(overrides=overrides):
                self.child.return_value = {**base, **overrides}
                result = self.select()
                self.assertEqual(result["status"], overrides.get("status", "inaccessible"))
                self.assertIsNone(result.get("selected"))
        self.menu.assert_not_called()
        self.native.assert_not_called()

    def test_terminal_menu_result_cannot_fall_back_to_list_or_msaa_actions(self):
        self.child.return_value = {
            "status": "inaccessible", "application": "tableau",
            "native_sheet_items": [{"name": "", "control_type": "ListItem"}]}
        for status, cause in (
                ("unsupported", "tableau_sheet_not_found"),
                ("ambiguous", "tableau_checked_sheet_ambiguous"),
                ("error", "tableau_menu_close_unverified"),
                ("timed_out", "tableau_window_menu_timeout")):
            with self.subTest(cause=cause):
                self.menu.reset_mock()
                self.menu.return_value = {
                    "status": status, "cause": cause, "provider": "tableau_window_menu"}
                result = self.select()
                self.assertEqual((result["status"], result["cause"]), (status, cause))
                self.menu.assert_called_once()
        self.native.assert_not_called()

    def test_capture_hint_routes_only_to_menu_even_when_hint_verification_fails(self):
        for hint, cause in (
                (_menu_hint(), "tableau_menu_hint_changed"),
                (dict(_menu_hint(), top_runtime_id=""), "invalid_tableau_menu_hint")):
            with self.subTest(cause=cause):
                self.menu.reset_mock()
                failure = {"status": "error", "cause": cause,
                           "provider": "tableau_window_menu", "retryable": False}
                self.menu.return_value = failure
                result = self.select(verify_only=True, _tableau_menu_hint=hint)
                self.assertIs(result, failure)
                self.menu.assert_called_once_with(
                    11, 42, "Synthetic Page", timeout=.5, verify_only=True,
                    _tableau_menu_hint=hint)
        self.child.assert_not_called()
        self.native.assert_not_called()


class TestNativePageCaptureIdentity(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.shot = Path(temporary, "page.png")
        self.clock = _Clock()
        self.enterContext(mock.patch.object(
            navigation.time, "sleep", side_effect=self.clock.sleep))
        self.enterContext(mock.patch.object(
            navigation.time, "monotonic", side_effect=self.clock.monotonic))
        self.owner = self.enterContext(mock.patch.object(
            desktop_window, "window_process_id", return_value=42))
        self.select = self.enterContext(mock.patch.object(
            navigation, "select_document_tab"))
        self.capture = self.enterContext(mock.patch.object(
            navigation, "_bounded_capture", side_effect=self.save))

    def save(self, hwnd, pid, path, timeout):
        Path(path).write_bytes(NATIVE_PNG)
        return {"status": "captured", "screenshot_blank": False}

    def test_initial_guard_failure_preserves_selection_diagnostics_before_attempt_zero(self):
        progress_guard = _document_guard(reason="owned_dialog", enabled=True, dialogs=[
            dict(_owned_guard_dialog(), title="Processing Request")])
        proof = {
            "phase": "document_guard_before_menu_cleanup", "basis": "shared_strict_uia",
            "progress_required": True, "wait_applied": False, "resume_authorized": False,
            "terminal_status": "inaccessible", "terminal_cause": "owned_dialog_or_disabled_document",
            "guard_elapsed_ms": 125, "started_ms": 125, "finished_ms": 125, "elapsed_ms": 0,
            "complete": True, "document_identity_after": True, "menu_identity_after": True,
            "dialogs": [{"hwnd": 31, "status": "observed", "busy_conclusive": True,
                         "metadata": strict_dialog_metadata()}],
        }
        for guard, classification, invoked in (
                (_document_guard(visible=False), None, 0),
                (_document_guard(reason="owned_dialog", enabled=True,
                                 dialogs=[_owned_guard_dialog()]), None, 0),
                (progress_guard, proof, 1)):
            with self.subTest(reason=guard["reason"]):
                self.select.reset_mock()
                failure = {
                    "status": "inaccessible", "cause": "owned_dialog_or_disabled_document",
                    "provider": "tableau_window_menu", "retryable": False,
                    "selected": None, "action_attempted": bool(invoked), "invoked": invoked,
                    "document_guard": guard,
                    "failure_details": {"operation": "document_dialogs", "guard_stop": True,
                                        "top_menu_name": "Window", "exception_type": "System.Exception",
                                        "inner_exception_type": None},
                }
                if classification is not None:
                    failure["document_guard_classification"] = classification
                original = copy.deepcopy(failure)
                self.select.return_value = failure
                result = navigation.capture_document_tab(
                    11, 42, "Synthetic Page", str(self.shot), timeout=2)
                self.assertEqual((result["status"], result["cause"]),
                                 ("inaccessible", "owned_dialog_or_disabled_document"))
                self.assertEqual(result["failure_phase"], "selection")
                self.assertIs(result["selection"], failure)
                self.assertEqual(result["selection"], original)
                self.assertEqual(result["selection"]["invoked"], invoked)
                if classification is not None:
                    self.assertIs(result["selection"]["document_guard_classification"], classification)
                    self.assertIs(classification["wait_applied"], False)
                    self.assertIs(classification["resume_authorized"], False)
                self.assertEqual(result["attempt_count"], 0)
                self.assertEqual(result["capture_attempts"], [])
                self.assertEqual(result["capture_errors"], [])
                self.assertNotIn("pre_capture_selection", result)
                self.assertNotIn("post_capture_selection", result)
                self.assertIsNone(result["screenshot"])
                self.assertIsNone(result["screenshot_blank"])
                self.select.assert_called_once_with(11, 42, "Synthetic Page", timeout=2)
                self.capture.assert_not_called()
                self.owner.assert_not_called()
                self.assertEqual(self.clock.sleeps, [])
                self.assertEqual(list(self.shot.parent.iterdir()), [])

    def test_fresh_image_with_same_native_identity_before_and_after_is_evidence(self):
        self.select.side_effect = [_verified(), _verified(), _verified()]
        result = navigation.capture_document_tab(
            11, 42, "Synthetic Page", str(self.shot), timeout=5)
        self.assertEqual(result["status"], "captured")
        self.assertEqual(result["screenshot"], str(self.shot))
        self.assertFalse(result["screenshot_blank"])
        self.assertTrue(self.shot.is_file())
        self.select.assert_has_calls([
            mock.call(11, 42, "Synthetic Page", timeout=5),
            mock.call(11, 42, "Synthetic Page", timeout=4.5, verify_only=True),
            mock.call(11, 42, "Synthetic Page", timeout=4.5, verify_only=True),
        ])
        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(list(self.shot.parent.iterdir()), [self.shot])

    def test_rebuilt_menu_item_ids_preserve_exact_document_and_checked_page_identity(self):
        observations = [_menu_verified(build=build) for build in range(3)]
        observations[0].update(
            invoked=1, action_attempted=True,
            document_guard=_document_guard(reason="document_disabled_and_owned_dialog", dialogs=[
                dict(_owned_guard_dialog(), title="Processing Request")]),
            document_guard_wait={
                "phase": "document_guard_wait", "status": "ready",
                "wait_applied": True, "resume_authorized": True,
                "invoked_at_start": 1, "invoked_at_end": 1},
        )
        self.select.side_effect = observations

        def capture(hwnd, pid, path, timeout):
            self.assertEqual(self.select.call_count, 2)
            self.assertTrue(all(item["menu_closed"] and item["fresh_check"]
                                for item in observations[:2]))
            return self.save(hwnd, pid, path, timeout)

        self.capture.side_effect = capture
        result = navigation.capture_document_tab(
            11, 42, "Synthetic Page", str(self.shot), timeout=2)
        self.assertEqual(result["status"], "captured")
        self.assertEqual(self.shot.read_bytes(), NATIVE_PNG)
        self.assertEqual(result["selection"], observations[0])
        self.assertEqual(result["selection"]["document_guard_wait"]["status"], "ready")
        self.assertEqual(result["selection"]["invoked"], 1)
        self.assertEqual(result["pre_capture_selection"], observations[1])
        self.assertEqual(result["capture_attempts"][0]["pre_capture_selection"],
                         observations[1])
        self.assertEqual(result["post_capture_selection"], observations[2])
        self.assertEqual(len({
            item["selected"]["raw_item_runtime_id"] for item in observations}), 3)
        self.assertTrue(all(call.kwargs.get("verify_only")
                            for call in self.select.call_args_list[1:]))
        self.assertEqual(self.select.call_args_list, [
            mock.call(11, 42, "Synthetic Page", timeout=2),
            mock.call(11, 42, "Synthetic Page", timeout=1.5, verify_only=True,
                      _tableau_menu_hint=_menu_hint()),
            mock.call(11, 42, "Synthetic Page", timeout=1.5, verify_only=True,
                      _tableau_menu_hint=_menu_hint()),
        ])
        self.assertEqual(result["attempt_count"], 1)

    def test_pre_or_post_menu_page_or_document_change_rejects_and_cleans_the_candidate(self):
        for stage in (1, 2):
            for scope, key, value in (
                    (("selected",), "document_runtime_id", "document:other"),
                    (("selected",), "checked_name", "Other View"),
                    (("selected",), "owner_pid", 99),
                    (("selected",), "menu_automation_id", "mainMenu.other"),
                    (("menu_snapshots", -1), "menu_bar_runtime_id", "bar:other"),
                    (("menu_snapshots", -1), "top_runtime_id", "top:other"),
                    ((), "fresh_check", False), ((), "menu_closed", False)):
                with self.subTest(stage=stage, scope=scope, key=key):
                    self.clock.now = 0
                    self.shot.unlink(missing_ok=True)
                    self.capture.reset_mock()
                    observations = [_menu_verified(build=build) for build in range(3)]
                    changed = observations[stage]
                    target = _menu_target(changed, scope)
                    target[key] = value
                    self.select.side_effect = observations
                    result = navigation.capture_document_tab(
                        11, 42, "Synthetic Page", str(self.shot), timeout=2)
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["cause"], "tab_selection_changed")
                    phase = "pre_capture_selection" if stage == 1 else "post_capture_selection"
                    self.assertEqual(result["failure_phase"], phase)
                    self.assertIs(result["selection"], observations[0])
                    self.assertIs(result[phase], changed)
                    self.assertEqual(result["attempt_count"], stage - 1)
                    self.assertEqual(len(result["capture_attempts"]), stage - 1)
                    self.assertEqual(result["capture_errors"], [])
                    self.assertIsNone(result["screenshot"])
                    self.assertEqual(self.capture.call_count, stage - 1)
                    self.assertEqual(list(self.shot.parent.iterdir()), [])

    def test_failed_pre_menu_query_preserves_raw_provenance_before_attempt_zero(self):
        for status, cause, operation in (
                ("error", "tableau_menu_hint_changed", "menu_hint_top"),
                ("error", "tableau_menu_close_unverified", "menu_close_verify"),
                ("tooling_unavailable", "powershell_not_found", "preflight"),
                ("timed_out", "powershell_timeout", "menu_expand"),
                ("inaccessible", "owned_dialog_or_disabled_document", "document_dialogs")):
            with self.subTest(cause=cause):
                self.clock.now = 0
                self.select.reset_mock()
                initial = _menu_verified(build=1)
                failure = {
                    "status": status, "cause": cause, "provider": "tableau_window_menu",
                    "retryable": False, "menu_closed": False,
                    "action_attempted": False, "invoked": 0,
                    "failure_details": {
                        "operation": operation, "guard_stop": True, "top_menu_name": "Window",
                        "exception_type": "System.Exception", "inner_exception_type": None},
                }
                if cause == "tableau_menu_close_unverified":
                    failure.update(
                        cleanup_cause=cause,
                        cleanup_failure_details=copy.deepcopy(failure["failure_details"]),
                        last_menu_close_observation={
                            "top_menu_name": "Window", "popup_hwnd": 31,
                            "menu_automation_id": "mainMenu.menu_Window",
                            "popup_native_state": "visible", "stable_closed_ms": 0})
                if status == "inaccessible":
                    failure["dialog_observation"] = {
                        "title": "File Recovery", "explicit_attention": True}
                self.select.side_effect = [initial, failure]
                result = navigation.capture_document_tab(
                    11, 42, "Synthetic Page", str(self.shot), timeout=2)
                self.assertEqual((result["status"], result["cause"]), (status, cause))
                self.assertEqual(result["failure_phase"], "pre_capture_selection")
                self.assertIs(result["selection"], initial)
                self.assertIs(result["pre_capture_selection"], failure)
                self.assertNotIn("post_capture_selection", result)
                self.assertEqual(result["attempt_count"], 0)
                self.assertEqual(result["capture_attempts"], [])
                self.assertEqual(result["capture_errors"], [])
                self.assertIsNone(result["screenshot"])
                self.assertIsNone(result["screenshot_blank"])
                self.assertEqual(self.select.call_args_list, [
                    mock.call(11, 42, "Synthetic Page", timeout=2),
                    mock.call(11, 42, "Synthetic Page", timeout=1.5, verify_only=True,
                              _tableau_menu_hint=_menu_hint()),
                ])
                self.capture.assert_not_called()
                self.assertEqual(list(self.shot.parent.iterdir()), [])

    def test_verified_pre_observation_is_preserved_when_its_query_exhausts_the_deadline(self):
        initial, pre = _menu_verified(build=1), _menu_verified(build=2)

        def select(*args, **kwargs):
            if kwargs.get("verify_only"):
                self.clock.now = 2
                return pre
            return initial

        self.select.side_effect = select
        result = navigation.capture_document_tab(
            11, 42, "Synthetic Page", str(self.shot), timeout=2)
        self.assertEqual((result["status"], result["cause"]), ("timed_out", "capture_timeout"))
        self.assertEqual(result["failure_phase"], "pre_capture_selection")
        self.assertIs(result["selection"], initial)
        self.assertIs(result["pre_capture_selection"], pre)
        self.assertNotIn("post_capture_selection", result)
        self.assertEqual(result["attempt_count"], 0)
        self.assertEqual(result["capture_attempts"], [])
        self.assertEqual(result["capture_errors"], [])
        self.assertEqual(result["duration_s"], 2)
        self.assertIsNone(result["screenshot"])
        self.capture.assert_not_called()
        self.assertEqual(self.select.call_count, 2)
        self.assertEqual(list(self.shot.parent.iterdir()), [])

    def test_capture_hint_is_never_cached_or_reused_for_another_page(self):
        for index, name in enumerate(("First Synthetic Page", "Second Synthetic Page")):
            with self.subTest(name=name):
                self.clock.now = 0
                self.shot.unlink(missing_ok=True)
                self.select.reset_mock()
                observations = [_menu_verified(name, build=build) for build in range(3)]
                hint = _menu_hint(name)
                if index:
                    hint.update(document_runtime_id="document:new",
                                menu_bar_runtime_id="bar:new", top_runtime_id="top:new")
                    for observation in observations:
                        observation["selected"]["document_runtime_id"] = "document:new"
                        for snapshot in observation["menu_snapshots"]:
                            snapshot.update(menu_bar_runtime_id="bar:new", top_runtime_id="top:new")
                self.select.side_effect = observations
                result = navigation.capture_document_tab(
                    11, 42, name, str(self.shot), timeout=2)
                self.assertEqual(result["status"], "captured")
                self.assertEqual(self.shot.read_bytes(), NATIVE_PNG)
                self.assertEqual(self.select.call_args_list, [
                    mock.call(11, 42, name, timeout=2),
                    mock.call(11, 42, name, timeout=1.5, verify_only=True,
                              _tableau_menu_hint=hint),
                    mock.call(11, 42, name, timeout=1.5, verify_only=True,
                              _tableau_menu_hint=hint),
                ])

    def test_unfresh_uncollapsed_or_two_on_initial_menu_never_starts_capture(self):
        for state in ("unfresh", "uncollapsed", "two_on"):
            with self.subTest(state=state):
                self.clock.now = 0
                observation = _menu_verified()
                if state == "two_on":
                    observation["discovered"][1].update(selected=True, toggle="On")
                    observation["menu_snapshots"][-1]["records"] = copy.deepcopy(
                        observation["discovered"])
                else:
                    observation["fresh_check" if state == "unfresh" else "menu_closed"] = False
                self.select.return_value = observation
                result = navigation.capture_document_tab(
                    11, 42, "Synthetic Page", str(self.shot), timeout=2)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["cause"], "selection_evidence_invalid")
                self.assertEqual(result["attempt_count"], 0)
                self.assertIsNone(result["screenshot"])
                self.assertEqual(list(self.shot.parent.iterdir()), [])
        self.capture.assert_not_called()

    def test_failed_menu_collapse_after_capture_preserves_raw_failure_without_publishing(self):
        failure = {
            "status": "error", "cause": "tableau_menu_close_unverified",
            "provider": "tableau_window_menu", "menu_closed": False,
            "cleanup_cause": "tableau_menu_close_unverified", "retryable": False}
        self.select.side_effect = [_menu_verified(build=1), _menu_verified(build=2), failure]
        result = navigation.capture_document_tab(
            11, 42, "Synthetic Page", str(self.shot), timeout=2)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["cause"], "tableau_menu_close_unverified")
        self.assertEqual(result["post_capture_selection"], failure)
        self.assertEqual(result["failure_phase"], "post_capture_selection")
        self.assertEqual(result["pre_capture_selection"], _menu_verified(build=2))
        self.assertIs(result["capture_attempts"][0]["post_capture_selection"], failure)
        self.assertEqual(result["attempt_count"], 1)
        self.capture.assert_called_once()
        self.assertIsNone(result["screenshot"])
        self.assertEqual(list(self.shot.parent.iterdir()), [])

    def test_menu_retries_keep_one_local_hint_but_fresh_pre_post_observations(self):
        observations = [_menu_verified(build=build) for build in range(5)]
        self.select.side_effect = observations
        candidates, budgets = [], []

        def capture(hwnd, pid, path, timeout):
            candidates.append(Path(path))
            budgets.append(timeout)
            self.clock.now += .1
            if len(candidates) == 1:
                Path(path).write_bytes(b"synthetic partial frame")
                return {"status": "error", "cause": "capture_invalid", "retryable": True}
            return self.save(hwnd, pid, path, timeout)

        self.capture.side_effect = capture
        result = navigation.capture_document_tab(
            11, 42, "Synthetic Page", str(self.shot), timeout=2)
        self.assertEqual(result["status"], "captured")
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["selection"], observations[0])
        self.assertEqual(result["pre_capture_selection"], observations[3])
        self.assertEqual(result["post_capture_selection"], observations[4])
        for index, attempt in enumerate(result["capture_attempts"]):
            self.assertEqual(attempt["pre_capture_selection"], observations[1 + 2 * index])
            self.assertEqual(attempt["post_capture_selection"], observations[2 + 2 * index])
        self.assertEqual(self.select.call_count, 5)
        self.assertNotIn("_tableau_menu_hint", self.select.call_args_list[0].kwargs)
        for call in self.select.call_args_list[1:]:
            self.assertTrue(call.kwargs["verify_only"])
            self.assertEqual(call.kwargs["_tableau_menu_hint"], _menu_hint())
        self.assertEqual([error["cause"] for error in result["capture_errors"]], ["capture_invalid"])
        self.assertLess(budgets[1], budgets[0])
        self.assertEqual(len(set(candidates)), 2)
        self.assertTrue(all(not path.exists() for path in candidates))
        self.assertEqual(self.shot.read_bytes(), NATIVE_PNG)
        self.assertEqual(list(self.shot.parent.iterdir()), [self.shot])

    def test_name_or_native_identity_change_during_capture_is_not_page_proof(self):
        for key, value in (("name", "Other Page"), ("runtime_id", "native:other"),
                           ("automation_id", "different"), ("provider", "uia")):
            with self.subTest(key=key):
                self.clock.now = 0
                self.shot.unlink(missing_ok=True)
                after = copy.deepcopy(_verified())
                after["selected"][key] = value
                self.select.side_effect = [_verified(), _verified(), after]
                result = navigation.capture_document_tab(
                    11, 42, "Synthetic Page", str(self.shot), timeout=5)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["cause"], "tab_selection_changed")
                self.assertIsNone(result["screenshot"])
                self.assertFalse(self.shot.exists())
                self.assertEqual(list(self.shot.parent.iterdir()), [])
                self.assertEqual(result["post_capture_selection"], after)

    def test_ambiguous_or_inaccessible_native_selection_never_captures(self):
        for status in ("ambiguous", "inaccessible", "timed_out", "error"):
            with self.subTest(status=status):
                self.select.side_effect = None
                self.select.return_value = {"status": status, "selected": None}
                result = navigation.capture_document_tab(
                    11, 42, "Synthetic Page", str(self.shot), timeout=5)
                self.assertEqual(result["status"], status)
                self.assertIsNone(result["screenshot"])
                self.assertFalse(self.shot.exists())
        self.capture.assert_not_called()

    def test_blank_then_valid_retries_fresh_destinations_with_one_shrinking_budget(self):
        self.select.return_value = _verified()
        destinations, budgets = [], []

        def capture(hwnd, pid, path, timeout):
            self.assertEqual((hwnd, pid), (11, 42))
            destinations.append(Path(path))
            budgets.append(timeout)
            self.clock.now += .1
            if len(destinations) == 1:
                Path(path).write_bytes(tiny_png(((9, 9, 9),), channels=3))
                return {"status": "error", "cause": "capture_blank",
                        "retryable": True, "screenshot_blank": True}
            Path(path).write_bytes(NATIVE_PNG)
            return {"status": "captured", "screenshot_blank": False}

        self.capture.side_effect = capture
        result = navigation.capture_document_tab(
            11, 42, "Synthetic Page", str(self.shot), timeout=2)
        self.assertEqual(result["status"], "captured")
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["capture_errors"][0]["cause"], "capture_blank")
        self.assertEqual(len(result["capture_errors"]), 1)
        self.assertFalse(result["screenshot_blank"])
        self.assertEqual(self.shot.read_bytes(), NATIVE_PNG)
        self.assertEqual(len(set(destinations)), 2)
        self.assertTrue(all(path != self.shot and not path.exists()
                            for path in destinations))
        self.assertEqual(list(self.shot.parent.iterdir()), [self.shot])
        self.assertLess(budgets[1], budgets[0])
        self.assertTrue(all(0 < budget <= 2 for budget in budgets))
        self.assertEqual(self.select.call_count, 5)
        self.assertTrue(all(call.kwargs.get("verify_only")
                            for call in self.select.call_args_list[1:]))

    def test_repeated_partial_or_blank_frames_exhaust_the_original_deadline_and_cleanup(self):
        for cause in ("capture_blank", "capture_invalid", "capture_failed"):
            with self.subTest(cause=cause):
                self.clock.now = 0
                self.select.return_value = _verified()
                candidates = []

                def capture(hwnd, pid, path, timeout):
                    candidates.append(Path(path))
                    Path(path).write_bytes(b"synthetic partial frame")
                    self.clock.now += .1
                    return {"status": "error", "cause": cause, "retryable": True}

                self.capture.side_effect = capture
                result = navigation.capture_document_tab(
                    11, 42, "Synthetic Page", str(self.shot), timeout=1)
                self.assertEqual(result["status"], "timed_out")
                self.assertEqual(result["cause"], "capture_timeout")
                self.assertEqual(result["attempt_count"], 2)
                self.assertEqual(len(result["capture_errors"]), 2)
                self.assertAlmostEqual(result["duration_s"], 1)
                self.assertFalse(self.shot.exists())
                self.assertTrue(all(not path.exists() for path in candidates))
                self.assertEqual(list(self.shot.parent.iterdir()), [])

    def test_terminal_tool_dialog_or_dimension_failures_do_not_retry(self):
        for status, cause in (
                ("tooling_unavailable", "powershell_not_found"),
                ("error", "powershell_malformed_json"),
                ("inaccessible", "owned_dialog"),
                ("error", "capture_dimensions_changed"),
                ("timed_out", "capture_timeout")):
            with self.subTest(cause=cause):
                self.clock.now = 0
                self.select.reset_mock()
                self.select.return_value = _verified()
                self.capture.reset_mock()
                self.capture.side_effect = None
                self.capture.return_value = {
                    "status": status, "cause": cause, "retryable": False}
                result = navigation.capture_document_tab(
                    11, 42, "Synthetic Page", str(self.shot), timeout=2)
                self.assertEqual((result["status"], result["cause"]), (status, cause))
                self.assertEqual(result["attempt_count"], 1)
                self.capture.assert_called_once()
                self.assertEqual(self.select.call_count, 2)
                self.assertFalse(self.shot.exists())
                self.assertEqual(list(self.shot.parent.iterdir()), [])

    def test_changed_owner_after_a_partial_frame_does_not_retry_or_publish(self):
        self.select.return_value = _verified()
        self.owner.side_effect = [42, 99]

        def partial(hwnd, pid, path, timeout):
            Path(path).write_bytes(b"synthetic partial")
            return {"status": "error", "cause": "capture_invalid", "retryable": True}

        self.capture.side_effect = partial
        result = navigation.capture_document_tab(
            11, 42, "Synthetic Page", str(self.shot), timeout=2)
        self.assertEqual(result["cause"], "window_ownership_changed")
        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(self.select.call_count, 2)
        self.assertFalse(self.shot.exists())
        self.assertEqual(list(self.shot.parent.iterdir()), [])

    def test_even_a_claimed_success_requires_a_real_nonblank_complete_png(self):
        self.select.return_value = _verified()
        attempts = 0

        def capture(hwnd, pid, path, timeout):
            nonlocal attempts
            attempts += 1
            Path(path).write_bytes(b"not PNG" if attempts == 1 else NATIVE_PNG)
            return {"status": "captured", "screenshot_blank": False}

        self.capture.side_effect = capture
        result = navigation.capture_document_tab(
            11, 42, "Synthetic Page", str(self.shot), timeout=2)
        self.assertEqual(result["status"], "captured")
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["capture_errors"][0]["cause"], "capture_invalid")
        self.assertEqual(self.shot.read_bytes(), NATIVE_PNG)

    def test_deadline_expiring_during_publication_retracts_the_final_and_temporary_png(self):
        self.select.return_value = _verified()
        original_open = Path.open
        clock = self.clock
        destination = self.shot

        class LateWrite:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def write(self, data):
                written = self.handle.write(data)
                clock.now = 2
                return written

            def __exit__(self, *args):
                self.handle.close()

        def open_file(path, mode="r", *args, **kwargs):
            handle = original_open(path, mode, *args, **kwargs)
            return LateWrite(handle) if path == destination and mode == "xb" else handle

        with mock.patch.object(Path, "open", autospec=True, side_effect=open_file):
            result = navigation.capture_document_tab(
                11, 42, "Synthetic Page", str(self.shot), timeout=2)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["cause"], "capture_timeout")
        self.assertEqual(result["attempt_count"], 1)
        self.assertIsNone(result["screenshot"])
        self.assertFalse(self.shot.exists())
        self.assertEqual(list(self.shot.parent.iterdir()), [])

    def test_destination_created_during_capture_is_neither_overwritten_nor_deleted(self):
        self.select.return_value = _verified()
        competing = b"Synthetic independent evidence"

        def capture(hwnd, pid, path, timeout):
            Path(path).write_bytes(NATIVE_PNG)
            self.shot.write_bytes(competing)
            return {"status": "captured", "screenshot_blank": False}

        self.capture.side_effect = capture
        result = navigation.capture_document_tab(
            11, 42, "Synthetic Page", str(self.shot), timeout=2)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["cause"], "capture_operation_failed")
        self.assertIsNone(result["screenshot"])
        self.assertEqual(self.shot.read_bytes(), competing)
        self.assertEqual(list(self.shot.parent.iterdir()), [self.shot])


class TestBoundedCaptureChild(unittest.TestCase):
    def test_tool_protocol_and_timeout_failures_are_terminal_redacted_results(self):
        with mock.patch.object(desktop_window, "wait_for_document_ready",
                               return_value={"status": "ready"}), \
                mock.patch.object(navigation.time, "monotonic", return_value=0), \
                mock.patch.object(navigation.subprocess, "run") as child:
            cases = [
                (subprocess.TimeoutExpired("synthetic", .05), None,
                 "timed_out", "capture_timeout"),
                (None, mock.Mock(returncode=9, stdout="", stderr="Synthetic body"),
                 "error", "capture_process_failed"),
                (None, mock.Mock(returncode=0, stdout="[]", stderr=""),
                 "error", "capture_protocol_failed"),
            ]
            for exception, response, status, cause in cases:
                with self.subTest(cause=cause):
                    child.side_effect = exception
                    child.return_value = response
                    result = navigation._bounded_capture(11, 42, Path("synthetic.png"), .05)
                    self.assertEqual((result["status"], result["cause"]), (status, cause))
                    self.assertIs(result["retryable"], False)
                    self.assertNotIn("Synthetic body", json.dumps(result))
                    self.assertEqual(child.call_args.kwargs["timeout"], .05)


class TestPageHostQualificationScript(unittest.TestCase):
    def test_powerbi_tabs_require_the_report_host_not_the_ribbon(self):
        source = navigation._SCRIPT
        self.assertIn("$processName -eq 'PBIDesktop'", source)
        self.assertIn("$e.Current.ClassName -ne 'carouselScrollPane'", source)
        self.assertIn("$p.Current.AutomationId -eq 'desktopExplorationHost'", source)
        self.assertIn("if (IsContainer $p) { return $true }", source)
        self.assertIn("$e.Current.ControlType -eq $T::TabItem -and -not $isPBI",
                      source)
        self.assertIn("$result.native_sheet_items = $sheetItems", source)
        self.assertIn("$_.Current.Name -ceq [string]$q.name", source)


if __name__ == "__main__":
    unittest.main()
