# Reading migration reports (on-demand reference)

Companion to the `tableau-to-powerbi` skill. The engine emits structured JSON +
HTML reports; here's how to interpret them.

## Assessment (`--assess`)

- **Overall score**: `GREEN` (no blockers), `YELLOW` (warnings, still migratable),
  `RED` (failures to resolve first).
- **Categories**: 14 checks (datasource, calculation, visual, filter, data model,
  interactivity, extract, scope, connection strings, performance, data volume,
  prep complexity, licensing, multi-datasource).
- Each check has `severity` (pass/info/warn/fail), a `detail`, and a
  `recommendation`. Act on `warn`/`fail` first.

## Real-world QA report card (MCP `qa`)

- Runs against an existing generated `.pbip`; pass its `project_dir` and,
  optionally, its `extraction_dir` to the MCP `qa` tool. The `qa FOLDER`
  shortcut performs batch migration and does not run this report card.
- Checks for stray sentinels, empty visuals, visual data-well contract issues,
  format coverage, dashboard zones, and orphan filters. The first three checks
  report errors; the remaining checks report warnings. The tool returns a
  report but does not gate batch migration or CI.
- The legacy `--qa` and `--qa-strict` flags are not available through the
  supported batch-only CLI workflow. Batch migration instead writes its
  unified quality JSON/HTML reports and batch summary.

## Remediation report (v44)

- `remediation.py` turns findings into plain-language explanations + suggested
  fixes, each with an **owning file/agent** and a **confidence** (high/medium/low).
- Low-confidence suggestions should get a human review before applying.

## Conversational assess (v44)

- `conversational.py` answers grounded questions ("what won't migrate cleanly?",
  "which datasources need a gateway?", "give me a plan") from the assessment
  payload — every answer carries evidence rows, no guessing.

## Where to fix

Findings route to an owning module/agent — see the ownership map in the main
SKILL.md. Fix at the *source generator*, never in the emitted artifact.
