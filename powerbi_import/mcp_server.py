"""MCP server for the Tableau → Power BI migration engine (v44, Sprint 216).

Exposes the migration engine as Model Context Protocol tools so agents/IDEs can
call migration capabilities directly. Transport is stdlib JSON-RPC 2.0 over stdio
— zero external dependencies for the core path.

Tools:
    assess         Pre-migration readiness assessment (no artifacts written)
    migrate        Full extract → generate pipeline, returns output dir
    qa             Real-world QA report card on a generated .pbip project
    parity_scan    Functionality-parity scan (graceful if registry absent)
    shared_model   Build a shared semantic model from several workbooks
    diff           Compare source extraction vs generated output
    deploy         Deploy to Fabric / Power BI Service (guarded, dry-run default)

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
import json
import logging
import os
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


def _extract_to_dir(file_path, extract_dir):
    """Extract a Tableau workbook into ``extract_dir``. Returns converted objects."""
    from extract_tableau_data import TableauExtractor  # type: ignore
    from powerbi_import.import_to_powerbi import PowerBIImporter

    extractor = TableauExtractor(file_path, output_dir=extract_dir)
    extractor.extract_all()
    importer = PowerBIImporter(extract_dir)
    return importer, importer._load_converted_objects()


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
        if not project_dir or not os.path.isdir(project_dir):
            return {"ok": False, "error": f"project_dir not found: {project_dir}"}
        autofix = bool(args.get("autofix", False))
        log = args.get("log")
        try:
            from powerbi_import.autoheal import AutoHealer, PbiDesktopSource
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
            from powerbi_import.openability import check_openability
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
        store_key = {"assessment": "assessment", "qa": "qa", "parity": "parity"}.get(kind)
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
