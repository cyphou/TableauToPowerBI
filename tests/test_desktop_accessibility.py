"""Native accessibility contracts; no COM provider or Desktop app is launched."""

import base64
import json
import os
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
    PROGRESS_CONTROL,
    inert_dialog_observer_script,
    layout_caption_transition_case,
    layout_dialog_case,
    layout_dialog_metadata,
    retained_layout_identity,
    strict_dialog_metadata,
)


class TestPowerShellDiscovery(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(root)
        self.windows = self.root / "SyntheticWindows"
        self.enterContext(mock.patch.object(accessibility.sys, "platform", "win32"))
        self.enterContext(mock.patch.dict(
            os.environ, {"PATH": str(self.root / "reduced"),
                         "SystemRoot": str(self.windows),
                         "WINDIR": str(self.windows)}, clear=True))
        self.enterContext(mock.patch.object(
            accessibility, "_windows_system_directories", return_value=[]))
        self.which = self.enterContext(mock.patch.object(
            accessibility.shutil, "which", return_value=None))
        self.child = self.enterContext(mock.patch.object(
            accessibility.subprocess, "run",
            side_effect=AssertionError("Discovery must not launch a tool")))

    def executable(self, directory):
        path = directory / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic placeholder, never executed")
        return path

    def test_reduced_path_still_finds_installed_windows_powershell(self):
        executable = self.executable(self.windows / "System32")
        self.assertEqual(accessibility.resolve_powershell_executable(),
                         str(executable.resolve()))
        self.which.assert_not_called()
        self.assertEqual(accessibility.powershell_preflight(),
                         {"status": "ready", "executable": str(executable.resolve())})
        self.child.assert_not_called()

    def test_system_directory_api_precedes_environment_and_path_guesses(self):
        system = self.root / "NativeSystem"
        executable = self.executable(system)
        self.executable(self.windows / "System32")
        with mock.patch.object(accessibility, "_windows_system_directories",
                               return_value=[system]):
            self.assertEqual(accessibility.resolve_powershell_executable(),
                             str(executable.resolve()))
        self.which.assert_not_called()

    def test_sysnative_alias_is_kept_for_a_32_bit_launcher(self):
        executable = self.executable(self.windows / "Sysnative")
        self.executable(self.windows / "System32")
        self.assertEqual(accessibility.resolve_powershell_executable(),
                         str(executable.absolute()))
        resolved = accessibility.resolve_powershell_executable()
        if resolved is None:
            self.fail("Installed Sysnative executable must be resolved")
        self.assertIn("Sysnative", resolved)

    def test_absence_is_explicit_and_never_substitutes_pwsh(self):
        with mock.patch.object(Path, "is_file", return_value=False):
            self.assertIsNone(accessibility.resolve_powershell_executable())
            result = accessibility.powershell_preflight()
        self.assertEqual(result, {
            "status": "tooling_unavailable", "cause": "powershell_not_found",
            "note": "powershell_not_found", "retryable": False})
        self.assertTrue(all(call.args == ("powershell.exe",)
                            for call in self.which.call_args_list))
        self.child.assert_not_called()


class TestPowerShellJsonRunner(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(
            accessibility.time, "monotonic", return_value=0))
        self.preflight = self.enterContext(mock.patch.object(
            accessibility, "powershell_preflight",
            return_value={"status": "ready", "executable": "SyntheticPowerShell.exe"}))
        self.child = self.enterContext(mock.patch.object(
            accessibility.subprocess, "run", return_value=mock.Mock(
                returncode=0, stdout='{"status":"unavailable","note":"provider absent"}',
                stderr="Synthetic stderr must never be reported")))

    def run_script(self, **kwargs):
        return accessibility.run_powershell_json(
            "$q = [Console]::In.ReadToEnd()", {"name": 'Synthetic "Étude"'},
            timeout=.125, **kwargs)

    def test_utf8_json_and_encoded_script_use_stdin_with_no_timeout_floor(self):
        for encoded_input in (False, True):
            with self.subTest(base64_input=encoded_input):
                result = self.run_script(base64_input=encoded_input)
                self.assertEqual(result, {"status": "unavailable", "note": "provider absent"})
                self.assertFalse(accessibility.is_powershell_failure(result))
                args, options = self.child.call_args
                self.assertEqual(args[0][:4], [
                    "SyntheticPowerShell.exe", "-NoProfile", "-NonInteractive",
                    "-EncodedCommand"])
                script = base64.b64decode(args[0][4]).decode("utf-16le")
                self.assertIn("[Console]::In.ReadToEnd()", script)
                self.assertNotIn("Étude", script)
                request = options["input"]
                if encoded_input:
                    request = base64.b64decode(request).decode("utf-8")
                self.assertEqual(json.loads(request), {"name": 'Synthetic "Étude"'})
                self.assertEqual(options["timeout"], .125)
                self.assertEqual(options["encoding"], "utf-8")
                self.assertNotIn("shell", options)

    def test_tooling_and_protocol_failures_are_distinct_fixed_redacted_causes(self):
        cases = [
            (FileNotFoundError("Synthetic details"), None,
             "tooling_unavailable", "powershell_launch_failed"),
            (PermissionError("Synthetic details"), None,
             "tooling_unavailable", "powershell_launch_failed"),
            (subprocess.TimeoutExpired("Synthetic command", .125,
                                       output="Synthetic body", stderr="Synthetic stderr"),
             None, "timed_out", "powershell_timeout"),
            (None, mock.Mock(returncode=7, stdout="Synthetic body",
                             stderr="Synthetic stderr"),
             "error", "powershell_nonzero_exit"),
            (None, mock.Mock(returncode=0, stdout="Synthetic body",
                             stderr="Synthetic stderr"),
             "error", "powershell_malformed_json"),
            (None, mock.Mock(returncode=0, stdout="[]", stderr="Synthetic stderr"),
             "error", "powershell_malformed_json"),
        ]
        for exception, response, status, cause in cases:
            with self.subTest(cause=cause, exception=type(exception).__name__):
                self.child.side_effect = exception
                self.child.return_value = response
                result = self.run_script()
                self.assertEqual(result["status"], status)
                self.assertEqual(result["cause"], cause)
                self.assertEqual(result["note"], cause)
                self.assertIs(result["retryable"], False)
                self.assertTrue(accessibility.is_powershell_failure(result))
                self.assertTrue(set(result) <= {
                    "status", "cause", "note", "retryable", "returncode"})
                serialized = json.dumps(result)
                for forbidden in ("Synthetic", "stderr", "ReadToEnd", "Étude"):
                    self.assertNotIn(forbidden, serialized)

    def test_absent_preflight_never_creates_a_child(self):
        self.preflight.return_value = {
            "status": "tooling_unavailable", "cause": "powershell_not_found",
            "note": "powershell_not_found", "retryable": False}
        self.assertEqual(self.run_script(), self.preflight.return_value)
        self.child.assert_not_called()

    def test_invalid_budget_or_exhausted_discovery_never_creates_a_child(self):
        for timeout in (0, -1, float("nan"), float("inf"), None):
            with self.subTest(timeout=timeout):
                result = accessibility.run_powershell_json("synthetic", {}, timeout=timeout)
                self.assertEqual(result["cause"], "invalid_powershell_request")
        with mock.patch.object(accessibility.time, "monotonic",
                               side_effect=[0, .125]):
            self.assertEqual(self.run_script()["cause"], "powershell_timeout")
        self.child.assert_not_called()

    def test_bom_provider_response_is_not_mislabeled_as_missing_tooling(self):
        self.child.return_value.stdout = '\ufeff {"status":"unavailable","retryable":true}\n'
        result = self.run_script()
        self.assertEqual(result, {"status": "unavailable", "retryable": True})
        self.assertFalse(accessibility.is_powershell_failure(result))


class TestNativePageTabScript(unittest.TestCase):
    """Keep the Windows-only bridge's role/identity guards explicit in CI."""

    def test_only_page_tabs_inside_page_tab_lists_enter_the_tab_inventory(self):
        source = accessibility.NATIVE_ACCESSIBILITY_SCRIPT
        self.assertRegex(
            source, r"if\s*\(role == 37 && pageTabList\)\s*\{\s*"
            r"//[^\n]*\n\s*result\.Tabs\.Add")
        self.assertEqual(source.count("result.Tabs.Add("), 1)
        self.assertIn("pageTabList || role == 60", source)
        self.assertRegex(
            source, r'Walk\(acc,\s*0,\s*0,\s*"root",\s*false,\s*hwnd')
        # A selected ROLE_SYSTEM_LISTITEM (34) is not worksheet activation.
        self.assertNotRegex(source, r"role\s*==\s*34")

    def test_selection_rechecks_role_name_owner_enabled_and_selected_state(self):
        source = accessibility.NATIVE_ACCESSIBILITY_SCRIPT
        tab_script = accessibility._TAB_SCRIPT
        self.assertIn("Owner.accSelect(2, ChildId)", source)
        self.assertIn("Owner.get_accRole(ChildId)) != 37", source)
        self.assertIn("$_.Name -ceq [string]$q.name", tab_script)
        self.assertRegex(tab_script, r"if\s*\(-not \$snapshot\.Complete\)")
        self.assertRegex(tab_script, r"\$matches\.Count -gt 1\)\s*\{\s*"
                         r"\$result\.status = 'ambiguous'")
        self.assertIn("$tab.Refresh()", tab_script)
        self.assertIn(
            "::Owned([long]$q.hwnd, [int]$q.pid)", tab_script)
        self.assertIn("::Enabled([long]$q.hwnd)", tab_script)
        self.assertIn("$tab.Name -cne [string]$q.name", tab_script)
        self.assertIn("($tab.State -band 2) -ne 0", tab_script)
        self.assertIn("($tab.State -band 0x18001) -eq 0", tab_script)
        self.assertIn("runtime_id=$tab.RuntimeId", tab_script)
        for unsafe in ("DoDefaultAction", "SendKeys", "SetCursorPos",
                       "mouse_event", "PostMessage"):
            self.assertNotIn(unsafe, tab_script)

    def test_powerbi_ribbon_tabs_cannot_use_the_generic_msaa_fallback(self):
        source = accessibility._TAB_SCRIPT
        native_only = source[len(accessibility.NATIVE_ACCESSIBILITY_SCRIPT):]
        self.assertRegex(
            native_only,
            r"ProcessName -eq 'PBIDesktop'\)\s*\{\s*"
            r"\$result\.note = '[^']*UI Automation'\s*\}\s*else\s*\{\s*"
            r"\$snapshot = \[NativeDesktopAccessibility\]::Read")
        self.assertNotIn("ListItem", native_only)


class TestNativeTabRequest(unittest.TestCase):
    def test_selection_and_read_only_verification_preserve_exact_request(self):
        name = 'Synthetic "North" — Étude'
        response = {"status": "inaccessible", "note": "Synthetic provider"}
        with mock.patch.object(accessibility, "run_accessibility_script",
                               return_value=response) as run:
            for verify_only, mode in ((False, "select"), (True, "verify")):
                with self.subTest(mode=mode):
                    result = accessibility.select_native_tab(
                        11, 42, name, timeout=.125, verify_only=verify_only)
                    self.assertIs(result, response)
                    run.assert_called_with(accessibility._TAB_SCRIPT, {
                        "hwnd": 11, "pid": 42, "name": name, "seconds": .125,
                        "mode": mode,
                    }, .125)
                    self.assertNotIn(name, run.call_args.args[0])


class TestSharedDesktopDialogObserver(unittest.TestCase):
    def test_both_consumers_use_the_same_read_only_strict_observer(self):
        source = accessibility.DESKTOP_DIALOG_OBSERVER_SCRIPT
        self.assertIn(source, navigation._TABLEAU_WINDOW_MENU_SCRIPT)
        self.assertIn(source, desktop_window._DIALOG_SCRIPT)
        self.assertEqual(source.count("function Read-DesktopDialogMetadata("), 1)
        self.assertEqual(source.count("function Get-DesktopDialogClassification("), 1)
        for unsafe in ("Start-Sleep", "SendKeys", ".Invoke(", ".Toggle(", ".Expand(",
                       ".Collapse(", "ReadToEnd"):
            self.assertNotIn(unsafe, source)
        self.assertIn(accessibility.DIALOG_ATTENTION_PATTERN, source)
        self.assertEqual(desktop_window._ATTENTION_TEXT.pattern,
                         accessibility.DIALOG_ATTENTION_PATTERN)


@unittest.skipUnless(os.name == "nt", "Isolated Windows PowerShell functions")
class TestDesktopDialogObserverPowerShell(unittest.TestCase):
    """Execute unchanged shared functions against inert UIA/native objects."""

    @classmethod
    def setUpClass(cls):
        if accessibility.powershell_preflight()["status"] != "ready":
            raise unittest.SkipTest("Windows PowerShell not installed")
        progress = {"type": "ProgressBar", "name": "", "hwnd": 0,
                    "identity": "progress:synthetic:1"}
        cancel = {"type": "Button", "name": "Cancel", "hwnd": 32,
                  "identity": "button:synthetic:2"}
        cases = [
            {"name": "progress"}, {"name": "title_only", "nodes": []},
            {"name": "layout", **layout_dialog_case()},
            {"name": "layout_caption_transition", **layout_caption_transition_case()},
            *[{"name": name, "caption_only": True,
               "expected": {"hwnd": 31, "owner_hwnd": 11, "title": "Processing Request",
                            "class_name": "SyntheticModal", "runtime_id": "dialog:synthetic:31"},
               "nodes": layout_caption_transition_case()["nodes"],
               "native_before": {"title": CAPTION_TRANSITION_TITLE}, **options}
              for name, options in (
                  ("fresh_caption", {}),
                  ("fresh_caption_class_changed", {"native_before": {
                      "title": CAPTION_TRANSITION_TITLE, "class_name": "OtherModal"}}),
                  ("fresh_caption_foreign", {"native_before": {
                      "title": CAPTION_TRANSITION_TITLE, "pid_after": 99, "owned_after": False}}),
                  ("fresh_caption_reused", {"after_identity": "dialog:reused"}),
                  ("fresh_caption_protected", {"native_before": {
                      "title": "Authentication required"}}),
                  ("fresh_caption_expired", {"seconds": 0}),
              )],
            {"name": "cancel_only", "nodes": [cancel]},
            {"name": "localized_cancel", "nodes": [progress, dict(cancel, name="Localized", hwnd=32)]},
            {"name": "french_cancel", "nodes": [progress, dict(cancel, name="Annuler", hwnd=33)]},
            {"name": "other_button", "nodes": [progress, dict(cancel, name="Retry", hwnd=33)]},
            {"name": "interactive", "nodes": [progress, {
                "type": "Edit", "name": "Synthetic input", "hwnd": 0, "identity": "input:synthetic:1"}]},
            {"name": "protected", "native_before": {"title": "File Recovery"}},
            {"name": "partial_controls", "nodes": [
                dict(progress, identity=f"progress:synthetic:{index}") for index in range(65)]},
            {"name": "partial_native", "native_seed": {
                "Complete": False, "Texts": [], "Buttons": [], "Progress": 0,
                "Interactive": 0, "Truncated": False}},
            {"name": "foreign_child", "child_pid": 99, "nodes": [dict(progress, hwnd=40)]},
            {"name": "changed_native", "native_after": {"title": "Changed Synthetic State"}},
            {"name": "changed_root", "after_identity": "dialog:other"},
            {"name": "expected_mismatch", "expected": {
                "owner_hwnd": 11, "title": "Original Synthetic State", "class_name": "SyntheticModal"}},
            {"name": "expired", "seconds": 0},
        ]
        body = layout_caption_transition_case()["nodes"]
        arbitrary_body = [*body, {"type": "Text", "name": "Synthetic response content",
                                  "hwnd": 0, "identity": "response:synthetic:1"}]
        unknown_body = [dict(body[0], name="Constructing synthetic content"), *body[1:]]
        malformed_timer = [body[0], dict(body[1], name="Elapsed time 00:99"), body[2]]
        cases.extend({
            "name": name, "post_approved": True, "expected": retained_layout_identity(),
            "nodes": body, **options,
        } for name, options in (
            ("post_native_before_changed", {"native_before": {"title": CAPTION_TRANSITION_TITLE}}),
            ("post_native_changes_during_read", {"native_after": {"title": CAPTION_TRANSITION_TITLE}}),
            ("post_root_already_changed", {"root_before": {"Name": CAPTION_TRANSITION_TITLE},
                                          "root_after": {"Name": CAPTION_TRANSITION_TITLE}}),
            ("post_both_captions_change", {"native_after": {"title": CAPTION_TRANSITION_TITLE},
                                          "root_after": {"Name": CAPTION_TRANSITION_TITLE}}),
            ("post_unknown_body", {"native_before": {"title": CAPTION_TRANSITION_TITLE},
                                   "nodes": unknown_body}),
            ("post_title_only", {"native_before": {"title": CAPTION_TRANSITION_TITLE}, "nodes": []}),
            ("post_arbitrary_body", {"nodes": arbitrary_body}),
            ("post_malformed_timer", {"nodes": malformed_timer}),
            ("post_extra_response", {"nodes": [*body, {
                "type": "Button", "name": "Retry", "hwnd": 33, "identity": "retry:synthetic:1"}]}),
            ("post_password", {"nodes": [*body, {
                "type": "Edit", "name": "", "hwnd": 0, "identity": "input:synthetic:1",
                "current": {"IsPassword": True}}]}),
            ("post_unexpected_control", {"nodes": [*body, {
                "type": "ListItem", "name": "", "hwnd": 0, "identity": "list:synthetic:1"}]}),
            ("post_new_progress", {"nodes": [*body, {
                "type": "ProgressBar", "name": "", "hwnd": 0, "identity": "progress:synthetic:1"}]}),
            ("post_protected_root", {"root_after": {"Name": "Authentication required"}}),
            ("post_foreign", {"native_after": {"pid_after": 99, "owned_after": False}}),
            ("post_uia_foreign", {"root_after": {"ProcessId": 99}}),
            ("post_hwnd_changed", {"native_after": {"hwnd": 33}}),
            ("post_owner_changed", {"native_after": {"owner_hwnd": 22}}),
            ("post_immutable_changed", {"native_after": {"class_name": "OtherModal"}}),
            ("post_runtime_reused", {"after_identity": "dialog:reused"}),
            ("post_partial", {"native_after": {"complete": False}}),
            ("post_truncated", {"native_after": {"truncated": True}}),
            ("post_expired", {"seconds": 0}),
            ("post_retired", {"expected": dict(retained_layout_identity(), absence_observed=True)}),
            ("post_foreign_hint", {"expected": dict(retained_layout_identity(), owner_pid=99)}),
            ("post_other_progress", {"expected": dict(retained_layout_identity(), progress_basis="msaa")}),
            ("post_diagnostic_conflict", {"caption_only": True}),
        ))
        classifications = []
        for key, control in (
                ("wrong_type", dict(PROGRESS_CONTROL, control_type="ControlType.Edit")),
                ("unnamed_identity", dict(PROGRESS_CONTROL, runtime_id="")),
                ("password", dict(PROGRESS_CONTROL, is_password=True))):
            classifications.append({"name": key, "metadata": strict_dialog_metadata(
                progress_controls=[control])})
        layout = layout_dialog_metadata()
        classifications.append({"name": "layout", "metadata": layout})
        for key, changes in (
                ("layout_missing_caption", {"texts": [LAYOUT_TEXTS[0], *LAYOUT_TEXTS[2:]]}),
                ("layout_missing_timer", {"texts": [*LAYOUT_TEXTS[:2], LAYOUT_TEXTS[3]]}),
                ("layout_unknown_caption", {"texts": [
                    LAYOUT_TEXTS[0], "Computing Synthetic Content", *LAYOUT_TEXTS[2:]]}),
                ("layout_malformed_timer", {"texts": [
                    *LAYOUT_TEXTS[:2], "Elapsed time unknown", LAYOUT_TEXTS[3]]}),
                ("layout_unknown_title", {"native_before": dict(
                    layout["native_before"], title="Synthetic Activity")}),
                ("layout_constructing_title", {"native_before": dict(
                    layout["native_before"], title=CAPTION_TRANSITION_TITLE),
                    "native_after": dict(layout["native_after"], title=CAPTION_TRANSITION_TITLE),
                    "texts": [CAPTION_TRANSITION_TITLE, "Computing View Layout",
                              "Elapsed time 00:05", "Cancel"]}),
                ("layout_no_cancel", {"buttons": []}),
                ("layout_retry", {"buttons": [{"name": "Retry", "control_id": 9}]}),
                ("layout_partial", {"native_complete": False}),
                ("layout_truncated", {"truncated": True}),
                ("layout_protected", {"texts": [*LAYOUT_TEXTS, "File Recovery"]}),
                ("layout_expired", {"status": "timed_out"})):
            classifications.append({"name": key, "metadata": layout_dialog_metadata(**changes)})
        script = inert_dialog_observer_script() + r"""
$q=[Console]::In.ReadToEnd() | ConvertFrom-Json
$cases=@()
foreach ($case in $q.cases) {
    Initialize-SyntheticDialog $case
    $expected=$(if ($null -ne $case.expected) {
        $case.expected } else { $script:syntheticDialogExpected })
    $metadata=Read-DesktopDialogMetadata 31 42 $case.seconds $expected $case.native_seed `
        ([bool]$case.caption_only) ([bool]$case.post_approved)
    $cases+=@{case=$case.name; metadata=$metadata;
        native_reads=[NativeDesktopDialogWindow]::Reads;
        uia_reads=[Windows.Automation.AutomationElement]::Reads;
        hwnd=[NativeDesktopDialogWindow]::LastHwnd; pid=[NativeDesktopDialogWindow]::LastPid}
}
$classifications=@()
foreach ($case in $q.classifications) {
    $classifications+=@{case=$case.name; result=(Get-DesktopDialogClassification $case.metadata)}
}
@{cases=$cases; classifications=$classifications} | ConvertTo-Json -Depth 9 -Compress
"""
        observation = accessibility.run_powershell_json(
            script, {"cases": [{"seconds": 2, **case} for case in cases],
                     "classifications": classifications}, timeout=30)
        if not isinstance(observation.get("cases"), list):
            raise AssertionError(
                "Isolated shared observer failed: " + observation.get("cause", "invalid_harness_result"))
        cls.results = {case["case"]: case for case in observation["cases"]}
        cls.classifications = {case["case"]: case["result"]
                               for case in observation["classifications"]}

    def test_complete_progress_requires_recorded_control_identity_and_exact_native_owner(self):
        case = self.results["progress"]
        metadata = case["metadata"]
        self.assertEqual(metadata["status"], "observed")
        self.assertEqual(metadata["progress_controls"], [PROGRESS_CONTROL])
        self.assertEqual(metadata["buttons"], [{"name": "Cancel", "control_id": 2}])
        self.assertEqual(metadata["interaction_controls"], [])
        self.assertEqual(metadata["uia_count"], 2)
        self.assertEqual(metadata["native_before"], metadata["native_after"])
        self.assertEqual((case["hwnd"], case["pid"]), (31, 42))
        self.assertEqual((case["native_reads"], case["uia_reads"]), (2, 2))
        self.assertEqual(metadata["strict_classification"], {
            "status": "busy", "basis": "shared_strict_uia", "complete": True,
            "progress_required": True, "protected_text": False, "cancel_only": True,
            "reason": "complete_progress_structure"})
        for scenario in ("localized_cancel", "french_cancel"):
            with self.subTest(scenario=scenario):
                self.assertEqual(self.results[scenario]["metadata"]["strict_classification"]["status"],
                                 "busy")

    def test_title_cancel_protected_and_interactive_controls_never_classify_as_strict_progress(self):
        for scenario, reason in (
                ("title_only", "progress_control_not_observed"),
                ("cancel_only", "progress_control_not_observed"),
                ("other_button", "interactive_controls"),
                ("interactive", "interactive_controls"), ("protected", "protected_text")):
            with self.subTest(scenario=scenario):
                classification = self.results[scenario]["metadata"]["strict_classification"]
                self.assertEqual((classification["status"], classification["reason"]),
                                 ("attention", reason))
                self.assertIs(classification["complete"], True)
        record = self.results["interactive"]["metadata"]["interaction_controls"][0]
        self.assertEqual((record["control_type"], record["runtime_id"]),
                         ("ControlType.Edit", "input:synthetic:1"))
        for key in ("wrong_type", "unnamed_identity", "password"):
            classification = self.classifications[key]
            self.assertEqual((classification["status"], classification["reason"]),
                             ("attention", "progress_control_unqualified"))
            self.assertIs(classification["complete"], False)

    def test_complete_known_layout_caption_timer_title_and_cancel_need_no_progress_bar(self):
        metadata = self.results["layout"]["metadata"]
        self.assertEqual(metadata["texts"], LAYOUT_TEXTS)
        self.assertEqual(metadata["buttons"], [{"name": "Cancel", "control_id": 2}])
        self.assertEqual(metadata["progress_controls"], [])
        self.assertEqual(metadata["progress"], 0)
        self.assertEqual(metadata["interaction_controls"], [])
        self.assertEqual(metadata["strict_classification"]["status"], "busy")
        self.assertIs(metadata["strict_classification"]["complete"], True)
        self.assertEqual(self.classifications["layout"]["status"], "busy")
        self.assertIs(self.classifications["layout"]["complete"], True)

    def test_known_layout_alternative_rejects_missing_signals_and_partial_or_protected_evidence(self):
        for key in (
                "layout_missing_caption", "layout_missing_timer", "layout_unknown_caption",
                "layout_malformed_timer", "layout_unknown_title", "layout_constructing_title",
                "layout_no_cancel",
                "layout_retry", "layout_partial", "layout_truncated",
                "layout_protected", "layout_expired"):
            with self.subTest(case=key):
                self.assertEqual(self.classifications[key]["status"], "attention")
        for key in ("layout_partial", "layout_truncated", "layout_expired"):
            self.assertIs(self.classifications[key]["complete"], False)

    def test_owned_layout_caption_change_keeps_raw_identity_failure_not_constructing_progress(self):
        metadata = self.results["layout_caption_transition"]["metadata"]
        self.assertEqual(metadata["status"], "identity_changed")
        self.assertEqual(metadata["texts"], [
            "Processing Request", "Computing View Layout", "Elapsed time 00:05", "Cancel"])
        self.assertEqual(metadata["progress_controls"], [])
        self.assertEqual(metadata["interaction_controls"], [])
        before, after = metadata["native_before"], metadata["native_after"]
        self.assertEqual(before["title"], "Processing Request")
        self.assertEqual(after["title"], CAPTION_TRANSITION_TITLE)
        self.assertEqual({key: value for key, value in before.items() if key != "title"},
                         {key: value for key, value in after.items() if key != "title"})
        self.assertTrue(metadata["native_complete"])
        self.assertTrue(metadata["native_ownership_complete"])
        self.assertTrue(metadata["uia_owned_before"] and metadata["uia_owned_after"])
        self.assertTrue(metadata["uia_visible_before"] and metadata["uia_visible_after"])
        self.assertEqual(metadata["uia_runtime_id_before"], metadata["uia_runtime_id_after"])
        self.assertIs(metadata["caption_changed"], True)
        self.assertEqual(metadata["identity_change_fields"], [])
        self.assertIs(metadata["immutable_identity_complete"], True)
        self.assertEqual(metadata["strict_classification"]["status"], "attention")
        self.assertIs(metadata["strict_classification"]["complete"], False)

    def test_fresh_caption_only_observation_is_diagnostic_not_constructing_authorization(self):
        metadata = self.results["fresh_caption"]["metadata"]
        self.assertEqual(metadata["status"], "observed")
        self.assertEqual(metadata["native_before"]["title"], CAPTION_TRANSITION_TITLE)
        self.assertEqual(metadata["native_after"]["title"], CAPTION_TRANSITION_TITLE)
        self.assertEqual(metadata["texts"], [
            CAPTION_TRANSITION_TITLE, "Computing View Layout", "Elapsed time 00:05", "Cancel"])
        self.assertIs(metadata["caption_changed"], True)
        self.assertIs(metadata["immutable_identity_complete"], True)
        self.assertEqual(metadata["identity_change_fields"], [])
        self.assertIs(metadata["diagnostic_only"], True)
        self.assertIs(metadata["wait_authorized"], False)
        self.assertIs(metadata["resume_authorized"], False)
        self.assertEqual(metadata["strict_classification"]["status"], "attention")
        self.assertEqual(metadata["progress_controls"], [])
        self.assertEqual((self.results["fresh_caption"]["native_reads"],
                          self.results["fresh_caption"]["uia_reads"]), (2, 2))

    def test_caption_diagnostic_cannot_hide_true_identity_protected_or_expired_transitions(self):
        for scenario, status in (
                ("fresh_caption_class_changed", "identity_changed"),
                ("fresh_caption_foreign", "ownership_changed"),
                ("fresh_caption_reused", "identity_changed"),
                ("fresh_caption_protected", "observed"),
                ("fresh_caption_expired", "timed_out")):
            with self.subTest(scenario=scenario):
                metadata = self.results[scenario]["metadata"]
                self.assertEqual(metadata["status"], status)
                self.assertIs(metadata["diagnostic_only"], True)
                self.assertIs(metadata["wait_authorized"], False)
                self.assertIs(metadata["resume_authorized"], False)
                self.assertEqual(metadata["strict_classification"]["status"], "attention")
                if scenario == "fresh_caption_protected":
                    self.assertIs(metadata["strict_classification"]["protected_text"], True)
                else:
                    self.assertIs(metadata["immutable_identity_complete"], False)
        self.assertEqual(self.results["fresh_caption_class_changed"]["metadata"]["identity_change_fields"],
                         ["class_name"])
        self.assertEqual(self.results["fresh_caption_reused"]["metadata"]["identity_change_fields"],
                         ["uia_runtime_id"])
        self.assertEqual((self.results["fresh_caption_expired"]["native_reads"],
                          self.results["fresh_caption_expired"]["uia_reads"]), (0, 0))

    def test_explicit_retained_layout_qualifies_body_across_native_and_root_caption_races(self):
        for scenario in (
                "post_native_before_changed", "post_native_changes_during_read",
                "post_root_already_changed", "post_both_captions_change"):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                metadata = case["metadata"]
                self.assertEqual(metadata["status"], "observed")
                self.assertIs(metadata["post_approved_layout_observation"], True)
                self.assertIs(metadata["immutable_identity_complete"], True)
                self.assertIs(metadata["native_complete"], True)
                self.assertIs(metadata["native_ownership_complete"], True)
                self.assertIs(metadata["uia_complete"], True)
                self.assertEqual(metadata["identity_change_fields"], [])
                self.assertEqual(metadata["body_texts"], [
                    "Computing View Layout", "Elapsed time 00:05", "Cancel"])
                self.assertIs(metadata["layout_body_controls_complete"], True)
                self.assertEqual(metadata["unexpected_controls"], [])
                self.assertEqual(metadata["progress_controls"], [])
                self.assertEqual(metadata["buttons"], [{"name": "Cancel", "control_id": 2}])
                self.assertEqual(metadata["strict_classification"], {
                    "status": "busy", "basis": "tableau_layout_timer", "complete": True,
                    "progress_required": True, "protected_text": False, "cancel_only": True,
                    "reason": "complete_owned_layout_timer_body"})
                self.assertNotIn("diagnostic_only", metadata)
                self.assertEqual((case["native_reads"], case["uia_reads"]), (2, 2))
                self.assertEqual((case["hwnd"], case["pid"]), (31, 42))
        native = self.results["post_native_changes_during_read"]["metadata"]
        self.assertEqual(native["native_before"]["title"], "Processing Request")
        self.assertEqual(native["native_after"]["title"], CAPTION_TRANSITION_TITLE)
        root = self.results["post_root_already_changed"]["metadata"]
        self.assertEqual(root["uia_root_name_before"], CAPTION_TRANSITION_TITLE)
        self.assertEqual(root["uia_root_name_after"], CAPTION_TRANSITION_TITLE)
        both = self.results["post_both_captions_change"]["metadata"]
        self.assertEqual(both["uia_root_name_before"], "Processing Request")
        self.assertEqual(both["uia_root_name_after"], CAPTION_TRANSITION_TITLE)
        self.assertEqual(both["native_after"]["title"], CAPTION_TRANSITION_TITLE)
        self.assertIs(both["caption_changed"], True)
        # Identical caption transitions without the explicit retained approval
        # remain conservative; the original diagnostic-only route never resumes.
        self.assertEqual(self.results["layout_caption_transition"]["metadata"]["status"],
                         "identity_changed")
        self.assertEqual(self.results["fresh_caption"]["metadata"]["strict_classification"]["status"],
                         "attention")
        self.assertIs(self.results["fresh_caption"]["metadata"]["resume_authorized"], False)

    def test_retained_layout_never_qualifies_unknown_interactive_or_partial_body_evidence(self):
        for scenario in (
                "post_unknown_body", "post_title_only", "post_arbitrary_body", "post_malformed_timer",
                "post_extra_response", "post_password", "post_unexpected_control",
                "post_new_progress", "post_protected_root", "post_foreign", "post_uia_foreign",
                "post_hwnd_changed", "post_owner_changed", "post_immutable_changed",
                "post_runtime_reused", "post_partial", "post_truncated", "post_expired"):
            with self.subTest(scenario=scenario):
                metadata = self.results[scenario]["metadata"]
                self.assertEqual(metadata["strict_classification"]["status"], "attention")
                self.assertIs(metadata["post_approved_layout_observation"], scenario != "post_expired")
        for scenario in ("post_unknown_body", "post_title_only", "post_arbitrary_body",
                         "post_malformed_timer"):
            self.assertEqual(self.results[scenario]["metadata"]["status"], "observed")
        self.assertEqual(self.results["post_foreign"]["metadata"]["status"], "ownership_changed")
        self.assertEqual(self.results["post_immutable_changed"]["metadata"]["identity_change_fields"],
                         ["class_name"])
        self.assertEqual(self.results["post_hwnd_changed"]["metadata"]["identity_change_fields"], ["hwnd"])
        self.assertEqual(self.results["post_owner_changed"]["metadata"]["identity_change_fields"],
                         ["owner_hwnd"])
        self.assertEqual(self.results["post_uia_foreign"]["metadata"]["status"], "ownership_changed")
        self.assertEqual(self.results["post_new_progress"]["metadata"]["strict_classification"]["reason"],
                         "incomplete_observation")
        self.assertIs(self.results["post_new_progress"]["metadata"]["layout_body_controls_complete"], False)
        self.assertEqual(self.results["post_new_progress"]["metadata"]["unexpected_controls"],
                         [{"control_type": "ControlType.ProgressBar", "name": "", "hwnd": 0}])
        self.assertEqual(self.results["post_runtime_reused"]["metadata"]["identity_change_fields"],
                         ["uia_runtime_id"])
        self.assertEqual(self.results["post_unexpected_control"]["metadata"]["unexpected_controls"],
                         [{"control_type": "ControlType.ListItem", "name": "", "hwnd": 0}])
        self.assertIs(self.results["post_unexpected_control"]["metadata"]["layout_body_controls_complete"],
                      False)
        self.assertIs(self.results["post_protected_root"]["metadata"]["strict_classification"][
            "protected_text"], True)
        for scenario in ("post_partial", "post_truncated", "post_expired"):
            self.assertIs(self.results[scenario]["metadata"]["strict_classification"]["complete"], False)
        self.assertEqual((self.results["post_expired"]["native_reads"],
                          self.results["post_expired"]["uia_reads"]), (0, 0))

    def test_retired_wrong_owner_other_provider_or_diagnostic_hint_cannot_enter_approved_mode(self):
        for scenario in ("post_retired", "post_foreign_hint", "post_other_progress",
                         "post_diagnostic_conflict"):
            with self.subTest(scenario=scenario):
                case = self.results[scenario]
                metadata = case["metadata"]
                self.assertEqual(metadata["cause"], "post_approved_layout_requires_retained_identity")
                self.assertEqual(metadata["strict_classification"]["status"], "attention")
                self.assertIs(metadata["post_approved_layout_observation"], False)
                self.assertEqual((case["native_reads"], case["uia_reads"]), (0, 0))

    def test_partial_changed_foreign_and_expired_observations_cannot_be_conclusive(self):
        for scenario, status in (
                ("partial_controls", "observed"), ("partial_native", "observed"),
                ("foreign_child", "ownership_changed"), ("changed_native", "identity_changed"),
                ("changed_root", "identity_changed"), ("expected_mismatch", "identity_changed"),
                ("expired", "timed_out")):
            with self.subTest(scenario=scenario):
                metadata = self.results[scenario]["metadata"]
                self.assertEqual(metadata["status"], status)
                self.assertEqual(metadata["strict_classification"]["status"], "attention")
                self.assertIs(metadata["strict_classification"]["complete"], False)
        partial = self.results["partial_controls"]["metadata"]
        self.assertIs(partial["truncated"], True)
        self.assertEqual(partial["progress"], 65)
        self.assertEqual(len(partial["progress_controls"]), 64)
        expired = self.results["expired"]
        self.assertEqual((expired["native_reads"], expired["uia_reads"]), (0, 0))
        self.assertEqual(expired["metadata"]["uia_error"], "dialog_observation_timeout")
        self.assertEqual(self.results["expected_mismatch"]["uia_reads"], 2)
        self.assertIs(self.results["expected_mismatch"]["metadata"]["caption_changed"], True)
        self.assertIs(self.results["expected_mismatch"]["metadata"]["immutable_identity_complete"], True)


if __name__ == "__main__":
    unittest.main()
