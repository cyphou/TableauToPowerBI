# Multi-Agent Architecture — Tableau to Power BI Migration

This project uses a **15-agent specialization model**. Each agent has scoped domain knowledge, file ownership, and clear boundaries. Four specialist agents (@dax, @wiring, @semantic, @visual) provide deep expertise, @converter and @generator remain as coordination layers, **@tableau** handles Tableau Server/Cloud interaction, **@reviewer** enforces a preceptorship quality loop on all generated artifacts, and **@web-designer** owns the end-user UI surfaces.

The current release is **v44.0.0**. All agents should use [ROADMAP.md](ROADMAP.md)
as the source of truth for the next work: v45 completes the Fabric artifact
contract and performance gates; v46 hardens Desktop openability, shared-model
quality, documentation parity, and release discipline. Do not describe the
Fabric-native output as production-ready until the v45/v46 release criteria pass.

## Quick Reference

| Agent | Invoke When | Owns |
|-------|-------------|------|
| **@orchestrator** | Pipeline coordination, CLI, batch, wizard | `migrate.py`, `import_to_powerbi.py`, `wizard.py`, `progress.py`, `incremental.py`, `plugins.py`, `notebook_api.py`, `api_server.py` |
| **@extractor** | Parsing Tableau XML (.twb/.twbx), Hyper files, Prep flow conversion | `tableau_export/extract_tableau_data.py`, `datasource_extractor.py`, `hyper_reader.py`, `pulse_extractor.py`, `prep_flow_parser.py` |
| **@tableau** | Tableau Server/Cloud REST API, JWT auth, site discovery, permissions, metadata lineage, Prep flow analysis | `tableau_export/server_client.py`, `tableau_export/prep_flow_analyzer.py` |
| **@dax** | DAX formula correctness, conversion, optimization, aggregation context, cross-table refs | `dax_converter.py`, `dax_optimizer.py` + DAX post-processing in `tmdl_generator.py` |
| **@wiring** | DAX↔M bridge, calc column vs measure classification, M generation, M step injection | `m_query_builder.py`, `calc_column_utils.py` + M functions in `tmdl_generator.py` |
| **@semantic** | TMDL semantic model, relationships, Calendar, RLS, hierarchies, parameters | `tmdl_generator.py` (structural), `fabric_semantic_model_generator.py` |
| **@visual** | PBIR report, visual containers, slicers, filters, bookmarks, themes, pages | `pbip_generator.py`, `visual_generator.py` |
| **@converter** | _(Coordination layer)_ Cross-cutting DAX+M tasks | Delegates to @dax and @wiring |
| **@generator** | _(Coordination layer)_ Fabric-native generation, cross-cutting model+report tasks | `fabric_project_generator.py`, `lakehouse_generator.py`, `dataflow_generator.py`, `notebook_generator.py`, `pipeline_generator.py`, `fabric_constants.py`, `fabric_naming.py` |
| **@assessor** | Migration readiness, scoring, strategy, diff reports, validation | `assessment.py`, `server_assessment.py`, `global_assessment.py`, `strategy_advisor.py`, `visual_diff.py`, `comparison_report.py`, `migration_report.py`, `equivalence_tester.py`, `regression_suite.py`, `schema_drift.py`, `validator.py` |
| **@merger** | Shared semantic model, multi-workbook merge, Fabric merge | `shared_model.py`, `merge_config.py` (+ co-owns `merge_assessment.py`, `merge_report_html.py`, `thin_report_generator.py`) |
| **@deployer** | Fabric/PBI deployment, auth, gateway, telemetry, multi-tenant | `deploy/*.py`, `gateway_config.py`, `telemetry.py`, `telemetry_dashboard.py`, `refresh_generator.py` |
| **@reviewer** | Artifact quality review, preceptorship loop, coaching feedback, fidelity scoring | `powerbi_import/preceptor.py` |
| **@web-designer** | End-user UI/UX, Tkinter light UI, layout clarity, presentation | `web/light_ui.py` |
| **@tester** | Tests, coverage, fixtures, regression | `tests/*.py` |

## Architecture Diagram

