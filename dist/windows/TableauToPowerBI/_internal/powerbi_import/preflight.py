"""Phase 1 — Pre-flight Rejection (Sprint 141 / v31.4.0).

Refuse migration **early** when the workbook is doomed to fail. Runs
*before* extraction, so we never spend cycles on inputs we cannot handle.

Design contract
---------------
* Pure read-only analysis — never mutates the input file.
* Zero external dependencies — stdlib only (zipfile, xml, os).
* Fast — should complete in < 200 ms for any workbook.
* Deterministic — same input → same `PreflightResult`.

Severity ladder
---------------
* ``BLOCKER``  — migration cannot proceed, exit immediately
* ``WARNING``  — proceed but flag (e.g. unsupported version)
* ``ADVISORY`` — informational (e.g. very large workbook)

The orchestrator (``migrate.py``) calls :func:`run_preflight` with the input
path. A non-empty ``blockers`` list is grounds for hard exit; ``--force`` (a
new flag) demotes blockers to warnings (escape hatch for power users).

See ``docs/ROADMAP.md`` for the full strategy.
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional

from powerbi_import.security_validator import SecurityError, safe_parse_xml

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
#  Constants
# ────────────────────────────────────────────────────────────────────

BLOCKER = "blocker"
WARNING = "warning"
ADVISORY = "advisory"

SUPPORTED_EXTENSIONS = {".twb", ".twbx", ".tds", ".tdsx", ".tfl", ".tflx"}

# Tableau version we officially support (read from `<workbook source-build>`).
# Anything strictly greater triggers a WARNING (require --force).
SUPPORTED_TABLEAU_MAJOR = 2024
SUPPORTED_TABLEAU_MINOR = 3

# Soft thresholds — anything above triggers an ADVISORY suggesting
# `--shared-model` or splitting.
LARGE_WORKBOOK_BYTES = 500 * 1024 * 1024     # 500 MB
LARGE_VISUAL_COUNT = 1_000

# Connectors we know we cannot translate. Maps connector class → suggestion.
UNSUPPORTED_CONNECTORS = {
    "splunk": "Splunk legacy connector — re-export to Parquet/CSV first",
    "hive-0.x": "Hive 0.x — upgrade to Hive 2.x or use SparkSQL",
    "essbase": "Essbase — no Power BI equivalent; export to flat file",
    "msolap-cube": "Multi-dimensional OLAP cubes are not supported by PBI",
    "alibabaaspark": "Alibaba MaxCompute — no native PBI driver",
}

# Path traversal / ZIP-slip patterns
_TRAVERSAL_PAT = re.compile(r"(^|/)\.\.(/|$)")


# ────────────────────────────────────────────────────────────────────
#  Result dataclass
# ────────────────────────────────────────────────────────────────────

@dataclass
class PreflightIssue:
    """A single issue raised by pre-flight."""
    severity: str           # 'blocker' | 'warning' | 'advisory'
    code: str               # short stable identifier (e.g. 'corrupt_xml')
    message: str            # human-readable description
    suggestion: str = ""    # how to fix / work around

    def as_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass
class PreflightResult:
    """Aggregated pre-flight result."""
    path: str
    issues: List[PreflightIssue] = field(default_factory=list)

    # ---- helpers -----------------------------------------------------
    def add(self, severity: str, code: str, message: str, suggestion: str = "") -> None:
        self.issues.append(PreflightIssue(severity, code, message, suggestion))

    @property
    def blockers(self) -> List[PreflightIssue]:
        return [i for i in self.issues if i.severity == BLOCKER]

    @property
    def warnings(self) -> List[PreflightIssue]:
        return [i for i in self.issues if i.severity == WARNING]

    @property
    def advisories(self) -> List[PreflightIssue]:
        return [i for i in self.issues if i.severity == ADVISORY]

    @property
    def ok(self) -> bool:
        """True iff no blockers."""
        return not self.blockers

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "ok": self.ok,
            "blockers": [i.as_dict() for i in self.blockers],
            "warnings": [i.as_dict() for i in self.warnings],
            "advisories": [i.as_dict() for i in self.advisories],
        }

    def format_console(self) -> str:
        """Return a human-readable summary for stdout."""
        if not self.issues:
            return "Pre-flight: OK (no issues detected)"
        lines = ["Pre-flight summary:"]
        for sev, label in (
            (BLOCKER, "BLOCKER"),
            (WARNING, "WARNING"),
            (ADVISORY, "ADVISORY"),
        ):
            for issue in [i for i in self.issues if i.severity == sev]:
                lines.append(f"  [{label}] {issue.code}: {issue.message}")
                if issue.suggestion:
                    lines.append(f"           → {issue.suggestion}")
        return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
#  Individual checks
# ────────────────────────────────────────────────────────────────────

def _check_path(path: str, result: PreflightResult) -> bool:
    """Reject empty / null-byte / non-existent / wrong-extension paths."""
    if not path:
        result.add(BLOCKER, "empty_path", "No workbook path provided")
        return False

    if "\x00" in path:
        result.add(BLOCKER, "null_byte_path",
                    "Path contains a null byte (security risk)")
        return False

    resolved = os.path.realpath(path)
    if not os.path.exists(resolved):
        result.add(BLOCKER, "missing_file",
                    f"File not found: {path}")
        return False

    ext = os.path.splitext(resolved)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        result.add(
            BLOCKER, "unsupported_extension",
            f"Extension {ext!r} is not supported",
            suggestion=f"Use one of {sorted(SUPPORTED_EXTENSIONS)}",
        )
        return False

    return True


def _check_size(path: str, result: PreflightResult) -> None:
    """Advise on very large workbooks."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size > LARGE_WORKBOOK_BYTES:
        mb = size // (1024 * 1024)
        result.add(
            ADVISORY, "large_workbook",
            f"Workbook is {mb} MB — migration may be slow",
            suggestion="Consider splitting via --shared-model",
        )


