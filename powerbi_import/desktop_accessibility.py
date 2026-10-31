"""Bounded native accessibility bridge for Desktop helpers (stdlib only).

Managed UIA does not always load Win32 client-side proxies. MSAA can still
provide native roles/names/states in that case. No coordinates, keyboard input,
default actions, or arbitrary ListItem selection are permitted here.
"""
from __future__ import annotations

import base64
import gzip
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

_POWERSHELL_CAUSES = {
    "windows_required", "powershell_not_found", "powershell_launch_failed",
    "powershell_nonzero_exit", "powershell_timeout", "powershell_malformed_json",
    "invalid_powershell_request",
}


def _powershell_error(status: str, cause: str, **details) -> dict:
    # Never include stderr, commands, exception text or observer input: these
    # can contain private source details. A fixed cause is sufficient to act on.
    return {"status": status, "cause": cause, "note": cause,
            "retryable": False, **details}


def is_powershell_failure(result: dict) -> bool:
    """Distinguish tooling/protocol failure from a valid provider observation."""
    return result.get("cause") in _POWERSHELL_CAUSES


class PowerShellFailure(RuntimeError):
    """Internal stop signal; public probes turn it back into structured evidence."""

    def __init__(self, observation: dict):
        self.observation = observation
        super().__init__(observation.get("cause", "native_observation_failed"))


