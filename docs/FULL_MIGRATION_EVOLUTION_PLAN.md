# Full Migration Evolution Plan

**Date:** 2026-09-04  
**Scope:** Tableau source to Power BI Desktop, PBIP, and Fabric migration  
**Primary outcome:** A migration is reported as complete only when its source inventory, target artifacts, behavior checks, and operator handoff are all backed by evidence.

## Definition of 100% Parity

A numerical score alone cannot prove Tableau and Power BI behave identically. For this project, **100% parity** means:

- Every in-use Tableau object and feature is inventoried.
- Every inventory item has one of four explicit outcomes: `exact`, `equivalent`, `healed`, or `unsupported`.
- `exact`, `equivalent`, and `healed` items have target-artifact and validation evidence.
- `unsupported` items never disappear; each has a visible impact, owner, workaround, and release decision.
- No generated report has dangling model references, invalid DAX/M, unresolved required connections, or unverified synthetic bindings.
- Visual geometry, field roles, filters, calculations, interactions, security, refresh behavior, and packaging are compared at their applicable level.
- Desktop and Fabric states are reported separately from local static validation.

A migration may be technically successful while still being `WARN` or `BLOCKED` when an authorized runtime check or source behavior cannot be proven.

## Current Baseline

The repository currently provides:

- Extraction and generation for Tableau workbooks and standalone Prep flows.
- PBIP/PBIR/TMDL and Fabric-native artifact generation.
- Assessment, parity registry, lineage, self-healing, quality reporting, and handoff packaging.
- Single-workbook and batch checkpoints with source/configuration invalidation.
- Demo corpus evidence: 10 workbooks plus 1 Prep flow processed successfully; the refreshed workbook quality set is 4 PASS, 6 WARN, 0 FAIL.
- Static openability and quality validation; live Power BI Desktop and Fabric evidence remains environment-gated.

The remaining work is therefore feature closure and runtime proof, not simply increasing test counts or suppressing warnings.

## Evolution Phases

### Phase 0 — Migration Contract and Inventory

**Goal:** Establish the complete source-of-truth inventory before conversion.

**Capabilities:**

- Normalize workbook, dashboard, worksheet, datasource, table, column, calculation, parameter, filter, action, story, security, extract, Prep, and custom-resource identities.
- Assign stable source IDs and source locations to extracted objects.
- Record connector, credential, gateway, published-datasource, and refresh prerequisites without storing secrets.
- Produce an inventory manifest before generation and compare it with generated target inventory afterward.

**Owners:** @extractor, @assessor, @tableau, @orchestrator  
**Gate:** No source object is absent from the inventory or marked as an unexplained drop.

### Phase 1 — Semantic Conversion Fidelity

**Goal:** Make Tableau calculations and data semantics execute correctly in Power BI.

**Capabilities:**

- Complete DAX conversion for basic calculations, LOD, table calculations, parameters, date logic, null/blank behavior, ranking, string logic, and cross-table references.
- Validate calculation classification as measure, calculated column, or Power Query transformation.
- Preserve relationship grain, cardinality, filter direction, aliases, sort order, hierarchies, groups, sets, bins, RLS, and time intelligence.
- Add representative value comparisons for totals, blanks, filters, dates, LOD grain, and table-calculation partitions.
- Keep approximations visible with confidence and remediation.

**Owners:** @dax, @wiring, @semantic, @extractor  
**Gate:** No production profile has an unresolved calculation dependency, invalid expression, or unexplained value mismatch.

### Phase 2 — Report, Visual, and Interaction Fidelity

**Goal:** Preserve what users see and how they operate the report.

**Capabilities:**

- Compare visual type, placement, dimensions, layering, titles, axes, legends, labels, number formats, color rules, themes, and analytics.
- Preserve report/page/visual filters, slicers, parameter controls, actions, bookmarks, navigation, drill-through, tooltips, and cross-filter behavior.
- Map unsupported visuals to documented replacements while retaining source intent and remediation.
- Add deterministic geometry and PBIR round-trip comparisons that tolerate generated IDs but not behavioral drift.

**Owners:** @visual, @generator, @extractor, @reviewer  
**Gate:** Every generated visual has source worksheet lineage, target binding evidence, and an accepted exact/equivalent/healed/unsupported decision.

### Phase 3 — Source and Runtime Readiness

**Goal:** Make each target refreshable and executable in its intended environment.

**Capabilities:**

- Persist Import, DirectQuery, Composite, Direct Lake, Dataflow, Notebook, and shared-model strategy decisions.
- Validate connector-specific M, native query, Hyper, published datasource, custom SQL, gateway, identity, and refresh prerequisites.
- Generate sanitized credential and environment-binding templates.
- Run semantic execution tests against representative data where an authorized runtime exists.