def _check_zip_integrity(path: str, result: PreflightResult) -> Optional[zipfile.ZipFile]:
    """For .twbx/.tdsx/.tflx: check ZIP can be opened and has no traversal."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".twbx", ".tdsx", ".tflx"):
        return None

    try:
        zf = zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile:
        result.add(
            BLOCKER, "corrupt_archive",
            "Workbook archive is corrupt or not a valid ZIP",
            suggestion="Re-export the workbook from Tableau Desktop",
        )
        return None
    except OSError as e:
        result.add(BLOCKER, "archive_io_error", f"Cannot read archive: {e}")
        return None

    # ZIP slip / traversal detection
    for name in zf.namelist():
        if name.startswith("/") or _TRAVERSAL_PAT.search(name) or "\x00" in name:
            result.add(
                BLOCKER, "zip_traversal",
                f"Archive entry contains path traversal: {name!r}",
                suggestion="The workbook may be malicious — do not migrate",
            )
            zf.close()
            return None

    twb_members = [name for name in zf.namelist()
                   if name.lower().endswith('.twb')]
    if len(twb_members) > 1:
        result.add(
            BLOCKER, "duplicate_twb",
            f"Workbook archive contains multiple .twb members: {twb_members!r}",
            suggestion="Keep exactly one workbook XML member in the archive",
        )

    return zf


def _read_twb_xml(path: str, zf: Optional[zipfile.ZipFile]) -> Optional[bytes]:
    """Return the raw .twb XML bytes for a .twb or .twbx."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".twb":
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None
    if zf is None:
        return None
    # Find the .twb inside
    for name in zf.namelist():
        if name.lower().endswith(".twb"):
            try:
                return zf.read(name)
            except (KeyError, OSError):
                return None
    return None


def _check_xml_well_formed(xml_bytes: bytes,
                             result: PreflightResult) -> Optional[ET.Element]:
    """Parse XML, recording a BLOCKER on failure."""
    if not xml_bytes:
        result.add(
            BLOCKER, "empty_xml",
            "Workbook XML is empty",
            suggestion="Re-export from Tableau Desktop",
        )
        return None
    try:
        return safe_parse_xml(xml_bytes)
    except (ET.ParseError, SecurityError) as e:
        result.add(
            BLOCKER, "corrupt_xml",
            f"Workbook XML is malformed: {e}",
            suggestion="Open in Tableau Desktop and re-save",
        )
        return None


def _check_xml_coherence(root: Optional[ET.Element], result: PreflightResult) -> None:
    """Check high-confidence references between Tableau XML object families."""
    if root is None:
        return

    def named(tag):
        return [node.attrib.get("name", "") for node in root.iter(tag)
                if node.attrib.get("name")]

    worksheets = set(named("worksheet"))
    datasource_root = next((node for node in root if node.tag == "datasources"), None)
    datasource_nodes = list(datasource_root) if datasource_root is not None else []
    datasources = {node.attrib.get("name", "") for node in datasource_nodes
                   if node.tag == "datasource" and node.attrib.get("name")}
    for tag, code, label in (("worksheet", "duplicate_worksheet_name", "worksheet"),
                             ("dashboard", "duplicate_dashboard_name", "dashboard")):
        names = named(tag)
        duplicates = sorted({name for name in names if names.count(name) > 1})
        for name in duplicates:
            result.add(BLOCKER, code, f"Duplicate {label} name: {name!r}",
                       "Rename the duplicate Tableau objects before migration")

    for dependency in root.iter("datasource-dependencies"):
        ref = dependency.attrib.get("datasource", "")
        if ref and ref not in datasources:
            result.add(BLOCKER, "missing_datasource_reference",
                       f"Worksheet references missing datasource {ref!r}",
                       "Restore the datasource or update the worksheet dependency")
    for node in root.iter("zone-contains"):
        text = (node.text or "").strip()
        match = re.match(r"Worksheet:\s*(.+)$", text)
        if match and match.group(1) not in worksheets:
            result.add(BLOCKER, "missing_worksheet_reference",
                       f"Dashboard references missing worksheet {match.group(1)!r}",
                       "Restore the worksheet or remove the dashboard zone")
    for node in root.iter():
        target = node.attrib.get("target-sheet", "")
        if target and target not in worksheets and target not in named("dashboard"):
            result.add(BLOCKER, "missing_navigation_target",
                       f"Navigation references missing sheet {target!r}",
                       "Restore the target sheet or update the navigation action")


