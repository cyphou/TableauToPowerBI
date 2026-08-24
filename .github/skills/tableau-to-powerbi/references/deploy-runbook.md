# Deployment runbook (on-demand reference)

Companion to the `tableau-to-powerbi` skill. Follow this order when deploying a
migrated project to Power BI Service / Microsoft Fabric.

## Prerequisites

- A generated, validated `.pbip` (open in Power BI Desktop first, or run `--qa`).
- A target workspace ID (or use `--create-workspace "Name"`).
- Credentials as **environment variables**, never CLI/chat arguments:
  - `FABRIC_TENANT_ID`, `FABRIC_CLIENT_ID`, `FABRIC_CLIENT_SECRET`, or a managed identity.
  - `FABRIC_WORKSPACE_ID` (optional default target).

## Steps

1. **Validate**: `python migrate.py workbook.twbx --qa` — confirm no error-severity failures.
2. **Dry run**: with the MCP `deploy` tool, call with `confirm=false` (default) to preview.
3. **Deploy**: `python migrate.py workbook.twbx --deploy WORKSPACE_ID --deploy-refresh`.
4. **Gateway** (on-prem sources): add `--gateway-bind GATEWAY_ID`.
5. **Verify**: check dataset refresh status; open the report in the Service.

## Safety

- The MCP `deploy` tool is **dry-run by default** and refuses to run if any
  argument name looks like a secret (`token`, `secret`, `password`, `key`).
- Never paste tokens into chat or a question tool — type them in the terminal.
- Prefer rolling/canary deploys (`--rolling`) for production targets.

## Rollback

- `--rollback` reverts the last change set; blue/green deploys auto-rollback on
  canary validation failure.
