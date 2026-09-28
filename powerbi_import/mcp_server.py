"""MCP server for the Tableau → Power BI migration engine (v44, Sprint 216).

Exposes the migration engine as Model Context Protocol tools so agents/IDEs can
call migration capabilities directly. Transport is stdlib JSON-RPC 2.0 over stdio
— zero external dependencies for the core path.

Tools:
    assess         Pre-migration readiness assessment (no artifacts written)
    migrate        Full extract → generate pipeline, returns output dir
    qa             Real-world QA report card on a generated .pbip project
    quality_report Unified deterministic quality report with openability diagnostics
    agent_handoff  Pull read-only quality priorities filtered to the requested owner
    agent_handoff_ack Record a proof-gated, process-memory acknowledgement
    parity_scan    Functionality-parity scan using the shipped parity registry
    shared_model   Build a shared semantic model from several workbooks
    diff           Compare source extraction vs generated output
    deploy         Deploy to Fabric / Power BI Service (guarded, dry-run default)
    llm_status     Report LLM gateway configuration and connectivity
    autoheal       Heal and re-validate a generated .pbip project
    verify_open    Preflight a generated .pbip for Power BI Desktop openability

Design principles (see docs/ROADMAP.md v44.0.0):
    * Tools are contracts — each has a typed input schema and structured output.
    * Secrets never transit tool args — the ``deploy`` tool reads credentials from
      the environment only, and refuses to run without an explicit ``confirm``.
    * Long work is synchronous per-call here; the REST ``api_server`` remains the
      job-oriented surface.

Usage:
    python -m powerbi_import.mcp_server            # stdio JSON-RPC loop
    python -m powerbi_import.mcp_server --list     # print tool catalogue as JSON
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import stat
import sys
import tempfile
import traceback

# Allow ``from tableau_export...`` / ``from powerbi_import...`` when run directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, os.path.join(_ROOT, "tableau_export")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logger = logging.getLogger("tableau_to_powerbi.mcp_server")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "tableau-to-powerbi"
SERVER_VERSION = "44.0.0"

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_ALLOWED_INPUT_EXT = (".twb", ".twbx", ".tds", ".tdsx")


# ════════════════════════════════════════════════════════════════════
#  Tool catalogue (the contract)
# ════════════════════════════════════════════════════════════════════

def _tool_catalogue():
    """Return the MCP ``tools/list`` payload. Order is stable for snapshots."""
    return [
        {
            "name": "assess",
            "description": "Run a pre-migration readiness assessment on a Tableau "
                           "workbook. Writes no artifacts; returns a GREEN/YELLOW/RED "
                           "report with per-category findings.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Path to .twb/.twbx/.tds/.tdsx"},
                },
                "required": ["file"],
            },
        },
        {
            "name": "migrate",
            "description": "Run the full extract -> generate pipeline and produce a "
                           ".pbip project (or Fabric-native artifacts).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Path to .twb/.twbx"},
                    "output_dir": {"type": "string", "description": "Output directory (optional)"},
                    "output_format": {"type": "string", "enum": ["pbip", "fabric"],
                                       "description": "Target format (default pbip)"},
                    "culture": {"type": "string", "description": "Locale, e.g. fr-FR (optional)"},
                },
                "required": ["file"],
            },
        },
        {
            "name": "qa",
            "description": "Run the real-world QA report card against a generated "
                           ".pbip project.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_dir": {"type": "string", "description": "Generated .pbip project dir"},
                    "extraction_dir": {"type": "string",
                                        "description": "Optional extraction JSON dir for zone matching"},
                },
                "required": ["project_dir"],
            },
        },
        {
            "name": "quality_report",
            "description": "Run the unified deterministic quality report, including "
                           "openability and semantic-context diagnostics.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Source Tableau workbook"},
                    "project_dir": {"type": "string", "description": "Generated project dir"},
                },
                "required": ["file", "project_dir"],
            },
        },
        {
            "name": "agent_handoff",
            "description": "Return a read-only packet of current quality-report priorities "
                           "owned by the requested agent. Requires quality_report to have "
                           "run in this MCP session; completion requires an external "
                           "agent/operator and a fresh quality report.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "Owner name, with optional @ prefix"},
                },
                "required": ["agent"],
            },
        },
        {
            "name": "agent_handoff_ack",
            "description": "Acknowledge a handoff issued by this MCP session. Applied "
                           "requires a fresh quality report for the same project, changed "
                           "PBIP definition artifacts, and a finding no longer prioritized. "
                           "Acknowledgements remain in process memory only.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "handoff_id": {"type": "string", "description": "ID from agent_handoff"},
                    "agent": {"type": "string", "description": "Owner token, with optional @ prefix"},
                    "outcome": {"type": "string", "enum": ["applied", "not_applicable"]},
                    "rationale": {"type": "string", "description": "Reason for this acknowledgement"},
                },
                "required": ["handoff_id", "agent", "outcome", "rationale"],
            },
        },
        {
            "name": "parity_scan",
            "description": "Scan a Tableau workbook for Tableau→Power BI functionality "
                           "parity (exact/approximated/healed/unsupported per feature).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Path to .twb/.twbx"},
                },
                "required": ["file"],
            },
        },
        {
            "name": "shared_model",
            "description": "Build a shared semantic model from several Tableau workbooks.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "files": {"type": "array", "items": {"type": "string"},
                              "description": "Two or more workbook paths"},
                    "model_name": {"type": "string", "description": "Shared model name"},
                    "output_dir": {"type": "string", "description": "Output directory (optional)"},
                },
                "required": ["files"],
            },
        },
        {
            "name": "diff",
            "description": "Compare a Tableau extraction against a generated .pbip "
                           "project and report field-level coverage gaps.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "extraction_dir": {"type": "string", "description": "Extraction JSON dir"},
                    "project_dir": {"type": "string", "description": "Generated .pbip project dir"},
                },
                "required": ["extraction_dir", "project_dir"],
            },
        },
        {
            "name": "deploy",
            "description": "Deploy a generated project to Fabric / Power BI Service. "
                           "GUARDED: dry-run by default; requires confirm=true to push; "
                           "credentials are read from environment variables only, never "
                           "from tool arguments.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_dir": {"type": "string", "description": "Generated .pbip project dir"},
                    "workspace_id": {"type": "string", "description": "Target workspace id"},
                    "confirm": {"type": "boolean",
                                "description": "Must be true to perform a real deploy"},
                    "dry_run": {"type": "boolean", "description": "Default true"},
                },
                "required": ["project_dir", "workspace_id"],
            },
        },
        {
            "name": "llm_status",
            "description": "Report the LLM gateway configuration and connectivity "
                           "(mode, route, provider, local/cloud reachability, budget). "
                           "Never returns secrets and performs only a cheap reachability "
                           "probe. Optional 'mode' overrides auto/online/offline.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["auto", "online", "offline"],
                             "description": "Optional mode override (default from env)"},
                },
                "required": [],
            },
        },
        {
            "name": "autoheal",
            "description": "Closed-loop autoheal of a generated .pbip so it opens "
                           "cleanly in Power BI Desktop: collect errors -> deterministic "
                           "heal (DAX/M/visual) -> optional LLM correction (via the LLM "
                           "gateway) -> re-validate -> apply only if it validates. Set "
                           "autofix=true to enable LLM correction; pass an error 'log' "
                           "exported from Desktop to target real load errors.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_dir": {"type": "string", "description": "Generated .pbip project dir"},
                    "autofix": {"type": "boolean", "description": "Enable LLM correction (default false)"},
                    "log": {"type": "string", "description": "Optional Desktop error export/FrownDump path"},
                    "mode": {"type": "string", "enum": ["auto", "online", "offline"],
                             "description": "LLM gateway mode (default from env)"},
                    "max_iterations": {"type": "integer", "description": "Heal loop cap (default 3)"},
                },
                "required": ["project_dir"],
            },
        },
        {
            "name": "verify_open",
            "description": "Preflight a generated .pbip for Power BI Desktop "
                           "openability WITHOUT opening Desktop. Validates every M "
                           "(Power Query) partition, DAX measure, JSON file, TMDL "
                           "presence, required project structure and PBIR schema. "
                           "Returns openable=true/false with blocking issues and "
                           "warnings. The Power Query check is the primary focus.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_dir": {"type": "string", "description": "Generated .pbip project dir"},
                },
                "required": ["project_dir"],
            },
        },
    ]


def _resource_catalogue():
    """Static resource templates the agent can read (report JSON files)."""
    return [
        {
            "uri": "ttpbi://reports/assessment",
            "name": "Assessment report (JSON)",
            "description": "Latest assessment report produced by the assess tool.",
            "mimeType": "application/json",
        },
        {
            "uri": "ttpbi://reports/qa",
            "name": "QA report card (JSON)",
            "description": "Latest QA report produced by the qa tool.",
            "mimeType": "application/json",
        },
        {
            "uri": "ttpbi://reports/quality",
            "name": "Unified quality report (JSON)",
            "description": "Latest quality report produced by the quality_report tool.",
            "mimeType": "application/json",
        },
        {
            "uri": "ttpbi://reports/parity",
            "name": "Parity scan (JSON)",
            "description": "Latest parity scan produced by the parity_scan tool.",
            "mimeType": "application/json",
        },
    ]


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════

def _validate_input_file(path):
    """Return an error string if ``path`` is not a valid workbook input, else ''."""
    if not path or not isinstance(path, str):
        return "missing 'file'"
    if "\x00" in path:
        return "invalid path (null byte)"
    if not path.lower().endswith(_ALLOWED_INPUT_EXT):
        return f"unsupported extension (expected one of {', '.join(_ALLOWED_INPUT_EXT)})"
    if not os.path.isfile(path):
        return f"file not found: {path}"
    return ""


def _validate_pbip_project_dir(path):
    """Validate a generated PBIP root before any MCP read or write."""
    if not path or not isinstance(path, str):
        return "missing 'project_dir'"
    if "\x00" in path:
        return "invalid project path (null byte)"
    absolute = os.path.abspath(path)
    output_error = _validate_output_path(absolute)
    if output_error:
        return output_error
    if os.path.islink(absolute) or not os.path.isdir(absolute):
        return "project_dir must be a real directory"
    pbips = [name for name in os.listdir(absolute)
             if name.casefold().endswith(".pbip")]
    reports = [name for name in os.listdir(absolute)
               if name.casefold().endswith(".report")]
    models = [name for name in os.listdir(absolute)
              if name.casefold().endswith(".semanticmodel")]
    if len(pbips) != 1 or len(reports) != 1 or len(models) != 1:
        return "project_dir must contain one .pbip, one .Report, and one .SemanticModel"
    if not os.path.isdir(os.path.join(absolute, reports[0], "definition")):
        return "PBIP report definition directory is missing"
    if not os.path.isdir(os.path.join(absolute, models[0], "definition")):
        return "PBIP semantic-model definition directory is missing"
    for current, dirs, filenames in os.walk(absolute, followlinks=False):
        if any(os.path.islink(os.path.join(current, name)) for name in dirs + filenames):
            return "project_dir must not contain symbolic links"
    try:
        _pbip_artifact_fingerprint(absolute)
    except ValueError as exc:
        return str(exc)
    return ""


def _validate_output_path(path):
    """Reject output paths containing existing symbolic-link components."""
    if not isinstance(path, str) or not path or "\x00" in path:
        return "invalid output path"
    current = os.path.abspath(path)
    while current and current != os.path.dirname(current):
        if os.path.lexists(current) and os.path.islink(current):
            return "output path must not contain symbolic links"
        current = os.path.dirname(current)
    return ""


def _validate_desktop_log(path, project_dir):
    """Allow only a bounded, regular log beneath the validated project root."""
    if not path or not isinstance(path, str):
        return "invalid Desktop log path"
    if "\x00" in path:
        return "invalid Desktop log path (null byte)"
    log_path = os.path.abspath(path)
    root = os.path.realpath(os.path.abspath(project_dir))
    if os.path.islink(log_path) or not os.path.isfile(log_path):
        return "Desktop log must be a regular non-symlink file"
    try:
        if os.path.commonpath((root, os.path.realpath(log_path))) != root:
            return "Desktop log must be inside project_dir"
        if os.path.getsize(log_path) > 10 * 1024 * 1024:
            return "Desktop log exceeds the 10 MiB limit"
    except OSError:
        return "Desktop log is unreadable"
    return ""


def _extract_to_dir(file_path, extract_dir):
    """Extract a Tableau workbook into ``extract_dir``. Returns converted objects."""
    from extract_tableau_data import TableauExtractor  # type: ignore
    from powerbi_import.import_to_powerbi import PowerBIImporter

    extractor = TableauExtractor(file_path, output_dir=extract_dir)
    extractor.extract_all()
    importer = PowerBIImporter(extract_dir)
    return importer, importer._load_converted_objects()


def _pbip_artifact_fingerprint(project_dir):
    """Hash only generated PBIP, report-definition, and semantic-model artifacts."""
    if not isinstance(project_dir, str) or not os.path.isdir(project_dir):
        raise ValueError("project directory is missing or unreadable")

    excluded_dirs = {"data", "log", "logs", "html", "mcp", "mcp_state", "mcp-state"}
    artifacts = []
    walk_errors = []

    def _on_walk_error(error):
        error_path = os.path.abspath(error.filename or project_dir)
        relative = os.path.relpath(error_path, project_dir)
        parts = relative.split(os.sep)
        if relative == "." or any(
            part.casefold().endswith((".report", ".semanticmodel")) for part in parts
        ):
            walk_errors.append(error)

    for current, dirs, filenames in os.walk(project_dir, onerror=_on_walk_error):
        dirs[:] = [name for name in dirs if name.casefold() not in excluded_dirs]
        relative_dir = os.path.relpath(current, project_dir)
        parts = [] if relative_dir == "." else relative_dir.split(os.sep)
        in_report = any(part.casefold().endswith(".report") for part in parts)
        in_model = any(part.casefold().endswith(".semanticmodel") for part in parts)

        for filename in filenames:
            extension = os.path.splitext(filename)[1].casefold()
            is_platform = filename.casefold() == ".platform"
            if not (
                extension == ".pbip"
                or (extension == ".tmdl" and in_model)
                or (extension == ".json" and in_report)
                or (is_platform and (in_report or in_model))
            ):
                continue
            path = os.path.join(current, filename)
            relative_path = os.path.relpath(path, project_dir).replace(os.sep, "/")
            artifacts.append((relative_path, path))

    if walk_errors:
        raise ValueError("a PBIP report/model artifact directory is unreadable")
    artifacts.sort(key=lambda entry: entry[0].casefold())
    if not any(path.casefold().endswith(".pbip") for _, path in artifacts):
        raise ValueError("PBIP project descriptor is missing")
    if not any(not path.casefold().endswith(".pbip") for _, path in artifacts):
        raise ValueError("PBIP report/model definition artifacts are missing")

    digest = hashlib.sha256()
    for relative_path, path in artifacts:
        try:
            if os.path.islink(path):
                raise ValueError("symbolic links are not valid PBIP artifacts")
            before = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("PBIP artifact is not a regular file")
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(before.st_size).encode("ascii"))
            digest.update(b"\0")
            with open(path, "rb") as artifact_file:
                while True:
                    chunk = artifact_file.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            after = os.stat(path, follow_symlinks=False)
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size, after.st_mtime_ns, after.st_ino
            ):
                raise ValueError("PBIP artifact changed while fingerprinting")
            digest.update(b"\0")
        except (OSError, ValueError) as exc:
            raise ValueError(f"PBIP artifact is missing or unreadable: {relative_path}") from exc
    return digest.hexdigest(), frozenset(relative_path for relative_path, _ in artifacts)


def _normalize_agent_token(agent):
    token = agent.strip()
    if token.startswith("@"):
        token = token[1:]
    return token.strip().casefold()


def _finding_signature(item):
    return json.dumps(
        {key: item.get(key) for key in ("owner", "action", "fix", "evidence")},
        ensure_ascii=False, sort_keys=True, default=str,
    )


# ════════════════════════════════════════════════════════════════════
#  Tool implementations
# ════════════════════════════════════════════════════════════════════

class MigrationTools:
    """Callable migration tools. Each returns a JSON-friendly dict.

    A small ``report_store`` keeps the most recent report per kind so the MCP
    ``resources/read`` surface can hand them back without re-running work.
    """

    def __init__(self):
        self.report_store = {}  # kind -> dict
        self._quality_revision = 0
        self._quality_context = None
        self._issued_handoffs = {}
        self.acknowledgements = {}

    # -- assess -------------------------------------------------------
    def assess(self, args):
        err = _validate_input_file(args.get("file"))
        if err:
            return {"ok": False, "error": err}
        from powerbi_import.assessment import run_assessment
        with tempfile.TemporaryDirectory(prefix="ttpbi_mcp_assess_") as tmp:
            _, converted = _extract_to_dir(args["file"], tmp)
            name = os.path.splitext(os.path.basename(args["file"]))[0]
            report = run_assessment(converted, workbook_name=name)
            payload = report.to_dict()
        self.report_store["assessment"] = payload
        return {"ok": True, "report": payload}

    # -- migrate ------------------------------------------------------
    def migrate(self, args):
        err = _validate_input_file(args.get("file"))
        if err:
            return {"ok": False, "error": err}
        out_dir = args.get("output_dir") or tempfile.mkdtemp(prefix="ttpbi_mcp_out_")
        output_error = _validate_output_path(out_dir)
        if output_error:
            return {"ok": False, "error": output_error}
        fmt = args.get("output_format", "pbip")
        if fmt not in ("pbip", "fabric"):
            return {"ok": False, "error": f"invalid output_format: {fmt}"}
        with tempfile.TemporaryDirectory(prefix="ttpbi_mcp_extract_") as tmp:
            importer, converted = _extract_to_dir(args["file"], tmp)
            name = os.path.splitext(os.path.basename(args["file"]))[0]
            importer.generate_powerbi_project(
                report_name=name,
                converted_objects=converted,
                output_dir=out_dir,
                culture=args.get("culture"),
                output_format=fmt,
            )
        return {"ok": True, "output_dir": out_dir, "report_name": name, "output_format": fmt}

    # -- qa -----------------------------------------------------------
    def qa(self, args):
        project_dir = args.get("project_dir")
        if not project_dir or not os.path.isdir(project_dir):
            return {"ok": False, "error": f"project_dir not found: {project_dir}"}
        from powerbi_import.qa_suite import run_qa_suite
        report = run_qa_suite(project_dir, extraction_dir=args.get("extraction_dir"))
        payload = report.to_dict()
        self.report_store["qa"] = payload
        return {"ok": True, "report": payload}

    # -- quality_report ----------------------------------------------
    def quality_report(self, args):
        err = _validate_input_file(args.get("file"))
        if err:
            return {"ok": False, "error": err}
        project_dir = args.get("project_dir")
        if not project_dir or not os.path.isdir(project_dir):
            return {"ok": False, "error": f"project_dir not found: {project_dir}"}
        from powerbi_import.migration_quality import build_quality_report
        with tempfile.TemporaryDirectory(prefix="ttpbi_mcp_quality_") as tmp:
            _, converted = _extract_to_dir(args["file"], tmp)
            name = os.path.splitext(os.path.basename(args["file"]))[0]
            report = build_quality_report(converted, project_dir, name)
            payload = report.to_dict()
        self.report_store["quality"] = payload
        self._quality_revision += 1
        self._quality_context = {
            "report": payload,
            "report_name": payload.get("report_name") or name,
            "project_dir": os.path.normcase(os.path.realpath(os.path.abspath(project_dir))),
            "revision": self._quality_revision,
        }
        return {"ok": True, "report": payload}

    # -- agent_handoff -----------------------------------------------
    def agent_handoff(self, args):
        agent = args.get("agent")
        if not isinstance(agent, str) or not agent.strip():
            return {"ok": False, "error": "agent is required and must be a non-blank string"}
        requested_agent = agent.strip()
        normalized_agent = _normalize_agent_token(requested_agent)
        if not normalized_agent:
            return {"ok": False, "error": "agent is required and must be a non-blank string"}

        report = self.report_store.get("quality")
        if not isinstance(report, dict):
            return {"ok": False, "error": "not ready: run quality_report in this MCP session first"}

        context = self._quality_context
        context_is_current = isinstance(context, dict) and context.get("report") is report
        report_name = report.get("report_name")
        if context_is_current:
            report_name = report_name or context.get("report_name")
        baseline_fingerprint = None
        project_identity = None
        project_dir = None
        issue_revision = self._quality_revision
        if context_is_current:
            project_dir = context.get("project_dir")
            project_identity = project_dir
            issue_revision = context.get("revision", self._quality_revision)
            try:
                baseline_fingerprint = _pbip_artifact_fingerprint(project_dir)
            except ValueError:
                # Issuance stays read-only; applied acknowledgements fail closed.
                baseline_fingerprint = None

        findings = []
        for index, item in enumerate(report.get("priorities", [])):
            if not isinstance(item, dict):
                continue
            owners = item.get("owner", "")
            if not isinstance(owners, str):
                continue
            owner_tokens = [_normalize_agent_token(token) for token in owners.split("/")]
            if normalized_agent not in owner_tokens:
                continue

            finding = {
                key: item[key]
                for key in ("priority", "action_kind", "action", "fix", "evidence", "owner")
                if key in item
            }
            if "blocker" in item:
                finding["blocker"] = item["blocker"]
            identity = json.dumps(
                [report.get("report_name"), index, item],
                ensure_ascii=False, sort_keys=True, default=str,
            ).encode("utf-8")
            finding["handoff_id"] = hashlib.sha256(identity).hexdigest()
            registry_key = (finding["handoff_id"], normalized_agent)
            self._issued_handoffs[registry_key] = {
                "agent": normalized_agent,
                "finding_signature": _finding_signature(item),
                "report_name": report_name,
                "project_dir": project_dir,
                "project_identity": project_identity,
                "baseline_fingerprint": baseline_fingerprint,
                "quality_revision": issue_revision,
            }
            findings.append(finding)

        return {
            "ok": True,
            "read_only": True,
            "report_name": report_name,
            "report_status": report.get("status"),
            "requested_agent": requested_agent,
            "count": len(findings),
            "status": "ready" if findings else "no_findings",
            "findings": findings,
            "completion_note": (
                "This is a read-only packet. Completion requires an external "
                "agent/operator and a fresh quality report."
            ),
        }

    # -- agent_handoff_ack -------------------------------------------
    def agent_handoff_ack(self, args):
        handoff_id = args.get("handoff_id")
        if not isinstance(handoff_id, str) or not handoff_id.strip():
            return {"ok": False, "persisted": False,
                    "error": "handoff_id is required and must be a non-blank string"}

        agent = args.get("agent")
        if not isinstance(agent, str) or not agent.strip():
            return {"ok": False, "persisted": False,
                    "error": "agent is required and must be a non-blank string"}
        normalized_agent = _normalize_agent_token(agent)
        if not normalized_agent:
            return {"ok": False, "persisted": False,
                    "error": "agent is required and must be a non-blank string"}

        outcome = args.get("outcome")
        if outcome not in ("applied", "not_applicable"):
            return {"ok": False, "persisted": False,
                    "error": "outcome must be 'applied' or 'not_applicable'"}
        rationale = args.get("rationale")
        if not isinstance(rationale, str):
            return {"ok": False, "persisted": False,
                    "error": "rationale is required and must be a string"}
        if outcome == "not_applicable" and not rationale.strip():
            return {"ok": False, "persisted": False,
                    "error": "not_applicable requires a non-blank rationale"}

        registry_key = (handoff_id.strip(), normalized_agent)
        issued = self._issued_handoffs.get(registry_key)
        if not isinstance(issued, dict):
            return {"ok": False, "persisted": False,
                    "error": "handoff_id was not issued to this agent by this MigrationTools instance"}
        if registry_key in self.acknowledgements:
            return {"ok": False, "persisted": False,
                    "error": "this handoff has already been acknowledged"}

        current_fingerprint = None
        if outcome == "applied":
            if issued.get("baseline_fingerprint") is None or not issued.get("project_dir"):
                return {"ok": False, "persisted": False,
                        "error": "issue baseline artifacts were missing or unreadable; applied ACK rejected"}

            latest_report = self.report_store.get("quality")
            context = self._quality_context
            if not isinstance(context, dict) or context.get("report") is not latest_report:
                return {"ok": False, "persisted": False,
                        "error": "no fresh quality_report is available in this MCP instance"}
            if context.get("revision", 0) <= issued.get("quality_revision", 0):
                return {"ok": False, "persisted": False,
                        "error": "applied ACK requires a fresh quality_report after the handoff"}
            if (context.get("report_name") != issued.get("report_name")
                    or context.get("project_dir") != issued.get("project_identity")):
                return {"ok": False, "persisted": False,
                        "error": "fresh quality_report must target the same report and project"}

            priorities = latest_report.get("priorities")
            if not isinstance(priorities, list):
                return {"ok": False, "persisted": False,
                        "error": "latest quality_report priorities are missing or unreadable"}
            if any(
                isinstance(item, dict)
                and _finding_signature(item) == issued.get("finding_signature")
                for item in priorities
            ):
                return {"ok": False, "persisted": False,
                        "error": "finding is still present in the latest quality_report priorities"}

            try:
                current_fingerprint = _pbip_artifact_fingerprint(issued["project_dir"])
            except ValueError as exc:
                return {"ok": False, "persisted": False,
                        "error": f"latest PBIP artifacts are missing or unreadable: {exc}"}
            baseline_hash, baseline_paths = issued["baseline_fingerprint"]
            current_hash, current_paths = current_fingerprint
            if not baseline_paths.issubset(current_paths):
                return {"ok": False, "persisted": False,
                        "error": "one or more baseline PBIP artifacts are missing"}
            if current_hash == baseline_hash:
                return {"ok": False, "persisted": False,
                        "error": "PBIP report/model definition artifacts have not changed since handoff"}

        self.acknowledgements[registry_key] = {
            "handoff_id": handoff_id.strip(),
            "agent": normalized_agent,
            "outcome": outcome,
            "rationale": rationale,
            "report_name": issued.get("report_name"),
            "artifact_fingerprint": current_fingerprint,
        }
        return {
            "ok": True,
            "accepted": True,
            "persisted": False,
            "handoff_id": handoff_id.strip(),
            "agent": normalized_agent,
            "outcome": outcome,
            "rationale": rationale,
        }

    # -- parity_scan --------------------------------------------------
    def parity_scan(self, args):
        err = _validate_input_file(args.get("file"))
        if err:
            return {"ok": False, "error": err}
        try:
            from powerbi_import import parity_registry  # type: ignore
        except Exception:
            note = ("parity registry not available in this build (Sprint 209 pending); "
                    "run 'assess' for readiness findings instead")
            payload = {"status": "unavailable", "note": note}
            self.report_store["parity"] = payload
            return {"ok": True, "report": payload}
        with tempfile.TemporaryDirectory(prefix="ttpbi_mcp_parity_") as tmp:
            _, converted = _extract_to_dir(args["file"], tmp)
            scan = parity_registry.scan_workbook(converted)  # type: ignore
            payload = scan.to_dict() if hasattr(scan, "to_dict") else scan
        self.report_store["parity"] = payload
        return {"ok": True, "report": payload}

    # -- shared_model -------------------------------------------------
    def shared_model(self, args):
        files = args.get("files") or []
        if not isinstance(files, list) or len(files) < 2:
            return {"ok": False, "error": "shared_model requires at least two files"}
        for f in files:
            e = _validate_input_file(f)
            if e:
                return {"ok": False, "error": f"{f}: {e}"}
        try:
            from powerbi_import import shared_model as sm  # type: ignore
        except Exception as exc:
            return {"ok": False, "error": f"shared_model module unavailable: {exc}"}
        out_dir = args.get("output_dir") or tempfile.mkdtemp(prefix="ttpbi_mcp_shared_")
        model_name = args.get("model_name") or "Shared Model"
        if not hasattr(sm, "assess_merge"):
            return {"ok": False, "error": "shared_model.assess_merge not available"}
        # Merge assessment is the safe, read-mostly capability to expose here.
        result = sm.assess_merge(files, model_name=model_name)  # type: ignore
        payload = result.to_dict() if hasattr(result, "to_dict") else result
        return {"ok": True, "model_name": model_name, "output_dir": out_dir, "assessment": payload}

    # -- diff ---------------------------------------------------------
    def diff(self, args):
        extraction_dir = args.get("extraction_dir")
        project_dir = args.get("project_dir")
        if not extraction_dir or not os.path.isdir(extraction_dir):
            return {"ok": False, "error": f"extraction_dir not found: {extraction_dir}"}
        if not project_dir or not os.path.isdir(project_dir):
            return {"ok": False, "error": f"project_dir not found: {project_dir}"}
        try:
            from powerbi_import import artifact_diff  # type: ignore
        except Exception as exc:
            return {"ok": False, "error": f"artifact_diff unavailable: {exc}"}
        if not hasattr(artifact_diff, "diff_artifacts"):
            return {"ok": False, "error": "artifact_diff.diff_artifacts not available"}
        result = artifact_diff.diff_artifacts(extraction_dir, project_dir)  # type: ignore
        payload = result.to_dict() if hasattr(result, "to_dict") else result
        return {"ok": True, "diff": payload}

    # -- deploy (guarded) --------------------------------------------
    def deploy(self, args):
        project_dir = args.get("project_dir")
        workspace_id = args.get("workspace_id")
        if not project_dir or not os.path.isdir(project_dir):
            return {"ok": False, "error": f"project_dir not found: {project_dir}"}
        output_error = _validate_output_path(project_dir)
        if output_error:
            return {"ok": False, "error": output_error}
        if not workspace_id:
            return {"ok": False, "error": "workspace_id is required"}
        # Refuse if secrets were smuggled through the arguments.
        for k in args:
            if any(tok in k.lower() for tok in ("secret", "password", "token", "key")):
                return {"ok": False, "error": "credentials must not be passed as tool "
                                              "arguments; set them in the environment"}
        dry_run = args.get("dry_run", True)
        confirm = args.get("confirm", False)
        if not confirm:
            return {
                "ok": True,
                "dry_run": True,
                "performed": False,
                "note": "dry-run: set confirm=true to perform a real deploy; "
                        "credentials are read from environment variables only",
                "workspace_id": workspace_id,
                "project_dir": project_dir,
            }
        if dry_run:
            return {"ok": True, "dry_run": True, "performed": False,
                    "note": "confirm=true but dry_run=true; nothing pushed"}
        # Real deploy path — delegated, credentials from environment.
        try:
            from powerbi_import.deploy.pbi_deployer import PBIServiceDeployer  # type: ignore
        except Exception as exc:
            return {"ok": False, "error": f"deployer unavailable: {exc}"}
        try:
            deployer = PBIServiceDeployer(workspace_id=workspace_id)
            result = deployer.deploy(project_dir)
            payload = result.to_dict() if hasattr(result, "to_dict") else {"status": str(result)}
            return {"ok": True, "dry_run": False, "performed": True, "result": payload}
        except Exception as exc:
            return {"ok": False, "error": f"deploy failed: {exc}"}

    # -- llm_status --------------------------------------------------
    def llm_status(self, args):
        # Refuse smuggled secrets (same guard as deploy); creds come from env.
        for k in args:
            if any(tok in k.lower() for tok in ("secret", "password", "token", "key")):
                return {"ok": False, "error": "credentials must not be passed as tool "
                                              "arguments; set them in the environment"}
        mode = args.get("mode")
        if mode and mode not in ("auto", "online", "offline"):
            return {"ok": False, "error": f"invalid mode: {mode}"}
        try:
            from powerbi_import.llm_gateway import LLMGateway
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"llm_gateway unavailable: {exc}"}
        gateway = LLMGateway(mode=mode)
        return {"ok": True, "status": gateway.status()}

    # -- autoheal ----------------------------------------------------
    def autoheal(self, args):
        for k in args:
            if any(tok in k.lower() for tok in ("secret", "password", "token", "key")):
                return {"ok": False, "error": "credentials must not be passed as tool "
                                              "arguments; set them in the environment"}
        project_dir = args.get("project_dir")
        project_error = _validate_pbip_project_dir(project_dir)
        if project_error:
            return {"ok": False, "error": project_error}
        autofix = bool(args.get("autofix", False))
        log = args.get("log")
        if log:
            log_error = _validate_desktop_log(log, project_dir)
            if log_error:
                return {"ok": False, "error": log_error}
        try:
            from powerbi_import.healing import AutoHealer, PbiDesktopSource
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"autoheal unavailable: {exc}"}
        gateway = None
        if autofix:
            try:
                from powerbi_import.llm_gateway import LLMGateway
                gateway = LLMGateway(mode=args.get("mode"))
            except Exception:  # noqa: BLE001
                gateway = None
        source = PbiDesktopSource(log_path=log) if log else None
        healer = AutoHealer(gateway=gateway, autofix=autofix,
                            max_iterations=int(args.get("max_iterations", 3)),
                            error_source=source)
        report = healer.heal_project(project_dir)
        return {"ok": True, "report": report.to_dict()}

    # -- verify_open -------------------------------------------------
    def verify_open(self, args):
        """PBI Desktop openability preflight (Power Query / M focus)."""
        project_dir = args.get("project_dir")
        if not project_dir or not os.path.isdir(project_dir):
            return {"ok": False, "error": f"project_dir not found: {project_dir}"}
        try:
            from powerbi_import.healing import check_openability
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"openability unavailable: {exc}"}
        report = check_openability(project_dir)
        return {"ok": True, "report": report.to_dict()}


# ════════════════════════════════════════════════════════════════════
#  JSON-RPC dispatch
# ════════════════════════════════════════════════════════════════════

class MCPServer:
    """Stateless-per-request JSON-RPC dispatcher for MCP methods."""

    def __init__(self, tools=None):
        self.tools = tools or MigrationTools()
        self._handlers = {
            "initialize": self._initialize,
            "ping": lambda p: {},
            "tools/list": lambda p: {"tools": _tool_catalogue()},
            "tools/call": self._tools_call,
            "resources/list": lambda p: {"resources": _resource_catalogue()},
            "resources/read": self._resources_read,
        }

    # -- protocol methods --------------------------------------------
    def _initialize(self, params):
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _tools_call(self, params):
        name = (params or {}).get("name")
        arguments = (params or {}).get("arguments") or {}
        handler = getattr(self.tools, name, None) if name else None
        valid = {t["name"] for t in _tool_catalogue()}
        if name not in valid or handler is None:
            raise _RpcError(METHOD_NOT_FOUND, f"unknown tool: {name}")
        result = handler(arguments)
        is_error = not result.get("ok", True)
        return {
            "content": [{"type": "text",
                         "text": json.dumps(result, ensure_ascii=False, default=str)}],
            "isError": is_error,
        }

    def _resources_read(self, params):
        uri = (params or {}).get("uri", "")
        kind = uri.rsplit("/", 1)[-1] if uri.startswith("ttpbi://reports/") else None
        store_key = {"assessment": "assessment", "qa": "qa", "quality": "quality",
                 "parity": "parity"}.get(kind)
        if not store_key or store_key not in self.tools.report_store:
            raise _RpcError(INVALID_PARAMS, f"no report available for uri: {uri}")
        payload = self.tools.report_store[store_key]
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(payload, ensure_ascii=False, default=str),
            }]
        }

    # -- request handling --------------------------------------------
    def handle_request(self, request):
        """Handle one parsed JSON-RPC request dict; return a response dict.

        Notifications (no ``id``) that succeed return ``None`` (nothing to send).
        """
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return _error_response(None, INVALID_REQUEST, "invalid JSON-RPC 2.0 request")
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        handler = self._handlers.get(method)
        if handler is None:
            if req_id is None:
                return None
            return _error_response(req_id, METHOD_NOT_FOUND, f"method not found: {method}")
        try:
            result = handler(params)
        except _RpcError as exc:
            return _error_response(req_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 — surface as JSON-RPC error
            logger.error("tool error: %s\n%s", exc, traceback.format_exc())
            return _error_response(req_id, INTERNAL_ERROR, str(exc))
        if req_id is None:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def handle_line(self, line):
        """Parse one JSON line and return the serialized response (or None)."""
        line = line.strip()
        if not line:
            return None
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            return json.dumps(_error_response(None, PARSE_ERROR, "parse error"))
        response = self.handle_request(request)
        if response is None:
            return None
        return json.dumps(response, ensure_ascii=False, default=str)

    def serve_stdio(self, stdin=None, stdout=None):
        """Run the newline-delimited JSON-RPC loop over stdio."""
        stdin = _utf8_stream(stdin or sys.stdin)
        stdout = _utf8_stream(stdout or sys.stdout)
        for line in stdin:
            out = self.handle_line(line)
            if out is not None:
                stdout.write(out + "\n")
                stdout.flush()


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _error_response(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════

def _utf8_stream(stream):
    """Best-effort force a text stream to UTF-8 (JSON-RPC requires UTF-8)."""
    try:
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — older/odd streams
        pass
    return stream


def main(argv=None):
    parser = argparse.ArgumentParser(description="Tableau->Power BI MCP server")
    parser.add_argument("--list", action="store_true",
                        help="Print the tool catalogue as JSON and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    _utf8_stream(sys.stdout)
    if args.list:
        print(json.dumps({"tools": _tool_catalogue(), "resources": _resource_catalogue()},
                         indent=2, ensure_ascii=False))
        return 0
    MCPServer().serve_stdio()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