def _windows_system_directories() -> list[Path]:
    """Prefer Windows' own directory APIs over environment-variable guesses."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        def directory(name):
            query = getattr(kernel32, name)
            query.argtypes = [ctypes.POINTER(ctypes.c_wchar), ctypes.c_uint]
            query.restype = ctypes.c_uint
            buffer = ctypes.create_unicode_buffer(32768)
            length = query(buffer, len(buffer))
            if 0 < length < len(buffer):
                path = Path(buffer.value)
                if path.is_absolute():
                    return path
            return None

        system = directory("GetSystemDirectoryW")
        windows = directory("GetWindowsDirectoryW")
        paths = []
        if windows and ctypes.sizeof(ctypes.c_void_p) == 4:
            paths.append(windows / "Sysnative")
        if system:
            paths.append(system)
        return paths
    except (AttributeError, OSError, TypeError, ValueError):
        return []


def resolve_powershell_executable() -> Optional[str]:
    """Find installed Windows PowerShell, including with a reduced PATH.

    These helpers require the Windows/.NET Framework UIA and MSAA components.
    Do not silently substitute pwsh, download a tool, or change execution policy.
    """
    if sys.platform != "win32":
        return None
    directories = _windows_system_directories()
    roots = dict.fromkeys(filter(None, (
        os.environ.get("SystemRoot"), os.environ.get("WINDIR"), r"C:\Windows")))
    for root in roots:
        # Sysnative allows a 32-bit Python to reach the installed native host.
        for system in ("Sysnative", "System32"):
            path = Path(root) / system
            if path.is_absolute():
                directories.append(path)

    def validated(candidate):
        try:
            path = Path(candidate)
            if path.name.casefold() != "powershell.exe" or not path.is_file():
                return None
            resolved = path.resolve()
            if resolved.name.casefold() != "powershell.exe" or not resolved.is_file():
                return None
            # Resolving the Sysnative alias to System32 would re-enable WOW64
            # redirection when a 32-bit Python starts the executable.
            if any(part.casefold() == "sysnative" for part in path.parts):
                return str(path.absolute())
            return str(resolved)
        except (OSError, ValueError):
            return None

    for directory in dict.fromkeys(directories):
        found = validated(directory / "WindowsPowerShell" / "v1.0" / "powershell.exe")
        if found:
            return found
    try:
        on_path = shutil.which("powershell.exe")
        if on_path:
            return validated(on_path)
    except (OSError, ValueError):
        pass
    return None


def powershell_preflight() -> dict:
    """Read-only executable discovery. Does not launch any process."""
    if sys.platform != "win32":
        return _powershell_error("tooling_unavailable", "windows_required")
    executable = resolve_powershell_executable()
    if not executable:
        return _powershell_error("tooling_unavailable", "powershell_not_found")
    return {"status": "ready", "executable": executable}


def run_powershell_json(script: str, payload: dict, *, timeout: float,
                        base64_input: bool = False) -> dict:
    """One bounded child, UTF-8 JSON, explicit redacted tooling failures.

    Successful provider objects are returned unchanged. Only a parsed provider
    response can describe an unreadable UI; a missing/failed tool never can.
    """
    try:
        timeout = float(timeout)
        if (not math.isfinite(timeout) or timeout <= 0
                or not isinstance(script, str) or not isinstance(payload, dict)):
            raise ValueError
        deadline = time.monotonic() + timeout
        request = json.dumps(payload, ensure_ascii=True)
        if base64_input:
            request = base64.b64encode(request.encode("utf-8")).decode("ascii")
        # EncodedCommand avoids locale/quoting differences and command-line
        # interpolation. Payloads travel only over stdin.
        preamble = (
            "$ErrorActionPreference = 'Stop'\n"
            "$ProgressPreference = 'SilentlyContinue'\n"
            "[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)\n"
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n")
        encoded = base64.b64encode(
            (preamble + script).encode("utf-16le")).decode("ascii")
        command_mode = "-EncodedCommand"
        if len(encoded) > 28000:
            # Windows caps CreateProcess command lines at 32767 characters.
            # Larger qualified native scripts use a stdlib/.NET gzip envelope;
            # stdin remains exclusively the JSON request, never executable code.
            packed = base64.b64encode(gzip.compress(
                (preamble + script).encode("utf-8"), mtime=0)).decode("ascii")
            bootstrap = (
                "$ErrorActionPreference='Stop'\n"
                "$m=[IO.MemoryStream]::new([Convert]::FromBase64String('" + packed + "'))\n"
                "$g=[IO.Compression.GZipStream]::new($m,"
                "[IO.Compression.CompressionMode]::Decompress)\n"
                "$r=[IO.StreamReader]::new($g,[Text.Encoding]::UTF8)\n"
                "try {$s=$r.ReadToEnd()} finally {$r.Dispose()}\n"
                "& ([ScriptBlock]::Create($s))\n")
            encoded = base64.b64encode(bootstrap.encode("utf-16le")).decode("ascii")
            if len(encoded) > 30000:
                # UTF-16/base64 can inflate even the compressed envelope beyond
                # CreateProcess' limit. This ASCII-only, generated bootstrap is
                # one direct argument (no shell); JSON still travels on stdin.
                command_mode, encoded = "-Command", bootstrap
    except (TypeError, ValueError, OverflowError):
        return _powershell_error("error", "invalid_powershell_request")
    tooling = powershell_preflight()
    if tooling["status"] != "ready":
        return tooling
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _powershell_error("timed_out", "powershell_timeout")
    command = [tooling["executable"], "-NoProfile", "-NonInteractive",
               command_mode, encoded]
    if len(subprocess.list2cmdline(command)) >= 32767:
        return _powershell_error("error", "invalid_powershell_request")
    try:
        proc = subprocess.run(
            command,
            input=request, text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=remaining,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (FileNotFoundError, PermissionError, OSError):
        return _powershell_error("tooling_unavailable", "powershell_launch_failed")
    except subprocess.TimeoutExpired:
        return _powershell_error("timed_out", "powershell_timeout")
    except (ValueError, subprocess.SubprocessError):
        return _powershell_error("error", "powershell_launch_failed")
    if time.monotonic() >= deadline:
        return _powershell_error("timed_out", "powershell_timeout")
    if proc.returncode:
        return _powershell_error(
            "error", "powershell_nonzero_exit", returncode=proc.returncode)
    try:
        result = json.loads(proc.stdout.lstrip("\ufeff").strip())
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (AttributeError, TypeError, ValueError):
        return _powershell_error("error", "powershell_malformed_json")


# Compiled only inside a short-lived Windows PowerShell child. Accessibility
# and oleacc are Windows platform components, not third-party dependencies.
NATIVE_ACCESSIBILITY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName Accessibility
Add-Type -ReferencedAssemblies Accessibility -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using Accessibility;

public sealed class NativeDesktopButton {
    public string Name;
    public int ControlId;
}
public sealed class NativeDesktopTab {
    public string Name, RuntimeId;
    public int State, ChildId;
    internal IAccessible Owner;
    public void Select() { Owner.accSelect(2, ChildId); } // TAKESELECTION only
    public void Refresh() {
        if (Convert.ToInt32(Owner.get_accRole(ChildId)) != 37)
            throw new InvalidOperationException("Native page-tab role changed");
        Name = Owner.get_accName(ChildId) ?? "";
        State = Convert.ToInt32(Owner.get_accState(ChildId));
    }
}
public sealed class NativeDesktopSnapshot {
    public bool Owned, Complete = true, Truncated;
    public int Count, Progress, Interactive;
    public List<string> Texts = new List<string>();
    public List<NativeDesktopButton> Buttons = new List<NativeDesktopButton>();
    public List<NativeDesktopTab> Tabs = new List<NativeDesktopTab>();
}
public static class NativeDesktopAccessibility {
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(
        IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsWindowEnabled(IntPtr hwnd);
    [DllImport("user32.dll")] static extern int GetDlgCtrlID(IntPtr hwnd);
    [DllImport("oleacc.dll")] static extern int AccessibleObjectFromWindow(
        IntPtr hwnd, uint id, ref Guid iid,
        [MarshalAs(UnmanagedType.Interface)] out IAccessible acc);
    [DllImport("oleacc.dll")] static extern int AccessibleChildren(
        IAccessible acc, int start, int count,
        [Out, MarshalAs(UnmanagedType.LPArray, SizeParamIndex=2)] object[] children,
        out int obtained);
    [DllImport("oleacc.dll")] static extern int WindowFromAccessibleObject(
        IAccessible acc, out IntPtr hwnd);
    public static bool Owned(long hwnd, int pid) {
        uint actual;
        return IsWindow(new IntPtr(hwnd)) &&
            GetWindowThreadProcessId(new IntPtr(hwnd), out actual) != 0 &&
            actual == (uint)pid;
    }
    public static int ControlId(long hwnd) {
        return GetDlgCtrlID(new IntPtr(hwnd));
    }
    public static bool Enabled(long hwnd) {
        return IsWindowEnabled(new IntPtr(hwnd));
    }
    public static NativeDesktopSnapshot Read(long hwnd, int pid, bool dialog) {
        var result = new NativeDesktopSnapshot();
        result.Owned = Owned(hwnd, pid);
        if (!result.Owned) { result.Complete = false; return result; }
        IAccessible acc;
        var iid = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71");
        int hr = AccessibleObjectFromWindow(new IntPtr(hwnd), unchecked((uint)-4),
                                           ref iid, out acc);
        if (hr != 0 || acc == null) { result.Complete = false; return result; }
        Walk(acc, 0, 0, "root", false, hwnd, dialog, result);
        return result;
    }
    static void Walk(IAccessible acc, int child, int depth, string path,
                     bool pageTabList, long hwnd, bool dialog,
                     NativeDesktopSnapshot result) {
        if (depth > 32 || result.Count >= 4000) {
            result.Truncated = true; result.Complete = false; return;
        }
        result.Count++;
        try {
            int role = Convert.ToInt32(acc.get_accRole(child));
            int state = Convert.ToInt32(acc.get_accState(child));
            string name = acc.get_accName(child) ?? "";
            if ((state & 0x8000) != 0) return; // invisible, not merely overflow
            if (dialog) {
                if (name.Length > 0 && result.Texts.Count < 128)
                    result.Texts.Add(name.Substring(0, Math.Min(500, name.Length)));
                if (role == 48) result.Progress++; // PROGRESSBAR
                if (role == 43) { // PUSHBUTTON
                    IntPtr control;
                    int id = WindowFromAccessibleObject(acc, out control) == 0
                        ? GetDlgCtrlID(control) : 0;
                    result.Buttons.Add(new NativeDesktopButton {
                        Name = name, ControlId = id });
                }
                if ((role == 42 && (state & 0x40) == 0) ||
                    role == 46 || role == 44 || role == 45 || role == 30)
                    result.Interactive++;
            }
            if (role == 37 && pageTabList) { // PAGETAB under PAGETABLIST, NOT LISTITEM
                result.Tabs.Add(new NativeDesktopTab {
                    Name = name, State = state, ChildId = child, Owner = acc,
                    RuntimeId = hwnd.ToString() + ":msaa:" + path + ":" + child });
            }
            if (child != 0) return;
            int count = acc.accChildCount;
            if (count <= 0) return;
            int take = Math.Min(4000 - result.Count, count);
            if (take < count) { result.Truncated = true; result.Complete = false; }
            if (take <= 0) return;
            var children = new object[take];
            int obtained;
            if (AccessibleChildren(acc, 0, take, children, out obtained) < 0 ||
                obtained != take) result.Complete = false;
            for (int i = 0; i < obtained; i++) {
                string next = path + ":" + i;
                if (children[i] is int)
                    Walk(acc, (int)children[i], depth+1, next,
                         pageTabList || role == 60, hwnd, dialog, result);
                else if (children[i] is IAccessible)
                    Walk((IAccessible)children[i], 0, depth+1, next,
                         pageTabList || role == 60, hwnd, dialog, result);
                else result.Complete = false;
            }
        } catch { result.Complete = false; }
    }
}
'@
"""