**Owners:** @wiring, @generator, @deployer, @semantic, @tableau  
**Gate:** Every required prerequisite is `configured`, `not_run`, or `blocked` with evidence and an owner; no missing prerequisite is silently treated as success.

### Phase 4 — Power BI Desktop Openability Proof

**Goal:** Prove that generated PBIP projects open, save, and reopen in the target Desktop version.

**Capabilities:**

- Launch the authorized Power BI Desktop version with a controlled smoke window.
- Capture opened, crashed, timed-out, and error states with redacted diagnostics.
- Save and reopen projects, then rerun static PBIP/PBIR/TMDL/M/DAX validation.
- Compare post-save artifacts while ignoring nondeterministic IDs.
- Promote confidence only through evidence: `STATIC_PASS` → `DESKTOP_SMOKE_PASS` → `DESKTOP_REOPEN_PASS`.

**Owners:** @visual, @orchestrator, @reviewer, @tester  
**Gate:** A project is Desktop-ready only after two healthy launches, save/reopen evidence, and a passing post-save contract.

### Phase 5 — Self-Healing and Recovery

**Goal:** Make repair useful, bounded, and auditable.

**Capabilities:**

- Detect invalid references, stale IDs, malformed TMDL/PBIR, unsafe M/DAX, and unsupported bindings.
- Apply deterministic repairs only when the repair strategy is known.
- Re-run the affected parity and openability checks after every repair.
- Reject or roll back repairs that reduce fidelity, introduce blockers, or change unrelated source intent.
- Resume failed migrations from extraction, generation, validation, packaging, or deployment handoff checkpoints.

**Owners:** @reviewer, @orchestrator, @dax, @wiring, @visual, @tester  
**Gate:** Every repair has before/after evidence, is idempotent, and is represented in the recovery ledger.

### Phase 6 — Fabric Delivery and Operations

**Goal:** Turn a locally validated migration into a controlled Fabric handoff.

**Capabilities:**

- Bind Lakehouse, Dataflow Gen2, Notebook, Pipeline, Semantic Model, and Report artifacts to an authorized workspace.
- Resolve artifact IDs and dependency order without placeholders at deployment time.
- Separate dry-run, deployed, refreshed, and post-deployment-validated states.
- Capture gateway, RBAC, identity, refresh, and rollback evidence.
- Keep production confirmation impossible unless the authorized target passes its checks.

**Owners:** @generator, @deployer, @semantic, @orchestrator  
**Gate:** No report says production-ready until deployment, refresh, semantic execution, and post-deployment checks pass in the authorized environment.

### Phase 7 — Migration Reporting and Operator Handoff

**Goal:** Deliver a report that another operator can trust and act on.

**Capabilities:**

- Produce one package containing source inventory, strategy, lineage, parity matrix, conversion findings, self-healing ledger, validation states, credentials template, rollback point, and known limitations.
- Provide workbook and portfolio views with status, blockers, warnings, owners, effort, and next action.
- Include source hashes, manifest version, timestamps, target paths, and environment states without credentials or customer payloads.
- Make report status deterministic: `PASS`, `WARN`, `BLOCKED`, or `UNVERIFIED`.

**Owners:** @assessor, @orchestrator, @reviewer, @web-designer  
**Gate:** A handoff package is complete only when a new operator can understand the source, target, differences, prerequisites, rollback, and validation level without reading transient logs.

## End-to-End Delivery Path

```text
Inventory → Assess → Plan strategy → Extract → Convert → Generate
    → Compare → Self-heal → Validate PBIP/PBIR/TMDL/M
    → Verify Desktop → Package handoff → Deploy Fabric
    → Refresh/execute → Post-deploy validate → Report complete
```

Every stage updates the same versioned manifest. A stage can stop the path only with a structured finding and resumable checkpoint.

## Release Policies

- Never convert a warning into a pass by changing score thresholds.
- Never claim Desktop or Fabric success from static files alone.
- Never hide an unsupported feature, missing connector prerequisite, or unresolved lineage edge.
- Never send raw credentials, customer data, or private server metadata into fixtures, AI prompts, or published reports.
- Use AI only for suggestions, summaries, and test generation; deterministic validators decide status.
- Treat the demo corpus as a living regression portfolio and prioritize its remaining WARN categories by user impact.

## Definition of Done

A migration is **well done** when its final report contains:

1. Complete source and target inventories.
2. 100% classified feature coverage with no silent drops.
3. Lineage from source object to target artifact or explicit remediation.
4. Validated DAX, M, TMDL, PBIR, relationships, filters, and security behavior.
5. Visual and interaction comparisons with accepted differences documented.
6. Checkpoint and recovery history.
7. Desktop confidence level backed by live evidence when available.
8. Fabric deployment and refresh states backed by authorized evidence when requested.
9. A portable package that another operator can use for handoff and rollback.
10. A final status that accurately distinguishes `PASS`, `WARN`, `BLOCKED`, and `UNVERIFIED`.