def _check_encryption(zf: Optional[zipfile.ZipFile],
                       result: PreflightResult) -> bool:
    """Detect password-protected ZIP entries."""
    if zf is None:
        return False
    for info in zf.infolist():
        # Bit 0 of the general purpose flag indicates encryption
        if info.flag_bits & 0x1:
            result.add(
                BLOCKER, "encrypted_workbook",
                "Workbook is password-protected",
                suggestion="Remove password in Tableau Desktop and re-save",
            )
            return True
    return False


def _check_tableau_version(root: Optional[ET.Element],
                            result: PreflightResult) -> None:
    """Warn when source-build is newer than the supported version."""
    if root is None:
        return
    build = root.attrib.get("source-build") or root.attrib.get("source-platform", "")
    # Pattern: '2024.3.1' or '2025.1.0' etc.
    m = re.match(r"(\d{4})\.(\d+)", build)
    if not m:
        return
    major = int(m.group(1))
    minor = int(m.group(2))
    if (major, minor) > (SUPPORTED_TABLEAU_MAJOR, SUPPORTED_TABLEAU_MINOR):
        result.add(
            WARNING, "newer_tableau_version",
            f"Workbook saved by Tableau {build} — newer than tested "
            f"({SUPPORTED_TABLEAU_MAJOR}.{SUPPORTED_TABLEAU_MINOR})",
            suggestion="Migration may work but features added after are unsupported. "
                      "Use --force to proceed.",
        )


def _check_connectors(root: Optional[ET.Element],
                       result: PreflightResult) -> None:
    """Flag every datasource connection class we cannot translate."""
    if root is None:
        return
    for conn in root.iter("connection"):
        cls = (conn.attrib.get("class") or "").lower()
        if cls in UNSUPPORTED_CONNECTORS:
            result.add(
                BLOCKER, "unsupported_connector",
                f"Datasource uses unsupported connector class {cls!r}",
                suggestion=UNSUPPORTED_CONNECTORS[cls],
            )


def _check_visual_count(root: Optional[ET.Element],
                         result: PreflightResult) -> int:
    """Count worksheets; advise when very large."""
    if root is None:
        return 0
    count = sum(1 for _ in root.iter("worksheet"))
    if count > LARGE_VISUAL_COUNT:
        result.add(
            ADVISORY, "many_worksheets",
            f"Workbook has {count} worksheets — Power BI handles this but "
            f"migration may be slow",
            suggestion="Consider --shared-model to split into thin reports",
        )
    return count


def _check_extracts(zf: Optional[zipfile.ZipFile],
                     root: Optional[ET.Element],
                     result: PreflightResult) -> None:
    """For .twbx with `<extract>` references, verify the .hyper files exist."""
    if zf is None or root is None:
        return
    archived = {n.lower() for n in zf.namelist()}
    for ext in root.iter("extract"):
        rel = (ext.attrib.get("connection") or
               ext.attrib.get("filename") or "")
        if not rel:
            continue
        if not any(name.endswith(rel.lower()) for name in archived):
            result.add(
                WARNING, "missing_extract",
                f"Workbook references extract {rel!r} not present in archive",
                suggestion="Re-publish from Tableau Desktop with extracts attached, "
                           "or re-run with --ignore-missing-extracts.",
            )


# ────────────────────────────────────────────────────────────────────
#  Public entry point
# ────────────────────────────────────────────────────────────────────

def run_preflight(path: str) -> PreflightResult:
    """Run all pre-flight checks against ``path``.

    Returns a :class:`PreflightResult`. Inspect ``result.ok`` to decide
    whether to proceed.
    """
    result = PreflightResult(path=path)

    if not _check_path(path, result):
        return result  # nothing else makes sense

    _check_size(path, result)

    zf = _check_zip_integrity(path, result)
    try:
        if not result.ok:
            return result

        encrypted = _check_encryption(zf, result)
        if encrypted:
            return result

        xml_bytes = _read_twb_xml(path, zf)
        if xml_bytes is None and path.lower().endswith((".twb", ".twbx")):
            result.add(
                BLOCKER, "missing_twb",
                "Could not locate .twb XML inside archive",
                suggestion="The file may be corrupt — re-export from Tableau Desktop",
            )
            return result

        root = _check_xml_well_formed(xml_bytes or b"", result) if xml_bytes is not None else None
        _check_tableau_version(root, result)
        _check_xml_coherence(root, result)
        _check_connectors(root, result)
        _check_visual_count(root, result)
        _check_extracts(zf, root, result)
    finally:
        if zf is not None:
            zf.close()

    return result


__all__ = [
    "BLOCKER", "WARNING", "ADVISORY",
    "PreflightIssue", "PreflightResult",
    "run_preflight",
]