# One protected-text rule for Python opening guards and the strict PS classifier.
DIALOG_ATTENTION_PATTERN = (
    r"\b(?:error|erreur|exception|failed|failure|échec|"
    r"sign[ -]?in|log[ -]?in|authentication|authentification|"
    r"credentials?|identifiants?|password|mot de passe|"
    r"licen[cs]e|activation|activate|EULA|trust|certificate|security|"
    r"confiance|certificat|sécurité|terms|conditions|contrat|"
    r"recovery|récupération)\b")
DIALOG_BUSY_TITLE_PATTERN = (
    r"^(?:Processing Request|Traitement (?:de la demande|de la requête|en cours)|"
    r"(?:Running|Executing) (?:Query|Queries)|"
    r"Exécution (?:de la requête|des requêtes)|Computing View Layout)"
    r"(?:\s*(?:\.\.\.|…))?$")
DIALOG_BUSY_TEXT_PATTERN = (
    r"\b(?:computing|processing|loading|query running|running quer(?:y|ies)|"
    r"executing|calcul|traitement|chargement|exécution|"
    r"requête en cours|requêtes en cours)\b")

# Importable, read-only functions. No global UIA aliases, action patterns, sleeps,
# or request reads: a live native guard can call these BEFORE its menu cleanup.
DESKTOP_DIALOG_OBSERVER_SCRIPT = r"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class NativeDesktopDialogWindow {
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(
        IntPtr h, out uint pid);
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr h, uint f);
    [DllImport("user32.dll")] static extern int GetWindowTextLengthW(IntPtr h);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    static extern int GetWindowTextW(IntPtr h, StringBuilder text, int length);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    static extern int GetClassNameW(IntPtr h, StringBuilder text, int length);
    [DllImport("user32.dll")] static extern int GetDlgCtrlID(IntPtr h);
    public sealed class Observation {
        public long hwnd, owner_hwnd;
        public int? pid_before, pid_after;
        public bool owned_before, owned_after, visible, complete, truncated;
        public string title = "", class_name = "";
    }
    public static int? Pid(long hwnd) {
        uint pid;
        IntPtr h = new IntPtr(hwnd);
        return IsWindow(h) && GetWindowThreadProcessId(h, out pid) != 0 && pid != 0
            ? (int?)pid : null;
    }
    public static int ControlId(long hwnd) { return GetDlgCtrlID(new IntPtr(hwnd)); }
    public static Observation Read(long hwnd, int pid) {
        IntPtr h = new IntPtr(hwnd);
        var result = new Observation { hwnd = hwnd, pid_before = Pid(hwnd) };
        result.owned_before = result.pid_before == pid;
        if (!result.owned_before) return result;
        result.owner_hwnd = GetWindow(h, 4).ToInt64();
        result.visible = IsWindowVisible(h);
        int length = GetWindowTextLengthW(h);
        var title = new StringBuilder(513);
        var className = new StringBuilder(257);
        int titleRead = GetWindowTextW(h, title, title.Capacity);
        int classRead = GetClassNameW(h, className, className.Capacity);
        result.pid_after = Pid(hwnd);
        result.owned_after = result.pid_after == pid;
        result.truncated = length > 512 || titleRead >= 512 || classRead >= 256;
        result.complete = result.owned_after && result.visible && result.owner_hwnd != 0 &&
            titleRead > 0 && classRead > 0 && !result.truncated;
        if (result.owned_after) {
            result.title = title.ToString(); result.class_name = className.ToString();
        }
        return result;
    }
}
'@
function Assert-DesktopDialogTime($clock, [double]$seconds) {
    if ($seconds -le 0 -or $clock.Elapsed.TotalSeconds -ge $seconds) {
        throw [TimeoutException]::new('Dialog observation deadline')
    }
}
function Get-DesktopDialogClassification($metadata) {
    $result = @{status='attention'; basis='shared_strict_uia'; complete=$false;
        progress_required=$true; protected_text=$false; cancel_only=$false;
        reason='incomplete_observation'}
    $postApprovedLayout=($metadata.post_approved_layout_observation -eq $true)
    $result.complete=($metadata.status -ceq 'observed' -and $metadata.uia_complete -eq $true -and
        $metadata.native_ownership_complete -eq $true -and $metadata.native_complete -eq $true -and
        -not $metadata.truncated -and -not $metadata.uia_error -and
        ($null -eq $metadata.native_accessibility_complete -or
         $metadata.native_accessibility_complete -eq $true))
    if ($postApprovedLayout -and (-not $metadata.immutable_identity_complete -or
        -not $metadata.layout_body_controls_complete -or $null -eq $metadata.body_texts)) {
        $result.complete=$false
    }
    $text = (@($metadata.native_before.title, $metadata.native_after.title,
               $metadata.uia_root_name_before, $metadata.uia_root_name_after) +
             @($metadata.texts)) -join "`n"
    $result.protected_text = [regex]::IsMatch($text, '__ATTENTION_PATTERN__',
        [Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if ($result.protected_text) { $result.reason='protected_text'; return $result }
    $cancelOnly=$true
    foreach ($button in $metadata.buttons) {
        if ($button.control_id -ne 2 -and
            ([string]$button.name).Trim().ToLowerInvariant() -cnotin @('cancel', 'annuler')) {
            $cancelOnly=$false
        }
    }
    $result.cancel_only=$cancelOnly
    if ($metadata.interactive -gt 0 -or -not $cancelOnly) {
        $result.reason='interactive_controls'; return $result
    }
    if (-not $result.complete) { return $result }
    if ($postApprovedLayout -or $metadata.progress_controls.Count -lt 1) {
        # Observed Tableau layout dialog has NO ProgressBar. Accept only its
        # complete EN multi-signal structure, never a title/Cancel/disable alone.
        $layout=$false; $timer=$false; $knownTexts=$true
        $captions=$(if ($postApprovedLayout) { @($metadata.body_texts) } else {
            @($metadata.texts)
        })
        foreach ($caption in $captions) {
            if ($caption -ceq 'Computing View Layout') { $layout=$true }
            elseif ($caption -cmatch '^Elapsed time [0-9]{2}:[0-5][0-9]$') { $timer=$true }
            elseif ($caption -cnotin @('Processing Request', 'Cancel')) { $knownTexts=$false }
        }
        # Only a retained, conclusively approved layout episode may qualify its
        # unchanged body independently of mutable native/UIA window captions.
        $titleQualified=($postApprovedLayout -or (
            $metadata.native_before.title -ceq 'Processing Request' -and
            [regex]::IsMatch($metadata.native_before.title, '__BUSY_TITLE_PATTERN__')))
        if ($titleQualified -and
            $layout -and $timer -and $knownTexts -and $metadata.buttons.Count -gt 0) {
            if ($postApprovedLayout -and (
                $metadata.progress_controls.Count -gt 0 -or $metadata.progress -gt 0)) {
                $result.reason='layout_body_changed'; return $result
            }
            $result.basis='tableau_layout_timer'; $result.status='busy'
            $result.reason=$(if ($postApprovedLayout) {
                'complete_owned_layout_timer_body'
            } else { 'complete_owned_layout_timer_structure' })
            return $result
        }
        $result.reason='progress_control_not_observed'; return $result
    }
    if (@($metadata.progress_controls | Where-Object {
        $_.control_type -cne 'ControlType.ProgressBar' -or
        [string]::IsNullOrWhiteSpace($_.runtime_id) -or $_.is_password }).Count -gt 0) {
        $result.complete=$false; $result.reason='progress_control_unqualified'
        return $result
    }
    $result.status='busy'; $result.reason='complete_progress_structure'
    return $result
}
function Read-DesktopDialogMetadata(
        [long]$hwnd, [int]$ownerPid, [double]$seconds, $expected=$null, $native=$null,
        [bool]$captionObservationOnly=$false, [bool]$postApprovedLayout=$false) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $phase='native_before'
    $metadata=@{status='unavailable'; texts=@(); buttons=@(); progress=0; interactive=0;
        progress_controls=@(); interaction_controls=@(); uia_count=0;
        truncated=$false; native_complete=$false; native_ownership_complete=$false;
        native_accessibility_complete=$null; uia_complete=$false; uia_error=$null;
        uia_owned_before=$false; uia_owned_after=$false; elapsed_ms=0;
        caption_changed=$false; identity_change_fields=@(); immutable_identity_complete=$false;
        post_approved_layout_observation=$false; body_texts=@(); unexpected_controls=@();
        layout_body_controls_complete=$false; uia_root_name_before=$null; uia_root_name_after=$null}
    if ($captionObservationOnly) {
        $metadata.diagnostic_only=$true
        $metadata.wait_authorized=$false; $metadata.resume_authorized=$false
    }
    try {
        Assert-DesktopDialogTime $watch $seconds
        if ($captionObservationOnly -and ($null -eq $expected -or
            [string]::IsNullOrWhiteSpace($expected.runtime_id))) {
            $metadata.cause='caption_observation_requires_retained_identity'
            throw [InvalidOperationException]::new('Retained dialog identity required')
        }
        if ($postApprovedLayout) {
            if ($captionObservationOnly -or $null -eq $expected -or
                $expected.progress_basis -cne 'tableau_layout_timer' -or
                $expected.absence_observed -ne $false -or
                $expected.hwnd -ne $hwnd -or $expected.owner_pid -ne $ownerPid -or
                $expected.owner_hwnd -le 0 -or
                [string]::IsNullOrWhiteSpace($expected.class_name) -or
                [string]::IsNullOrWhiteSpace($expected.runtime_id)) {
                $metadata.cause='post_approved_layout_requires_retained_identity'
                throw [InvalidOperationException]::new('Approved layout identity required')
            }
            $metadata.post_approved_layout_observation=$true
        }
        $before=[NativeDesktopDialogWindow]::Read($hwnd, $ownerPid)
        $metadata.native_before=$before
        Assert-DesktopDialogTime $watch $seconds
        if (-not $before.owned_before -or -not $before.owned_after -or
            $before.pid_before -ne $ownerPid -or $before.pid_after -ne $ownerPid) {
            $metadata.status='ownership_changed'
            throw [InvalidOperationException]::new('Dialog ownership')
        }
        if ($null -ne $expected) {
            $metadata.caption_changed=($before.title -cne $expected.title)
            if ($before.hwnd -ne $hwnd -or ($expected.hwnd -and $expected.hwnd -ne $hwnd)) {
                $metadata.identity_change_fields+= 'hwnd'
            }
            if ($before.owner_hwnd -ne $expected.owner_hwnd) {
                $metadata.identity_change_fields+= 'owner_hwnd'
            }
            if ($before.class_name -cne $expected.class_name) {
                $metadata.identity_change_fields+= 'class_name'
            }
            if ($metadata.identity_change_fields.Count -gt 0) {
                $metadata.status='identity_changed'
                throw [InvalidOperationException]::new('Dialog identity')
            }
            # Captions are metadata, never an immutable identity/reuse detector.
            # Normal callers remain conservative; only the explicit approved
            # layout mode can qualify its exact body through a caption change.
        }
        if ($null -ne $native) {
            $metadata.native_accessibility_complete=[bool]$native.Complete
            $metadata.texts=@($native.Texts)
            $metadata.buttons=@($native.Buttons | Select-Object -First 128 |
                ForEach-Object {
                    $label=[string]$_.Name
                    if ($label.Length -gt 500) { $metadata.truncated=$true }
                    @{name=$label.Substring(0, [Math]::Min(500, $label.Length));
                        control_id=$_.ControlId}})
            $metadata.progress=[int]$native.Progress
            $metadata.interactive=[int]$native.Interactive
            $metadata.truncated=($metadata.truncated -or $native.Truncated -or
                $native.Buttons.Count -gt 128 -or $native.Texts.Count -ge 128)
        }
        $automationType=[Windows.Automation.AutomationElement]
        $controlTypes=[Windows.Automation.ControlType]
        $phase='uia_root'
        $dialogRoot=$automationType::FromHandle([IntPtr]$hwnd)
        Assert-DesktopDialogTime $watch $seconds
        $current=$dialogRoot.Current
        $metadata.uia_owned_before=($current.ProcessId -eq $ownerPid -and
                                    [long]$current.NativeWindowHandle -eq $hwnd)
        $metadata.uia_visible_before=-not $current.IsOffscreen
        $metadata.uia_runtime_id_before=$dialogRoot.GetRuntimeId() -join ':'
        if (-not $metadata.uia_owned_before -or -not $metadata.uia_runtime_id_before) {
            $metadata.status='ownership_changed'
            throw [InvalidOperationException]::new('UIA dialog ownership')
        }
        if ($null -ne $expected -and $expected.runtime_id -and
            $metadata.uia_runtime_id_before -cne $expected.runtime_id) {
            $metadata.identity_change_fields+= 'uia_runtime_id'
            $metadata.status='identity_changed'
            throw [InvalidOperationException]::new('UIA dialog identity')
        }
        $rootName=[string]$current.Name
        $metadata.uia_root_name_before=$rootName.Substring(0, [Math]::Min(500, $rootName.Length))
        if ($rootName) {
            if ($rootName.Length -gt 500 -or $metadata.texts.Count -ge 128) {
                $metadata.truncated=$true
            }
            if ($metadata.texts.Count -lt 128) {
                $metadata.texts+= $rootName.Substring(0, [Math]::Min(500, $rootName.Length))
            }
        }
        $phase='uia_descendants'
        Assert-DesktopDialogTime $watch $seconds
        $nodes=$dialogRoot.FindAll([Windows.Automation.TreeScope]::Descendants,
                                  [Windows.Automation.Condition]::TrueCondition)
        Assert-DesktopDialogTime $watch $seconds
        $metadata.uia_count=$nodes.Count
        if ($nodes.Count -gt 1000) { $metadata.truncated=$true }
        for ($i=0; $i -lt $nodes.Count -and $i -lt 1000; $i++) {
            $phase='uia_control'
            Assert-DesktopDialogTime $watch $seconds
            $element=$nodes[$i]; $c=$element.Current
            if ($c.IsOffscreen) { continue }
            if ($c.ProcessId -ne $ownerPid -or ($c.NativeWindowHandle -gt 0 -and
                [NativeDesktopDialogWindow]::Pid($c.NativeWindowHandle) -ne $ownerPid)) {
                $metadata.status='ownership_changed'
                throw [InvalidOperationException]::new('UIA child ownership')
            }
            $name=[string]$c.Name
            if ($name) {
                if ($metadata.texts.Count -ge 128 -or $name.Length -gt 500) {
                    $metadata.truncated=$true
                }
                if ($metadata.texts.Count -lt 128) {
                    $metadata.texts+= $name.Substring(0, [Math]::Min(500, $name.Length))
                }
                if ($metadata.body_texts.Count -lt 128) {
                    $metadata.body_texts+= $name.Substring(0, [Math]::Min(500, $name.Length))
                } else { $metadata.truncated=$true }
            }
            if ($c.ControlType.ProgrammaticName -cnotin @(
                'ControlType.Text', 'ControlType.Button', 'ControlType.Window',
                'ControlType.Pane', 'ControlType.Group')) {
                if ($metadata.unexpected_controls.Count -ge 64) {
                    $metadata.truncated=$true
                } else {
                    $metadata.unexpected_controls+=@{
                        control_type=$c.ControlType.ProgrammaticName;
                        name=$name.Substring(0, [Math]::Min(500, $name.Length));
                        hwnd=[long]$c.NativeWindowHandle}
                }
            }
            if ($c.ControlType -eq $controlTypes::Button) {
                if ($metadata.buttons.Count -ge 128) { $metadata.truncated=$true } else {
                    $metadata.buttons+=@{name=$name.Substring(0, [Math]::Min(500, $name.Length));
                        control_id=[NativeDesktopDialogWindow]::ControlId($c.NativeWindowHandle)}
                }
            } elseif ($c.ControlType -eq $controlTypes::ProgressBar -or
                $c.ControlType -in @($controlTypes::Edit, $controlTypes::ComboBox,
                    $controlTypes::Hyperlink, $controlTypes::CheckBox,
                    $controlTypes::RadioButton) -or $c.IsPassword) {
                $record=@{control_type=$c.ControlType.ProgrammaticName;
                    name=$name.Substring(0, [Math]::Min(500, $name.Length));
                    hwnd=[long]$c.NativeWindowHandle; enabled=[bool]$c.IsEnabled;
                    is_password=[bool]$c.IsPassword; runtime_id=($element.GetRuntimeId() -join ':')}
                if (-not $record.runtime_id) {
                    throw [InvalidOperationException]::new('UIA control identity')
                }
                if ($c.ControlType -eq $controlTypes::ProgressBar) {
                    $metadata.progress++
                    if ($metadata.progress_controls.Count -ge 64) {
                        $metadata.truncated=$true
                    } else { $metadata.progress_controls+= $record }
                } else {
                    $metadata.interactive++
                    if ($metadata.interaction_controls.Count -ge 64) {
                        $metadata.truncated=$true
                    } else { $metadata.interaction_controls+= $record }
                }
            }
            Assert-DesktopDialogTime $watch $seconds
        }
        $metadata.layout_body_controls_complete=(
            $metadata.unexpected_controls.Count -eq 0 -and -not $metadata.truncated)
        $phase='uia_after'
        Assert-DesktopDialogTime $watch $seconds
        $again=$automationType::FromHandle([IntPtr]$hwnd)
        $metadata.uia_runtime_id_after=$again.GetRuntimeId() -join ':'
        $againCurrent=$again.Current
        $metadata.uia_owned_after=($againCurrent.ProcessId -eq $ownerPid -and
                                  [long]$againCurrent.NativeWindowHandle -eq $hwnd)
        $metadata.uia_visible_after=-not $againCurrent.IsOffscreen
        $rootName=[string]$againCurrent.Name
        if ($rootName.Length -gt 500) { $metadata.truncated=$true }
        $metadata.uia_root_name_after=$rootName.Substring(0, [Math]::Min(500, $rootName.Length))
        $phase='native_after'
        Assert-DesktopDialogTime $watch $seconds
        $after=[NativeDesktopDialogWindow]::Read($hwnd, $ownerPid)
        $metadata.native_after=$after
        Assert-DesktopDialogTime $watch $seconds
        $metadata.native_ownership_complete=(
            $before.owned_before -and $before.owned_after -and
            $after.owned_before -and $after.owned_after -and
            $before.pid_before -eq $ownerPid -and $before.pid_after -eq $ownerPid -and
            $after.pid_before -eq $ownerPid -and $after.pid_after -eq $ownerPid)
        $metadata.native_complete=($before.complete -and $after.complete -and
            $before.visible -and $after.visible -and
            ($null -eq $native -or $native.Complete))
        $metadata.truncated=($metadata.truncated -or $before.truncated -or $after.truncated)
        $metadata.uia_complete=($metadata.uia_visible_before -and $metadata.uia_visible_after)
        $metadata.caption_changed=($metadata.caption_changed -or $before.title -cne $after.title)
        if ($before.hwnd -ne $after.hwnd -or $after.hwnd -ne $hwnd) {
            $metadata.identity_change_fields+= 'hwnd'
        }
        if ($before.class_name -cne $after.class_name) {
            $metadata.identity_change_fields+= 'class_name'
        }
        if ($before.owner_hwnd -ne $after.owner_hwnd) {
            $metadata.identity_change_fields+= 'owner_hwnd'
        }
        if ($metadata.uia_runtime_id_before -cne $metadata.uia_runtime_id_after) {
            $metadata.identity_change_fields+= 'uia_runtime_id'
        }
        $metadata.immutable_identity_complete=(
            $metadata.native_ownership_complete -and $metadata.native_complete -and
            $metadata.uia_complete -and $metadata.uia_owned_before -and $metadata.uia_owned_after -and
            $metadata.identity_change_fields.Count -eq 0 -and -not $metadata.truncated)
        if (-not $metadata.native_ownership_complete -or -not $metadata.uia_owned_after) {
            $metadata.status='ownership_changed'
        } elseif ($metadata.identity_change_fields.Count -gt 0) {
            $metadata.status='identity_changed'
        } elseif (-not $postApprovedLayout -and (
            $before.title -cne $after.title -or
            ($metadata.caption_changed -and -not $captionObservationOnly))) {
            # Compatibility: normal callers still stop. Caption is separate,
            # mutable metadata, not an immutable identity_change_fields entry.
            $metadata.status='identity_changed'
        } else {
            $metadata.status='observed'
        }
    } catch {
        $metadata.uia_error='ui_provider_unreadable'
        $metadata.exception_type=$_.Exception.GetType().FullName
        if ($_.Exception -is [TimeoutException] -or
            $watch.Elapsed.TotalSeconds -ge $seconds) {
            $metadata.status='timed_out'; $metadata.uia_error='dialog_observation_timeout'
        }
    }
    $metadata.operation=$phase; $metadata.elapsed_ms=[long]$watch.Elapsed.TotalMilliseconds
    $metadata.strict_classification=Get-DesktopDialogClassification $metadata
    return $metadata
}
""".replace("__ATTENTION_PATTERN__", DIALOG_ATTENTION_PATTERN).replace(
    "__BUSY_TITLE_PATTERN__", DIALOG_BUSY_TITLE_PATTERN)

_TAB_SCRIPT = NATIVE_ACCESSIBILITY_SCRIPT + r"""
$q = [Console]::In.ReadToEnd() | ConvertFrom-Json
$clock = [Diagnostics.Stopwatch]::StartNew()
$result = @{status='inaccessible'; note='No uniquely named native page tab';
            discovered=@(); selected=$null; provider='msaa';
            discovery_phase='before_selection'}
if (-not [NativeDesktopAccessibility]::Owned([long]$q.hwnd, [int]$q.pid)) {
    @{status='ownership_changed'; cause='window_ownership_changed';
      note='window_ownership_changed'; retryable=$false} | ConvertTo-Json -Compress
    exit
}
# A native ribbon tab must not stand in for a Power BI page. Its WebView report
# host must be qualified by the existing UIA path; no MSAA fallback for PBI.
if ([Diagnostics.Process]::GetProcessById([int]$q.pid).ProcessName -eq 'PBIDesktop') {
    $result.note = 'Power BI page-host scoping requires UI Automation'
} else {
    $snapshot = [NativeDesktopAccessibility]::Read([long]$q.hwnd, [int]$q.pid, $false)
    if (-not $snapshot.Owned) { throw 'Document window ownership changed' }
    $result.discovered = @($snapshot.Tabs | ForEach-Object {
        @{name=$_.Name; selected=(($_.State -band 2) -ne 0);
          offscreen=(($_.State -band 0x18000) -ne 0); runtime_id=$_.RuntimeId;
          control_type='PAGETAB'; provider='msaa'}
    })
    $matches = @($snapshot.Tabs | Where-Object { $_.Name -ceq [string]$q.name })
    if (-not $snapshot.Complete) {
        $result.note = 'Native accessibility tree incomplete; uniqueness cannot be proved'
        $result.cause = 'partial_provider_observation'
        $result.retryable = $false
    } elseif ($matches.Count -gt 1) {
        $result.status = 'ambiguous'; $result.note = 'Multiple exact-name native page tabs'
    } elseif ($matches.Count -eq 1) {
        $tab = $matches[0]
        if (($tab.State -band 0x18001) -eq 0 -and
            [NativeDesktopAccessibility]::Enabled([long]$q.hwnd)) {
            if ($q.mode -eq 'select') { $tab.Select() }
            do {
                $tab.Refresh()
                if (-not [NativeDesktopAccessibility]::Owned([long]$q.hwnd, [int]$q.pid)) {
                    throw 'Document window ownership changed'
                }
                if (-not [NativeDesktopAccessibility]::Enabled([long]$q.hwnd)) {
                    throw 'Document window became disabled during native selection'
                }
                if ($tab.Name -cne [string]$q.name) { throw 'Native page-tab identity changed' }
                if (($tab.State -band 2) -ne 0 -and ($tab.State -band 0x18001) -eq 0) {
                    $result.status = 'verified'
                    $result.note = 'Exact native PAGETAB name and SELECTED state verified'
                    $result.selected = @{name=$tab.Name; runtime_id=$tab.RuntimeId;
                        automation_id=''; provider='msaa'}
                    $result.discovered = @($snapshot.Tabs | ForEach-Object {
                        $_.Refresh()
                        @{name=$_.Name; selected=(($_.State -band 2) -ne 0);
                          offscreen=(($_.State -band 0x18000) -ne 0);
                          runtime_id=$_.RuntimeId; control_type='PAGETAB'; provider='msaa'}
                    })
                    $result.discovery_phase = 'after_selection'
                    break
                }
                if ($q.mode -eq 'verify') { break }
                Start-Sleep -Milliseconds 100
            } while ($clock.Elapsed.TotalSeconds -lt [double]$q.seconds)
        }
    }
}
$result | ConvertTo-Json -Depth 6 -Compress
"""


def run_accessibility_script(script: str, payload: dict, timeout: float) -> dict:
    """Run only the helper child, with no minimum that inflates a small budget."""
    return run_powershell_json(script, payload, timeout=timeout)


def select_native_tab(hwnd: int, pid: int, name: str, *, timeout: float,
                      verify_only: bool = False) -> dict:
    """Only named MSAA PAGETABs in PAGETABLISTs; unknown ListItems fail closed."""
    result = run_accessibility_script(_TAB_SCRIPT, {
        "hwnd": hwnd, "pid": pid, "name": name, "seconds": timeout,
        "mode": "verify" if verify_only else "select",
    }, timeout)
    if not is_powershell_failure(result) and result.get("status") not in {
            "verified", "inaccessible", "ambiguous", "timed_out", "ownership_changed"}:
        return {"status": "error", "cause": "native_tab_result_invalid",
                "note": "native_tab_result_invalid", "retryable": False}
    return result
