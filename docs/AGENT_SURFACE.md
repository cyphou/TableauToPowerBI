# Agent Surface

The **agent surface** turns the migration engine into a tool-callable, AI-assisted
assistant. It has three layers, all stdlib-first and grounded in the same
generators and reports the CLI uses.

```
┌──────────────────────────────────────────────────────────────┐
│  GitHub Copilot skill  (.github/skills/tableau-to-powerbi/)    │  discovery + guidance
│  MCP server            (powerbi_import/mcp_server.py)          │  tool-callable capabilities
│  Remediation + Q&A     (remediation.py, conversational.py)     │  explain + suggest + answer
└──────────────────────────────────────────────────────────────┘
                 │  all delegate to  ▼
   extract_tableau_data · import_to_powerbi · assessment · qa_suite
```

## 1. Copilot skill

- `.github/skills/tableau-to-powerbi/SKILL.md` — trigger-rich frontmatter, the
  2-step pipeline, canonical commands, output layout, ownership map,
  troubleshooting, safety.
- `references/flags.md`, `references/reading-reports.md`, `references/deploy-runbook.md`
  — on-demand deep dives (progressive disclosure).
- `scripts/validate_skills.py` — lints frontmatter, relative links, referenced CLI
  flags, and embedded secrets. Run: `python scripts/validate_skills.py`.

## 2. MCP server

Run the stdio JSON-RPC server:

```bash
python -m powerbi_import.mcp_server          # stdio loop
python -m powerbi_import.mcp_server --list   # print tool + resource catalogue
```

### Tools

| Tool | Required args | Notes |
|------|---------------|-------|
| `assess` | `file` | Readiness report; writes nothing |
| `migrate` | `file` | Full pipeline → `.pbip`/Fabric; optional `output_dir`, `output_format`, `culture` |
| `qa` | `project_dir` | QA report card; optional `extraction_dir` |
| `quality_report` | `file`, `project_dir` | Unified deterministic report; stores the latest quality payload in the current MCP server process |
| `agent_handoff` | `agent` | Read-only pull of priorities owned by this agent from the latest quality report; accepts `@name` or `name` |
| `agent_handoff_ack` | `handoff_id`, `agent`, `outcome`, `rationale` | In-memory ACK; `applied` requires changed PBIP definition files and a fresh report where the finding disappeared |
| `parity_scan` | `file` | Graceful `unavailable` status until the parity registry ships |
| `shared_model` | `files` (≥2) | Merge assessment for a shared model |
| `diff` | `extraction_dir`, `project_dir` | Field-coverage comparison |
| `deploy` | `project_dir`, `workspace_id` | **Guarded**: dry-run default; `confirm=true` to push |
| `llm_status` | _(none)_ | LLM gateway config + connectivity (mode/route/provider/budget); no secrets |
| `autoheal` | `project_dir` | Closed-loop heal so the .pbip opens cleanly in Desktop: collect errors → deterministic heal → optional LLM correction → re-validate → apply only if valid |
| `verify_open` | `project_dir` | **Openability preflight** (no Desktop needed): validates every Power Query (M) partition, DAX measure, JSON file, TMDL presence, project structure and PBIR schema; returns `openable` + blocking issues/warnings |

### Resources

`ttpbi://reports/assessment`, `ttpbi://reports/qa`, `ttpbi://reports/quality`,
and `ttpbi://reports/parity` return the latest report JSON from the
corresponding tool run.

### Protocol

JSON-RPC 2.0 over newline-delimited stdio. Methods: `initialize`, `ping`,
`tools/list`, `tools/call`, `resources/list`, `resources/read`.

### LLM connectivity

The `llm_status` tool reports the `LLMGateway` route: `auto`/`online`/`offline`,
cloud vs local (Ollama/LM Studio/vLLM) provider, reachability, and budget. The
gateway (`powerbi_import/llm_gateway.py`) powers on-the-fly correction with an
offline-first, redaction-safe, budget-capped, opt-in policy.

