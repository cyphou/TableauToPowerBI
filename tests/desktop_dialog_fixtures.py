"""Shared dialog evidence and inert provider fakes; no UIA, Win32 or app access."""

import copy
from typing import NotRequired, TypedDict

from powerbi_import import desktop_accessibility as accessibility

PROGRESS_CONTROL = {
    "control_type": "ControlType.ProgressBar", "name": "", "hwnd": 0,
    "enabled": True, "is_password": False, "runtime_id": "progress:synthetic:1",
}

LAYOUT_TEXTS = [
    "Processing Request", "Computing View Layout", "Elapsed time 00:03", "Cancel",
]
CAPTION_TRANSITION_TITLE = "Constructing "


class _LayoutNode(TypedDict):
    type: str
    name: str
    hwnd: int
    identity: str


class _LayoutDialogCase(TypedDict):
    nodes: list[_LayoutNode]
    native_after: NotRequired[dict[str, str]]


def layout_dialog_case() -> _LayoutDialogCase:
    """The verified layout shape has text/timer/Cancel, but no ProgressBar."""
    return {
        "nodes": [
            {"type": "Text", "name": LAYOUT_TEXTS[1], "hwnd": 0,
             "identity": "caption:synthetic:1"},
            {"type": "Text", "name": LAYOUT_TEXTS[2], "hwnd": 0,
             "identity": "timer:synthetic:1"},
            {"type": "Button", "name": LAYOUT_TEXTS[3], "hwnd": 32,
             "identity": "button:synthetic:2"},
        ],
    }


def layout_caption_transition_case() -> _LayoutDialogCase:
    """Only the native caption changes during the owned layout observation."""
    case = layout_dialog_case()
    case["nodes"][1]["name"] = "Elapsed time 00:05"
    case["native_after"] = {"title": CAPTION_TRANSITION_TITLE}
    return case


def retained_layout_identity():
    """Pinned identity of this page's first complete, conclusively busy layout."""
    return {
        "hwnd": 31, "owner_pid": 42, "owner_hwnd": 11,
        "title": "Processing Request", "class_name": "SyntheticModal",
        "runtime_id": "dialog:synthetic:31", "progress_basis": "tableau_layout_timer",
        "absence_observed": False,
    }


def strict_dialog_metadata(**changes):
    native = {
        "hwnd": 31, "owner_hwnd": 11, "pid_before": 42, "pid_after": 42,
        "owned_before": True, "owned_after": True, "visible": True,
        "complete": True, "truncated": False,
        "title": "Processing Request", "class_name": "SyntheticModal",
    }
    return {
        "status": "observed", "native_before": copy.deepcopy(native),
        "native_after": copy.deepcopy(native),
        "native_complete": True, "native_ownership_complete": True,
        "native_accessibility_complete": None, "uia_complete": True,
        "uia_error": None, "truncated": False, "texts": ["Processing Request"],
        "buttons": [{"name": "Cancel", "control_id": 2}],
        "progress": 1, "interactive": 0,
        "progress_controls": [copy.deepcopy(PROGRESS_CONTROL)], "interaction_controls": [],
        "strict_classification": {
            "status": "busy", "basis": "shared_strict_uia", "complete": True,
            "progress_required": True, "protected_text": False, "cancel_only": True,
            "reason": "complete_progress_structure",
        },
        **changes,
    }


def layout_dialog_metadata(**changes):
    metadata = strict_dialog_metadata(**{
        "texts": LAYOUT_TEXTS.copy(), "progress": 0, "progress_controls": [],
        "uia_count": 3, "uia_owned_before": True, "uia_owned_after": True,
        "uia_visible_before": True, "uia_visible_after": True,
        "uia_runtime_id_before": "dialog:synthetic:31",
        "uia_runtime_id_after": "dialog:synthetic:31", **changes,
    })
    # This is raw provider evidence, not an assumed classifier result.
    metadata.pop("strict_classification")
    return metadata


