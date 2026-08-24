# Agent Surface (v44.0.0)

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
| `parity_scan` | `file` | Graceful `unavailable` status until the parity registry ships |
| `shared_model` | `files` (≥2) | Merge assessment for a shared model |
| `diff` | `extraction_dir`, `project_dir` | Field-coverage comparison |
| `deploy` | `project_dir`, `workspace_id` | **Guarded**: dry-run default; `confirm=true` to push |
| `llm_status` | _(none)_ | LLM gateway config + connectivity (mode/route/provider/budget); no secrets |
| `autoheal` | `project_dir` | Closed-loop heal so the .pbip opens cleanly in Desktop: collect errors → deterministic heal → optional LLM correction → re-validate → apply only if valid |
| `verify_open` | `project_dir` | **Openability preflight** (no Desktop needed): validates every Power Query (M) partition, DAX measure, JSON file, TMDL presence, project structure and PBIR schema; returns `openable` + blocking issues/warnings |

### Resources

`ttpbi://reports/assessment`, `ttpbi://reports/qa`, `ttpbi://reports/parity` —
return the latest report JSON from the corresponding tool run.

### Protocol

JSON-RPC 2.0 over newline-delimited stdio. Methods: `initialize`, `ping`,
`tools/list`, `tools/call`, `resources/list`, `resources/read`.

### LLM connectivity (Sprint 221)

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
It runs six checks — `structure`, `json_parse`, `tmdl_present`, `power_query`,
`dax`, `schema` — and returns `openable` plus blocking issues and warnings. The
**`power_query` check is the focus**: it extracts every M partition embedded in the
TMDL (`extract_m_partitions`) and validates each with the M validator, catching the
Power Query generation errors that are a top cause of silent load failures. The same
M-partition validation is wired into `autoheal`'s default error source. Reachable
from the CLI via `migrate.py --verify-open` (writes `openability_report.json` and
exits non-zero if the project would not open).

## 3. Remediation & conversational Q&A

- `remediation.remediate_assessment(report)` → per-finding explanation + suggested
  fix + owning agent + confidence. `refine_with_llm(report, client)` optionally
  augments low-confidence items.
- `conversational.answer_question(report, question)` → grounded answer with
  evidence rows. `build_plan_summary(report)` → ordered, effort-scored plan.

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