The `autoheal` tool (`powerbi_import/autoheal.py`) runs the closed loop that makes
a `.pbip` open cleanly in Power BI Desktop: collect errors (static validators, or a
Desktop error-export log via `PbiDesktopSource`/`LogFileSource`) → deterministic
heal (DAX/M/visual) → optional LLM correction via the gateway → re-validate → apply
a fix ONLY when it re-validates clean (never degrading).

The `verify_open` tool (`powerbi_import/openability.py`) is a **read-only preflight**
that answers "will Power BI Desktop open this project?" without launching Desktop.
It runs the current static check set, including `structure`, `json_parse`,
`tmdl_present`, `power_query`, `dax`, `schema`, `references`, data-file presence,
calculated-column dependencies, path length, bookmark indexes, and literal grammar,
and returns `openable` plus blocking issues and warnings. The
**`power_query` check is the focus**: it extracts every M partition embedded in the
TMDL (`extract_m_partitions`) and validates each with the M validator, catching the
Power Query generation errors that are a top cause of silent load failures. The same
M-partition validation is wired into `autoheal`'s default error source. Reachable
from the CLI via `migrate.py --verify-open` (writes `openability_report.json` and
exits non-zero if the project would not open).

For real Desktop evidence, use `scripts/probe_projects.py` after generation. Its
default mode waits for the report window, verifies aggregate row counts through
Desktop's local model, and captures only after data is confirmed. `--skip-data-check`
is diagnostic only. The external `DESKTOP_OPENING_VALIDATION.html` report separates
`OPENED`/`CRASHED` from `verified`/`empty`/`unavailable` data states.

## 3. Remediation & conversational Q&A

- `remediation.remediate_assessment(report)` → per-finding explanation + suggested
  fix + owning agent + confidence. `refine_with_llm(report, client)` optionally
  augments low-confidence items.
- `conversational.answer_question(report, question)` → grounded answer with
  evidence rows. `build_plan_summary(report)` → ordered, effort-scored plan.

## 4. Preceptor report and owner-pull handoff

The CLI runs the Python preceptor after generation by default. It writes
`preceptor_report.json`, and the quality report can include its dimension, fix,
and evidence fields in the findings queue. It does **not** invoke Copilot's
`@reviewer` agent or push findings to owners.

An MCP-connected owner agent can pull its packet in the same server process:

1. Call `quality_report` with the source workbook and generated `project_dir`.
2. Call `agent_handoff` with your agent name, for example `{"agent":"@dax"}`.
  The tool returns only findings whose owner token matches, including
  `handoff_id`, priority, action, fix, and evidence. Slash-separated owners are
  matched to either owner.
3. For a non-applicable finding, call `agent_handoff_ack` with
  `outcome="not_applicable"` and a rationale. For a repair, change the PBIP,
  rerun `quality_report` for the same source/project, then call
  `agent_handoff_ack` with `outcome="applied"` and a concise rationale. The
  tool accepts the applied ACK only when PBIP definition artifacts changed and
  the same finding is absent from the fresh report.

`agent_handoff` is **read-only** and does not invoke another agent. The separate
`agent_handoff_ack` accepts `not_applicable` only with a rationale; `applied`
requires a fresh quality report for the same report/project, changed PBIP
definition artifacts, and the finding absent from current priorities. ACKs are
held only in the MCP process (`persisted: false`); they are not durable
resolution records or automatic preceptor-cycle dispatch. The report cache and
handoff registry are process-local. `--no-preceptor` opts out;
`--preceptor-block` opts into blocking after escalation. The existing `autoheal`
tool is separate and does not consume preceptor coaching automatically.

## Safety model

- **Secrets never transit tool args.** The `deploy` tool refuses arguments whose
  names contain `secret`/`password`/`token`/`key`; credentials come from the
  environment only. The skill instructs agents to have users type secrets into
  the terminal, never into chat.
- **Deploy is gated.** Dry-run by default; a real push needs `confirm=true` and
  `dry_run=false`.
- **AI is opt-in and assistive.** Remediation works fully offline; the LLM path
  only augments and never auto-applies fixes.
- **Contracts are snapshot-guarded.** `tests/test_agent_contracts.py` fails if the
  tool/resource surface drifts; `tests/test_agent_safety.py` guards the safety
  rules.