def inert_dialog_observer_script():
    """Run the unchanged shared PS functions against inert, in-memory providers."""
    marker = "function Assert-DesktopDialogTime("
    functions = marker + accessibility.DESKTOP_DIALOG_OBSERVER_SCRIPT.split(marker, 1)[1]
    return r"""
Add-Type -TypeDefinition @'
using System;
namespace Windows.Automation {
    public enum TreeScope { Descendants, Children }
    public enum ExpandCollapseState { Collapsed, Expanded, LeafNode }
    public enum ToggleState { Off, On }
    public class Condition { public static object TrueCondition = new object(); }
    public class PropertyCondition {
        public object Property, Value;
        public PropertyCondition(object property, object value) { Property=property; Value=value; }
    }
    public class ExpandCollapsePattern { public static object Pattern = "expand"; }
    public class InvokePattern { public static object Pattern = "invoke"; }
    public class TogglePattern { public static object Pattern = "toggle"; }
    public class TreeWalker { public static object RawViewWalker; }
    public class ControlType {
        public string ProgrammaticName;
        ControlType(string name) { ProgrammaticName = "ControlType." + name; }
        public static ControlType Window = new ControlType("Window");
        public static ControlType Button = new ControlType("Button");
        public static ControlType Text = new ControlType("Text");
        public static ControlType ProgressBar = new ControlType("ProgressBar");
        public static ControlType Edit = new ControlType("Edit");
        public static ControlType ComboBox = new ControlType("ComboBox");
        public static ControlType Hyperlink = new ControlType("Hyperlink");
        public static ControlType CheckBox = new ControlType("CheckBox");
        public static ControlType RadioButton = new ControlType("RadioButton");
        public static ControlType MenuBar = new ControlType("MenuBar");
        public static ControlType Menu = new ControlType("Menu");
        public static ControlType MenuItem = new ControlType("MenuItem");
        public static ControlType Pane = new ControlType("Pane");
        public static ControlType Group = new ControlType("Group");
        public static ControlType Separator = new ControlType("Separator");
        public static ControlType ListItem = new ControlType("ListItem");
    }
    public static class AutomationElement {
        public static object Root, AfterRoot, Document;
        public static object AutomationIdProperty = "AutomationId";
        public static object ControlTypeProperty = "ControlType";
        public static int Reads;
        public static Action ReadHook;
        public static object FromHandle(IntPtr hwnd) {
            if (hwnd.ToInt64() == 11 && Document != null) return Document;
            Reads++;
            if (ReadHook != null) ReadHook();
            return Reads == 1 ? Root : AfterRoot;
        }
    }
}
public static class NativeDesktopDialogWindow {
    public static object Before, After;
    public static Func<long,int,object> SnapshotHook;
    public static Action SnapshotReturnedHook;
    public static int Reads, ChildPid = 42, LastPid;
    public static long LastHwnd;
    public static object Read(long hwnd, int pid) {
        LastHwnd = hwnd; LastPid = pid;
        Reads++;
        object snapshot = SnapshotHook != null ? SnapshotHook(hwnd, pid)
            : Reads == 1 ? Before : After;
        if (SnapshotReturnedHook != null) SnapshotReturnedHook();
        return snapshot;
    }
    public static int Pid(long hwnd) { return ChildPid; }
    public static int ControlId(long hwnd) { return hwnd == 32 ? 2 : 9; }
}
'@
function Synthetic-DialogNode($identity, $current) {
    $node=[pscustomobject]@{Identity=$identity; Current=$current}
    $node | Add-Member ScriptMethod GetRuntimeId { return @($this.Identity) }
    return $node
}
function Set-SyntheticDialogNodes($records) {
    $script:syntheticDialogNodes=@()
    foreach ($record in $records) {
        $type=[Windows.Automation.ControlType]::($record.type)
        $current=@{Name=[string]$record.name; ProcessId=42; NativeWindowHandle=[long]$record.hwnd;
            IsOffscreen=$false; IsEnabled=$true; IsPassword=$false; ControlType=$type}
        foreach ($property in $record.current.PSObject.Properties) {
            $current[$property.Name]=$property.Value
        }
        $script:syntheticDialogNodes+=Synthetic-DialogNode $record.identity $current
    }
}
function Initialize-SyntheticDialog($case, $expected=$null) {
    $before=[pscustomobject]@{
        hwnd=31; owner_hwnd=11; pid_before=42; pid_after=42;
        owned_before=$true; owned_after=$true; visible=$true; complete=$true; truncated=$false;
        title='Processing Request'; class_name='SyntheticModal'}
    if ($null -ne $expected) {
        foreach ($key in @('hwnd','owner_hwnd','title','class_name')) {
            $before.$key=$expected.$key
        }
    }
    foreach ($property in $case.native_before.PSObject.Properties) {
        $before.($property.Name)=$property.Value
    }
    $after=$before | Select-Object *
    foreach ($property in $case.native_after.PSObject.Properties) {
        $after.($property.Name)=$property.Value
    }
    $script:syntheticDialogExpected=$before | Select-Object *
    $rootCurrent=@{Name=$before.title; ProcessId=42; NativeWindowHandle=$before.hwnd;
        IsOffscreen=$false; IsEnabled=$true; IsPassword=$false;
        ControlType=[Windows.Automation.ControlType]::Window}
    foreach ($property in $case.root_before.PSObject.Properties) {
        $rootCurrent[$property.Name]=$property.Value
    }
    $identity=$(if ($case.root_identity) { $case.root_identity } else { 'dialog:synthetic:31' })
    $root=Synthetic-DialogNode $identity $rootCurrent
    $afterCurrent=$rootCurrent.Clone()
    foreach ($property in $case.root_after.PSObject.Properties) {
        $afterCurrent[$property.Name]=$property.Value
    }
    $afterIdentity=$(if ($case.after_identity) { $case.after_identity } else { $identity })
    $again=Synthetic-DialogNode $afterIdentity $afterCurrent
    $records=$(if ($null -ne $case.nodes) { @($case.nodes) } else {
        @(@{type='ProgressBar'; name=''; hwnd=0; identity='progress:synthetic:1'},
          @{type='Button'; name='Cancel'; hwnd=32; identity='button:synthetic:2'})
    })
    Set-SyntheticDialogNodes $records
    $root | Add-Member ScriptMethod FindAll {
        param($scope, $condition)
        return ,@($script:syntheticDialogNodes)
    }
    $again | Add-Member ScriptMethod FindAll {
        param($scope, $condition)
        return ,@($script:syntheticDialogNodes)
    }
    [Windows.Automation.AutomationElement]::Root=$root
    [Windows.Automation.AutomationElement]::AfterRoot=$again
    [Windows.Automation.AutomationElement]::Reads=0
    [Windows.Automation.AutomationElement]::ReadHook=$null
    [NativeDesktopDialogWindow]::Before=$before
    [NativeDesktopDialogWindow]::After=$after
    [NativeDesktopDialogWindow]::Reads=0
    [NativeDesktopDialogWindow]::SnapshotHook=$null
    [NativeDesktopDialogWindow]::SnapshotReturnedHook=$null
    [NativeDesktopDialogWindow]::LastHwnd=0; [NativeDesktopDialogWindow]::LastPid=0
    [NativeDesktopDialogWindow]::ChildPid=$(if ($case.child_pid) { $case.child_pid } else { 42 })
}
""" + functions


