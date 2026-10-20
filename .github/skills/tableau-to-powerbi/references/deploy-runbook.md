# Deployment runbook (on-demand reference)

Companion to the `tableau-to-powerbi` skill. Follow this order when deploying a
migrated project to Power BI Service / Microsoft Fabric.

## Prerequisites

- A generated `.pbip`. Batch generation runs the static openability gate and
  writes quality reports; open the project in Power BI Desktop when a live
  Desktop check is required. The separate MCP `qa` report card is optional.
- A target workspace ID.
- Credentials as **environment variables**, never CLI/chat arguments:
  - `FABRIC_TENANT_ID`, `FABRIC_CLIENT_ID`, `FABRIC_CLIENT_SECRET`, or a managed identity.
  - `FABRIC_WORKSPACE_ID` (optional default target).

## Steps

1. **Generate**: `python migrate.py batch .\workbooks --output-dir .\output`.
   Review the batch quality reports and `batch_quality_summary.json`.
2. **Optional QA report card**: call the MCP `qa` tool with the generated
   project's `project_dir` and, if available, its `extraction_dir`.
3. **Preview deployment**: call the MCP `deploy` tool with the project
   directory and workspace ID. Dry-run is the default.
4. **Deploy**: after reviewing the preview, call the MCP `deploy` tool with
   `dry_run=false` and `confirm=true`.
5. **Verify**: open the report in the Service and check its status there.

## Safety

- Batch dispatch does not run the legacy `--qa` suite or forward deployment,
  refresh, or gateway-binding flags. Use the MCP tools for the separate QA
  report card and deployment.
- The MCP `deploy` tool is **dry-run by default**; an actual deployment
  requires `dry_run=false` and `confirm=true`.
- Never paste tokens into chat or a question tool — type them in the terminal.

## Rollback

- Follow the deployment tool's reported outcome and use the Power BI/Fabric
  service controls to restore the previous deployment if necessary.