```
                        ┌──────────────┐
                        │ Orchestrator │  ← CLI entry, pipeline coordination
                        └──────┬───────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
        ┌─────▼─────┐   ┌─────▼─────┐   ┌─────▼─────┐
        │ Extractor  │   │ Converter │   │ Generator  │
        │(XML parse) │   │ (coord.)  │   │ (coord.)   │
        └──────┬─────┘   └─────┬─────┘   └─────┬──────┘
               │          ┌────┴────┐     ┌─────┴──────┐
        ┌──────▼──────┐   │         │     │            │
        │  Tableau    │┌──▼───┐ ┌───▼───┐ ┌▼────────┐ ┌▼──────┐
        │(Server API) ││ DAX  │ │Wiring │ │Semantic │ │Visual │
        └─────────────┘│(formulas)│(DAX↔M)│ │(TMDL)   │ │(PBIR) │
                       └───────┘ └───────┘ └─────────┘ └───────┘
                                              │
                        ┌─────────────────┬────┴────┐
                        │                 │         │
                  ┌─────▼─────┐    ┌──────▼──┐  ┌───▼────┐
                  │  Assessor  │    │ Merger  │  │Deployer│
                  │ (Analysis) │    │ (Merge) │  │(Fabric)│
                  └────────────┘    └─────────┘  └────────┘

              ┌────────────────────────────────────────────┐
              │                 Reviewer                    │
              │    (Preceptorship loop — reviews artifacts  │
              │     from Semantic + Visual + DAX + Wiring)  │
              └────────────────────────────────────────────┘

              ┌────────────────────────────────────────────┐
              │                  Tester                     │
              │    (Cross-cutting — reads all, writes       │
              │     only to tests/)                         │
              └────────────────────────────────────────────┘
```

## The Preceptorship Loop

Every migration passes through a **quality gate** before artifacts are finalized:

```
DRAFT (Agent)  ──→  REVIEW (@reviewer)  ──→  APPROVE? (≥ 4★?)
     ↑                                           │
     │                  YES ─────────────────────→ DONE (artifacts ready)
     │                   NO ─────────────────────→ COACH (structured feedback)
     │                                                │
     └────────────────────────────────────────────────┘
                       (max 3 cycles, then escalate)
```

### Review Dimensions (5-star scoring)

| Dimension | What @reviewer Checks |
|-----------|----------------------|
| **Completeness** | All source objects have corresponding output (no missing tables, measures, visuals) |
| **DAX Correctness** | No Tableau function leakage, valid DAX syntax, correct aggregation context |
| **M Query Validity** | Balanced if/then/else, proper quoting, valid connector expressions |
| **TMDL Structure** | Valid relationships, proper cardinality, Calendar table wired, RLS roles valid |
| **PBIR Fidelity** | Visual types mapped correctly, filters at right level, layout reasonable |
| **Visual Equivalence** | SSIM screenshot comparison between Tableau source and Power BI output visuals |

### Scoring Rules

- **≥ 4★ average** across all 6 dimensions → **APPROVE** — artifact is ready
- **< 4★ average** → **COACH** — @reviewer provides specific, actionable feedback per dimension
- **After 3 failed cycles** → **ESCALATE** to user with two options:
  - **Accept with warnings** — proceed with quality annotations in the migration report
  - **Block** — halt and request manual intervention

### Coaching Feedback Format

```
COACH FEEDBACK — Cycle {n}/3
═══════════════════════════
Dimension: {dimension_name} — {score}★
Issue: {specific problem found}
Location: {file path or artifact reference}
Fix: {concrete action the owning agent should take}
Example: {before → after, if applicable}
```

### Pipeline Integration

The preceptorship loop triggers:
1. **After generation** — automatic review of the full .pbip output
2. **On `--review` flag** — explicit review of existing artifacts
3. **On `--qa` flag** — combined with existing QA checks for deeper analysis

The `PreceptorLoop` class in `powerbi_import/preceptor.py` drives the cycle, consuming:
- `ArtifactValidator` results (structural checks)
- `MigrationReport` fidelity data (conversion coverage)
- `RecoveryReport` repair actions (self-healing effectiveness)
- Extraction JSON files (source-of-truth for completeness)

## Specialist Agent Decomposition

The original 8-agent model had two overloaded agents:
- **@converter** owned all DAX conversion + all M generation → now split into **@dax** + **@wiring**
- **@generator** owned all TMDL model + all PBIR report + Fabric → now split into **@semantic** + **@visual** (Fabric stays with @generator)

### @dax — DAX Formula Specialist
- Owns: `dax_converter.py`, `dax_optimizer.py`
- Co-owns: DAX post-processing blocks in `tmdl_generator.py` (SUM wrapping, measure unwrapping, RELATED/LOOKUPVALUE)
- Expertise: Aggregation context (bare column refs vs iterator row context), cross-table semantics, DAX optimization