def inert_tableau_menu_lifecycle_script():
    """Provider/clock fakes for execution of the unchanged production menu flow."""
    return inert_dialog_observer_script() + r"""
Add-Type -TypeDefinition @'
using System;
public static class TableauMenuWindow {
    public static Func<long,int,bool,object> GuardHook;
    public static bool PopupVisible, DialogVisible, DialogForeign, DialogGone;
    public static int DocumentPid = 42;
    public static bool Owned(long hwnd, int pid) { return DocumentPid == pid; }
    public static string PopupState(long hwnd, int pid) {
        if (hwnd == 31 && DialogForeign) return "foreign";
        if (hwnd == 31 && DialogGone) return "gone";
        if (hwnd == 31) return DocumentPid != pid ? "foreign" : DialogVisible ? "visible" : "hidden";
        return DocumentPid != pid ? "foreign" : PopupVisible ? "visible" : "hidden";
    }
    public static object ObserveGuard(long hwnd, int pid, bool dialogs) {
        return GuardHook(hwnd, pid, dialogs);
    }
}
'@
function New-SyntheticMenuGuard([bool]$dialogs) {
    $ownerPid=[TableauMenuWindow]::DocumentPid
    $enabled=-not $script:layoutBusy
    $ownedDialogs=@()
    if ($dialogs -and $script:layoutBusy -and -not $case.disabled_no_dialog) {
        $ownedDialogs+=@{hwnd=31; owner_hwnd=11; pid_before=42; pid_after=42;
            owned_before=$true; owned_after=$true; visible_before=$true; visible_after=$true;
            title=$script:syntheticDialogExpected.title;
            class_name=$script:syntheticDialogExpected.class_name;
            metadata_complete=$true; title_truncated=$false}
    }
    if ($case.disabled_after_wait -and $script:busyResolved) { $enabled=$false }
    $guard=@{basis='win32'; reason=$null; elapsed_ms=0; document_hwnd=11; expected_pid=42;
        document_pid_before=$ownerPid; document_pid_after=$ownerPid;
        document_owned_before=($ownerPid -eq 42); document_owned_after=($ownerPid -eq 42);
        document_enabled=$enabled; document_enabled_after=$enabled;
        document_visible=$true; document_visible_after=$true;
        dialog_check_performed=$dialogs; owned_dialog_present=($ownedDialogs.Count -gt 0);
        dialogs_complete=$dialogs; dialogs_truncated=$false; owned_dialogs=@($ownedDialogs)}
    [void]$script:menuEvents.Add(@{
        event='guard'; elapsed_ms=$clock.Elapsed.TotalMilliseconds;
        enabled=$enabled; dialog_count=$ownedDialogs.Count; dialogs_checked=$dialogs})
    return $guard
}
function Start-Sleep([int]$Milliseconds) {
    if ($Milliseconds -le 0 -or $script:menuSleeps.Count -gt 1000) {
        throw 'Synthetic invalid menu wait'
    }
    [void]$script:menuSleeps.Add($Milliseconds)
    $clock.Elapsed.TotalMilliseconds+=$Milliseconds
    $clock.Elapsed.TotalSeconds=$clock.Elapsed.TotalMilliseconds / 1000.0
    if ($script:layoutBusy -and -not $case.persistent_busy -and
        $clock.Elapsed.TotalMilliseconds -ge $script:invokedAt+$case.resolve_after_ms) {
        $script:layoutBusy=$false; $script:busyResolved=$true
        [TableauMenuWindow]::DialogForeign=[bool]$case.dialog_foreign_after_wait
        [TableauMenuWindow]::DialogGone=($case.absence_state -eq 'gone')
        [TableauMenuWindow]::DialogVisible=$false
        [Windows.Automation.AutomationElement]::Root.Current.IsOffscreen=$true
        [Windows.Automation.AutomationElement]::AfterRoot.Current.IsOffscreen=$true
        if ($case.hidden_reused) {
            [Windows.Automation.AutomationElement]::AfterRoot.Identity='dialog:reused'
        }
        [void]$script:menuEvents.Add(@{event='busy_resolved';
            elapsed_ms=$clock.Elapsed.TotalMilliseconds})
    }
    if ($script:layoutBusy -and $case.change_during_wait -and
        $clock.Elapsed.TotalMilliseconds -ge $script:invokedAt+500) {
        if ($case.change_during_wait -eq 'document') {
            $script:syntheticMenuDocument.Identity='document:changed'
        }
        if ($case.change_during_wait -eq 'menu') {
            $script:syntheticMenuBar.Identity='bar:changed'
        }
        if ($case.change_during_wait -eq 'dialog') {
            [Windows.Automation.AutomationElement]::AfterRoot.Identity='dialog:reused'
        }
    }
}
function Initialize-SyntheticMenu($case) {
    $script:menuEvents=[Collections.Generic.List[object]]::new()
    $script:menuSleeps=[Collections.Generic.List[int]]::new()
    $script:layoutBusy=$false; $script:busyResolved=$false
    $script:invokeCalls=0; $script:expandCalls=0; $script:collapseCalls=0
    $script:invokedAt=$null
    [TableauMenuWindow]::DocumentPid=42
    [TableauMenuWindow]::DialogForeign=$false
    [TableauMenuWindow]::DialogGone=$false
    [TableauMenuWindow]::DialogVisible=$false
    [TableauMenuWindow]::PopupVisible=$false
    Initialize-SyntheticDialog $case.dialog_case
    [NativeDesktopDialogWindow]::SnapshotHook=[Func[long,int,object]]{
        param($hwnd, $ownerPid)
        $snapshot=$(if ([NativeDesktopDialogWindow]::Reads -eq 1) {
            [NativeDesktopDialogWindow]::Before | Select-Object *
        } else { [NativeDesktopDialogWindow]::After | Select-Object * })
        if ($case.caption_transition_read) {
            if ([NativeDesktopDialogWindow]::Reads -eq $case.caption_transition_read-1 -and
                $null -ne $case.transition_nodes) {
                Set-SyntheticDialogNodes $case.transition_nodes
            }
            if ([NativeDesktopDialogWindow]::Reads -ge $case.caption_transition_read) {
                $snapshot.title=$case.transition_title
                foreach ($property in $case.transition_native.PSObject.Properties) {
                    $snapshot.($property.Name)=$property.Value
                }
                [Windows.Automation.AutomationElement]::AfterRoot.Current.Name=$case.transition_title
                if ([NativeDesktopDialogWindow]::Reads -eq $case.caption_transition_read -and
                    $case.transition_read_ms) {
                    $clock.Elapsed.TotalMilliseconds+=$case.transition_read_ms
                    $clock.Elapsed.TotalSeconds=$clock.Elapsed.TotalMilliseconds / 1000.0
                }
            }
        }
        if ($script:busyResolved -and -not $script:layoutBusy) {
            $snapshot.visible=$false; $snapshot.complete=$false
        }
        return $snapshot
    }
    if ($case.caption_fresh_expire) {
        [NativeDesktopDialogWindow]::SnapshotReturnedHook=[Action]{
            if ([NativeDesktopDialogWindow]::Reads -eq $case.caption_transition_read+1) {
                $clock.Elapsed.TotalMilliseconds=$case.budget*1000
                $clock.Elapsed.TotalSeconds=$case.budget
            }
        }
    }
    $document=Synthetic-DialogNode 'document:synthetic:11' @{
        Name='Synthetic Document'; ProcessId=42; NativeWindowHandle=11;
        ControlType=[Windows.Automation.ControlType]::Window}
    $bar=Synthetic-DialogNode 'bar:main' @{
        Name='Main Menu'; AutomationId='mainMenu'; ClassName='QMenuBar'; ProcessId=42;
        ControlType=[Windows.Automation.ControlType]::MenuBar; IsOffscreen=$false}
    $topItem=Synthetic-DialogNode 'top:window' @{
        Name='Window'; ClassName='QAction'; ProcessId=42; IsOffscreen=$false; IsEnabled=$true;
        ControlType=[Windows.Automation.ControlType]::MenuItem}
    $menuPopup=Synthetic-DialogNode 'popup:initial' @{
        Name='Window'; AutomationId='mainMenu.menu_Window'; ClassName='TableauMenu';
        ProcessId=42; NativeWindowHandle=41; IsOffscreen=$true;
        ControlType=[Windows.Automation.ControlType]::Menu}
    $expand=[pscustomobject]@{Current=[pscustomobject]@{
        ExpandCollapseState=[Windows.Automation.ExpandCollapseState]::Collapsed}}
    $expand | Add-Member ScriptMethod Expand {
        $script:expandCalls++
        if ($case.reappear_after_absence -and $script:busyResolved -and $script:expandCalls -ge 3) {
            $script:layoutBusy=$true
            [TableauMenuWindow]::DialogGone=$false
            [TableauMenuWindow]::DialogVisible=$true
            [Windows.Automation.AutomationElement]::Root.Current.IsOffscreen=$false
            [Windows.Automation.AutomationElement]::AfterRoot.Current.IsOffscreen=$false
            [void]$script:menuEvents.Add(@{event='dialog_reappeared';
                elapsed_ms=$clock.Elapsed.TotalMilliseconds})
        }
        $this.Current.ExpandCollapseState=[Windows.Automation.ExpandCollapseState]::Expanded
        [TableauMenuWindow]::PopupVisible=$true
        $script:syntheticMenuPopup.Current.IsOffscreen=$false
        $script:syntheticMenuPopup.Identity="popup:build-$script:expandCalls"
        [void]$script:menuEvents.Add(@{event='expand'; build=$script:expandCalls;
            elapsed_ms=$clock.Elapsed.TotalMilliseconds; busy=$script:layoutBusy})
    }
    $expand | Add-Member ScriptMethod Collapse {
        $script:collapseCalls++
        $this.Current.ExpandCollapseState=[Windows.Automation.ExpandCollapseState]::Collapsed
        [TableauMenuWindow]::PopupVisible=$false
        $script:syntheticMenuPopup.Current.IsOffscreen=$true
        [void]$script:menuEvents.Add(@{event='collapse';
            elapsed_ms=$clock.Elapsed.TotalMilliseconds})
    }
    $topItem | Add-Member ScriptMethod TryGetCurrentPattern {
        param($type, [ref]$pattern)
        if ($type -eq 'expand') { $pattern.Value=$script:syntheticMenuExpand; return $true }
        return $false
    }
    $document | Add-Member ScriptMethod FindAll {
        param($scope, $condition)
        if ($condition.Value -eq 'mainMenu') { return ,@($script:syntheticMenuBar) }
        if ([TableauMenuWindow]::PopupVisible) { return ,@($script:syntheticMenuPopup) }
        return ,@()
    }
    $bar | Add-Member ScriptMethod FindAll {
        param($scope, $condition)
        return ,@($script:syntheticMenuTopItem)
    }
    $menuPopup | Add-Member ScriptMethod FindAll {
        param($scope, $condition)
        $separator=Synthetic-DialogNode 'separator:synthetic' @{
            ControlType=[Windows.Automation.ControlType]::Separator}
        $items=@($separator)
        foreach ($name in @('Previous Synthetic Page', 'Synthetic Page')) {
            $item=Synthetic-DialogNode "item:$name`:build-$script:expandCalls" @{
                Name=$name; ClassName='QAction'; ProcessId=42; IsEnabled=$true; IsOffscreen=$false;
                ControlType=[Windows.Automation.ControlType]::MenuItem}
            $item | Add-Member ScriptMethod TryGetCurrentPattern {
                param($type, [ref]$pattern)
                if ($type -eq 'expand') { return $false }
                if ($type -eq 'toggle') {
                    $active=$(if ($script:invokeCalls -gt 0) {
                        'Synthetic Page' } else { 'Previous Synthetic Page' })
                    $state=$(if ($this.Current.Name -ceq $active) {
                        [Windows.Automation.ToggleState]::On
                    } else { [Windows.Automation.ToggleState]::Off })
                    $pattern.Value=[pscustomobject]@{Current=[pscustomobject]@{ToggleState=$state}}
                    return $true
                }
                if ($type -eq 'invoke') {
                    $invoke=[pscustomobject]@{}
                    $invoke | Add-Member ScriptMethod Invoke {
                        $script:invokeCalls++
                        if ($script:invokeCalls -gt 1) { throw 'Synthetic duplicate sheet invocation' }
                        $script:layoutBusy=$true
                        [TableauMenuWindow]::DialogVisible=$true
                        $script:invokedAt=$clock.Elapsed.TotalMilliseconds
                        [void]$script:menuEvents.Add(@{event='invoke'; elapsed_ms=$script:invokedAt})
                    }
                    $pattern.Value=$invoke
                    return $true
                }
                return $false
            }
            $items+=$item
        }
        [void]$script:menuEvents.Add(@{event='sheets'; build=$script:expandCalls;
            elapsed_ms=$clock.Elapsed.TotalMilliseconds; busy=$script:layoutBusy})
        return ,@($items)
    }
    $walker=[pscustomobject]@{}
    $walker | Add-Member ScriptMethod GetParent {
        param($item)
        if ($item.Identity -like 'item:*' -or $item.Identity -like 'separator:*') {
            return $script:syntheticMenuPopup
        }
        if ([object]::ReferenceEquals($item, $script:syntheticMenuPopup)) {
            return $script:syntheticMenuTopItem
        }
        if ([object]::ReferenceEquals($item, $script:syntheticMenuTopItem)) {
            return $script:syntheticMenuBar
        }
        if ([object]::ReferenceEquals($item, $script:syntheticMenuBar)) {
            return $script:syntheticMenuDocument
        }
        return $null
    }
    [Windows.Automation.TreeWalker]::RawViewWalker=$walker
    [Windows.Automation.AutomationElement]::Document=$document
    [TableauMenuWindow]::GuardHook=[Func[long,int,bool,object]]{
        param($hwnd, $ownerPid, $dialogs)
        return New-SyntheticMenuGuard $dialogs
    }
    # Explicit provider slots avoid dynamic caller locals and cross-case reuse.
    $script:syntheticMenuDocument=$document; $script:syntheticMenuBar=$bar
    $script:syntheticMenuTopItem=$topItem; $script:syntheticMenuPopup=$menuPopup
    $script:syntheticMenuExpand=$expand
}
"""
