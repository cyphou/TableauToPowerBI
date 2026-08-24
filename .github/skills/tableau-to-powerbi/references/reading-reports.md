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

## QA report card (`--qa`)

- Runs against the generated `.pbip`. Checks: no stray sentinels, no empty
  visuals, format coverage, zones matched, no orphan filters.
- `error`-severity failures are CI-gating; `warning` are advisory.

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