### @wiring — DAX↔M Bridge Specialist
- Owns: `m_query_builder.py`, `calc_column_utils.py`
- Co-owns: M functions in `tmdl_generator.py` (`_dax_to_m_expression()`, `_inject_m_steps_into_partition()`, `_build_m_transform_steps()`, `_fix_m_if_else_balance()`, `_quote_m_identifiers()`)
- Expertise: Calc column vs measure classification, M pushdown decisions, M step chaining

### @semantic — Semantic Model Specialist
- Owns: `tmdl_generator.py` (structural parts: tables, relationships, Calendar, RLS, hierarchies, parameters, self-healing, TMDL writers)
- Owns: `fabric_semantic_model_generator.py`
- Expertise: TMDL structure, relationship cardinality, join graph analysis, data model correctness

### @visual — Report Visual Specialist
- Owns: `pbip_generator.py` (report parts: pages, visuals, slicers, filters, bookmarks, layout, formatting)
- Owns: `visual_generator.py`
- Expertise: PBIR v4.0 schema, visual type mapping (190), slicer configuration, filter levels

## Data Flow

```
1. Orchestrator receives CLI command (migrate.py)
2. Orchestrator delegates to Extractor → 23 JSON files
3. Orchestrator delegates to conversion:
   a. @dax converts Tableau formulas → DAX expressions
   b. @wiring classifies measure vs calc column, builds M queries
4. Orchestrator delegates to generation:
   a. @semantic builds TMDL model (tables, relationships, Calendar, RLS)
   b. @visual builds PBIR report (pages, visuals, slicers, filters)
   c. @generator coordinates Fabric output (Lakehouse, Dataflow, Notebook, Pipeline)
5. @semantic runs self-healing (TMDL self-repair)
6. (Optional) @assessor → readiness report
7. (Optional) @merger → shared semantic model
8. (Optional) @deployer → Fabric/PBI workspace
9. @tester validates all steps with 7,072+ tests
```

## Handoff Protocol

When an agent encounters work outside its domain:

1. **Complete your part** — finish everything within your file scope
2. **State the handoff** — clearly describe what needs to happen next
3. **Name the target agent** — e.g., "Hand off to @semantic for TMDL updates"
4. **List artifacts** — specify files, functions, and data structures involved
5. **Include context** — provide any intermediate results (dicts, JSON) the next agent needs

## File Ownership Rules

- **One owner per file** — each source file has exactly one owning agent
- **Read access is universal** — any agent can read any file for context
- **Write access is restricted** — only the owning agent modifies a file
- **Tester is special** — reads all source files, writes only to `tests/`
- **Co-owned functions** — `tmdl_generator.py` has shared ownership: @semantic owns structural parts, @dax owns DAX post-processing, @wiring owns M functions
- **Cross-cutting** — `security_validator.py` is used by Extractor, Orchestrator, and Deployer (no single owner — all contributors coordinate)

## When NOT to Use Specialized Agents

Use the **default agent** (or @orchestrator) for:
- Quick questions about the project
- Multi-domain tasks that touch 3+ agents
- Documentation updates (CHANGELOG, README, etc.)
- Sprint planning and gap analysis
- Git operations (commit, push, branch)

## Agent Files

All agent definitions are in `.github/agents/`:
- `shared.instructions.md` — Base rules inherited by all agents
- `orchestrator.agent.md` — Pipeline coordination
- `extractor.agent.md` — Tableau parsing
- `dax.agent.md` — DAX formula specialist (NEW)
- `wiring.agent.md` — DAX↔M bridge specialist (NEW)
- `semantic.agent.md` — Semantic model specialist (NEW)
- `visual.agent.md` — Report visual specialist (NEW)
- `converter.agent.md` — Formula coordination layer (delegates to @dax + @wiring)
- `generator.agent.md` — Generation coordination layer (delegates to @semantic + @visual, owns Fabric)
- `assessor.agent.md` — Migration analysis + validation
- `merger.agent.md` — Multi-workbook merge (PBIP + Fabric)
- `deployer.agent.md` — Fabric/PBI deployment + multi-tenant
- `reviewer.agent.md` — Artifact quality review + preceptorship loop (NEW)
- `tester.agent.md` — Test creation and validation
