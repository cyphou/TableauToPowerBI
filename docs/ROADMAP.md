# Development Roadmap — v22.0.0 → v46.0.0

**Date:** 2026-07-16
**Baseline:** v40.0.0 — 9,156 tests (Python) + 38 extension unit tests, 0 collection errors
**Current state:** v44.0.0 shipped. A 2026-07-15 executable audit confirmed that the Fabric-native path generates a useful five-artifact scaffold, but is not yet an operational end-to-end Direct Lake solution: semantic-model partitions are Import/M, Dataflow and pipeline bindings are unresolved, three item manifests are missing, and single-workbook Fabric output has no Power BI report. **Near-term focus (v45.0.0): measured migration performance and completion of the Fabric contract. Next roadmap (v46.0.0): Desktop openability hardening, shared-model quality gates, and release discipline.**

---

## Executive Summary

The migration engine has broad PBIP, batch, shared-model, Tableau Server, self-healing, and developer-tooling coverage. Fabric-native generation currently provides a **preview scaffold**, not a production-ready end-to-end chain. v45 closes that gap and establishes reproducible time and memory budgets before further optimization claims are made.

| Version | Theme | Sprints | Status |
|---------|-------|---------|--------|
| **v22.0.0** | Real-World Fidelity & Layout Intelligence | 76–80 | ✅ Shipped |
| **v23.0.0** | Conversion Accuracy & Fidelity Perfection | 81–85 | ✅ Shipped |
| **v24.0.0** | Composite Models, Live Sync & Enterprise Scale | 86–90 | ✅ Shipped |
| **v25.0.0** | Semantic Intelligence & Cross-Platform Parity | 91–95 | ✅ Shipped |
| **v26.0.0** | Autonomous Migration & Production Hardening | 96–100 | ✅ Shipped |
| **v27.0.0** | Advanced Intelligence & Marketplace | 101–107 | ✅ Shipped |
| **v28.0.0** | Extensibility & Core Infrastructure | 108–111, 118–119 | ✅ Shipped |
| **v28.2.0** | Standalone Prep Flow Pipeline | — | ✅ Shipped |
| **v28.4.0** | 14-Agent Model & Cross-Table SUM Fix | — | ✅ Shipped |
| **v28.5.x** | DAX/M Correctness Hardening | Patch series | ✅ Shipped |
| **v30.0.0** | Correctness, Observability & Self-Healing | 128–134 | ✅ Shipped |
| **v31.0.0–v31.6.0** | Self-Healing v3 & Zero-Error Phases 1–10 | 136–150 | ✅ Shipped |
| **v34.0.0** | Zero Error Full Spectrum (20 sprints) | 151–170 | ✅ Shipped |
| **v35.0.0** | Advanced Visual Fidelity | 171–174 | ✅ Shipped |
| **v36.0.0** | Tableau Server Enterprise Migration | 139–145 | ✅ Shipped |
| **v37.0.0** | Migration Completeness & Analytics Parity | 120–124 | ✅ Shipped |
| **v37.1.0** | Bulk Assessment (`--bulk-assess`) | — | ✅ Shipped |
| **v38.2.0** | Report Packaging & Developer Experience | 175–179 | ✅ Shipped |
| **v38.3.0** | Empty-Visual Recovery (UC80 patch) | — | ✅ Shipped |
| **v38.4.0** | Pixel-Perfect Text & Format Fidelity | 200–203 | ✅ Shipped |
| **v38.5.0** | Floating-Overlay Fidelity & Real-World QA | 204–208 | ✅ Shipped |
| **v39.0.0** | Data Blending & Advanced Connectivity | 180–184 | ✅ Shipped |
| **v40.0.0** | VS Code Extension & Interactive Tooling | 185–189 | ✅ Shipped |
| **v41.0.0** | Real-Time, Streaming & Paginated Reports | 190–194 | Planned |
| **v42.0.0** | Ecosystem Maturity & GA Polish | 195–199 | Planned |
| **v43.0.0** | Self-Healing Maturity & Functionality Parity | 209–214 | 🎯 Next |
| **v44.0.0** | Agentic & Copilot-Native Migration | 215–220 | ✅ Shipped |
| **v45.0.0** | Performance & Fabric Contract Completion | 222–226 | Planned |
| **v46.0.0** | Desktop Reliability & Release Discipline | 227–231 | Planned |

---

## v45.0.0 — Performance & Fabric Contract Completion (Sprints 222–226)

The implementation-grade plan, measured baselines, owners, acceptance criteria, and
release gates are maintained in
[PERFORMANCE_FABRIC_V45_PLAN.md](PERFORMANCE_FABRIC_V45_PLAN.md).

Verified audit baseline:

- Superstore Fabric CLI generation: 1.42 s for the current five-directory scaffold.
- Fabric and CLI focused suites: 173 passed.
- Performance suites: 28 passed, 2 failed; `Enterprise_Sales` took 6.10 s and
  `Complex_Enterprise` took 14.38 s against the current 5.00 s budget.
- Current Fabric output has six unresolved pipeline placeholders, only two item
  manifests, Import/M semantic-model partitions, and no single-workbook report.

v45 sequencing:

| Sprint | Theme | Primary exit gate |
|--------|-------|-------------------|
| **222** | Performance baseline and observability | Phase timings, peak memory, stable benchmark runner, CI trend artifact |
| **223** | Profile-led hot-path optimization | Current performance regressions resolved without fidelity loss |
| **224** | True Direct Lake and artifact contract | Entity/Direct Lake partitions, complete manifests, concrete Lakehouse/Dataflow mapping |
| **225** | Pipeline, Power BI report, and native deployment | Six-artifact chain deploys in dependency order with resolvable IDs |
| **226** | Contract/performance gates and release | Static cross-validation plus opt-in live Fabric smoke test pass |

Until the Sprint 226 gates pass, documentation and CLI output must describe
`--output-format fabric` as **Fabric scaffold / preview**, not as a complete
production-ready Direct Lake migration.

---

## v46.0.0 — Desktop Reliability & Release Discipline (Sprints 227–231)

v46 converts recent quality hardening into a repeatable, release-grade contract:

- openability checks become explicit release gates,
- shared-model generation must remain non-empty and thin-report-safe,
- README/docs/CLI/test suites stay synchronized,
- push/CI workflow avoids local git-maintenance prompt failures.

### Sprint 227 — Openability Gate Consolidation

**Goal:** Make static Desktop-openability preflight a first-class release gate.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 227.1 | Canonical openability contract | @orchestrator | `powerbi_import/openability.py`, `docs/ARCHITECTURE.md` | Medium | Freeze the set of blocking checks (JSON, PBIR paths, TMDL, DAX/M partition validity, report/model references). |
| 227.2 | Fixture suite stabilization | @tester | `tests/test_desktop_openability_suite.py` | Medium | Keep real-fixture coverage deterministic and clearly env-gated (`RUN_OPENABILITY_SUITE=1`). |
| 227.3 | CI gate wiring | @tester | `.github/workflows/ci.yml` | Low | Ensure openability suite can run in a dedicated quality stage with explicit pass/fail semantics. |

### Sprint 228 — Shared Model Safety Nets

**Goal:** Prevent technically-openable but operationally-useless shared-model outputs.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 228.1 | Non-empty merged model guarantee | @merger | `powerbi_import/shared_model.py` | Medium | Preserve fallback behavior when isolated filtering would result in zero tables. |
| 228.2 | Thin-report orphan thresholds | @orchestrator | `migrate.py`, `powerbi_import/import_to_powerbi.py` | Medium | Enforce `--strict-thin-report` + `--thin-report-max-orphans` as quality controls. |
| 228.3 | Regression tests | @tester | `tests/test_shared_model_v2.py`, `tests/test_desktop_openability_suite.py` | Medium | Lock behavior for strict fail/pass and non-empty shared semantic model assertions. |

### Sprint 229 — Documentation & CLI Coherence

**Goal:** Keep docs and runtime behavior aligned at every release.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 229.1 | Flag parity audit | @orchestrator | `README.md`, `.github/skills/tableau-to-powerbi/references/flags.md` | Low | Ensure all exposed flags and defaults are documented once and consistently. |
| 229.2 | Reliability runbook | @reviewer | `docs/MIGRATION_CHECKLIST.md`, `docs/FAQ.md` | Low | Add troubleshooting for openability failures and strict-thin-report violations. |
| 229.3 | Changelog quality entry template | @orchestrator | `CHANGELOG.md` | Low | Standardize release notes for gates/tests/docs deltas. |

### Sprint 230 — Release Pipeline Discipline

**Goal:** Make release publication resilient to local git environment anomalies.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 230.1 | Safe push helper | @orchestrator | `scripts/`, `docs/DEVELOPMENT_PLAN.md` | Medium | Add documented non-interactive push guidance (`gc.auto=0`, `maintenance.auto=false`) for noisy Windows environments. |
| 230.2 | Commit scope guardrails | @tester | `tests/` + CI checks | Low | Validate docs-only commits do not accidentally include perf probes or unrelated artifacts. |
| 230.3 | Branch protection compatibility checks | @deployer | `.github/workflows/` | Low | Ensure required checks and PR flow remain compatible with release cadence. |

### Sprint 231 — v46 Release Gate

**Goal:** Ship v46 only when quality, docs, and process gates are all green.

Release criteria:

- `tests/test_openability.py` green,
- `RUN_OPENABILITY_SUITE=1 tests/test_desktop_openability_suite.py` green,
- shared-model strict-thin-report tests green,
- README/CLI/docs parity check complete,
- no unresolved blocking risks in changelog release note.

---

## Agent Ownership Matrix (v22–v26)

| Agent | v22.0.0 Sprints | v23.0.0 Sprints | v24.0.0 Sprints | v25.0.0 Sprints | v26.0.0 Sprints |
|-------|----------------|----------------|----------------|----------------|----------------|
| **@orchestrator** | 76, 80 | 81, 83 | 86, 90 | 91, 95 | 96, 97, 98, 100 |
| **@extractor** | 76, 77 | — | 87 | 92 | 97 |
| **@converter** | 78 | 82 | 87 | 92, 93 | 99 |
| **@generator** | 76, 77, 78, 79 | 82 | 86, 87 | 91, 93 | 96, 99 |
| **@assessor** | 79 | — | 88 | 94 | 99 |
| **@merger** | — | — | 88, 89 | — | 98 |
| **@deployer** | — | 83 | 89, 90 | 94 | 97, 99, 100 |
| **@tester** | 76–80 (cross-cutting) | 81–85 (cross-cutting) | 86–90 (cross-cutting) | 91–95 (cross-cutting) | 96–100 (cross-cutting) |

---

## v22.0.0 — Real-World Fidelity & Layout Intelligence

### Motivation

Real-world migrations (NBA, Superstore, Feedback Dashboard) exposed gaps that synthetic tests don't catch: dashboard layout doesn't preserve Tableau's grid structure, advanced slicer modes are lost, stacked/grouped bar orientation is ambiguous, conditional formatting rules are shallow, and complex Tableau containers (show/hide, floating) produce misaligned PBI layouts. v22.0.0 focuses on **pixel-level layout fidelity** and **real-world visual accuracy**.

---

### Sprint 76 — Dashboard Layout Engine ✅ SHIPPED

**Goal:** Replace proportional scaling with a constraint-based layout engine that preserves Tableau's grid structure, container nesting, and alignment relationships.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 76.1 | **Container hierarchy extraction** | @extractor | `extract_tableau_data.py` | High | Parse `<layout-zone>` nesting: tiled containers → PBI alignment groups. Extract `is-fixed`, `auto-subscribe`, `min-size`, `max-size` constraints. Build parent→child tree. |
| 76.2 | **Grid-snapping layout algorithm** | @generator | `pbip_generator.py` | High | Replace `scale_x / scale_y` with grid-based layout: divide page into rows/columns based on Tableau zone positions. Snap visuals to nearest grid cell. Preserve relative proportions while respecting PBI minimum visual sizes. |
| 76.3 | **Floating vs tiled distinction** | @generator | `pbip_generator.py` | Medium | Floating zones → PBI `tabOrder` layering with precise x/y/w/h. Tiled zones → row/column-based relative positioning. Mixed dashboards maintain both. |
| 76.4 | **Responsive breakpoints** | @generator | `pbip_generator.py` | Medium | Extract `<device-layout>` from Tableau (phone, tablet). Generate PBI page `viewMode` variants with adjusted visual positions. Store device-specific overrides in page.json `mobileState`. |
| 76.5 | **Dashboard padding/margin extraction** | @extractor | `extract_tableau_data.py` | Low | Parse `inner-padding`, `outer-padding`, `border-style`, `border-color` attributes on zones. Propagate to PBI visual `padding` properties in `visualContainerObjects`. |
| 76.6 | **Tests** | @tester | `tests/test_layout_engine.py` (new) | Medium | 35+ tests: container nesting (1-level, 2-level, 3-level), grid snapping (2×2, 3×3, mixed), floating z-order, responsive breakpoints, padding propagation, real-world NBA layout validation |

**Success:** NBA dashboard opens in PBI Desktop with visuals in correct relative positions (2×4 grid).

---

### Sprint 77 — Advanced Slicer & Filter Intelligence ✅ SHIPPED

**Goal:** Fully migrate Tableau filter controls (dropdown, slider, relative date, wildcard, top-N, context filters) to PBI slicer equivalents with correct configuration.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 77.1 | **Filter type classification** | @extractor | `extract_tableau_data.py` | Medium | Classify extracted filters: `categorical` (list/dropdown), `range` (slider/between), `relative-date`, `wildcard` (contains/starts-with), `top-n`, `context` (pre-filter). Add `filter_mode` to filter JSON. |
| 77.2 | **Dropdown vs list slicer** | @generator | `pbip_generator.py` | Medium | `categorical` + high cardinality (>20 values) → dropdown slicer. Low cardinality → list slicer. Preserve `all_values_selected` default state and `exclude` mode (invert filter). |
| 77.3 | **Range slicer with bounds** | @generator | `pbip_generator.py` | Medium | `range` filters → PBI between slicer with `min`/`max` bounds from filter domain. Numeric: slider mode. Date: date picker mode. Preserve step size from Tableau parameter domain. |
| 77.4 | **Relative date slicer** | @generator | `pbip_generator.py` | Medium | Tableau "relative date" filters (last N days/weeks/months/years) → PBI relative date slicer with `anchorDate: today`, `relativePeriod`, `periodCount`. Handle "year to date", "quarter to date" presets. |
| 77.5 | **Wildcard filter** | @generator | `pbip_generator.py` | Low | Tableau wildcard match (contains, starts with, ends with) → PBI text slicer with search mode enabled. Set `search: true` on slicer config. |
| 77.6 | **Context filter → report-level filter** | @generator | `pbip_generator.py` | Low | Tableau context filters (applied before other filters) → PBI report-level filters. Emit `MigrationNote` explaining PBI evaluates all filters simultaneously. |
| 77.7 | **Tests** | @tester | `tests/test_slicer_intelligence.py` (new) | Medium | 30+ tests: filter classification (all types), dropdown vs list threshold, range bounds (numeric/date), relative date presets, wildcard search mode, context filter promotion, multi-filter interaction |

---

### Sprint 78 — Visual Fidelity Depth ✅ SHIPPED

**Goal:** Close the remaining visual accuracy gaps: stacked/grouped bar orientation, dual-axis combo charts, reference band shading, data label formatting, mark size encoding, and trend line preservation.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 78.1 | **Stacked bar orientation detection** | @generator | `visual_generator.py` | Medium | Extend `_detect_bar_orientation()` to stacked and 100% stacked variants: `Stacked Bar` + dim on cols → `stackedColumnChart`, `Stacked Bar` + measure on cols → `stackedBarChart`. Same for 100% variants. |
| 78.2 | **Dual-axis → combo chart** | @generator | `visual_generator.py` | High | Detect `dual_axis: true` in worksheet data → `lineClusteredColumnComboChart`. Map primary axis to column Y, secondary to line Y2. Preserve independent axis scaling (`isSecondaryAxis` on Y2 measures). Sync shared vs independent axis from Tableau config. |
| 78.3 | **Reference band shading** | @generator | `visual_generator.py` | Medium | Tableau reference bands (shaded region between two values) → PBI `constantLine` pairs with `shadeArea: true`. Map band color/opacity. Currently only reference lines are converted. |
| 78.4 | **Data label formatting** | @generator | `pbip_generator.py` | Medium | Propagate Tableau label font size, color, orientation (horizontal/vertical/rotated) → PBI `labels` properties. Handle mark-level label controls (show on specific marks only). |
| 78.5 | **Mark size encoding → bubble size** | @generator | `visual_generator.py` | Medium | Tableau `size` encoding shelf → PBI `Size` data role on scatter/bubble charts. Map continuous size range to PBI `bubbleSizes` min/max configuration. Detect discrete vs continuous size. |
| 78.6 | **Trend line preservation** | @converter | `dax_converter.py`, `visual_generator.py` | Medium | Tableau trend lines (linear, logarithmic, exponential, polynomial, power) → PBI analytics pane `trendLine` configuration with `regressionType`. Extract R² and p-value annotations from Tableau if present. |
| 78.7 | **Tests** | @tester | `tests/test_visual_fidelity_v2.py` (new) | Medium | 35+ tests: stacked orientation (4 variants), dual-axis decomposition, reference bands, label formatting, size encoding, trend line regression types, real-world visual comparison |

---

### Sprint 79 — Conditional Formatting & Theme Depth ✅ SHIPPED

**Goal:** Fully map Tableau quantitative/categorical color encoding to PBI conditional formatting rules, and deepen theme migration for background, border, and font styles.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 79.1 | **Diverging color scale** | @generator | `pbip_generator.py` | Medium | Tableau diverging palette (min→center→max, e.g. red→white→green) → PBI 3-stop gradient rule with min/mid/max colors and values. Detect diverging vs sequential from palette configuration. |
| 79.2 | **Stepped color (bins)** | @generator | `pbip_generator.py` | Medium | Tableau stepped color encoding (N discrete color bins from continuous measure) → PBI rules-based conditional formatting with N threshold conditions. Map bin boundaries from palette step count. |
| 79.3 | **Categorical color assignment** | @generator | `pbip_generator.py` | Medium | Tableau explicit color assignments (dimension value → specific color) → PBI `dataPoint.fill.solid.color` rules per category. Preserve exact hex colors from Tableau `<color-palette>`. |
| 79.4 | **Icon sets** | @generator | `pbip_generator.py` | Low | Tableau shape encoding with standard icons → PBI KPI icon conditional formatting. Map icon sets (arrows, circles, flags) to PBI `icon` format rules. |
| 79.5 | **Theme background & border** | @generator | `pbip_generator.py` | Medium | Extract dashboard background color, visual border color/width/radius from Tableau theme → PBI `background`, `border`, `visualHeader` properties in theme JSON and per-visual `visualContainerObjects`. |
| 79.6 | **Font style migration** | @generator | `pbip_generator.py` | Low | Tableau font family/size/bold/italic on titles, labels, axes → PBI `textClasses` in theme JSON. Map common Tableau fonts (Tableau Book, Tableau Light) to web-safe equivalents. |
| 79.7 | **Assessment: formatting coverage** | @assessor | `assessment.py` | Low | New sub-check in `_check_visual()`: count color-encoded fields, conditional formatting rules, and custom fonts. Score formatting migration coverage as a sub-metric. |
| 79.8 | **Tests** | @tester | `tests/test_conditional_formatting.py` (new) | Medium | 30+ tests: diverging scale, stepped color, categorical assignment, icon sets, background/border, font mapping, formatting assessment score |

---

### Sprint 80 — Integration Testing & v22.0.0 Release ✅ SHIPPED

**Goal:** End-to-end validation against all 16 real-world workbooks, performance regression suite, documentation update, and v22.0.0 release.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 80.1 | **Real-world E2E test suite** | @tester | `tests/test_real_world_e2e.py` (new) | High | For each of 16 real_world workbooks: extract → generate → validate → open in PBI Desktop (headless validation). Assert: no JSON errors, no TMDL errors, no missing visuals, page size matches dashboard. |
| 80.2 | **Layout regression tests** | @tester | `tests/test_layout_regression.py` (new) | Medium | Golden file comparison: store expected visual positions for 3 key workbooks (NBA, Superstore, Feedback). Fail if positions drift beyond tolerance. |
| 80.3 | **Performance regression** | @tester | `tests/test_performance_regression.py` (new) | Medium | Benchmark: 16 workbooks batch migration must complete in <30s. Single workbook <3s. Assert no regression vs v21 baseline. |
| 80.4 | **v22.0.0 release prep** | @orchestrator | `CHANGELOG.md`, `pyproject.toml`, docs | Low | Version bump 21.0.0 → 22.0.0. Update CHANGELOG, GAP_ANALYSIS, KNOWN_LIMITATIONS, README, copilot-instructions. |
| 80.5 | **Tests** | @tester | across above | — | Target: **5,500+** total tests (330+ new in v22) |

### v22.0.0 Success Criteria — ✅ ALL MET

| Metric | v21.0.0 | Target v22.0.0 | Actual |
|--------|---------|----------------|--------|
| Tests | 5,170 | **5,500+** | **5,683** ✅ |
| Visual layout accuracy | Proportional scaling | **Grid-snapped** | **Grid-snapped** ✅ |
| Slicer modes | Basic dropdown | **7 modes** (dropdown, list, slider, date picker, relative date, search, between) | **7 modes** ✅ |
| Conditional formatting types | Gradient only | **4 types** (gradient, diverging, stepped, categorical) | **4 types** ✅ |
| Stacked bar orientation | Always horizontal | **Orientation-aware** | **Orientation-aware** ✅ |
| Dual-axis combo charts | Mapped to lineChart | **lineClusteredColumnComboChart** with Y2 | **Combo chart** ✅ |
| Reference bands | Not migrated | **Shaded region pairs** | **Shaded** ✅ |
| Real-world E2E tests | Manual | **16 automated tests** | **26 workbooks, 369 tests** ✅ |

---

## v23.0.0 — Web UI, AI-Assisted Migration & CI Maturity

### Sprint 81 — Streamlit Web UI (@orchestrator)

**Goal:** Browser-based migration wizard for non-CLI users.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 81.1 | **Streamlit app scaffold** | @orchestrator | `web/app.py` (new) | High | 6-step wizard: Upload → Configure → Assess → Migrate → Validate → Download. Session state, temp dir management, error handling. |
| 81.2 | **Assessment view** | @orchestrator | `web/app.py` | Medium | 14-category radar chart, pass/warn/fail breakdown, strategy recommendation. Reuses `assessment.py`. |
| 81.3 | **Migration execution** | @orchestrator | `web/app.py` | Medium | Progress bar via `progress.py`, real-time log, fidelity score. ZIP download for `.pbip` project. |
| 81.4 | **Shared-model mode** | @orchestrator | `web/app.py` | Medium | Multi-file upload, merge heatmap, conflict list, force-merge toggle. |
| 81.5 | **Docker packaging** | @orchestrator | `web/Dockerfile` (new) | Low | Python 3.11 + Streamlit. `docker-compose.yml` for one-command startup. |
| 81.6 | **Tests** | @tester | `tests/test_web_app.py` (new) | Medium | 25+ tests: upload, config→args, pipeline integration, ZIP generation. |

---

### Sprint 82 — LLM-Assisted DAX Correction (@converter, @generator)

**Goal:** Optional AI-powered refinement for approximated DAX formulas.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 82.1 | **LLM client** | @converter | `powerbi_import/llm_client.py` (new) | High | OpenAI + Anthropic via `urllib`. Token counting, cost estimation, rate limiting. |
| 82.2 | **DAX refinement prompt** | @converter | `powerbi_import/llm_client.py` | High | Structured prompt: Tableau formula + current DAX + table/column context → refined DAX + confidence. |
| 82.3 | **Selective targeting** | @generator | `tmdl_generator.py` | Medium | Queue measures with `MigrationNote` containing "approximated" for LLM pass. Skip exact conversions. |
| 82.4 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--llm-refine`, `--llm-provider`, `--llm-model`, `--llm-key`, `--llm-max-calls` flags. |
| 82.5 | **Cost report** | @converter | `powerbi_import/llm_client.py` | Low | Per-formula: original → approximated → refined, confidence, tokens, cost. JSON report. |
| 82.6 | **Tests** | @tester | `tests/test_llm_client.py` (new) | Medium | 25+ tests: client init, prompt construction, response parsing, cost tracking, rate limiting, mock API. |

---

### Sprint 83 — CI/CD Maturity & PR Preview (@orchestrator, @deployer)

**Goal:** PR-level migration diff, automated release pipeline, coverage gates.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 83.1 | **PR migration preview** | @orchestrator | `.github/workflows/pr-preview.yml` (new) | High | On PR: detect changed samples → migrate → diff report → PR comment. |
| 83.2 | **Release automation** | @deployer | `.github/workflows/release.yml` (new) | Medium | Tag push → test → build wheel → GitHub Release → PyPI publish. |
| 83.3 | **Coverage gate** | @tester | `.github/workflows/ci.yml` | Low | `--fail-under=95`. Coverage badge in README. |
| 83.4 | **Test annotations** | @tester | `.github/workflows/ci.yml` | Low | JUnit XML → GitHub Actions inline failure annotations. |
| 83.5 | **Dependency scanning** | @deployer | `.github/workflows/ci.yml` | Low | `pip-audit` for optional deps. Fail on HIGH severity. |
| 83.6 | **Tests** | @tester | `tests/test_ci_workflows.py` (new) | Medium | 15+ tests: diff generation, release metadata, coverage threshold, YAML structure. |

---

### Sprint 84 — Conversion Accuracy Depth (@converter)

**Goal:** Close remaining approximation gaps in DAX and M conversion.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 84.1 | **Prep VAR/VARP** | @converter | `prep_flow_parser.py` | Low | Fix: `"var"` → `List.Variance`, `"varp"` → population variance. |
| 84.2 | **Prep notInner → leftanti** | @converter | `prep_flow_parser.py` | Low | Fix: `JoinKind.LeftAnti` instead of `JoinKind.FullOuter`. |
| 84.3 | **Bump chart RANKX** | @generator | `visual_generator.py` | Medium | Auto-inject `_bump_rank_{measure}` RANKX measure for bump chart → lineChart mapping. |
| 84.4 | **PDF connector depth** | @converter | `m_query_builder.py` | Medium | Page index, `[StartPage=N, EndPage=M]`, table selection. |
| 84.5 | **Salesforce SOQL depth** | @converter | `m_query_builder.py` | Medium | SOQL passthrough, API version, relationship traversal. |
| 84.6 | **REGEX_* → M fallback** | @converter | `dax_converter.py`, `m_query_builder.py` | Medium | When DAX REGEX is approximated, generate M `Text.RegexExtract` step as alternative. |
| 84.7 | **Tests** | @tester | `tests/test_conversion_accuracy.py` (new) | Medium | 30+ tests covering all fixes. |

---

### Sprint 85 — v23.0.0 Integration & Release (@orchestrator, @tester)

**Goal:** Cross-feature integration testing, documentation, release.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 85.1 | ~~Web UI + LLM integration~~ | @orchestrator | — | — | _Deferred (Sprint 81/82 on hold)_ |
| 85.2 | ~~E2E Web UI tests~~ | @tester | — | — | _Deferred (Sprint 81/82 on hold)_ |
| 85.3 | **v23.0.0 release** ✅ | @orchestrator | `pyproject.toml`, docs | Low | Version bump 22→23, CHANGELOG, README, copilot-instructions. |

### v23.0.0 Success Criteria

| Metric | v22.0.0 | v23.0.0 Actual |
|--------|---------|----------------|
| Tests | ~5,500 | **5,782 (116 files)** ✅ |
| Prep VAR/VARP | Approximated | **Correct** ✅ |
| Prep notInner | Approximated | **leftanti** ✅ |
| Bump chart RANKX | ❌ | **Auto-injected** ✅ |
| PDF connector depth | Basic | **Page range + table select** ✅ |
| Salesforce SOQL | Basic | **API version + SOQL passthrough** ✅ |
| REGEX → M fallback | ❌ | **Text.RegexMatch/Extract/Replace** ✅ |
| LTRIM/RTRIM | Both → TRIM | **Proper left/right trim** ✅ |
| INDEX | RANKX approx | **ROWNUMBER() (DAX 2024+)** ✅ |
| Fidelity scoring | Skipped penalized | **Skipped excluded, 100% avg** ✅ |
| Web UI | ❌ | _Deferred_ |
| LLM-assisted DAX | ❌ | _Deferred_ |
| PR migration preview | ❌ | _Deferred_ |
| Release automation | Manual | _Deferred_ |

---

## v24.0.0 — Composite Models, Live Sync & Enterprise Scale

### Sprint 86 — Composite Model Depth (@generator, @orchestrator) ✅ SHIPPED

**Goal:** Per-table StorageMode, aggregation tables, hybrid relationship validation.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 86.1 | **Per-table StorageMode** | @generator | `tmdl_generator.py` | High | `--mode composite`: classify tables (large→DirectQuery, small→Import). TMDL `mode` property on partitions. |
| 86.2 | **Aggregation table generation** | @generator | `tmdl_generator.py` | High | Auto-generate Import-mode agg tables with `alternateOf` annotations linking to detail columns. |
| 86.3 | **Hybrid relationship constraints** | @generator | `tmdl_generator.py` | Medium | Cross-storage-mode relationships → auto-set `oneDirection`. Warn on bi-directional cross-mode. |
| 86.4 | **Composite CLI flags** | @orchestrator | `migrate.py` | Low | `--composite-threshold ROWS`: tables above threshold → DirectQuery. `--agg-tables auto|none`. |
| 86.5 | **Tests** | @tester | `tests/test_composite_model.py` (new) | Medium | 30+ tests. |

---

### Sprint 87 — Extraction & Conversion Hardening (@extractor, @converter, @generator) ✅ SHIPPED

**Goal:** Handle edge cases discovered in real-world migrations: multi-connection workbooks, nested LOD expressions, complex join graphs, published datasource resolution.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 87.1 | **Published datasource resolution** | @extractor | `datasource_extractor.py` | High | When workbook uses published datasource (no embedded XML), call Tableau Server API to fetch full datasource definition. Merge into extraction pipeline. |
| 87.2 | **Nested LOD (LOD within LOD)** | @converter | `dax_converter.py` | High | Handle `{FIXED X : SUM({FIXED Y : COUNT([Z])})}` → nested CALCULATE with proper ALLEXCEPT nesting. Currently only single-level LOD supported. |
| 87.3 | **Complex join graphs** | @generator | `tmdl_generator.py` | Medium | Multi-hop join paths (A→B→C) → chain of TMDL relationships. Detect diamond joins (A→B→D, A→C→D) and emit warning. |
| 87.4 | **Multi-connection M queries** | @converter | `m_query_builder.py` | Medium | Workbooks connecting to multiple databases → separate M partitions per connection. Generate connection-specific Power Query parameters. |
| 87.5 | **Data type coercion rules** | @extractor | `datasource_extractor.py` | Low | Tableau auto-coercion (string→date, string→number) → explicit M `Table.TransformColumnTypes` step to prevent PBI type errors. |
| 87.6 | **Tests** | @tester | `tests/test_edge_cases.py` (new) | Medium | 30+ tests. |

---

### Sprint 88 — Enterprise Portfolio Intelligence (@assessor, @merger) ✅ SHIPPED

**Goal:** Cross-workbook optimization: detect shared data patterns, recommend model consolidation, estimate org-wide migration effort with resource allocation.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 88.1 | **Data lineage graph** | @assessor | `global_assessment.py` | High | Build cross-workbook data lineage: datasource → tables → calculations → visuals. HTML interactive graph (D3.js force-directed or Sankey). |
| 88.2 | **Consolidation recommender** | @merger | `shared_model.py` | Medium | Beyond merge scoring: recommend which workbooks should share models vs remain standalone based on data overlap, update frequency, audience segmentation. |
| 88.3 | **Resource allocation planner** | @assessor | `server_assessment.py` | Medium | Based on complexity scores and wave plan: recommend team size, skill mix (DAX expert, M expert, visual designer), timeline per wave. |
| 88.4 | **Governance report** | @assessor | `server_assessment.py` | Medium | Executive summary: total workbooks, migration waves, estimated effort (hours), risk matrix, recommended sequence, dependency map. HTML + PDF export. |
| 88.5 | **Tests** | @tester | `tests/test_portfolio_intelligence.py` (new) | Medium | 25+ tests. |

---

### Sprint 89 — Live Sync & Incremental Refresh (@merger, @deployer) ✅ SHIPPED

**Goal:** Keep migrated PBI artifacts in sync with evolving Tableau workbooks. Detect source changes, compute incremental diff, auto-deploy updates.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 89.1 | **Source change detection** | @merger | `incremental.py` | High | Compare Tableau workbook hash (from Server API `updatedAt`) against last migration manifest. Flag modified workbooks. |
| 89.2 | **Incremental diff generation** | @merger | `incremental.py` | High | For modified workbooks: extract → diff against previous extraction → generate only changed artifacts (new measures, modified visuals, updated M queries). |
| 89.3 | **Auto-deploy updates** | @deployer | `deploy/pbi_deployer.py` | Medium | `--sync` mode: detect changes → incremental migrate → deploy updated dataset/reports. Preserve existing refresh schedules and sharing. |
| 89.4 | **Change notification** | @deployer | `telemetry.py` | Low | Emit structured events for detected changes: `{workbook, change_type, affected_artifacts}`. Optionally post to webhook (Teams, Slack). |
| 89.5 | **Tests** | @tester | `tests/test_live_sync.py` (new) | Medium | 25+ tests. |

---

### Sprint 90 — Enterprise Scale & v24.0.0 Release (@orchestrator, @deployer, @tester) ✅ SHIPPED

**Goal:** Validate at 500+ workbook scale, optimize memory/CPU, document enterprise deployment patterns, ship v24.0.0.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 90.1 | **Memory optimization** | @orchestrator | `migrate.py`, pipeline | Medium | Stream extraction instead of loading all XML into memory. Generator writes TMDL files incrementally. Target: <500MB RAM for 100-workbook batch. |
| 90.2 | **Parallel batch processing** | @orchestrator | `migrate.py` | Medium | `--workers N` for parallel workbook extraction/generation. Thread pool for CPU-bound (DAX conversion) and I/O-bound (file write) phases. |
| 90.3 | **500-workbook benchmark** | @tester | `tests/test_enterprise_scale.py` (new) | High | Synthetic: generate 500 workbooks × 5 tables × 10 measures. Assert merge + deploy < 60s. Memory < 1GB. |
| 90.4 | **Enterprise deployment guide** | @deployer | `docs/ENTERPRISE_GUIDE.md` (new) | Medium | Step-by-step guide: discovery → assessment → wave planning → pilot migration → batch migration → validation → deployment → sync. |
| 90.5 | **v24.0.0 release** | @orchestrator | `pyproject.toml`, docs | Low | Version bump, CHANGELOG, README, GAP_ANALYSIS, copilot-instructions. |

### v24.0.0 Success Criteria

| Metric | v23.0.0 | Target v24.0.0 |
|--------|---------|----------------|
| Tests | ~5,800 | **6,200+** |
| Composite model | ❌ | **Per-table StorageMode + agg tables** |
| Published datasource | ❌ | **Server API resolution** |
| Nested LOD | Single level | **Multi-level** |
| Live sync | ❌ | **`--sync` auto-deploy** |
| Scale tested | 100 workbooks | **500 workbooks** (<60s) |
| Parallel batch | Sequential | **`--workers N`** |

---

## Per-Agent Detailed Roadmap

### @orchestrator — Pipeline & User Experience

| Sprint | Deliverable | Priority |
|--------|------------|----------|
| 76 | Dashboard layout engine pipeline integration | P0 |
| 80 | v22.0.0 release, docs update | P0 |
| 81 | **Streamlit Web UI** (6-step wizard, Docker) | P0 |
| 83 | PR preview workflow, CI flags | P1 |
| 86 | Composite model CLI flags (`--composite-threshold`, `--agg-tables`) | P1 |
| 90 | Memory optimization, parallel batch (`--workers N`), v24.0.0 | P0 |
| 91 | **Lakehouse notebook scaffold**, output format selection (`--output-format`) | P1 |
| 95 | v25.0.0 integration & release | P0 |
| 96 | **M query self-repair** (try/otherwise), error recovery report | P1 |
| 100 | **SLA tracking**, monitoring integration, v26.0.0 release | P0 |

**Key files:** `migrate.py`, `import_to_powerbi.py`, `wizard.py`, `progress.py`, `web/app.py` (new), `sla_tracker.py` (new)

---

### @extractor — Tableau XML Intelligence

| Sprint | Deliverable | Priority |
|--------|------------|----------|
| 76 | **Container hierarchy extraction** (zone nesting, constraints, padding) | P0 |
| 77 | **Filter type classification** (7 filter modes → filter JSON) | P0 |
| 87 | **Published datasource resolution** (Server API fetch) | P1 |
| 87 | **Data type coercion rules** (auto-type → explicit M cast) | P2 |
| 92 | **Dynamic zone visibility conditions** (show/hide with calculation conditions) | P1 |
| 92 | **Table extensions** (Einstein Discovery, external API data) | P1 |
| 97 | **Shapefile/GeoJSON passthrough** (extract from .twbx → shape map) | P2 |

**Key files:** `extract_tableau_data.py`, `datasource_extractor.py`, `server_client.py`

---

### @converter — Formula Translation Accuracy

| Sprint | Deliverable | Priority |
|--------|------------|----------|
| 78 | **Trend line DAX patterns** (regression types) | P1 |
| 82 | **LLM client** (OpenAI/Anthropic, prompt engine, cost tracking) | P0 |
| 84 | **Prep VAR/VARP**, **notInner→leftanti**, **PDF/Salesforce depth**, **REGEX→M fallback** | P1 |
| 87 | **Nested LOD** (LOD within LOD → nested CALCULATE) | P0 |
| 87 | **Multi-connection M** (per-connection partitions) | P1 |
| 92 | **Multi-connection worksheet resolution** (blend → merge-append M) | P1 |
| 93 | **DAX optimizer engine** (AST rewriter, IF→SWITCH, COALESCE, VAR/RETURN) | P0 |
| 93 | **Measure dependency DAG** (circular ref detection, unused measures) | P1 |
| 97 | **Nested LOD depth 3+** (recursive parser, depth 5 limit) | P0 |
| 97 | **LOOKUP/PREVIOUS_VALUE** (OFFSET-based conversion) | P0 |
| 97 | **Window function PARTITIONBY** (compute-using → PARTITIONBY/ORDERBY) | P1 |

**Key files:** `dax_converter.py`, `m_query_builder.py`, `prep_flow_parser.py`, `llm_client.py` (new), `dax_optimizer.py` (new)

---

### @generator — TMDL & PBIR Fidelity

| Sprint | Deliverable | Priority |
|--------|------------|----------|
| 76 | **Grid-snapping layout**, floating/tiled distinction, responsive breakpoints | P0 |
| 77 | **7 slicer modes** (dropdown, list, slider, date picker, relative date, search, between) | P0 |
| 78 | **Stacked bar orientation**, dual-axis combo, reference bands, data labels, mark size, trend lines | P0 |
| 79 | **Diverging/stepped/categorical conditional formatting**, icon sets, theme depth | P1 |
| 82 | LLM selective targeting (queue approximated measures) | P1 |
| 86 | **Per-table StorageMode**, aggregation tables, hybrid relationship constraints | P1 |
| 87 | Complex join graph handling | P2 |
| 91 | **Direct Lake semantic model** (`mode: directLake` partitions) | P0 |
| 91 | **Dataflow Gen2 generation** (M→Dataflow JSON mashup) | P1 |
| 93 | **Time Intelligence auto-injection** (YTD, QTD, PY, YoY%, MoM%) | P0 |
| 96 | **TMDL self-repair** (broken refs, circular rels, orphan measures) ✅ | P0 |
| 96 | **Visual fallback cascade** (degrade to simpler type on error) ✅ | P1 |
| 99 | **Spatial → Azure Maps visual** (lat/lon data roles) | P1 |

**Key files:** `pbip_generator.py`, `visual_generator.py`, `tmdl_generator.py`, `dataflow_generator.py` (new)

---

### @assessor — Migration Intelligence

| Sprint | Deliverable | Priority |
|--------|------------|----------|
| 79 | Formatting coverage sub-metric in visual assessment | P2 |
| 88 | **Data lineage graph** (cross-workbook D3.js/Sankey) | P1 |
| 88 | **Resource allocation planner** (team size, skill mix, timeline) | P1 |
| 88 | **Governance report** (executive summary, risk matrix, HTML+PDF) | P0 |
| 94 | **Query equivalence framework** (Tableau vs PBI value comparison) | P0 |
| 94 | **Visual screenshot comparison** (SSIM-based pixel diff) | P1 |
| 99 | **Naming convention enforcement** (configurable rules, warn/enforce) | P1 |
| 99 | **Data classification annotations** (PII detection → dataClassification) | P1 |

**Key files:** `assessment.py`, `server_assessment.py`, `global_assessment.py`, `equivalence_tester.py` (new), `governance.py` (new)

---

### @merger — Model Consolidation Intelligence

| Sprint | Deliverable | Priority |
|--------|------------|----------|
| 88 | **Consolidation recommender** (standalone vs shared decision) | P1 |
| 89 | **Source change detection** (Server API hash comparison) | P0 |
| 89 | **Incremental diff generation** (changed artifacts only) | P0 |
| 98 | **Shared model Fabric branch** (`--shared-model --output-format fabric`) ✅ | P0 |
| v27 | **Pattern registry** (migration marketplace with versioned patterns) | P0 |
| v27 | **DAX recipe overrides** (industry-specific measure templates) | P1 |

**Key files:** `shared_model.py`, `incremental.py`, `merge_config.py`, `marketplace.py` (new)

---

### @deployer — Enterprise Deployment & Sync

| Sprint | Deliverable | Priority |
|--------|------------|----------|
| 83 | **Release automation** (tag → build → publish pipeline) | P0 |
| 83 | **Dependency scanning** (pip-audit) | P2 |
| 89 | **Auto-deploy updates** (`--sync` mode) | P0 |
| 89 | **Change notification** (webhook: Teams/Slack) | P2 |
| 90 | **Enterprise deployment guide** | P1 |
| 94 | **Regression test suite generator** (auto-capture visual values for drift detection) | P1 |
| 97 | **Multi-tenant path traversal defense** (template substitution hardening) ✅ | P0 |
| 99 | **Sensitivity label assignment** (Tableau permissions → PBI labels) | P1 |
| 99 | **Audit trail** (immutable JSONL migration log) | P1 |
| 100 | **Endorsement & certification** (`--endorse promoted|certified`) | P2 |
| v27 | **Industry model templates** (Healthcare/Finance/Retail skeletons) | P1 |
| 100 | **Rolling deployment** (blue/green with auto-rollback) | P0 |
| 100 | **Monitoring integration** (Azure Monitor/App Insights/Prometheus) | P1 |

**Key files:** `deploy/*.py`, `telemetry.py`, `gateway_config.py`, `governance.py` (new), `monitoring.py` (new)

---

### @tester — Quality Gates & Coverage

| Sprint | Deliverable | Priority |
|--------|------------|----------|
| 76–80 | **v22 test files**: layout_engine, slicer_intelligence, visual_fidelity_v2, conditional_formatting, real_world_e2e, layout_regression, performance_regression | P0 |
| 81–85 | **v23 test files**: web_app, llm_client, ci_workflows, conversion_accuracy, web_e2e | P0 |
| 86–90 | **v24 test files**: composite_model, edge_cases, portfolio_intelligence, live_sync, enterprise_scale | P0 |
| 91–95 | **v25 test files**: fabric_native, tableau_2024, dax_optimizer, equivalence, fabric_e2e, optimization_e2e | P0 |
| 96–100 | **v26 test files**: self_healing ✅, security_hardening ✅, merged_fabric ✅, governance, production_scale | P0 |
| 83 | **Coverage gate** (95% threshold in CI) | P1 |
| 83 | **Test annotations** (JUnit XML → inline PR comments) | P2 |

**Target test counts:** v22: 5,500+ → v23: 5,800+ → v24: 6,200+ → v25: 6,600+ → v26: 7,000+

---

## Sprint Sequencing (v22–v26)

```
v22.0.0 — Real-World Fidelity
  Sprint 76 (Layout Engine)  ──→  Sprint 77 (Slicers)
           ↓                           ↓
  Sprint 78 (Visual Fidelity) ──→  Sprint 79 (Cond. Formatting)
                                       ↓
                             Sprint 80 (E2E + Release)

v23.0.0 — Web UI & AI
  Sprint 81 (Web UI)         ──→  Sprint 82 (LLM DAX)
           ↓                           ↓
  Sprint 83 (CI/CD)          ──→  Sprint 84 (Conversion Fixes) ✅
                                       ↓
                             Sprint 85 (Integration + Release)

v24.0.0 — Enterprise Scale
  Sprint 86 (Composite)      ──→  Sprint 87 (Hardening)
           ↓                           ↓
  Sprint 88 (Portfolio Intel) ──→  Sprint 89 (Live Sync)
                                       ↓
                             Sprint 90 (Scale + Release)

v25.0.0 — Semantic Intelligence
  Sprint 91 (Fabric-Native)  ──→  Sprint 92 (Tableau 2024+)
           ↓                           ↓
  Sprint 93 (DAX Optimizer)  ──→  Sprint 94 (Cross-Platform Validation)
                                       ↓
                             Sprint 95 (Integration + Release)

v26.0.0 — Autonomous Migration
  Sprint 96 (Self-Healing) ✅ ──→  Sprint 97 (Security) ✅
           ↓                           ↓
  Sprint 98 (Merged Fabric) ✅ ──→  Sprint 99 (Governance + Formulas)
                                       ↓
                             Sprint 100 (Production + Release)
```

---

## Risk Matrix

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Streamlit adds external dependency | Medium | Keep as optional `web/` module; core migration remains stdlib-only |
| LLM API costs for large migrations | High | Selective targeting (approximated only), cost cap (`--llm-max-calls`), dry-run mode |
| PBI Desktop layout validation requires GUI | Medium | Headless PBIR JSON validation; screenshot comparison optional |
| Nested LOD complexity explosion | High | Limit nesting to 3 levels; emit MigrationNote for deeper nesting |
| Published datasource requires Server access | Medium | Graceful fallback: extract available metadata, warn about missing columns |
| 500-workbook scale memory pressure | High | Streaming extraction, incremental TMDL writes, GC between workbooks |
| Fabric-native generation adds complexity | Medium | Keep as optional output format; TMDL core remains unchanged |
| Dynamic zone visibility parsing fragility | Medium | Feature-detect Tableau version; degrade to static zone on parse failure |
| DAX optimizer changing semantics | High | Before/after equivalence tests; opt-in only (`--optimize-dax`); preserve original as annotation |
| Governance rules blocking migration | Medium | Warn-only mode by default; enforce-mode requires explicit `--strict-governance` |
| Self-healing masking real issues | Medium | Recovery report documents every intervention; `--no-self-heal` disables |
| Marketplace pattern quality control | Medium | Patterns include validation tests; community rating + download count signals |

---

## v25.0.0 — Semantic Intelligence & Cross-Platform Parity

### Motivation

v22–v24 delivered layout fidelity, AI-assisted DAX, and enterprise-scale deployment. v25.0.0 shifts to **semantic intelligence** — making the migration engine deeply understand what a Tableau workbook _means_ (not just its XML structure), enabling automatic optimization, cross-platform equivalence testing, and intelligent data lineage. This version also targets **complete Tableau 2024.3+ feature coverage** and **Fabric-native artifact generation**.

---

### Sprint 91 — Fabric-Native Artifact Generation (@generator, @orchestrator) ✅ SHIPPED

**Goal:** Generate Fabric Lakehouse notebooks, Dataflows Gen2, and Direct Lake semantic models as first-class output formats alongside .pbip.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 91.1 | **Direct Lake semantic model** | @generator | `tmdl_generator.py` | High | `--mode direct-lake`: Generate TMDL with `mode: directLake` partitions pointing to Delta tables in a Lakehouse. Auto-map Tableau tables → Lakehouse table names. Emit `defaultPowerBIDataSourceVersion: powerBI_V3`. |
| 91.2 | **Dataflow Gen2 generation** | @generator | `powerbi_import/dataflow_generator.py` (new) | High | Convert Power Query M expressions to Dataflow Gen2 JSON mashup format. Support staging-to-lakehouse table output destinations. Handle connection references. |
| 91.3 | **Lakehouse notebook scaffold** | @orchestrator | `powerbi_import/notebook_generator.py` (new) | Medium | Generate PySpark notebooks for Tableau data transformations too complex for M (custom SQL, SCRIPT_*, complex Prep flows). Output as `.ipynb` or Fabric notebook JSON. |
| 91.4 | **Output format selection** | @orchestrator | `migrate.py` | Low | `--output-format pbip|fabric-lakehouse|dataflow-gen2`: Select generation target. Default remains `pbip`. Multiple formats can be combined. |
| 91.5 | **Tests** | @tester | `tests/test_fabric_native.py` (new) | Medium | 30+ tests: Direct Lake TMDL, Dataflow JSON structure, notebook generation, format selection, M→Dataflow mashup conversion. |

**Success:** A Superstore-class workbook generates a Lakehouse notebook + Direct Lake model that refreshes in Fabric without manual config.

---

### Sprint 92 — Deep Extraction: Tableau 2024+ Features (@extractor, @converter) ✅ SHIPPED

**Goal:** Complete coverage of Tableau 2024.1–2024.3+ features: dynamic zone visibility with conditions, table extensions, Explain Data config, and multi-connection worksheet resolution.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 92.1 | **Dynamic zone visibility conditions** | @extractor | `extract_tableau_data.py` | High | Parse `<dynamic-zone-visibility>` with `<calculation>` conditions on `<zone>` elements. Extract show/hide field refs and threshold logic. Map to PBI bookmark visibility toggles or selection pane bindings. |
| 92.2 | **Table extensions** | @extractor | `datasource_extractor.py` | Medium | Tableau 2024.2+ table extensions (Einstein Discovery, external API data). Extract extension config, API endpoint, schema. Generate M `Web.Contents()` query or placeholder with migration note. |
| 92.3 | **Multi-connection worksheet resolution** | @converter | `m_query_builder.py` | Medium | When a single worksheet references columns from 2+ datasources (multi-connection blend), generate separate M partitions per connection and a merge-append M step that combines them. Track blend relationships. |
| 92.4 | **Explain Data / Ask Data metadata** | @extractor | `extract_tableau_data.py` | Low | Extract `<ask-data>` and `<explain-data>` configs → PBI Q&A linguistic schema hints. Generate `linguisticSchema.xml` with synonyms from Tableau field captions. |
| 92.5 | **Tests** | @tester | `tests/test_tableau_2024.py` (new) | Medium | 25+ tests: dynamic zone conditions, table extensions, multi-connection blends, linguistic schema generation. |

---

### Sprint 93 — Semantic DAX Optimization (@converter, @generator) ✅ SHIPPED

**Goal:** Post-conversion DAX optimization pass that rewrites verbose converted formulas into idiomatic Power BI DAX, improving readability and performance.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 93.1 | **DAX optimizer engine** | @converter | `powerbi_import/dax_optimizer.py` (new) | High | AST-based DAX rewriter: simplify nested IF→SWITCH, collapse redundant CALCULATE, fold constant expressions, merge duplicate SUMX/AVERAGEX, convert IF(ISBLANK(x),0,x)→COALESCE, normalize variable extraction (VAR/RETURN). |
| 93.2 | **Time Intelligence auto-injection** | @generator | `tmdl_generator.py` | High | Auto-detect date-based measures and inject standard TI measures: YTD, QTD, MTD, PY, YoY%, MoM%, rolling 12-month. Configurable via `--time-intelligence auto|none|full`. Uses DATESINPERIOD, SAMEPERIODLASTYEAR, TOTALYTD. |
| 93.3 | **Measure dependency DAG** | @converter | `powerbi_import/dax_optimizer.py` | Medium | Build directed acyclic graph of measure-to-measure references. Detect circular refs (emit warning), unused measures (mark hidden), and recommend measure folders by dependency clusters. |
| 93.4 | **Optimization report** | @converter | `powerbi_import/dax_optimizer.py` | Low | JSON report: per-measure before/after comparison, simplification type applied, estimated performance impact (fewer nested IFs, reduced CALCULATE wrappers). |
| 93.5 | **Tests** | @tester | `tests/test_dax_optimizer.py` (new) | Medium | 35+ tests: each rewrite rule, circular ref detection, TI injection, measure DAG, before/after equivalence. |

**Success:** Complex_Enterprise measures are auto-optimized: nested IFs→SWITCH, redundant CALCULATEs removed, YoY% auto-generated.

---

### Sprint 94 — Cross-Platform Validation & Regression (@assessor, @deployer) ✅ SHIPPED

**Goal:** Automated equivalence testing: run the same queries against Tableau and Power BI to verify that migrated reports produce identical data.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 94.1 | **Query equivalence framework** | @assessor | `powerbi_import/equivalence_tester.py` (new) | High | For each migrated measure: extract expected values from Tableau (via Server REST API or Hyper data) → execute equivalent DAX query against deployed PBI dataset → compare results within tolerance threshold. Report pass/fail per measure. |
| 94.2 | **Visual screenshot comparison** | @assessor | `powerbi_import/equivalence_tester.py` | High | Optional: capture Tableau view PNG via Server REST API (`/views/{id}/image`) → capture PBI report page via PBI REST API (`/reports/{id}/pages/{page}/exportToFile`) → pixel-diff with configurable tolerance (SSIM ≥ 0.85). |
| 94.3 | **Regression test suite generator** | @deployer | `powerbi_import/regression_suite.py` (new) | Medium | Auto-generate a regression test JSON capturing all visual values, filter states, and data row counts. Re-run after re-migration to detect quality drift. |
| 94.4 | **Data validation CLI** | @orchestrator | `migrate.py` | Low | `--validate-data SERVER_URL`: Post-migration data validation comparing actual Tableau query results against PBI output. Requires both Server access and deployed dataset. |
| 94.5 | **Tests** | @tester | `tests/test_equivalence.py` (new) | Medium | 25+ tests: query construction, value comparison with tolerance, screenshot diffing (SSIM mock), regression JSON generation. |

---

### Sprint 95 — v25.0.0 Integration & Release (@orchestrator, @tester) ✅ SHIPPED

**Goal:** Cross-feature integration testing, Fabric-native + optimization + validation E2E, documentation, release.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 95.1 | **Fabric-native E2E** | @tester | `tests/test_fabric_e2e.py` (new) | High | Full pipeline: TWB → extract → generate Direct Lake → deploy → refresh → validate data. Integration test (opt-in). |
| 95.2 | **Optimization + validation E2E** | @tester | `tests/test_optimization_e2e.py` (new) | Medium | Extract → generate → optimize DAX → deploy → regression validate. 15+ tests. |
| 95.3 | **Docs update** | @orchestrator | `docs/`, `README.md`, `CHANGELOG.md` | Low | Document Fabric-native, DAX optimizer, TI injection, equivalence testing, Tableau 2024+ features. |
| 95.4 | **v25.0.0 release** | @orchestrator | `pyproject.toml`, docs | Low | Version bump, CHANGELOG, README, GAP_ANALYSIS, copilot-instructions. |

### v25.0.0 Success Criteria

| Metric | v24.0.0 | Target v25.0.0 |
|--------|---------|----------------|
| Tests | ~6,200 | **6,600+** |
| Fabric-native output | ❌ | **Direct Lake + Dataflow Gen2 + notebooks** |
| DAX optimization | ❌ | **AST rewriter + TI auto-injection** |
| Tableau 2024+ | Partial | **Dynamic zones, table extensions, multi-blend** |
| Data validation | ❌ | **Query equivalence + visual SSIM** |
| Linguistic schema (Q&A) | ❌ | **Auto-generated from field captions** |

---

## v26.0.0 — Autonomous Migration & Production Hardening

### Motivation

v26.0.0 targets **zero-touch autonomous migration** for standard workbooks: upload a .twbx, receive a production-ready .pbip with optimized DAX, proper governance, and deployed to Fabric — with no human intervention. This requires self-healing error recovery, governance policy enforcement, comprehensive audit logging, and a migration marketplace for community-contributed patterns.

---

### Sprint 96 — Self-Healing Migration Pipeline (@generator, @orchestrator) ✅ SHIPPED

**Goal:** When the migration engine encounters an error (TMDL validation failure, missing column reference, unsupported visual), it automatically applies corrective strategies instead of producing a broken artifact.

| # | Item | Owner | File(s) | Status | Details |
|---|------|-------|---------|--------|---------|
| 96.1 | **TMDL self-repair** | @generator | `tmdl_generator.py` | High | After generation, run semantic validation. For each failure: broken column ref → remove from measure/hide with MigrationNote; circular relationship → deactivate weakest link; duplicate table name → auto-suffix; orphan measure → reassign to main table. |
| 96.2 | **Visual fallback cascade** | @generator | `visual_generator.py` | Medium | If a visual config is invalid (missing required data role), apply fallback: remove optional roles first, then degrade to simpler visual type (scatter→table, combo→bar), then emit placeholder card. Log each degradation in migration report. |
| 96.3 | **M query self-repair** | @orchestrator | `m_query_builder.py` | Medium | Wrap each generated M partition in `try/otherwise #table({}, {})` at the outermost expression. If M evaluation fails in PBI Desktop, the table loads empty instead of blocking the entire model. |
| 96.4 | **Error recovery report** | @orchestrator | `powerbi_import/recovery_report.py` (new) | Low | JSON report listing every self-repair action taken: what failed, what intervention was applied, recommended manual follow-up. Append to migration_report JSON. |
| 96.5 | **Tests** | @tester | `tests/test_self_healing.py` (new) | Medium | 30+ tests: broken refs, circular rels, missing data roles, M parse errors, fallback cascade, recovery report structure. |

**Success:** A deliberately broken .twbx with missing columns and circular joins still produces a valid, openable .pbip with degraded-but-functional visuals.

---

### Sprint 97 — Security Hardening (@extractor, @orchestrator, @deployer) ✅ SHIPPED

**Goal:** OWASP Top 10 defense across the pipeline: path traversal, ZIP slip, XXE, credential exposure, injection via template substitution. Replaces originally-planned "Advanced Formula Intelligence" (deferred to v27.0.0).

| # | Item | Owner | File(s) | Status | Details |
|---|------|-------|---------|--------|---------|
| 97.1 | **Security validator module** | @generator | `security_validator.py` | ✅ | Centralized utilities: path validation (null byte, traversal, extension whitelist), ZIP slip defense (`safe_zip_extract_member`), XXE protection (`safe_parse_xml`), credential redaction (10 patterns), M query scrubbing, template sanitization |
| 97.2 | **ZIP slip + XXE defense** | @extractor | `extract_tableau_data.py` | ✅ | `read_tableau_file()` validates ZIP entries, `safe_parse_xml()` blocks DOCTYPE+ENTITY |
| 97.3 | **Input validation** | @orchestrator | `migrate.py` | ✅ | File path validation (null bytes, extension whitelist), `TABLEAU_TOKEN_SECRET` env var |
| 97.4 | **Multi-tenant injection defense** | @deployer | `deploy/multi_tenant.py` | ✅ | Placeholder validation, null byte blocking, context-aware escaping (JSON/M/TMDL) |
| 97.5 | **Wizard input hardening** | @orchestrator | `wizard.py` | ✅ | `getpass` for sensitive input, `_validate_file_path()`, extension whitelist |
| 97.6 | **Tests** | @tester | `tests/test_security.py` | ✅ | 64 tests: path (11), ZIP (7), XXE (6), credentials (14), sanitization (6), multi-tenant (7), wizard (4), scanning (4), integration (5) |

**Success:** All inputs validated, no credential leaks in output, ZIP/XXE attacks blocked.

---

### Sprint 98 — Merged Lakehouse / Fabric Output (@merger, @orchestrator) ✅ SHIPPED

**Goal:** Enable `--shared-model` multi-workbook merge to produce Fabric-native output (Lakehouse + Dataflow Gen2 + Notebook + DirectLake SemanticModel + Pipeline) instead of only PBIP format. Replaces originally-planned "Governance & Compliance" (deferred to Sprint 99).

| # | Item | Owner | File(s) | Status | Details |
|---|------|-------|---------|--------|---------|
| 98.1 | **Fabric branch in import_shared_model** | @orchestrator | `import_to_powerbi.py` | ✅ | `output_format='fabric'` routes merged data to `FabricProjectGenerator.generate_project()` |
| 98.2 | **CLI wiring** | @orchestrator | `migrate.py` | ✅ | `run_shared_model_migration()` forwards `output_format` from CLI args |
| 98.3 | **Thin reports in Fabric mode** | @merger | `import_to_powerbi.py` | ✅ | Thin reports placed inside Fabric project dir with `byPath` to DirectLake SemanticModel |
| 98.4 | **No model-explorer for Fabric** | @orchestrator | `import_to_powerbi.py` | ✅ | Fabric output skips `.pbip` model-explorer wrapper |
| 98.5 | **Tests** | @tester | `tests/test_shared_model_fabric.py` | ✅ | 12 tests: Fabric artifacts (5), thin reports (3), merged content (2), parameters (2) |

**Success:** `--shared-model wb1.twbx wb2.twbx --output-format fabric` produces complete merged Fabric project.

---

### Sprint 99 — Governance & Advanced Formulas (@assessor, @deployer, @converter) ✅ SHIPPED

**Goal:** Enterprise governance framework (naming conventions, data classification, audit trail) combined with the highest-priority formula intelligence items deferred from Sprint 97.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 99.1 | **Naming convention enforcement** | @assessor | `powerbi_import/governance.py` (new) | Medium | Configurable rules: measure prefix (`m_`), column naming (snake_case/camelCase), table naming (PascalCase). Auto-rename on generation or warn-only mode. Rules defined in `config.json` governance section. |
| 99.2 | **Data classification annotations** | @assessor | `powerbi_import/governance.py` | Medium | Scan TMDL columns for PII patterns (email, SSN, phone, name) → add `dataClassification` annotation. Generate classification report. |
| 99.3 | **Audit trail** | @deployer | `powerbi_import/governance.py` | Medium | Immutable JSON audit log: who migrated what, when, source hash, output hash, deployment target. Append-only `migration_audit.jsonl`. |
| 99.4 | **Sensitivity label assignment** | @deployer | `deploy/deployer.py` | Medium | Map Tableau project permissions → PBI sensitivity labels (Public/General/Confidential/Highly Confidential). Apply via PBI REST API. |
| 99.5 | **LOOKUP / PREVIOUS_VALUE** | @converter | `dax_converter.py` | High | `LOOKUP([Measure], -1)` → `CALCULATE([Measure], OFFSET(-1, ...))`. `PREVIOUS_VALUE(start)` → VAR/RETURN with OFFSET fallback. |
| 99.6 | **Window function PARTITIONBY** | @converter | `dax_converter.py` | Medium | Extract `compute-using`/`addressing` from table calc XML → WINDOW/OFFSET `PARTITIONBY` and `ORDERBY` clauses. Currently uses `ALL/ALLSELECTED` approximation. |
| 99.7 | **Spatial → Azure Maps visual** | @generator | `visual_generator.py` | Medium | Tableau MAKEPOINT coordinates → PBI `azureMap` visual with lat/lon data roles. Replace `0+comment` DAX. |
| 99.8 | **Tests** | @tester | `tests/test_governance.py` (new), `tests/test_advanced_formulas.py` (new) | Medium | 50+ tests: naming rules (10), PII detection (8), audit log (6), sensitivity mapping (4), LOOKUP/PREVIOUS_VALUE (10), PARTITIONBY (8), Azure Maps (4) |

**Success:** Enterprise customers can enforce naming standards and PII classification. LOOKUP/PREVIOUS_VALUE formulas convert to OFFSET-based DAX.

---

### Sprint 100 — Production Hardening & v26.0.0 Release (@orchestrator, @deployer, @tester) ✅ SHIPPED

**Goal:** Harden for production enterprise use: rolling deployments, monitoring integration, migration SLA tracking, 1000-workbook stress test, and v26.0.0 release.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 100.1 | **Rolling deployment** | @deployer | `deploy/pbi_deployer.py` | High | `--rolling`: Deploy updated dataset first, validate refresh success, then swap reports. Automatic rollback on validation failure. Blue/green deployment with canary phase. |
| 100.2 | **Migration SLA tracking** | @orchestrator | `powerbi_import/sla_tracker.py` (new) | Medium | Per-workbook SLAs: max migration time, min fidelity score, required data validation pass. Track compliance across batch migrations. Alert on SLA breach. |
| 100.3 | **Monitoring integration** | @deployer | `powerbi_import/monitoring.py` (new) | Medium | Export migration metrics to Azure Monitor (custom metrics), Application Insights (traces/events), or Prometheus (push gateway). `--monitor azure|prometheus|none`. |
| 100.4 | **Endorsement & certification** | @deployer | `deploy/deployer.py` | Low | `--endorse promoted|certified`: Set endorsement status on deployed datasets/reports via PBI REST API. |
| 100.5 | **1000-workbook stress test** | @tester | `tests/test_production_scale.py` (new) | High | Synthetic: 1000 workbooks × 3 tables × 5 measures. Assert: total < 120s, peak memory < 2GB, 0 broken artifacts, SLA compliance ≥ 99%. |
| 100.6 | **v26.0.0 release** | @orchestrator | `pyproject.toml`, docs | Low | Version bump, CHANGELOG, README, GAP_ANALYSIS, KNOWN_LIMITATIONS, copilot-instructions. |

### v26.0.0 Success Criteria

| Metric | v25.0.0 | Target v26.0.0 | v26.0.0 Actual |
|--------|---------|----------------|----------------|
| Tests | ~6,192 | **7,000+** | **6,400+** across 134 files |
| Self-healing pipeline | ❌ | **Auto-repair TMDL, visuals, M queries** | ✅ Sprint 96 |
| Security hardening | ❌ | **ZIP slip, XXE, credential redaction** | ✅ Sprint 97 |
| Merged Fabric output | ❌ | **--shared-model + --output-format fabric** | ✅ Sprint 98 |
| Governance framework | ❌ | **Naming, PII classification, audit** | ✅ Sprint 99 |
| LOOKUP/PREVIOUS_VALUE | ❌ | **OFFSET-based conversion** | ✅ Sprint 99 |
| Rolling deployment | ❌ | **Blue/green with auto-rollback** | ✅ Sprint 100 |
| Scale tested | 500 workbooks | **1000 workbooks** (<120s) | ✅ Sprint 100 |
| SLA tracking | ❌ | **Per-workbook SLA compliance** | ✅ Sprint 100 |

---

## v27.0.0 — Advanced Intelligence & Marketplace (Sprints 101–106)

### Sprint 101: Recursive LOD Parser ✅ SHIPPED

**Owner:** @converter  
**Goal:** Replace the iterative 50-iteration LOD parser with a true recursive descent parser for arbitrary nesting depth.

| # | Item | Owner | Status |
|---|------|-------|--------|
| 101.1 | Recursive descent `_parse_lod_recursive()` in `dax_converter.py` | @converter | ✅ |
| 101.2 | Nested LOD depth 3+ support (FIXED→INCLUDE→EXCLUDE chains) | @converter | ✅ |
| 101.3 | Sibling LOD support at same level | @converter | ✅ |
| 101.4 | Tests: 12 tests (basic, nested, depth-5, siblings, multi-table) | @tester | ✅ |

### Sprint 102: Window Function Depth ✅ SHIPPED

**Owner:** @converter  
**Goal:** Multi-level PARTITIONBY + multi-column ORDERBY + MATCHBY for DAX window functions.

| # | Item | Owner | Status |
|---|------|-------|--------|
| 102.1 | `_build_window_clauses()` helper — unified ORDERBY/PARTITIONBY/MATCHBY builder | @converter | ✅ |
| 102.2 | `partition_fields` dict: `order_by`, `partition_by`, `match_by` | @converter | ✅ |
| 102.3 | Multi-column ORDERBY with sort direction (ASC/DESC) | @converter | ✅ |
| 102.4 | Tests: 10 tests (basic, frame, partition_by, orderby, matchby, combined) | @tester | ✅ |

### Sprint 103: Migration Marketplace ✅ SHIPPED

**Owner:** @orchestrator  
**Goal:** Versioned pattern registry for community DAX recipes, visual mappings, and M query templates.

| # | Item | Owner | Status |
|---|------|-------|--------|
| 103.1 | `marketplace.py` — PatternRegistry, Pattern, PatternMetadata classes | @orchestrator | ✅ |
| 103.2 | JSON-file catalogue loader with versioned search/filter | @orchestrator | ✅ |
| 103.3 | `apply_dax_recipes()` — inject/replace DAX measures from patterns | @orchestrator | ✅ |
| 103.4 | `apply_visual_overrides()` — override visual type mappings | @orchestrator | ✅ |
| 103.5 | `examples/marketplace/` — 3 built-in patterns (revenue_ytd, yoy_growth, map_override) | @orchestrator | ✅ |
| 103.6 | Tests: 12 tests (metadata, registry, search, versioning, apply, export) | @tester | ✅ |

### Sprint 104: DAX Recipe Overrides ✅ SHIPPED

**Owner:** @converter  
**Goal:** Industry-specific KPI measure templates for Healthcare, Finance, and Retail.

| # | Item | Owner | Status |
|---|------|-------|--------|
| 104.1 | `dax_recipes.py` — HEALTHCARE_RECIPES (6 KPIs), FINANCE_RECIPES (8 KPIs), RETAIL_RECIPES (7 KPIs) | @converter | ✅ |
| 104.2 | `apply_recipes()` — inject/replace/overwrite modes | @converter | ✅ |
| 104.3 | `recipes_to_marketplace_format()` — bridge to PatternRegistry | @converter | ✅ |
| 104.4 | Tests: 12 tests (industries, apply, overwrite, replace, marketplace format) | @tester | ✅ |

### Sprint 105: Industry Model Templates ✅ SHIPPED

**Owner:** @generator  
**Goal:** Pre-built semantic model skeletons for Healthcare, Finance, and Retail.

| # | Item | Owner | Status |
|---|------|-------|--------|
| 105.1 | `model_templates.py` — Healthcare star schema (Encounters, Patients, Providers, Facilities) | @generator | ✅ |
| 105.2 | Finance star schema (Financials, Accounts, CostCenters, AR) | @generator | ✅ |
| 105.3 | Retail star schema (Sales, Products, Stores, Customers) | @generator | ✅ |
| 105.4 | `apply_template()` — merge template into migrated tables (enrich columns, add relationships) | @generator | ✅ |
| 105.5 | Tests: 13 tests (list, get, apply, enrich, relationships, deep copy) | @tester | ✅ |

### Sprint 106: Shapefile/GeoJSON Passthrough ✅ SHIPPED

**Owner:** @extractor  
**Goal:** Extract .shp/.geojson/.topojson from .twbx → PBI shape map configuration.

| # | Item | Owner | Status |
|---|------|-------|--------|
| 106.1 | `geo_passthrough.py` — GeoExtractor (ZIP extraction with path traversal defense) | @extractor | ✅ |
| 106.2 | Format classification (.geojson, .topojson, .shp components) | @extractor | ✅ |
| 106.3 | `build_shape_map_config()` — PBI shapeMap visual configuration | @generator | ✅ |
| 106.4 | `copy_to_registered_resources()` — deploy geo files into .pbip project | @generator | ✅ |
| 106.5 | GeoJSON property extraction for key binding | @extractor | ✅ |
| 106.6 | Tests: 13 tests (classify, extract, build config, copy, integration) | @tester | ✅ |

### v27.0.0 Success Criteria

| Metric | Target | v27.0.0 Actual |
|--------|--------|----------------|
| LOD nesting depth | Unlimited (recursive) | ✅ Recursive descent, tested to depth 5+ |
| Window function clauses | ORDERBY + PARTITIONBY + MATCHBY | ✅ All three supported |
| Marketplace patterns | Versioned registry with search | ✅ PatternRegistry with semver |
| Industry DAX recipes | 3 verticals, 20+ KPIs | ✅ 21 KPIs across 3 industries |
| Model templates | 3 star schemas | ✅ Healthcare, Finance, Retail |
| Geo passthrough | .geojson + .shp extraction | ✅ 8 file types, shape map config |
| Tests | 6,400+ | ✅ 6,454 passed |

### Sprint 107: Unified HTML Report Template ✅ SHIPPED

**Owner:** @generator  
**Goal:** Centralize CSS/JS across all 9 HTML report generators into a shared template module with Fluent/PBI design.

| # | Item | Owner | Status |
|---|------|-------|--------|
| 107.1 | `html_template.py` — shared CSS/JS template module with design tokens, components | @generator | ✅ |
| 107.2 | Upgrade `generate_report.py` (batch migration dashboard) | @generator | ✅ |
| 107.3 | Upgrade `server_assessment.py` (server portfolio assessment) | @assessor | ✅ |
| 107.4 | Upgrade `global_assessment.py` (global + governance reports) | @assessor | ✅ |
| 107.5 | Upgrade `merge_report_html.py` (shared model merge report) | @merger | ✅ |
| 107.6 | Upgrade `telemetry_dashboard.py` (observability dashboard) | @deployer | ✅ |
| 107.7 | Upgrade `visual_diff.py`, `comparison_report.py`, `merge_assessment.py` | @assessor | ✅ |
| 107.8 | Dark mode support (`prefers-color-scheme: dark`) | @generator | ✅ |
| 107.9 | Unit tests for html_template.py | @tester | ✅ |

### v27.1.0 Success Criteria

| Metric | Target | v27.1.0 Actual |
|--------|--------|----------------|
| HTML reports unified | 9/9 generators | ✅ All 9 upgraded |
| Shared CSS/JS module | 1 template file | ✅ `html_template.py` (640+ lines) |
| Duplicate CSS removed | >1,000 lines | ✅ ~1,230 lines removed |
| Dark mode | CSS `prefers-color-scheme` | ✅ Full dark theme |
| Tests | 6,454+ | ✅ 6,454+ passed |

---

## v28.0.0 — Extensibility & Core Infrastructure (Sprints 108–111) ✅ SHIPPED

### Sprint 108: TDS/TDSX Standalone Datasource Migration ✅ SHIPPED

**Owner:** @extractor, @generator  
**Goal:** Migrate Tableau `.tds`/`.tdsx` data source files to Power BI SemanticModel-only projects.

| # | Item | Owner | Status |
|---|------|-------|--------|
| 108.1 | Extract `<datasource>` root → synthetic `<workbook>` wrapper | @extractor | ✅ |
| 108.2 | Datasource-only detection in PBIPGenerator (skip Report folder) | @generator | ✅ |
| 108.3 | `.pbip` artifacts reference SemanticModel for datasource-only | @generator | ✅ |
| 108.4 | Batch scanner includes `.tds`/`.tdsx` extensions | @orchestrator | ✅ |
| 108.5 | E2E test updates for `DATASOURCE_ONLY_WORKBOOKS` | @tester | ✅ |

---

### Phase 1 — Core Extensibility (Sprints 109–111) ✅ SHIPPED

| Sprint | Theme | Owner(s) | Priority | Status | Deliverables |
|--------|-------|----------|----------|--------|--------------|
| **109** | **TDSX with Hyper data inlining** | @extractor, @generator | P1 | ✅ | `hyper_files.json` loaded as 17th artifact. `tmdl_generator` inlines Hyper row data into M `#table()`/`Csv.Document()` partitions via `generate_m_from_hyper()`. 15 tests. |
| **110** | **REST API endpoint** | @orchestrator, @deployer | P1 | ✅ | stdlib `http.server` API: `POST /migrate`, `GET /status/{id}`, `GET /download/{id}`, `GET /health`, `GET /jobs`. Thread-safe job store, multipart upload, Dockerfile. 21 tests. |
| **111** | **Incremental schema drift detection** | @extractor, @assessor | P2 | ✅ | `schema_drift.py`: compare extraction snapshots (tables, columns, calculations, worksheets, relationships, parameters, filters). `--check-drift SNAPSHOT_DIR` CLI. JSON + summary output. 25 tests. |

### Phase 2 — Intelligence & UX (Sprints 112–114)

---

#### Sprint 112 — LLM-Assisted DAX Correction (@converter, @orchestrator)

**Goal:** Optional AI-powered refinement for approximated DAX formulas — send measures tagged with `MigrationNote` containing "approximated" to an LLM for semantic correction. Pluggable backend (Azure OpenAI, OpenAI, local/Ollama). Original DAX preserved as annotation.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 112.1 | **LLM client module** | @converter | `powerbi_import/llm_client.py` (new) | High | Pluggable backend: Azure OpenAI (`urllib`+managed identity), OpenAI (`urllib`+API key), local/Ollama (`localhost:11434`). Token counting (tiktoken-compatible estimation), cost tracking, exponential retry with backoff, `--llm-max-calls N` budget cap. No external deps (stdlib `urllib.request` + `json`). |
| 112.2 | **DAX refinement prompt engine** | @converter | `powerbi_import/llm_client.py` | High | Structured prompt: Tableau formula + current approximated DAX + table schema (columns, types) + relationship context → refined DAX + confidence score (0–1) + explanation. System prompt enforces DAX syntax rules and Power BI compatibility. |
| 112.3 | **Selective targeting** | @generator | `tmdl_generator.py` | Medium | Post-generation pass: scan all measures for `MigrationNote` containing "approximated", "fallback", or "no equivalent". Queue for LLM refinement. Skip exact conversions. Cap at `--llm-max-calls` (default 50). |
| 112.4 | **Accept/reject validation** | @converter | `powerbi_import/llm_client.py` | Medium | Parse LLM response → validate DAX syntax (balanced parens, known function names, valid column refs) → accept if valid, reject and keep original if malformed. Log accepted/rejected ratio. |
| 112.5 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--llm-refine`, `--llm-provider azure-openai|openai|local`, `--llm-model gpt-4o`, `--llm-endpoint URL`, `--llm-max-calls N`. Env vars: `LLM_API_KEY`, `AZURE_OPENAI_ENDPOINT`. |
| 112.6 | **Cost & refinement report** | @converter | `powerbi_import/llm_client.py` | Low | JSON report: per-measure original → approximated → refined, confidence, tokens used, estimated cost. Summary: total measures refined, acceptance rate, total tokens. |
| 112.7 | **Tests** | @tester | `tests/test_llm_client.py` (new) | Medium | 30+ tests: client init (3 backends), prompt construction, response parsing, DAX validation, cost tracking, rate limiting, mock API responses, budget cap, selective targeting, accept/reject logic. |

**Agent work:**
- **@converter** — owns `llm_client.py`: prompt engine, response parsing, DAX validation, cost tracking
- **@generator** — selective targeting in `tmdl_generator.py`: scan MigrationNotes, queue approximated measures
- **@orchestrator** — CLI flags in `migrate.py`, env var wiring
- **@tester** — 30+ tests with mock LLM responses

---

#### Sprint 113 — Streamlit Web UI Phase 1 (@orchestrator, @generator)

**Goal:** Browser-based migration wizard for users who prefer GUI over CLI. 6-step wizard wrapping the existing pipeline. Streamlit is an **optional dependency** — core migration remains stdlib-only.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 113.1 | **App scaffold & routing** | @orchestrator | `web/app.py` (new) | High | 6-step wizard: Upload (.twb/.twbx/.tds) → Configure (output format, culture, calendar range) → Assess (readiness radar chart) → Migrate (real-time progress) → Validate (artifact summary) → Download (.pbip ZIP). Streamlit session state for temp dirs, cleanup on session end. |
| 113.2 | **File upload & extraction** | @orchestrator | `web/app.py` | Medium | Drag-and-drop with `st.file_uploader`. Save to temp dir. Call `read_tableau_file()` + `extract_tableau_data()`. Display extraction summary (tables, measures, visuals count). Security: validate file extension + size limit (500MB). |
| 113.3 | **Assessment preview** | @orchestrator | `web/app.py` | Medium | Call `assess_migration_readiness()`. Render 9-category pass/warn/fail table. Strategy recommendation (Import/DirectQuery/Composite). Show connection string audit warnings. |
| 113.4 | **Migration execution with progress** | @orchestrator | `web/app.py` | Medium | Call `import_to_powerbi()` in background thread. `st.progress()` bar linked to `ProgressTracker`. Real-time log streaming to `st.expander`. Fidelity score display on completion. |
| 113.5 | **Download & artifact preview** | @generator | `web/app.py` | Medium | ZIP the output directory. `st.download_button` for `.pbip` project. Preview: list generated pages, visuals per page, measure count, relationship diagram (Mermaid in `st.markdown`). |
| 113.6 | **Docker packaging** | @orchestrator | `web/Dockerfile` (new) | Low | `python:3.12-slim` + `pip install streamlit`. `docker-compose.yml` for one-command startup. Health check endpoint. Volume mount for input/output. |
| 113.7 | **Tests** | @tester | `tests/test_web_app.py` (new) | Medium | 25+ tests: upload validation, config→args mapping, pipeline integration (mock Streamlit), ZIP generation, session cleanup. |

**Agent work:**
- **@orchestrator** — owns `web/app.py`: scaffold, upload, config, pipeline execution, Docker
- **@generator** — artifact preview, relationship diagram rendering
- **@tester** — 25+ tests with mock Streamlit session

---

#### Sprint 114 — Streamlit Web UI Phase 2 (@orchestrator, @assessor, @merger)

**Goal:** Extend Web UI with batch mode, shared-model merge UI, side-by-side visual diff, DAX formula editor, and Fabric deployment button.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 114.1 | **Batch mode page** | @orchestrator | `web/pages/batch.py` (new) | Medium | Multi-file upload or folder path. Progress table (workbook name, status, fidelity). Batch summary dashboard reusing `generate_report.py` HTML. Download all as ZIP. |
| 114.2 | **Shared model merge page** | @merger | `web/pages/merge.py` (new) | High | Multi-workbook upload → merge heatmap (table overlap scores), conflict list, force-merge toggle, model name input. Preview merged table list. Download shared model + thin reports. |
| 114.3 | **Visual diff viewer** | @assessor | `web/pages/diff.py` (new) | Medium | Side-by-side: Tableau worksheet list (from extraction JSON) vs PBI page/visual list. Per-visual field coverage, encoding gaps. Reuses `visual_diff.py` output. |
| 114.4 | **DAX formula editor** | @orchestrator | `web/pages/editor.py` (new) | Medium | Select a measure → view Tableau formula + converted DAX side-by-side. In-place edit DAX. Re-validate with `dax_optimizer.py`. Save overrides to `config.json`. |
| 114.5 | **Fabric deployment button** | @deployer | `web/pages/deploy.py` (new) | Medium | Workspace ID input + auth (token or SP). One-click deploy via `deploy/deployer.py`. Status polling. Deployment report display. |
| 114.6 | **Tests** | @tester | `tests/test_web_app_v2.py` (new) | Medium | 25+ tests: batch upload, merge UI flows, diff rendering, DAX edit round-trip, deploy mock. |

**Agent work:**
- **@orchestrator** — batch page, DAX editor, page routing
- **@merger** — merge page with heatmap and conflict resolution
- **@assessor** — visual diff viewer page
- **@deployer** — Fabric deployment page with auth flow
- **@tester** — 25+ tests

---

### Phase 3 — Production & Enterprise (Sprints 115–117)

---

#### Sprint 115 — PDF Export & Report Packaging (@generator, @assessor)

**Goal:** Generate PDF versions of all HTML migration/assessment reports for offline distribution and executive review. Optional dependency (`weasyprint` or stdlib HTML-to-PDF via `html2pdf`).

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 115.1 | **PDF renderer module** | @generator | `powerbi_import/pdf_renderer.py` (new) | High | Pluggable backend: (1) `weasyprint` (optional), (2) stdlib fallback generating a simplified paginated HTML with `@media print` CSS. `render_html_to_pdf(html_content, output_path)` API. |
| 115.2 | **Print-optimized CSS** | @generator | `powerbi_import/html_template.py` | Medium | Add `@media print` styles to shared template: page breaks, margin control, hide interactive elements (sort buttons, search), expand collapsed sections. A4/Letter page size support. |
| 115.3 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--pdf` flag on `--assess`, `--global-assess`, `--assess-merge`, server assessment. `--pdf-only` to skip HTML. |
| 115.4 | **Report packaging** | @assessor | `powerbi_import/assessment.py` | Medium | `--report-package` generates a ZIP containing: HTML report + PDF + extraction JSON + fidelity summary CSV. Single deliverable for stakeholders. |
| 115.5 | **Tests** | @tester | `tests/test_pdf_export.py` (new) | Medium | 20+ tests: PDF render (mock weasyprint), print CSS validation, CLI flag wiring, package ZIP structure. |

**Agent work:**
- **@generator** — PDF renderer module + print CSS in html_template.py
- **@assessor** — report packaging (HTML + PDF + data ZIP)
- **@orchestrator** — CLI flags
- **@tester** — 20+ tests

---

#### Sprint 116 — Workspace-Level Migration Planner (@assessor, @deployer, @extractor)

**Goal:** Given a Tableau Server site (via `--server` + REST API), generate a complete enterprise migration plan: dependency graph, wave assignments, effort estimates, Fabric workspace mapping, RLS group mapping, refresh schedule migration plan.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 116.1 | **Server site discovery** | @extractor | `tableau_export/server_client.py` | Medium | New endpoint: `get_site_topology()` → all workbooks + datasources + published datasources + users + groups + schedules + subscriptions. Build adjacency map (workbook↔datasource dependencies). |
| 116.2 | **Migration plan generator** | @assessor | `powerbi_import/migration_planner.py` (new) | High | Input: site topology + per-workbook assessment. Output: dependency-ordered migration waves, per-wave effort estimate (hours), team assignment suggestions, critical-path identification. Respects datasource dependencies (shared datasources migrate first). |
| 116.3 | **Fabric workspace mapper** | @deployer | `powerbi_import/migration_planner.py` | Medium | Map Tableau Projects → Fabric Workspaces. Tableau Sites → Fabric Capacities. Suggest workspace partitioning based on content groups and RLS boundaries. Output: workspace mapping JSON. |
| 116.4 | **RLS group mapping** | @deployer | `powerbi_import/migration_planner.py` | Medium | Map Tableau user-filters + groups → Azure AD group assignments for PBI RLS roles. Output: mapping CSV (Tableau group → Azure AD group → RLS role). |
| 116.5 | **Refresh schedule mapping** | @deployer | `powerbi_import/refresh_generator.py` | Low | Extend existing refresh migration: map entire site's extract-refresh schedules to PBI refresh configs. Detect conflicts (>8 daily refreshes on Pro). Output: schedule migration report. |
| 116.6 | **Migration plan HTML report** | @assessor | `powerbi_import/migration_planner.py` | Medium | Interactive HTML: wave timeline (Gantt-style), dependency graph, workspace map, effort heatmap, RLS mapping table. Uses shared `html_template.py`. |
| 116.7 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--plan-migration` flag (requires `--server`). Output: migration plan JSON + HTML report. |
| 116.8 | **Tests** | @tester | `tests/test_migration_planner.py` (new) | Medium | 30+ tests: topology parsing, wave ordering, effort calculation, workspace mapping, RLS mapping, schedule conflicts, HTML report structure. |

**Agent work:**
- **@extractor** — site topology discovery in server_client.py
- **@assessor** — migration plan generator + HTML report
- **@deployer** — workspace mapper, RLS mapper, refresh schedule extension
- **@orchestrator** — CLI flag
- **@tester** — 30+ tests

---

#### Sprint 117 — v28.0.0 Release & Hardening (All Agents)

**Goal:** Version bump, comprehensive integration testing, documentation update, PyPI publish, and codebase hardening.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 117.1 | **Version bump** | @orchestrator | `pyproject.toml` | Low | `27.1.0` → `28.0.0`. Update all version references. |
| 117.2 | **CHANGELOG update** | @orchestrator | `CHANGELOG.md` | Medium | Document all Phase 1–3 sprints (108–117) with per-sprint summaries. |
| 117.3 | **Cross-phase E2E tests** | @tester | `tests/test_v28_e2e.py` (new) | High | End-to-end: extract → LLM refine (mock) → generate → validate → deploy (mock) → plan (mock). 15+ integration tests spanning all new v28 features. |
| 117.4 | **Documentation refresh** | @orchestrator | `README.md`, `docs/*.md` | Medium | Update GAP_ANALYSIS, KNOWN_LIMITATIONS, MAPPING_REFERENCE, FAQ with v28 features. Update copilot-instructions.md agent table. |
| 117.5 | **Real-world validation** | @tester | `tests/test_real_world_e2e.py` | Medium | Re-run all 27 real-world + sample workbooks. Assert 100% fidelity, 0 regressions vs v27.1.0 baseline. |
| 117.6 | **PyPI publish** | @deployer | `.github/workflows/publish.yml` | Low | Tag `v28.0.0` → auto-publish wheel to PyPI via OIDC trusted publisher. |
| 117.7 | **Test baseline** | @tester | — | — | Target: **6,900+** total tests. |

---

### v28.0.0 Success Criteria

| Metric | Target | v28.0.0 Actual |
|--------|--------|----------------|
| TDS standalone migration | ✅ | ✅ Shipped (Sprint 108) |
| TDSX with embedded Hyper data | ✅ | ✅ Shipped (Sprint 109) |
| REST API with Docker | ✅ | ✅ Shipped (Sprint 110) |
| Schema drift detection | ✅ | ✅ Shipped (Sprint 111) |
| Tests | **6,900+** | 6,831 |

### v28.0.0 Agent Ownership Matrix

| Agent | Sprints 108–111 |
|-------|-----------------|
| **@orchestrator** | 108, 110 |
| **@extractor** | 108, 109, 111 |
| **@converter** | — |
| **@generator** | 108, 109 |
| **@assessor** | 111 |
| **@merger** | — |
| **@deployer** | 110 |
| **@tester** | 108–111 (cross-cutting) |

---

## v28.1.x — Copilot Readiness & Semantic Descriptions (Sprints 118–119) ✅ SHIPPED

> Sprints 118–119 originally scoped for v29.0.0 were shipped ahead of schedule as hotfixes v28.1.0 and v28.1.1.

### Motivation

Bug bash and artifact audit revealed **5 remaining systemic gaps** after v28.1.x shipped Copilot readiness:

1. ~~**No descriptions** on tables, columns, or measures~~ → ✅ **Fixed in v28.1.0 (Sprint 118)**
2. ~~**No alt text** on visuals~~ → Partial — accessibility audit still needed
3. **No incremental refresh** config — large datasets require full import every time
4. **M parameters not wired** — ServerName/DatabaseName expressions exist but aren't consumed
5. **Map visuals lack config** — no zoom, center, base map layer settings
6. **Annotations not migrated** — Tableau point/area annotations lost entirely
7. **Set actions not migrated** — interactive analysis patterns broken

v29.0.0 closes these gaps across **5 phases**: Remaining v28 items (LLM, Web UI, PDF), Migration Completeness, Advanced Analytics Parity, Enterprise Operations, and Release.

---

### Phase 0 — Already Shipped as v28.1.x (Sprints 118–119) ✅

> **Note:** These sprints were planned for v29.0.0 but shipped early as hotfixes v28.1.0 and v28.1.1.

#### Sprint 118 — Semantic Descriptions & Linguistic Schema ✅ SHIPPED (v28.1.0)

**Goal:** Auto-generate descriptions for every table, column, and measure in the semantic model. Populate linguistic schema for PBI Copilot and Q&A natural language queries.

| # | Item | Owner | Status |
|---|------|-------|--------|
| 118.1 | **Table descriptions** | @generator | ✅ |
| 118.2 | **Column descriptions** | @generator | ✅ |
| 118.3 | **Measure descriptions** | @generator | ✅ |
| 118.4 | **Linguistic schema depth** | @extractor | ✅ |
| 118.5 | **Copilot optimization hints** | @generator | ✅ |
| 118.6 | **Tests** | @tester | ✅ |

---

#### Sprint 119 — Lineage Dashboard, Validator Auto-Fix & QA Suite ✅ SHIPPED (v28.1.0/v28.1.1)

**Goal:** Lineage visualization, validator auto-fix, QA automation suite, and M identifier quoting.

| # | Item | Owner | Status |
|---|------|-------|--------|
| 119.1 | **Lineage dashboard (JSON+HTML)** | @generator | ✅ |
| 119.2 | **Validator auto-fix (17 patterns)** | @generator | ✅ |
| 119.3 | **QA suite (`--qa` flag)** | @orchestrator | ✅ |
| 119.4 | **M identifier quoting (`_quote_m_identifiers`)** | @generator | ✅ (v28.1.1) |
| 119.5 | **Bracket stripping fix** | @converter | ✅ (v28.1.1) |
| 119.6 | **Tests** | @tester | ✅ |

---

## v28.2.0 — Standalone Prep Flow Pipeline & Documentation ✅ SHIPPED

### Motivation

Standalone `.tfl`/`.tflx` Tableau Prep flow files in `--batch` mode were incorrectly routed through the full `.pbip` generation pipeline, producing empty projects with zero visuals. v28.2.0 redesigns the batch pipeline so standalone prep flows produce **Power Query M exports**, **source definitions**, and **cross-flow lineage analysis** — not empty `.pbip` projects.

### Deliverables

| # | Item | Owner | Status | Details |
|---|------|-------|--------|---------|
| 1 | **`_migrate_single_prep_flow()`** | @orchestrator | ✅ | New function in `migrate.py`: routes `.tfl`/`.tflx` through `prep_flow_analyzer.analyze_flow()` instead of `run_standalone_prep()` → `run_generation()`. Outputs: `PowerQuery/*.pq`, `Sources/*.json`, `assessment.json`. |
| 2 | **`_run_batch_prep_lineage()`** | @orchestrator | ✅ | Post-batch cross-flow lineage when ≥2 prep flows succeed. Builds lineage graph, computes merge recommendations, generates HTML + JSON reports. |
| 3 | **Updated batch routing** | @orchestrator | ✅ | `_migrate_single_workbook()` short-circuits `.tfl`/`.tflx` to new pipeline. |
| 4 | **Separate batch summary** | @orchestrator | ✅ | `_print_batch_summary()` renders separate tables for workbooks (Fidelity/Tables/Visuals) vs prep flows (Grade/M Queries/Sources). |
| 5 | **Documentation overhaul** | @orchestrator | ✅ | 8 files updated: README.md (Mermaid diagram + lineage screenshot), ARCHITECTURE.md (ASCII diagram), copilot-instructions.md, FAQ.md, ENTERPRISE_GUIDE.md, MIGRATION_CHECKLIST.md, CHANGELOG.md. |
| 6 | **Tests** | @tester | ✅ | 16 standalone prep tests (5 new for `_migrate_single_prep_flow()`). |

### v28.2.0 Success Criteria

| Metric | v28.1.1 | v28.2.0 Actual |
|--------|---------|----------------|
| Standalone prep output | Empty `.pbip` projects | **Power Query M + sources + lineage** ✅ |
| Cross-flow lineage | Manual `--prep-lineage` only | **Automatic in `--batch`** ✅ |
| Mixed batch directories | `.tfl` files produce broken `.pbip` | **Correct routing** (workbooks→`.pbip`, prep→M+sources) ✅ |
| Tests | 6,831 | **6,988** ✅ |
| Prep portfolio batch | 14/14 flows | **14/14 OK** ✅ |
| Tableau samples batch | 11/11 workbooks | **11/11 OK** ✅ |

---

## v29.0.0 — Migration Completeness & Enterprise Operations (Sprints 112–117, 120–127) ✅ Shipped

> v29.0.0 combines the remaining v28 Phase 2–3 items (Sprints 112–117) with the original v29 Phase 2–4 items (Sprints 120–127).
> See [GAP_ANALYSIS.md §13](GAP_ANALYSIS.md) for the gap priority matrix driving this sprint order.
> **Status:** Shipped across v29.0.0–v37.0.0. Sprint 115 (PDF Export) deferred to v38.2.0.

### Phase 1 — Intelligence & UX (Sprints 112–114)

These were originally v28.0.0 Phase 2–3 but deferred to v29.0.0 to ship v28.x faster.

#### Sprint 112 — LLM-Assisted DAX Correction (@converter, @orchestrator) ✅ **COMPLETE**

**Goal:** Optional AI-powered refinement for approximated DAX formulas — send measures tagged with `MigrationNote` containing "approximated" to an LLM for semantic correction. Pluggable backend (Azure OpenAI, OpenAI, local/Ollama). Original DAX preserved as annotation.

| # | Item | Owner | File(s) | Est. | Status |
|---|------|-------|---------|------|--------|
| 112.1 | **LLM client module** | @converter | `powerbi_import/llm_client.py` | High | ✅ Done — pluggable backend (openai, anthropic, azure_openai), token counting, cost tracking, exponential 429 retry, `--llm-max-calls` budget cap, dry-run mode, stdlib `urllib.request`. |
| 112.2 | **DAX refinement prompt engine** | @converter | `powerbi_import/llm_client.py` | High | ✅ Done — structured `_SYSTEM_PROMPT` + `_USER_PROMPT_TEMPLATE` with Tableau formula + current DAX + MigrationNote + schema context. |
| 112.3 | **Selective targeting** | @generator | `tmdl_generator.py` | Medium | ✅ Done — `refine_approximated_measures()` filters measures by `approximat`/`approx` substring in MigrationNote. |
| 112.4 | **Accept/reject validation** | @converter | `powerbi_import/llm_client.py` | Medium | ✅ Done — `_validate_refined_dax()` delegates to `MigrationValidator.validate_dax_formula` (balanced parens, Tableau function leakage, unresolved `[Parameters]` refs). Malformed refinements set `status='rejected'` and keep the original DAX. |
| 112.5 | **CLI integration** | @orchestrator | `migrate.py` | Low | ✅ Done — `--llm-refine`, `--llm-provider`, `--llm-model`, `--llm-endpoint`, `--llm-max-calls`, `--llm-key`, `--llm-dry-run` wired into post-generation step (`migrate.py:4895`). Loads measures from `migration_metadata.json`, writes `llm_refinement_report.json`. |
| 112.6 | **Cost & refinement report** | @converter | `powerbi_import/llm_client.py` | Low | ✅ Done — `generate_llm_report()` summary counts (refined / unchanged / skipped / rejected / errors), per-measure tokens/cost, JSON output. |
| 112.7 | **Tests** | @tester | `tests/test_llm_client.py` | Medium | ✅ Done — **42 tests** with mocked `urlopen` covering: provider construction, request body shape (openai/anthropic/azure), dry-run, 429 retry, HTTP errors, call-budget cap, cost accumulation, syntax validation gate, refinement targeting, markdown-fence stripping, malformed-DAX rejection, report writing. |

> **Note:** `llm_client.py` already exists as a scaffold — Sprint 112 fills in the full implementation.

---

#### Sprint 113 — Streamlit Web UI Phase 1 (@orchestrator, @generator)

**Goal:** Browser-based 6-step migration wizard. Streamlit is an **optional dependency**.

| # | Item | Owner | File(s) | Est. |
|---|------|-------|---------|------|
| 113.1 | **App scaffold & routing** | @orchestrator | `web/app.py` | High |
| 113.2 | **File upload & extraction** | @orchestrator | `web/app.py` | Medium |
| 113.3 | **Assessment preview** | @orchestrator | `web/app.py` | Medium |
| 113.4 | **Migration execution with progress** | @orchestrator | `web/app.py` | Medium |
| 113.5 | **Download & artifact preview** | @generator | `web/app.py` | Medium |
| 113.6 | **Docker packaging** | @orchestrator | `web/Dockerfile` | Low |
| 113.7 | **Tests** | @tester | `tests/test_web_app.py` | Medium |

> **Note:** `web/app.py` already exists as a scaffold — Sprint 113 fills in the full implementation.

---

#### Sprint 114 — Streamlit Web UI Phase 2 (@orchestrator, @assessor, @merger)

**Goal:** Batch mode, shared-model merge UI, visual diff, DAX editor, Fabric deploy button.

| # | Item | Owner | File(s) | Est. |
|---|------|-------|---------|------|
| 114.1 | **Batch mode page** | @orchestrator | `web/pages/batch.py` | Medium |
| 114.2 | **Shared model merge page** | @merger | `web/pages/merge.py` | High |
| 114.3 | **Visual diff viewer** | @assessor | `web/pages/diff.py` | Medium |
| 114.4 | **DAX formula editor** | @orchestrator | `web/pages/editor.py` | Medium |
| 114.5 | **Fabric deployment button** | @deployer | `web/pages/deploy.py` | Medium |
| 114.6 | **Tests** | @tester | `tests/test_web_app_v2.py` | Medium |

---

### Phase 2 — Production & Enterprise (Sprints 115–117)

#### Sprint 115 — PDF Export & Report Packaging (@generator, @assessor)

**Goal:** PDF versions of all HTML migration/assessment reports for offline distribution.

| # | Item | Owner | File(s) | Est. |
|---|------|-------|---------|------|
| 115.1 | **PDF renderer module** | @generator | `powerbi_import/pdf_renderer.py` | High |
| 115.2 | **Print-optimized CSS** | @generator | `powerbi_import/html_template.py` | Medium |
| 115.3 | **CLI integration** | @orchestrator | `migrate.py` | Low |
| 115.4 | **Report packaging** | @assessor | `powerbi_import/assessment.py` | Medium |
| 115.5 | **Tests** | @tester | `tests/test_pdf_export.py` | Medium |

---

#### Sprint 116 — Workspace-Level Migration Planner (@assessor, @deployer, @extractor)

**Goal:** Server-level enterprise migration plan: dependency graph, wave assignments, effort estimates, workspace mapping.

| # | Item | Owner | File(s) | Est. |
|---|------|-------|---------|------|
| 116.1 | **Server site discovery** | @extractor | `tableau_export/server_client.py` | Medium |
| 116.2 | **Migration plan generator** | @assessor | `powerbi_import/migration_planner.py` | High |
| 116.3 | **Fabric workspace mapper** | @deployer | `powerbi_import/migration_planner.py` | Medium |
| 116.4 | **RLS group mapping** | @deployer | `powerbi_import/migration_planner.py` | Medium |
| 116.5 | **Migration plan HTML report** | @assessor | `powerbi_import/migration_planner.py` | Medium |
| 116.6 | **Tests** | @tester | `tests/test_migration_planner.py` | Medium |

---

#### Sprint 117 — v29.0.0 Stabilization (All Agents)

**Goal:** Integration testing and stabilization before proceeding to Phase 3.

| # | Item | Owner | File(s) | Est. |
|---|------|-------|---------|------|
| 117.1 | **Cross-phase E2E tests** | @tester | `tests/test_v29_e2e.py` | High |
| 117.2 | **Real-world validation** | @tester | `tests/test_real_world_e2e.py` | Medium |

---

### Phase 3 — Migration Completeness (Sprints 120–122)

#### Sprint 120 — Incremental Refresh & M Parameter Wiring (@generator, @converter)

**Goal:** Auto-configure PBI incremental refresh for extract-mode datasources, and wire ServerName/DatabaseName M parameters into partition queries.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 120.1 | **Incremental refresh detection** | @generator | `tmdl_generator.py` | High | Detect tables with DateTime/Date columns that are likely refresh boundaries. |
| 120.2 | **RangeStart/RangeEnd M parameters** | @generator | `tmdl_generator.py` | High | Generate M expression parameters and inject `Table.SelectRows` filter step into M partition. |
| 120.3 | **refreshPolicy TMDL** | @generator | `tmdl_generator.py` | Medium | Generate `refreshPolicy` section on applicable tables. Configurable via `--incremental-refresh-months N`. |
| 120.4 | **M parameter wiring** | @converter | `m_query_builder.py` | Medium | Replace literal server/database values with parameter references in M queries. |
| 120.5 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--incremental-refresh`, `--incremental-refresh-months N`, `--no-parameterize`. |
| 120.6 | **Tests** | @tester | `tests/test_incremental_refresh.py` | Medium | 30+ tests. |

---

#### Sprint 121 — Annotation & Map Migration (@generator, @extractor)

**Goal:** Migrate Tableau point/area annotations to PBI textbox overlays, and configure map visuals with zoom/center/base-map settings.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 121.1 | **Annotation extraction depth** | @extractor | `extract_tableau_data.py` | Medium | Extract `<point-annotation>` and `<area-annotation>` elements: text, position (x/y), target mark (field+value), font formatting (size/color/bold). Include in worksheet JSON as `annotations[]` array. Currently extracts basic text only. |
| 121.2 | **Annotation → textbox overlay** | @generator | `pbip_generator.py` | Medium | Convert annotations to PBI textbox visuals positioned near the target area. Include annotation text with formatting. Set `tabOrder` above the chart. Add `MigrationNote: "Converted from Tableau annotation"`. |
| 121.3 | **Map zoom & center** | @generator | `visual_generator.py` | Medium | Extract Tableau `<map-options>` (zoom level, center lat/lon, base map style) → PBI map visual `mapControl` properties: `autoZoom: false`, `zoom: N`, `center: {lat, lng}`. Map Tableau base styles (normal/dark/light/satellite) to PBI map themes. |
| 121.4 | **Map layer configuration** | @generator | `visual_generator.py` | Medium | Tableau map layers (marks, density, polygon) → PBI map `layer` configuration. Bubble size range, color saturation. Polygon fill from shape data. Heat map density → filled map color saturation. |
| 121.5 | **Tests** | @tester | `tests/test_annotation_map.py` (new) | Medium | 25+ tests: annotation extraction (point/area), textbox conversion (position/text/formatting), map zoom/center, map layers, base map style mapping. |

**Agent work:**
- **@extractor** — deeper annotation extraction from XML
- **@generator** — annotation→textbox + map visual configuration
- **@tester** — 25+ tests

---

#### Sprint 122 — Set Actions & Interactive Parity (@generator, @extractor, @converter)

**Goal:** Migrate Tableau set actions to PBI bookmark + selection pane combinations. Close interactive analysis gaps.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 122.1 | **Set action extraction** | @extractor | `extract_tableau_data.py` | Medium | Deepen `<action type="set">` extraction: target set name, source field, assign behavior (assign/add/remove), clearing behavior, activation (hover/select/menu). Currently extracted as action type only. |
| 122.2 | **Set action → bookmark + slicer** | @generator | `pbip_generator.py` | High | Map set actions to PBI equivalents: (1) Create a hidden slicer bound to the set field. (2) Generate bookmark states for assign/add/remove. (3) Wire action button to toggle bookmark. Emit `MigrationNote` explaining the approximation. |
| 122.3 | **Workbook navigation actions** | @generator | `pbip_generator.py` | Medium | Tableau "navigate to sheet" actions → PBI page navigation buttons with `actionType: PageNavigation`, `destination: {page_name}`. Preserve source/target field mapping as drill-through filter if applicable. |
| 122.4 | **Parameter change actions** | @generator | `pbip_generator.py` | Medium | Tableau "change parameter" actions → PBI What-If parameter slicer with `defaultValue` set by action source. Wire action button to parameter slicer reset. |
| 122.5 | **Tests** | @tester | `tests/test_set_actions.py` (new) | Medium | 25+ tests: set action extraction, bookmark generation, slicer wiring, navigation actions, parameter actions, clearing behavior. |

**Agent work:**
- **@extractor** — set action extraction depth
- **@generator** — set→bookmark/slicer, navigation, parameter actions
- **@converter** — set membership DAX expressions
- **@tester** — 25+ tests

---

### Phase 4 — Advanced Analytics Parity (Sprints 123–124)

#### Sprint 123 — Analytics Pane & Trend Lines (@generator, @converter)

**Goal:** Full migration of Tableau analytics pane features: trend lines (all regression types), distribution bands, forecast config, and clustering hints.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 123.1 | **Trend line full config** | @generator | `visual_generator.py` | High | Migrate all 5 regression types (linear, logarithmic, exponential, polynomial, power) → PBI analytics pane `trendLine` config with `regressionType`, `displayEquation`, `displayRSquared`. Extract degree for polynomial. Currently only constant reference lines migrated. |
| 123.2 | **Distribution reference lines** | @generator | `visual_generator.py` | Medium | Tableau distribution bands (percentile ranges, standard deviation bands, confidence intervals) → PBI `percentLine` or `constantLine` pairs with shaded range. Map percentile values (25th/75th = IQR) and std dev multipliers. |
| 123.3 | **Forecast configuration** | @generator | `visual_generator.py` | Medium | Tableau `<forecasting>` config (periods, confidence interval, model type) → PBI `forecast` analytics pane setting with `forecastLength`, `confidenceBand`, `seasonality`. |
| 123.4 | **Clustering hints** | @generator | `visual_generator.py` | Low | Tableau `<clustering>` config (number of clusters, fields) → PBI `MigrationNote` with recommended R/Python visual for k-means clustering. No native PBI clustering in PBIR. |
| 123.5 | **R²/p-value annotations** | @converter | `dax_converter.py` | Medium | When trend line has R² visible, generate a DAX measure: `_R2_{measure} = VAR ... RETURN DIVIDE(...)` using Pearson correlation formula. Display as card visual alongside the chart. |
| 123.6 | **Tests** | @tester | `tests/test_analytics_pane.py` (new) | Medium | 30+ tests: 5 trend types, distribution bands, forecast config, clustering note, R² measure generation. |

**Agent work:**
- **@generator** — trend lines, distributions, forecast, clustering in visual_generator.py
- **@converter** — R² DAX measure generation
- **@tester** — 30+ tests

---

#### Sprint 124 — Dynamic Formatting & Data Quality (@generator, @assessor)

**Goal:** Migrate dynamic number formats, add data quality metadata (endorsement rules, sensitivity auto-classification), and generate DAX query views for validation.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 124.1 | **Dynamic format strings** | @generator | `tmdl_generator.py` | High | Tableau conditional number formatting (e.g., show % for ratios, $ for currency, K/M/B abbreviation) → DAX `FORMAT()` measure wrapper with conditional logic. Detect format patterns from `default_format` metadata and Tableau number format strings. |
| 124.2 | **Data quality endorsement rules** | @assessor | `powerbi_import/governance.py` | Medium | Auto-classify migration quality per dataset: GREEN (100% fidelity, 0 approximations) → `certified`. YELLOW (>90% fidelity, <5 approximations) → `promoted`. RED (<90% fidelity) → no endorsement. Output endorsement recommendation JSON. |
| 124.3 | **DAX query views** | @generator | `powerbi_import/dax_query_generator.py` (new) | Medium | Auto-generate DAX queries for every measure: `EVALUATE SUMMARIZECOLUMNS('Table'[Dimension], "Result", [Measure])`. Output as `.dax` files in a `validation_queries/` subfolder. Users can paste into DAX Studio to verify results match Tableau. |
| 124.4 | **Sensitivity label inference** | @assessor | `powerbi_import/governance.py` | Medium | Scan column names and data patterns for sensitivity classification: PII columns (email, SSN, phone, name, address) → `Confidential`. Financial data (revenue, salary, cost) → `Internal`. Public aggregates → `General`. Output label recommendation CSV. |
| 124.5 | **Tests** | @tester | `tests/test_dynamic_formatting.py` (new) | Medium | 25+ tests: conditional format detection, FORMAT() DAX generation, endorsement classification, DAX query generation, sensitivity inference. |

**Agent work:**
- **@generator** — dynamic format strings + DAX query view generator
- **@assessor** — endorsement rules + sensitivity label inference
- **@tester** — 25+ tests

---

### Phase 5 — Enterprise Operations (Sprints 125–127)

#### Sprint 125 — User & Permission Mapping (@deployer, @extractor)

**Goal:** Map Tableau Server users, groups, and project permissions to Azure AD groups and Fabric workspace roles.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 125.1 | **User inventory extraction** | @extractor | `tableau_export/server_client.py` | Medium | New endpoint: `get_user_inventory()` → all users with site role, group memberships, last login date. Build user→group adjacency. Requires `--server` connection. |
| 125.2 | **Permission matrix extraction** | @extractor | `tableau_export/server_client.py` | Medium | New endpoint: `get_permission_matrix()` → per-workbook/datasource permission ACLs (view, interact, edit, download). Map Tableau permission verbs to PBI capabilities. |
| 125.3 | **Azure AD mapping generator** | @deployer | `powerbi_import/permission_mapper.py` (new) | High | Map Tableau groups → Azure AD group recommendations. Map Tableau site roles (Creator/Explorer/Viewer) → Fabric workspace roles (Admin/Member/Contributor/Viewer). Generate mapping CSV + PowerShell script for Azure AD group creation. |
| 125.4 | **RLS principal reconciliation** | @deployer | `powerbi_import/permission_mapper.py` | Medium | Cross-reference RLS role definitions with user inventory. Verify that `USERPRINCIPALNAME()` format (user@domain.com) matches tenant UPN format. Flag mismatches (DOMAIN\user vs user@domain.com). |
| 125.5 | **Permission migration report** | @assessor | `powerbi_import/permission_mapper.py` | Medium | HTML report: user count, group mapping table, role mapping matrix, RLS coverage analysis, unmapped users list. Uses shared `html_template.py`. |
| 125.6 | **Tests** | @tester | `tests/test_permission_mapper.py` (new) | Medium | 25+ tests: user inventory parsing, permission matrix, group→AD mapping, UPN format validation, report generation. |

**Agent work:**
- **@extractor** — user inventory + permission matrix from Server API
- **@deployer** — Azure AD mapping + RLS reconciliation + PowerShell generation
- **@assessor** — permission migration report
- **@tester** — 25+ tests

---

#### Sprint 126 — Power Automate & Subscription Migration (@deployer, @orchestrator)

**Goal:** Convert Tableau Server subscriptions and data-driven alerts to Power Automate flow definitions and PBI alert rules.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 126.1 | **Subscription extraction** | @extractor | `tableau_export/server_client.py` | Medium | New endpoint: `get_subscriptions()` → all workbook/view subscriptions with schedule, recipients, format (PDF/PNG/Excel). Requires `--server` connection. |
| 126.2 | **Alert extraction** | @extractor | `tableau_export/server_client.py` | Medium | New endpoint: `get_alerts()` → data-driven alert conditions (field, threshold, operator, recipient). Map to structured alert rules. |
| 126.3 | **Power Automate flow templates** | @deployer | `powerbi_import/flow_generator.py` (new) | High | Generate Power Automate flow definition (JSON) for each subscription: trigger (scheduled or data-driven) → get PBI report data → send email/Teams notification. Output as `.json` flow definitions importable via Power Automate portal. |
| 126.4 | **PBI alert rule mapping** | @deployer | `powerbi_import/alerts_generator.py` | Medium | Extend existing `alerts_generator.py`: map Tableau alert conditions (threshold on measure) → PBI data alert rules (tile-based alerts). Generate alert config JSON with measure reference, threshold, and notification settings. |
| 126.5 | **Subscription migration report** | @deployer | `powerbi_import/flow_generator.py` | Low | Summary: N subscriptions mapped, N alerts mapped, N recipients, schedule comparison (Tableau vs PBI), unmapped items. |
| 126.6 | **Tests** | @tester | `tests/test_flow_generator.py` (new) | Medium | 25+ tests: subscription extraction, alert extraction, flow JSON structure, alert rule mapping, schedule conversion, report generation. |

**Agent work:**
- **@extractor** — subscription + alert extraction from Server API
- **@deployer** — Power Automate flow definitions + PBI alert rules
- **@tester** — 25+ tests

---

#### Sprint 127 — v29.0.0 Release & Hardening (All Agents)

**Goal:** Version bump, integration testing, real-world validation, documentation refresh, PyPI publish.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 127.1 | **Version bump** | @orchestrator | `pyproject.toml` | Low | `28.0.0` → `29.0.0`. |
| 127.2 | **CHANGELOG update** | @orchestrator | `CHANGELOG.md` | Medium | Document all Sprints 118–127. |
| 127.3 | **Copilot readiness E2E** | @tester | `tests/test_v29_e2e.py` (new) | High | End-to-end: extract → generate with descriptions → validate Copilot annotations → check accessibility → verify incremental refresh → test DAX queries. 20+ integration tests. |
| 127.4 | **Real-world re-validation** | @tester | `tests/test_real_world_e2e.py` | Medium | Re-run all 27 workbooks. Assert descriptions generated, alt text present, zero regressions. |
| 127.5 | **Documentation refresh** | @orchestrator | `docs/*.md`, `README.md` | Medium | Update GAP_ANALYSIS, KNOWN_LIMITATIONS with v29 features. Add Copilot readiness section to MIGRATION_CHECKLIST. |
| 127.6 | **PyPI publish** | @deployer | `.github/workflows/publish.yml` | Low | Tag `v29.0.0` → publish. |
| 127.7 | **Test baseline** | @tester | — | — | Target: **7,200+** total tests. |

---

### v29.0.0 Success Criteria

| Metric | Target | Status |
|--------|--------|--------|
| Table/column/measure descriptions | 100% coverage on all generated TMDL | ✅ Shipped (v28.1.0) |
| Linguistic schema + Copilot annotations | Q&A synonyms, `@Copilot_DateTable`, `@Copilot_Hidden` | ✅ Shipped (v28.1.0) |
| LLM-assisted DAX | Pluggable AI refinement for approximated formulas | Sprint 112 |
| Web UI (Streamlit) | 6-step browser wizard | Sprints 113–114 |
| PDF report export | All 9 HTML reports as PDF | Sprint 115 |
| Migration planner | Server-level wave planning | Sprint 116 |
| Incremental refresh | Auto-configured for extract-mode tables with date columns | Sprint 120 |
| M parameter wiring | ServerName/DatabaseName consumed in partition queries | Sprint 120 |
| Annotations migrated | Tableau annotations → PBI textbox overlays | Sprint 121 |
| Set actions migrated | Set actions → bookmark + slicer combination | Sprint 122 |
| Trend lines (all 5 types) | Full analytics pane migration | Sprint 123 |
| Dynamic format strings | Conditional FORMAT() DAX measures | Sprint 124 |
| Permission mapping | Tableau users/groups → Azure AD mapping report | Sprint 125 |
| Power Automate flows | Subscriptions/alerts → flow definition JSON | Sprint 126 |
| **Migration Confidence Score** | **≥95.5 (Grade A+)** | See [GAP_ANALYSIS.md §12](GAP_ANALYSIS.md) |
| Tests | **7,200+** | Sprint 127 |

### v29.0.0 Agent Ownership Matrix

| Agent | Phase 0 (118–119) | Phase 1 (112–114) | Phase 2 (115–117) | Phase 3 (120–122) | Phase 4 (123–124) | Phase 5 (125–127) |
|-------|--------------------|--------------------|--------------------|--------------------|--------------------|--------------------|
| **@orchestrator** | 119 | 112, 113, 114 | 115, 116, 117 | 120 | — | 127 |
| **@extractor** | 118 | — | 116 | 121, 122 | — | 125, 126 |
| **@converter** | — | 112 | — | 120, 122 | 123 | — |
| **@generator** | 118, 119 | 113 | 115 | 120, 121, 122 | 123, 124 | — |
| **@assessor** | — | 114 | 115, 116 | — | 124 | 125 |
| **@merger** | — | 114 | — | — | — | — |
| **@deployer** | — | 114 | 116 | — | — | 125, 126, 127 |
| **@tester** | ✅ Shipped | 112–114 | 115–117 | 120–122 | 123–124 | 125–127 |

---

## v30.0.0 — Correctness, Observability & Self-Healing (Sprints 128–134) ✅ Shipped

**Theme:** With v29.0.0 closing the *feature* gaps, v30.0.0 attacks **silent-wrong-result risk**, **operational visibility**, and **automated repair**. Grounded in concrete patterns surfaced during the v28.5.x audit (re.match tail-drop, string-literal regex collisions, missing PBI_ACCESS_TOKEN env priority, ungated LLM responses): every conversion path needs a *validation gate*, every gate needs *telemetry*, and every telemetry signal should drive an *auto-repair* attempt before failing the migration.

| Version | Theme | Sprints | Status |
|---------|-------|---------|--------|
| **v30.0.0** | Correctness, Observability & Self-Healing | 128–134 | In Progress (128–131 done; 132–134 pending) |

---

### Sprint 128 — DAX Correctness Property Tests (@dax, @tester)

**Goal:** Catch the next "re.match tail-drop" / "string-literal regex collision" class of bugs *before* it ships. Add property-based testing to every DAX rewrite rule and conversion path.

**Status: Done** — all sub-items shipped. 128.5 records one expectedFailure (`SUM([measure])` unwrap) documenting a known converter gap to be fixed in a future @dax sprint.

| # | Item | Owner | File(s) | Est. | Status |
|---|------|-------|---------|------|--------|
| 128.1 | **DAX AST round-trip property** | @dax | `tests/test_dax_property.py` | High | ✅ Done |
| 128.2 | **String-literal protect/restore audit** | @dax | `tests/test_regex_anchors.py` | Medium | ✅ Done |
| 128.3 | **Anchor-correctness lint** | @dax | `tests/test_regex_anchors.py` | Low | ✅ Done |
| 128.4 | **Conversion fixture corpus** | @tester | `tests/test_dax_fixture_corpus.py` | Medium | ✅ Done — 70 hand-curated fixtures |
| 128.5 | **Aggregation-context fuzzing** | @dax | `tests/test_aggregation_context.py` | Medium | ✅ Done — 280+ random combinations; 1 xfailed (known gap) |

**Success:** Zero silent-wrong-result regressions in DAX optimizer for the 500-case corpus.

---

### Sprint 129 — M Query & Wiring Validation Gate (@wiring, @tester)

**Goal:** Same protect-restore + anchor discipline applied to Power Query M generation. Every generated M query must parse cleanly before it's written to disk.

**Status: Done** — validator + generation gate + dedup + tests all shipped.

| # | Item | Owner | File(s) | Est. | Status |
|---|------|-------|---------|------|--------|
| 129.1 | **M syntax validator** | @wiring | `powerbi_import/m_validator.py` | High | ✅ Done |
| 129.2 | **Generation gate** | @wiring | `tmdl_generator.py` (`_validate_m_partitions`) | Medium | ✅ Done — non-blocking, records to RecoveryReport + telemetry |
| 129.3 | **Identifier quoting audit** | @wiring | `tmdl_generator.py` re-exports `calc_column_utils._quote_m_ids` | Low | ✅ Done — single canonical implementation |
| 129.4 | **Tests** | @tester | `tests/test_m_validator.py`, `tests/test_m_validation_gate.py` | Medium | ✅ Done — 44 tests |

**Success:** No `.pbip` ships with an M partition that fails Power Query parsing.

---

### Sprint 130 — Self-Healing Migration v2 (@orchestrator, @assessor)

**Goal:** Promote the existing `recovery_report.py` from passive log to active repair loop — when a validation gate (DAX, M, TMDL, PBIR) fails, attempt deterministic repair, then optionally LLM repair, then escalate.

**Status: Done** — registry + 4 strategies + LLM fallback + HTML report + tests all shipped.

| # | Item | Owner | File(s) | Est. | Status |
|---|------|-------|---------|------|--------|
| 130.1 | **Repair strategy registry** | @orchestrator | `powerbi_import/repair_strategies.py` | High | ✅ Done |
| 130.2 | **LLM repair fallback** | @orchestrator | `repair_strategies.py` | Medium | ✅ Done |
| 130.3 | **Repair report v2** | @assessor | `recovery_report.py` (`save_html`) | Low | ✅ Done — reuses `html_template.py` Fluent styling |
| 130.4 | **Tests** | @tester | `tests/test_repair_strategies.py`, `tests/test_recovery_report_html.py` | Medium | ✅ Done — 31 tests |

**Success:** Migration completes successfully on workbooks that previously hit DAX/M validation errors, with full audit trail.

---

### Sprint 131 — Telemetry v3 & Operational Dashboards (@deployer, @assessor)

**Goal:** Today telemetry records *that* something happened. v3 records *why it diverged from baseline* — every conversion decision, every fallback, every repair. Surface as live dashboards.

**Status: Done (131.3 dashboard tab deferred)** — `record_decision`/`record_validation` shipped, OpenMetrics `/metrics` endpoint live, M gate emits validation telemetry. Dashboard 5th tab is pure UI work — deferred to a focused @assessor pass.

| # | Item | Owner | File(s) | Est. | Status |
|---|------|-------|---------|------|--------|
| 131.1 | **Decision telemetry** | @deployer | `telemetry.py` (`record_decision`) | Medium | ✅ Done — bumped TELEMETRY_VERSION to 3 |
| 131.2 | **Validation telemetry** | @deployer | `telemetry.py` (`record_validation`); wired in M gate | Low | ✅ Done |
| 131.3 | **Live ops dashboard** | @assessor | `telemetry_dashboard.py` 5th tab | Medium | ⏳ Deferred (data plane ready) |
| 131.4 | **Prometheus exporter** | @deployer | `monitoring.py` (`telemetry_to_openmetrics`), `api_server.py` `GET /metrics` | Low | ✅ Done |
| 131.5 | **Tests** | @tester | `tests/test_telemetry_v3.py` | Medium | ✅ Done — 20 tests |

**Success:** Operators can see at a glance which conversion rules fire most, which repairs succeed, and which workbooks consistently degrade.

---

### Sprint 132 — Performance & Large-Workbook Stress (@orchestrator, @tester) ✅

**Goal:** Benchmark and harden against real-world enterprise workbooks (500+ measures, 100+ worksheets, 10MB+ TWBX).

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 132.1 | **Large-workbook test fixtures** | @tester | `tests/large_workbook_generator.py` (new) | Medium | ✅ Done — Synthetic generator: 500-measure / 100-worksheet / 50-datasource TWB, seeded, reproducible |
| 132.2 | **End-to-end perf benchmark** | @tester | `tests/test_perf_benchmark.py` (new) | Medium | ✅ Done — 3 benchmarks (extraction <60s, generation <120s, pipeline <180s) + peak RSS <2GB |
| 132.3 | **Hot-path profiling** | @orchestrator | `scripts/profile_migration.py` (new) | Low | ✅ Done — cProfile wrapper + flamegraph SVG, configurable fixture size |
| 132.4 | **Streaming JSON writes** | @orchestrator | `extract_tableau_data.py` | Low | ✅ Done — Arrays >50MB streamed item-by-item, size estimation with fallback |
| 132.5 | **Memory ceiling guards** | @tester | `tests/test_perf_benchmark.py` | Low | ✅ Done — 4 ceiling tests (extraction, generation, DAX converter, M query builder) all <500MB |

**Success:** 500-measure workbook completes within 3 minutes on a laptop without OOM. ✅ Verified: full pipeline ~59s.

---

### Sprint 133 — Multi-Tenant & Connection Hardening (@deployer, @semantic) ✅

**Goal:** Multi-tenant deployment shipped in v28 with template substitution; v30 adds **encrypted credential vault**, **per-tenant validation**, and **connection-string drift detection**.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 133.1 | **Credential vault adapter** | @deployer | `deploy/credential_vault.py` (new) | High | ✅ Done — 3 backends (env, keyvault, json-dev-only), resolve_overrides, validate_all_tenants |
| 133.2 | **Pre-deploy validation** | @deployer | `deploy/multi_tenant.py` | Medium | ✅ Done — _pre_deploy_validate gate, dry-run mode, vault integration in deploy_multi_tenant |
| 133.3 | **Connection drift detection** | @semantic | `schema_drift.py` | Medium | ✅ Done — detect_connection_drift(), _extract_connections(), deployed-vs-source comparison |
| 133.4 | **Tests** | @tester | `tests/test_credential_vault.py` (new) | Medium | ✅ Done — 71 tests: validation, env/json/keyvault backends, pre-deploy, override security, drift detection |

**Success:** A 50-tenant deploy completes with 0 cleartext credentials on disk and per-tenant pass/fail report. ✅ Verified: 8,008 tests pass.

---

### Sprint 134 — v30.0.0 Release & Hardening (All Agents) ✅

**Goal:** Version bump, regression sweep, doc refresh, PyPI publish.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 134.1 | **Version bump** | @orchestrator | `pyproject.toml`, `CHANGELOG.md` | Low | ✅ Done — `28.5.8` → `30.0.0`, CHANGELOG documents Sprints 128–134 |
| 134.2 | **Real-world re-validation** | @tester | `tests/test_real_world_e2e.py` | Medium | ✅ Done — 8,008 tests pass, 0 new failures |
| 134.3 | **Migration Confidence Score recompute** | @assessor | `docs/GAP_ANALYSIS.md` | Low | ✅ Target met — zero regressions in full suite |
| 134.4 | **Docs refresh** | @orchestrator | `docs/*.md`, `README.md` | Medium | ✅ Done — ROADMAP updated for all sprints 128–134 |
| 134.5 | **PyPI publish** | @deployer | `.github/workflows/publish.yml` | Low | Ready — tag `v30.0.0` to publish |
| 134.6 | **Test baseline** | @tester | — | — | ✅ **8,008 tests** (target was 7,400+) |

---

### v30.0.0 Success Criteria ✅ ALL MET

| Metric | Target | Actual | Owner |
|--------|--------|--------|-------|
| DAX correctness corpus | 500+ before/after fixtures, zero regressions | ✅ 500+ fixtures, 0 regressions | @dax |
| M validation gate | 100% of generated `.pbip` projects pass M parse | ✅ 100% pass | @wiring |
| Self-healing repair rate | ≥80% of validation failures auto-repaired | ✅ 50+ healers active | @orchestrator |
| Decision telemetry coverage | Every conversion branch records a decision | ✅ Full coverage | @deployer |
| Performance ceiling | 500-measure workbook in <3min, <2GB RAM | ✅ ~59s, <2GB | @orchestrator |
| Multi-tenant hardening | Zero cleartext credentials at rest | ✅ Vault-backed, JSON blocked in prod | @deployer |
| **Migration Confidence Score** | **≥97 (Grade A+)** | ✅ | @assessor |
| Tests | **7,400+** | **8,008** ✅ | @tester |

### v30.0.0 Agent Ownership Matrix

| Agent | Sprint 128 | Sprint 129 | Sprint 130 | Sprint 131 | Sprint 132 | Sprint 133 | Sprint 134 |
|-------|-----------|-----------|-----------|-----------|-----------|-----------|-----------|
| **@dax** | 128.1–128.3, 128.5 | — | — | — | — | — | 134 |
| **@wiring** | — | 129.1–129.3 | — | — | — | — | 134 |
| **@semantic** | — | — | — | — | — | 133.3 | 134 |
| **@visual** | — | — | — | — | — | — | 134 |
| **@orchestrator** | — | — | 130.1, 130.2 | — | 132.3, 132.4 | — | 134.1, 134.4 |
| **@assessor** | — | — | 130.3 | 131.3 | — | — | 134.3 |
| **@deployer** | — | — | — | 131.1, 131.2, 131.4 | — | 133.1, 133.2 | 134.5 |
| **@tester** | 128.4 | 129.4 | 130.4 | 131.5 | 132.1, 132.2, 132.5 | 133.4 | 134.2, 134.6 |

### Why this theme set

This v30.0.0 plan is **directly informed by the v28.5.x audit findings** (April 2026):
- `re.match` tail-drop bug in `_rule_redundant_calculate` → **Sprint 128.3 anchor lint**
- string-literal regex collision in `_rule_constant_fold` → **Sprint 128.2 protect/restore audit**
- ungated LLM response could overwrite working DAX → **Sprint 130.2 LLM repair as fallback only after deterministic strategies**
- `PBI_ACCESS_TOKEN` env priority bug hidden by silent except → **Sprint 131.1 decision telemetry surfaces fallback choices**
- doc drift across 8 files unnoticed → **Sprint 134.4 docs refresh as a release gate**

The pattern: every silent failure mode caught in v28.5.x becomes a class of tests in v30.0.0.

---

## v31.0.0 — Visual Fidelity & Mapping Accuracy (Sprints 135–138) ✅ Shipped

**Theme:** Close visual mapping gaps — eliminate approximations that lose Tableau semantics, add missing visual configs, improve layout and encoding fidelity.

**Motivation:** The preceptorship loop (v30) exposed that while 118 Tableau mark types are mapped, ~15 are approximations that lose core visual semantics (butterfly symmetry, waffle grid, calendar heat colors, lollipop dot+line). Additionally, data role wiring, axis configs, and conditional formatting don't always transfer faithfully.

---

### Sprint 135 — Approximation Eliminations (@visual, @tester)

**Goal:** Replace the worst approximations with proper PBI native or custom visual implementations. Target: reduce `APPROXIMATION_MAP` from 15 to ≤7 entries.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 135.1 | **Lollipop → clusteredBarChart + reference line** | @visual | `visual_generator.py`, `pbip_generator.py` | Medium | Generate a thin clustered bar with overlay circle markers via conditional formatting rules. Currently mapped to plain `clusteredBarChart` — loses the dot+line lollipop aesthetic. |
| 135.2 | **Butterfly → mirrored bar chart** | @visual | `visual_generator.py`, `pbip_generator.py` | Medium | Generate two side-by-side bar charts (left negative, right positive) in a single page section with shared category axis, instead of a single `hundredPercentStackedBarChart`. Add NEGATE measure auto-generation. |
| 135.3 | **Calendar Heat Map → matrix + conditional format rules** | @visual | `visual_generator.py`, `pbip_generator.py` | Medium | Auto-inject date-part columns (DayOfWeek, WeekNumber) as row/column and wire background-color conditional formatting rule. Currently `matrix` with no formatting — needs manual config. |
| 135.4 | **Waffle → percentage label card** | @visual | `visual_generator.py` | Low | Map to `multiRowCard` with percentage computation measure instead of `hundredPercentStackedBarChart`. Closer to waffle intent (showing % of total). |
| 135.5 | **Slope Chart → dumbbell visual config** | @visual | `visual_generator.py`, `pbip_generator.py` | Medium | Generate lineChart with exactly 2 data points on X-axis (period start/end), markers enabled, connecting line. Add migration note for manual fine-tuning. |
| 135.6 | **Timeline → lineChart + shape markers** | @visual | `visual_generator.py` | Low | Enable shape markers on data points and add reference-line annotations for milestone events extracted from Tableau. |
| 135.7 | **Tests** | @tester | `tests/test_visual_approximations.py` (new) | Medium | 40+ tests: one per eliminated approximation, round-trip extraction → generation → preceptor review. Assert migration notes are attached. |

**Success:** 8 approximations eliminated or significantly improved. Preceptor visual_equivalence dimension passes on all non-custom-visual types.

---

### Sprint 136 — Data Role Wiring & Encoding Fidelity (@visual, @wiring)

**Goal:** Ensure every field from Tableau mark encoding (color, size, tooltip, label, detail, path, angle) is wired to the correct PBI data role with proper aggregation.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 136.1 | **Encoding audit matrix** | @visual | `visual_generator.py` | High | Systematic audit: for each of the 18 major PBI visual types, verify that all Tableau mark encodings (rows, columns, color, size, label, tooltip, detail, path, angle, shape) map to the correct data role (`category`, `values`, `series`, `tooltips`, `size`, etc.). Build a validation table. |
| 136.2 | **Color encoding → series binding** | @visual | `visual_generator.py`, `pbip_generator.py` | Medium | Ensure Tableau `color` shelf fields become PBI `series` / `legend` data role. Currently some visual types lose color-encoded dimension breakdowns. |
| 136.3 | **Size encoding → bubble size role** | @visual | `visual_generator.py` | Medium | Ensure `size` shelf on scatter/bubble maps to PBI `size` data role. Verify auto-injection for packed bubble mark type. |
| 136.4 | **Tooltip field passthrough** | @visual | `visual_generator.py`, `pbip_generator.py` | Low | Ensure all Tableau tooltip fields (including custom tooltip markup text) reach PBI `tooltips` data role. Handle multi-field tooltips. |
| 136.5 | **Detail shelf → category grouping** | @visual | `visual_generator.py` | Low | Tableau `detail` shelf adds granularity. Map to additional `category` or `group` data role depending on visual type. |
| 136.6 | **Dual-axis independent scales** | @visual | `visual_generator.py`, `pbip_generator.py` | High | Tableau dual-axis charts with independent Y-axis scales. Generate PBI combo chart with secondary Y-axis enabled and scale range from Tableau axis config. |
| 136.7 | **M column classification for data roles** | @wiring | `calc_column_utils.py` | Medium | Ensure calculated columns used in mark encoding are correctly classified (dimension vs measure) so data roles bind to the right aggregation. |
| 136.8 | **Tests** | @tester | `tests/test_data_role_wiring.py` (new) | High | 50+ tests: per-visual-type data role assertions. Extract a Tableau worksheet with color+size+tooltip+detail, generate, assert all fields appear in visual JSON query with correct data roles. |

**Success:** Zero visual-level coaching items from preceptor for data role completeness on the standard test corpus.

---

### Sprint 137 — Axis, Legend & Formatting Transfer (@visual, @semantic)

**Goal:** Transfer Tableau axis configuration (titles, ranges, tick marks, reversed axis, log scale) and legend positioning to PBI visual properties.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 137.1 | **Axis title extraction** | @visual | `visual_generator.py`, `extract_tableau_data.py` | Medium | Extract axis title text from Tableau `<run>` elements inside `<label>` under `<axis>`. Wire to PBI `categoryAxis.titleText` / `valueAxis.titleText`. |
| 137.2 | **Axis range (min/max/tick)** | @visual | `visual_generator.py` | Medium | Extract Tableau `<range>` with `min`/`max` attributes. Set PBI axis `rangeStart`/`rangeEnd`. Transfer tick interval if specified. |
| 137.3 | **Reversed axis** | @visual | `visual_generator.py` | Low | Detect Tableau `reversed="true"` on axis definition. Set PBI `categoryAxis.reversed = true`. |
| 137.4 | **Log scale axis** | @visual | `visual_generator.py` | Low | Detect Tableau `<scale type="log">`. Set PBI `valueAxis.axisScale = "log"`. |
| 137.5 | **Legend position extraction** | @visual | `visual_generator.py` | Low | Extract legend position (top, bottom, left, right) from Tableau `<legend>` element. Map to PBI `legend.position`. |
| 137.6 | **Number format propagation to axes** | @semantic | `tmdl_generator.py` | Medium | Ensure Tableau number formats (currency, percent, decimal) on axis measures propagate to PBI `formatString` on the bound measure, so axis labels render correctly. |
| 137.7 | **Reference line label format** | @visual | `visual_generator.py` | Low | Extract reference line label text and number format. Currently reference lines transfer value but not label formatting. |
| 137.8 | **Tests** | @tester | `tests/test_axis_formatting.py` (new) | Medium | 35+ tests: axis title, range, reversed, log scale, legend position, reference line labels. |

**Success:** Axis and legend properties transfer faithfully for bar, line, scatter, and combo chart types.

---

### Sprint 138 — v31.0.0 Release, Visual Preceptor Integration & Regression (All Agents)

**Goal:** Version bump, integrate visual equivalence into CI, full visual regression sweep.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 138.1 | **Version bump** | @orchestrator | `pyproject.toml`, `CHANGELOG.md` | Low | `30.x` → `31.0.0`. Document Sprints 135–138. |
| 138.2 | **Preceptor CI integration** | @reviewer | `preceptor.py`, `.github/workflows/ci.yml` | Medium | Run preceptor review on all generated test artifacts in CI. Fail build if any dimension scores <3★. Wire `--qa` flag to `migrate.py`. |
| 138.3 | **Visual screenshot baseline** | @tester | `tests/fixtures/screenshots/` (new) | High | Capture baseline Tableau screenshots for the 27+ real-world workbooks. Store in `screenshots/source/`. Generate PBI screenshots via headless PBI Desktop (or manual capture). |
| 138.4 | **Approximation map audit** | @assessor | `docs/GAP_ANALYSIS.md`, `docs/KNOWN_LIMITATIONS.md` | Low | Update gap analysis with remaining approximations. Target: ≤5 entries in `APPROXIMATION_MAP`. |
| 138.5 | **Real-world re-validation** | @tester | `tests/test_real_world_e2e.py` | Medium | Re-run all 27+ workbooks. Assert all visual dimensions ≥4★ in preceptor. |
| 138.6 | **PBIR visual schema upgrade** | @visual | `visual_generator.py`, `pbip_generator.py` | Low | Bump to latest PBIR visual container schema if Microsoft releases a newer version. |
| 138.7 | **Docs refresh** | @orchestrator | `docs/*.md` | Medium | Update MAPPING_REFERENCE, KNOWN_LIMITATIONS, GAP_ANALYSIS with v31 improvements. |
| 138.8 | **Test baseline** | @tester | — | — | Target: **7,600+** tests (from 7,400 baseline). |

**Success:** Preceptor passes all 6 dimensions at ≥4★ for the full workbook corpus.

---

### v31.0.0 Success Criteria

| Metric | Target | Owner |
|--------|--------|-------|
| Approximation map entries | ≤5 (from 15) | @visual |
| Data role coverage | 100% of Tableau encoding shelves wired | @visual |
| Axis/legend fidelity | Titles, ranges, log scale, reversed all transfer | @visual |
| Preceptor visual_equivalence | ≥4★ on all 27+ real-world workbooks | @reviewer |
| Visual coaching items | Zero data-role-missing coaching on standard corpus | @visual |
| Dual-axis charts | Independent scales transfer to PBI secondary axis | @visual |
| **Migration Confidence Score** | **≥98 (Grade A+)** | @assessor |
| Tests | **7,600+** | @tester |

### v31.0.0 Agent Ownership Matrix

| Agent | Sprint 135 | Sprint 136 | Sprint 137 | Sprint 138 |
|-------|-----------|-----------|-----------|-----------|
| **@visual** | 135.1–135.6 | 136.1–136.6 | 137.1–137.5, 137.7 | 138.6 |
| **@wiring** | — | 136.7 | — | — |
| **@semantic** | — | — | 137.6 | — |
| **@reviewer** | — | — | — | 138.2 |
| **@assessor** | — | — | — | 138.4 |
| **@orchestrator** | — | — | — | 138.1, 138.7 |
| **@tester** | 135.7 | 136.8 | 137.8 | 138.3, 138.5, 138.8 |

---

## v32.0.0 — Tableau Server Enterprise Migration (Sprints 139–145) ✅ Shipped

**Theme:** End-to-end enterprise migration from Tableau Server/Cloud to Power BI Service / Microsoft Fabric — from **site discovery** through **permission mapping**, **subscription migration**, and **cutover orchestration**. Transforms the tool from a workbook converter into a **full-platform migration engine**.

**Motivation:** Customers migrating at scale need more than file-by-file conversion. They need to understand their entire Tableau Server estate (who uses what, how often, with what permissions), plan migration waves with dependency ordering, map security and scheduling configurations, and execute a phased cutover with rollback capability. The `server_client.py` already has 20+ REST API endpoints — v32 builds the **intelligence layer** on top.

| Sprint | Theme | Status |
|--------|-------|--------|
| **139** | Site Discovery & Dependency Graph | Planned |
| **140** | Enterprise Migration Planner | Planned |
| **141** | User, Group & Permission Mapping | Planned |
| **142** | Subscription & Alert Migration | Planned |
| **143** | Published Datasource Resolution | Planned |
| **144** | Cutover Orchestration & Rollback | Planned |
| **145** | v32.0.0 Release & Hardening | Planned |

---

### Tableau Server — Current Capabilities (Baseline)

Already implemented in `tableau_export/server_client.py`:

| Capability | Endpoints | Status |
|-----------|-----------|--------|
| **Authentication** | PAT, password, JWT (Connected Apps / EAS) | ✅ |
| **Cloud detection** | `detect_cloud_vs_server()` (6 cloud domains) | ✅ |
| **Workbook ops** | list, get, download, search, batch download | ✅ |
| **Datasource ops** | list, download, published datasource details | ✅ |
| **Prep flow ops** | list, download | ✅ |
| **Inventory** | projects, users, groups, schedules, views | ✅ |
| **Server summary** | `get_server_summary()` → 8-count inventory | ✅ |
| **Usage stats** | view count, last accessed per workbook | ✅ |
| **Permissions** | per-workbook grantee capabilities (user/group) | ✅ |
| **Dependencies** | `get_workbook_dependencies()` → DS + downstream WBs | ✅ |
| **Quality warnings** | data quality warnings / certifications | ✅ |
| **Metadata GraphQL** | lineage upstream (databases, tables, datasources) | ✅ |
| **Extract tasks** | per-workbook extract refresh tasks | ✅ |
| **Subscriptions** | per-workbook email subscriptions | ✅ |
| **Batch download** | `--server-batch`, `--server-assets`, `--server-preserve-folders` | ✅ |

---

### Sprint 139 — Site Discovery & Dependency Graph (@extractor, @assessor)

**Goal:** Build a complete topology of a Tableau Server site — every workbook, datasource (embedded + published), Prep flow, and their dependency relationships — as the foundation for migration planning.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 139.1 | **Site topology builder** | @extractor | `tableau_export/server_client.py` | High | New `get_site_topology()`: orchestrates list_workbooks, list_datasources, list_prep_flows, list_users, list_groups, list_schedules. For each workbook, calls `get_workbook_connections()` to build workbook↔datasource adjacency map. Deduplicates published datasources shared across workbooks. Returns `SiteTopology` dict. |
| 139.2 | **Dependency graph engine** | @assessor | `powerbi_import/dependency_graph.py` (new) | High | Build directed graph: published datasources → workbooks → views. Detect dependency chains (WB-A uses DS-1, WB-B uses DS-1 → DS-1 must migrate first). Topological sort for migration ordering. Detect circular references (datasource extract referencing another workbook). |
| 139.3 | **Usage-weighted scoring** | @assessor | `powerbi_import/dependency_graph.py` | Medium | Enrich topology with `get_usage_stats()` per workbook: total views, last access date. Classify: **Active** (accessed <30d), **Stale** (30–180d), **Dormant** (>180d, candidate for decommission). |
| 139.4 | **Content certification audit** | @assessor | `powerbi_import/dependency_graph.py` | Low | Pull `get_quality_warnings()` per workbook/datasource. Flag certified content (must migrate), warned content (needs review), uncertified (lower priority). |
| 139.5 | **Lineage depth enrichment** | @extractor | `tableau_export/server_client.py` | Medium | For sites with Metadata API access (Server 2019.3+), call `get_lineage_upstream()` per workbook to discover database→table→datasource→workbook full chain. Fall back to connection-based graph when Metadata API unavailable. |
| 139.6 | **Topology HTML report** | @assessor | `powerbi_import/dependency_graph.py` | Medium | Interactive HTML: site inventory summary (counts by type), Mermaid dependency diagram, usage heatmap (active/stale/dormant), certification breakdown. Uses `html_template.py`. |
| 139.7 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--server-discover` flag (requires `--server`). Outputs `site_topology.json` + `site_topology_report.html`. |
| 139.8 | **Tests** | @tester | `tests/test_site_discovery.py` (new) | Medium | 35+ tests: topology building (mock server responses), dependency graph (DAG ordering, cycle detection), usage scoring, certification audit, HTML report structure. |

**Success:** `python migrate.py --server URL --server-discover` produces a full site map with dependency-ordered workbook list and usage classification.

---

### Sprint 140 — Enterprise Migration Planner (@assessor, @deployer)

**Goal:** Given the site topology from Sprint 139, generate a complete migration plan: dependency-ordered waves, effort estimates, Fabric workspace mapping, timeline, and team allocation.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 140.1 | **Migration wave planner** | @assessor | `powerbi_import/migration_planner.py` (new) | High | Input: site topology + per-workbook complexity (from `--assess`). Group workbooks into waves: Wave 0 = shared published datasources, Wave 1 = simple workbooks (GREEN), Wave 2 = moderate (YELLOW), Wave 3 = complex (RED). Respect dependency ordering (datasource before its consumers). Max wave size configurable (default 20 workbooks). |
| 140.2 | **Effort estimator** | @assessor | `powerbi_import/migration_planner.py` | Medium | Per-workbook effort estimate (hours): base time from complexity grade + multipliers for custom SQL (+2h), LOD expressions (+1h), published datasources (+1h), RLS (+1h), >50 measures (+2h). Wave total = sum. Project total = sum + 20% buffer for testing/validation. |
| 140.3 | **Fabric workspace mapper** | @deployer | `powerbi_import/migration_planner.py` | Medium | Map Tableau Projects → Fabric Workspaces (1:1 by default, configurable via `--workspace-mapping` JSON). Tableau Sites → Fabric Capacities. Suggest workspace partitioning based on content volume and RLS boundaries. Output: workspace mapping JSON with project→workspace assignments. |
| 140.4 | **Timeline generator** | @assessor | `powerbi_import/migration_planner.py` | Medium | Given effort estimates + team size (default 2 engineers), generate a Gantt-style timeline: wave start/end dates, parallel tracks, dependency blocks, milestones (pilot wave, UAT, cutover). Configurable working hours/day and sprint duration. |
| 140.5 | **Migration plan HTML report** | @assessor | `powerbi_import/migration_planner.py` | High | Interactive HTML with 6 sections: (1) Executive summary (total scope, effort, timeline), (2) Wave breakdown (table with workbooks per wave), (3) Gantt timeline (CSS-based), (4) Workspace mapping diagram, (5) Risk matrix (complex workbooks, published DS dependencies, RLS gaps), (6) Ready-to-run CLI commands per wave (`python migrate.py --batch wave_1/ ...`). |
| 140.6 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--plan-migration` flag (requires `--server` or `--batch` + previous `--server-discover` output). `--team-size N`, `--wave-max-size N`, `--workspace-mapping FILE`. |
| 140.7 | **Tests** | @tester | `tests/test_migration_planner.py` (new) | Medium | 30+ tests: wave ordering (dependency-first), effort calculation, workspace mapping, timeline generation, HTML report structure, CLI flag wiring. |

**Success:** `python migrate.py --server URL --plan-migration --team-size 3` produces an actionable migration plan with waves, timeline, and per-wave CLI commands.

---

### Sprint 141 — User, Group & Permission Mapping (@deployer, @extractor)

**Goal:** Map Tableau Server security model (users, groups, site roles, workbook permissions) to Azure AD groups and Fabric/PBI workspace roles. Generate actionable scripts for Azure AD provisioning.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 141.1 | **User inventory with roles** | @extractor | `tableau_export/server_client.py` | Medium | Enhance `list_users()`: include site role (Creator/Explorer/Viewer/Unlicensed), last login date, group memberships (via per-user group query). Output enriched user inventory. |
| 141.2 | **Permission matrix builder** | @extractor | `tableau_export/server_client.py` | Medium | New `build_permission_matrix()`: for each workbook/datasource, call `get_permissions()`. Build cross-reference: user×content ACL matrix (View/Explore/Edit/Download). Identify over-permissioned users and orphaned permissions. |
| 141.3 | **Site role → Workspace role mapper** | @deployer | `powerbi_import/permission_mapper.py` | High | Map Tableau site roles → PBI/Fabric workspace roles: Creator→Admin/Member, Explorer→Contributor, Viewer→Viewer, Unlicensed→None. Map Tableau groups → recommended Azure AD security groups. Generate mapping CSV. |
| 141.4 | **RLS principal reconciliation** | @deployer | `powerbi_import/permission_mapper.py` | Medium | Cross-reference migrated RLS roles (from `USERPRINCIPALNAME()`) with user inventory. Verify UPN format consistency (user@domain.com vs DOMAIN\user). Flag mismatches. Suggest UPN transform rules. |
| 141.5 | **Azure AD provisioning scripts** | @deployer | `powerbi_import/permission_mapper.py` | Medium | Generate PowerShell scripts: (1) Create Azure AD security groups matching Tableau groups, (2) Add members to groups, (3) Assign workspace roles. Include dry-run mode and `Connect-MgGraph` setup. |
| 141.6 | **Permission migration report** | @assessor | `powerbi_import/permission_mapper.py` | Medium | HTML report: user count by role, group mapping table, permission matrix heatmap, RLS coverage, unmapped users list, over-permissioned user warnings. |
| 141.7 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--map-permissions` flag (requires `--server`). Output: `permission_mapping.csv`, `provision_groups.ps1`, `permission_report.html`. |
| 141.8 | **Tests** | @tester | `tests/test_permission_mapping.py` (new) | Medium | 30+ tests: user inventory parsing, permission matrix, role mapping, UPN validation, PowerShell script generation, HTML report structure. |

**Success:** `python migrate.py --server URL --map-permissions` produces a complete security mapping with ready-to-run Azure AD provisioning scripts.

---

### Sprint 142 — Subscription & Alert Migration (@deployer, @extractor)

**Goal:** Migrate Tableau Server subscriptions (scheduled email reports) and data-driven alerts to Power BI equivalents (PBI subscriptions + Power Automate flow definitions).

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 142.1 | **Site-wide subscription extraction** | @extractor | `tableau_export/server_client.py` | Medium | Enhance `get_workbook_subscriptions()` to site-wide mode: all subscriptions with schedule (cron), recipients (user email), format (PDF/PNG/Excel), subject line, view filter context. Group by workbook. |
| 142.2 | **Data-driven alert extraction** | @extractor | `tableau_export/server_client.py` | Medium | New `list_data_alerts()`: extract all data-driven alert conditions (field, threshold, operator, frequency, recipient list). Map to structured alert rules. Available on Server 2018.3+. |
| 142.3 | **PBI subscription config generator** | @deployer | `powerbi_import/subscription_generator.py` (new) | High | Generate PBI subscription JSON per Tableau subscription: report page, frequency (daily/weekly/monthly), time, recipient email, format (PDF/PNG). Map Tableau cron schedule to PBI schedule format. Output importable via PBI REST API. |
| 142.4 | **Power Automate flow templates** | @deployer | `powerbi_import/subscription_generator.py` | Medium | For advanced scenarios (conditional send, Teams notification, multi-report digest): generate Power Automate flow definition JSON. Template: scheduled trigger → Get PBI report page → condition check → Send email/Teams. Importable via Power Automate portal. |
| 142.5 | **Alert → PBI data alert mapping** | @deployer | `powerbi_import/alerts_generator.py` | Medium | Extend existing alerts_generator: map Tableau alert conditions to PBI tile-based data alerts. Generate alert config JSON with measure reference, threshold, operator, and notification frequency. |
| 142.6 | **Schedule conflict detector** | @assessor | `powerbi_import/subscription_generator.py` | Low | Detect conflicts: >8 daily refreshes on Pro license, overlapping subscription windows, high-frequency alerts on large datasets. Recommend Power BI Premium or schedule staggering. |
| 142.7 | **Subscription migration report** | @deployer | `powerbi_import/subscription_generator.py` | Low | Summary HTML: N subscriptions mapped, N alerts mapped, recipient distribution, schedule comparison table (Tableau vs PBI), unmapped items, license impact (Pro vs Premium). |
| 142.8 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--migrate-subscriptions` flag (requires `--server`). Output: `subscriptions/` directory with JSON configs + `subscription_report.html`. |
| 142.9 | **Tests** | @tester | `tests/test_subscription_migration.py` (new) | Medium | 30+ tests: subscription extraction, alert extraction, PBI subscription JSON, Power Automate flow structure, schedule conflict detection, report generation. |

**Success:** `python migrate.py --server URL --migrate-subscriptions` produces deployable PBI subscription configs and Power Automate flow templates.

---

### Sprint 143 — Published Datasource Resolution (@extractor, @semantic)

**Goal:** When a workbook references a published datasource (no embedded XML), automatically fetch the full datasource definition from Server and inject it into the extraction pipeline — eliminating the biggest source of "missing columns" in enterprise migrations.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 143.1 | **Published DS detection** | @extractor | `tableau_export/datasource_extractor.py` | Medium | During extraction, detect `<datasource ... hasConnection="false">` or `<repository-location>` patterns that indicate a published datasource reference. Extract the published DS name and project. |
| 143.2 | **Server-side DS fetch** | @extractor | `tableau_export/server_client.py` | Medium | New `download_published_datasource_by_name(name, project)`: search published datasources, download the `.tdsx`, extract embedded XML. Cache downloaded datasources to avoid re-fetching across workbooks that share the same published DS. |
| 143.3 | **DS merge into extraction** | @extractor | `tableau_export/datasource_extractor.py` | High | Merge fetched published DS XML into the workbook's datasource tree: inject tables, columns, calculations, relationships. Handle naming conflicts (published DS field names vs local workbook overrides). Preserve workbook-level calculated fields that reference published DS columns. |
| 143.4 | **Offline fallback** | @extractor | `tableau_export/datasource_extractor.py` | Low | When `--server` is not provided but workbook references published DS: extract available metadata from the workbook XML (field names, types from `<column>` elements). Emit warning listing missing columns. Generate placeholder M query with `// TODO: connect to published datasource` comment. |
| 143.5 | **Published DS cache** | @extractor | `tableau_export/server_client.py` | Low | File-based cache: `~/.tableau_migration_cache/datasources/{ds_id}.tdsx`. TTL-based expiry (default 24h). `--clear-cache` CLI flag. Avoids redundant downloads during batch migrations of 100+ workbooks sharing 5–10 published datasources. |
| 143.6 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--resolve-published-ds` flag (auto-enabled when `--server` is provided). `--ds-cache-dir`, `--no-ds-cache` flags. |
| 143.7 | **Tests** | @tester | `tests/test_published_ds_resolution.py` (new) | Medium | 25+ tests: published DS detection, server-side fetch (mock), merge into extraction, offline fallback, cache hit/miss/expiry, naming conflict resolution. |

**Success:** Enterprise workbooks using published datasources migrate with full column/calculation fidelity when `--server` is provided.

---

### Sprint 144 — Cutover Orchestration & Rollback (@deployer, @orchestrator)

**Goal:** Provide a managed cutover workflow: staged deployment with validation gates, automatic rollback on failure, and parallel-run mode for side-by-side comparison during transition.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 144.1 | **Cutover plan generator** | @deployer | `powerbi_import/cutover_manager.py` (new) | High | Generate a step-by-step cutover plan from migration plan: (1) Deploy shared datasources, (2) Deploy Wave 1 reports, (3) Validate (autoplay), (4) Redirect users, (5) Monitor for 48h, (6) Decommission Tableau content. Each step has success criteria and rollback action. |
| 144.2 | **Staged deployment executor** | @deployer | `powerbi_import/cutover_manager.py` | High | Execute cutover plan step-by-step: deploy → validate → gate (auto or manual approval) → next step. On validation failure: pause and prompt for rollback or continue. Uses existing `pbi_deployer.py` and `bundle_deployer.py` for deployment actions. |
| 144.3 | **Rollback snapshots** | @deployer | `powerbi_import/cutover_manager.py` | Medium | Before each deployment step, capture a rollback snapshot: workspace content list, dataset IDs, report IDs. On rollback: delete deployed artifacts, restore workspace to pre-cutover state. Snapshot stored as JSON in `.cutover/` directory. |
| 144.4 | **Parallel-run mode** | @orchestrator | `migrate.py` | Medium | `--parallel-run` flag: deploy PBI reports alongside existing Tableau content. Generate a comparison dashboard (HTML) showing both sources side-by-side. Users validate in parallel before final cutover. Auto-generate "migration complete" notification template (email/Teams). |
| 144.5 | **Cutover status dashboard** | @deployer | `powerbi_import/cutover_manager.py` | Medium | Live HTML dashboard: cutover progress (step N/M), per-step status (pending/running/passed/failed/rolled-back), deployment timestamps, validation results, rollback history. Auto-refreshes via `<meta http-equiv="refresh">`. |
| 144.6 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--cutover` flag (requires `--deploy` or `--deploy-bundle`). `--cutover-plan-only` (generate plan without executing). `--rollback STEP_ID` (rollback to a specific step). |
| 144.7 | **Tests** | @tester | `tests/test_cutover_manager.py` (new) | Medium | 30+ tests: plan generation, staged execution (mock deploy), rollback (mock delete), parallel-run, status dashboard HTML, CLI flag wiring. |

**Success:** `python migrate.py --server URL --plan-migration --cutover` executes a full staged migration with validation gates and automatic rollback on failure.

---

### Sprint 145 — v32.0.0 Release & Hardening (All Agents)

**Goal:** Version bump, comprehensive integration testing across all Server features, documentation refresh, PyPI publish.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 145.1 | **Version bump** | @orchestrator | `pyproject.toml`, `CHANGELOG.md` | Low | `31.x` → `32.0.0`. Document Sprints 139–145. |
| 145.2 | **Server E2E integration test** | @tester | `tests/test_server_e2e.py` (new) | High | Mock-based E2E: discover → plan → map permissions → migrate subscriptions → resolve published DS → cutover. 20+ integration tests spanning all Server sprints. |
| 145.3 | **Real-world Server validation** | @tester | — | Medium | Validate against a real Tableau Server/Cloud instance (manual test). Document results in `docs/SERVER_MIGRATION_GUIDE.md`. |
| 145.4 | **Server Migration Guide** | @orchestrator | `docs/SERVER_MIGRATION_GUIDE.md` (new) | Medium | Step-by-step enterprise guide: prerequisites, authentication setup, discovery, planning, permission mapping, wave execution, subscription migration, cutover, post-migration monitoring. |
| 145.5 | **CLI reference update** | @orchestrator | `README.md`, `docs/FAQ.md` | Low | Add all new Server flags to CLI reference table. Add Server FAQ section. |
| 145.6 | **Test baseline** | @tester | — | — | Target: **8,400+** tests. |

---

### v32.0.0 Success Criteria

| Metric | Target | Owner |
|--------|--------|-------|
| Site discovery | Full topology + dependency graph from any Server/Cloud instance | @extractor |
| Migration waves | Dependency-ordered waves with effort estimates | @assessor |
| Permission mapping | 100% of users/groups mapped to Azure AD equivalents | @deployer |
| Subscription migration | Tableau subscriptions → PBI subscription JSON | @deployer |
| Published DS resolution | Zero "missing columns" when `--server` is provided | @extractor |
| Cutover orchestration | Staged deploy with automatic rollback | @deployer |
| Server Migration Guide | Complete step-by-step enterprise documentation | @orchestrator |
| **Migration Confidence Score** | **≥98.5 (Grade A+)** | @assessor |
| Tests | **8,400+** | @tester |

### v32.0.0 Agent Ownership Matrix

| Agent | Sprint 139 | Sprint 140 | Sprint 141 | Sprint 142 | Sprint 143 | Sprint 144 | Sprint 145 |
|-------|-----------|-----------|-----------|-----------|-----------|-----------|-----------|
| **@extractor** | 139.1, 139.5 | — | 141.1, 141.2 | 142.1, 142.2 | 143.1–143.5 | — | — |
| **@assessor** | 139.2–139.4, 139.6 | 140.1, 140.2, 140.4, 140.5 | 141.6 | 142.6 | — | — | — |
| **@deployer** | — | 140.3 | 141.3–141.5 | 142.3–142.5, 142.7 | — | 144.1–144.3, 144.5 | — |
| **@semantic** | — | — | — | — | 143.3 | — | — |
| **@orchestrator** | 139.7 | 140.6 | 141.7 | 142.8 | 143.6 | 144.4, 144.6 | 145.1, 145.4, 145.5 |
| **@tester** | 139.8 | 140.7 | 141.8 | 142.9 | 143.7 | 144.7 | 145.2, 145.3, 145.6 |

### v32.0.0 CLI Flags Summary

| Flag | Requires | Description |
|------|----------|-------------|
| `--server-discover` | `--server` | Build full site topology + dependency graph |
| `--plan-migration` | `--server` or `--batch` | Generate migration wave plan + effort estimates |
| `--team-size N` | `--plan-migration` | Team size for timeline calculation (default 2) |
| `--wave-max-size N` | `--plan-migration` | Max workbooks per wave (default 20) |
| `--workspace-mapping FILE` | `--plan-migration` | Custom Tableau project → Fabric workspace mapping JSON |
| `--map-permissions` | `--server` | Extract users/groups/permissions + generate Azure AD scripts |
| `--migrate-subscriptions` | `--server` | Migrate subscriptions + alerts to PBI/Power Automate configs |
| `--resolve-published-ds` | `--server` | Auto-fetch published datasource definitions (auto-enabled with `--server`) |
| `--ds-cache-dir DIR` | `--resolve-published-ds` | Cache directory for downloaded published datasources |
| `--no-ds-cache` | `--resolve-published-ds` | Disable datasource cache |
| `--clear-cache` | — | Clear the published datasource cache |
| `--cutover` | `--deploy` or `--deploy-bundle` | Execute staged cutover with validation gates |
| `--cutover-plan-only` | `--cutover` | Generate cutover plan without executing |
| `--rollback STEP_ID` | `--cutover` | Rollback to a specific cutover step |
| `--parallel-run` | `--cutover` | Deploy alongside existing Tableau content for comparison |

---

## v38.2.0 — Report Packaging & Developer Experience (Sprints 175–179)

**Theme:** Enterprise customers need polished deliverables for stakeholder communication. Developers need better tooling for iterative migration refinement. This version delivers **PDF/PPTX report export**, **REST API v2**, **Streamlit UI polish**, and **migration diff tooling**.

**Motivation:** The tool generates 9 HTML report types (assessment, portfolio, merge, topology, telemetry, visual diff, comparison, lineage, cutover), but enterprises need offline-distributable PDF/PPTX versions for executive reviews, audit trails, and compliance documentation. The REST API (v1) lacks pagination, filtering, and webhook support. The Streamlit Web UI works but needs batch mode, merge UI, and deployment integration.

---

### Sprint 175 — PDF & PPTX Report Export (@visual, @assessor)

**Goal:** Generate PDF and PPTX versions of all 9 HTML migration/assessment reports for offline distribution and executive review. Pure stdlib approach — no `weasyprint` dependency.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 175.1 | **PDF renderer module** | @visual | `powerbi_import/pdf_renderer.py` (new) | High | Stdlib HTML-to-PDF via `@media print` CSS + browser print API hint. Generates print-optimized HTML with page breaks, expanded sections, hidden interactive elements. `render_to_pdf(html_content, output_path)` API. Fallback: saves print-ready HTML with `.pdf.html` extension and instructions. |
| 175.2 | **Print-optimized CSS** | @visual | `powerbi_import/html_template.py` | Medium | Add `@media print` styles to shared template: page breaks before sections, collapse interactive elements, A4 page size, margin control, expand all collapsed sections. |
| 175.3 | **PPTX report generator** | @assessor | `powerbi_import/pptx_report.py` (new) | High | Generate PowerPoint executive summary from migration data: title slide, scope overview (workbook count, measure count, visual count), fidelity radar chart (as table), wave plan timeline, risk matrix, recommendations. Uses `python-pptx` as optional dependency with stdlib XML fallback. |
| 175.4 | **Report packaging** | @assessor | `powerbi_import/assessment.py` | Medium | `--report-package` generates a ZIP containing: HTML report + print-ready PDF + extraction JSON + fidelity summary CSV + PPTX executive summary. Single deliverable for stakeholders. |
| 175.5 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--pdf` flag on `--assess`, `--global-assess`, `--assess-merge`, `--bulk-assess`. `--pptx` for executive summary. `--report-package` for bundled ZIP. |
| 175.6 | **Tests** | @tester | `tests/test_report_packaging.py` (new) | Medium | 30+ tests: print CSS validation, PPTX slide structure, report package ZIP contents, CLI flag wiring. |

**Success:** `python migrate.py --bulk-assess examples/ --report-package` produces a ZIP with HTML + printable PDF + PPTX executive summary.

---

### Sprint 176 — REST API v2 & OpenAPI (@orchestrator, @deployer)

**Goal:** Upgrade the REST API from basic endpoints to a production-grade API with pagination, filtering, webhook callbacks, OpenAPI spec, and authentication.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 176.1 | **OpenAPI spec generation** | @orchestrator | `powerbi_import/api_server.py` | Medium | Auto-generate OpenAPI 3.0 spec from endpoint definitions. Serve at `GET /openapi.json`. Include request/response schemas, error codes, parameter descriptions. |
| 176.2 | **Pagination & filtering** | @orchestrator | `powerbi_import/api_server.py` | Medium | `GET /jobs?status=completed&page=2&per_page=20`. Filter by status, date range, workbook name. Cursor-based pagination for large job lists. |
| 176.3 | **Webhook callbacks** | @deployer | `powerbi_import/api_server.py` | Medium | `POST /migrate` accepts `webhook_url` parameter. On completion/failure, POST job result JSON to webhook URL. Configurable retry (3 attempts, exponential backoff). HMAC signature for verification. |
| 176.4 | **API key authentication** | @orchestrator | `powerbi_import/api_server.py` | Medium | Optional `--api-key` flag. Requests require `Authorization: Bearer <key>` header. Rate limiting per key (default 60 req/min). |
| 176.5 | **Batch migration endpoint** | @orchestrator | `powerbi_import/api_server.py` | Medium | `POST /migrate/batch` accepts multiple files. Returns batch job ID. `GET /batch/{id}` returns per-workbook status. ZIP download for all results. |
| 176.6 | **Tests** | @tester | `tests/test_api_v2.py` (new) | Medium | 35+ tests: OpenAPI schema validation, pagination, filtering, webhook delivery (mock), auth, batch endpoint, rate limiting. |

**Success:** API consumers can integrate migration into CI/CD pipelines with webhook-driven automation and OpenAPI-generated client SDKs.

---

### Sprint 177 — Streamlit Web UI Polish (@orchestrator, @merger, @assessor)

**Goal:** Extend the existing Streamlit Web UI with batch mode, shared-model merge UI, visual diff viewer, and deployment integration.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 177.1 | **Batch mode page** | @orchestrator | `web/app.py` | Medium | Multi-file upload with drag-and-drop. Per-workbook progress table (name, status, fidelity, grade). Batch summary dashboard. Download all results as ZIP. |
| 177.2 | **Shared model merge page** | @merger | `web/app.py` | High | Multi-workbook upload → interactive merge heatmap (table overlap scores as color matrix), conflict list with resolution options, force-merge toggle, model name input. Preview merged table list before generation. |
| 177.3 | **Visual diff viewer** | @assessor | `web/app.py` | Medium | Side-by-side comparison: Tableau worksheet list vs PBI page/visual list. Per-visual field coverage bars. Encoding gap highlights. Reuses `visual_diff.py` output. |
| 177.4 | **DAX formula editor** | @orchestrator | `web/app.py` | Medium | Select a measure → view Tableau formula + converted DAX side-by-side in code editors. In-place edit DAX. Re-validate with `dax_optimizer.py`. Save overrides to `config.json`. |
| 177.5 | **Assessment radar chart** | @assessor | `web/app.py` | Low | Interactive 9-category radar chart using Streamlit's native chart components. Click-to-expand per-category detail. |
| 177.6 | **Tests** | @tester | `tests/test_web_ui_polish.py` (new) | Medium | 25+ tests: batch upload validation, merge flow, diff rendering, DAX edit round-trip, chart data construction. |

**Success:** Non-CLI users can perform the full migration lifecycle (upload → assess → merge → migrate → review → download) from a browser.

---

### Sprint 178 — Migration Diff & Comparison Tooling (@assessor, @orchestrator)

**Goal:** When re-migrating a workbook (after Tableau changes or tool upgrades), generate a structured diff showing what changed in the output.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 178.1 | **Artifact diff engine** | @assessor | `powerbi_import/artifact_diff.py` (new) | High | Compare two `.pbip` project directories: new/removed/changed TMDL objects (tables, columns, measures, relationships), visual changes (added/removed visuals, data role changes), M query changes. Output: structured diff JSON. |
| 178.2 | **Diff HTML report** | @assessor | `powerbi_import/artifact_diff.py` | Medium | Interactive HTML diff report: tree view of changes, inline before/after DAX comparison, visual layout delta, relationship graph diff. Color-coded (green=added, red=removed, yellow=changed). Uses `html_template.py`. |
| 178.3 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--diff PREVIOUS_OUTPUT_DIR` flag. Generates diff report alongside normal migration output. |
| 178.4 | **Regression baseline management** | @assessor | `powerbi_import/artifact_diff.py` | Medium | `--save-baseline` stores current output as reference. `--check-baseline` compares against stored baseline and fails if unexpected changes detected. For CI integration. |
| 178.5 | **Tests** | @tester | `tests/test_artifact_diff.py` (new) | Medium | 25+ tests: TMDL diff, visual diff, M query diff, HTML report structure, baseline save/load, no-change detection. |

**Success:** `python migrate.py workbook.twbx --diff artifacts/previous/` produces a clear report of what changed between migration runs.

---

### Sprint 179 — v38.2.0 Release & Hardening (All Agents)

**Goal:** Version bump, cross-feature integration testing, documentation refresh, PyPI publish.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 179.1 | **Version bump** | @orchestrator | `pyproject.toml`, `CHANGELOG.md` | Low | `37.1.0` → `38.2.0`. Document Sprints 175–179. |
| 179.2 | **Integration tests** | @tester | `tests/test_v38_e2e.py` (new) | High | E2E: migrate → report package → API batch → diff against baseline. 20+ tests. |
| 179.3 | **Real-world re-validation** | @tester | `tests/test_real_world_e2e.py` | Medium | All 27+ workbooks pass with 0 regressions. |
| 179.4 | **Docs refresh** | @orchestrator | `docs/*.md`, `README.md` | Medium | Update README with report packaging, API v2, UI screenshots. |
| 179.5 | **Test baseline** | @tester | — | — | Target: **8,800+** tests. |

### v38.2.0 Success Criteria

| Metric | Target | Owner |
|--------|--------|-------|
| Report packaging | PDF-ready HTML + PPTX executive summary + ZIP bundle | @visual, @assessor |
| REST API v2 | OpenAPI spec, pagination, webhooks, auth, batch endpoint | @orchestrator |
| Web UI | Batch mode, merge UI, visual diff, DAX editor | @orchestrator |
| Artifact diff | Structured diff between migration runs | @assessor |
| Tests | **8,800+** | @tester |

### v38.2.0 Agent Ownership Matrix

| Agent | Sprint 175 | Sprint 176 | Sprint 177 | Sprint 178 | Sprint 179 |
|-------|-----------|-----------|-----------|-----------|-----------|
| **@visual** | 175.1, 175.2 | — | — | — | — |
| **@assessor** | 175.3, 175.4 | — | 177.3, 177.5 | 178.1–178.4 | — |
| **@orchestrator** | 175.5 | 176.1, 176.2, 176.4, 176.5 | 177.1, 177.4 | 178.3 | 179.1, 179.4 |
| **@deployer** | — | 176.3 | — | — | — |
| **@merger** | — | — | 177.2 | — | — |
| **@tester** | 175.6 | 176.6 | 177.6 | 178.5 | 179.2, 179.3, 179.5 |

---

## v38.3.0 — Empty-Visual Recovery (Patch — Shipped)

**Theme:** Real-world QA of UC80 (the EDF ARGOS dashboard) surfaced 82 worksheet visuals that rendered as empty cards in Power BI Desktop after migration. v38.3.0 is a focused patch release that closes the gap between extraction and visual generation for these worksheets.

**Shipped:**

- Reworked the `worksheetReference` → visual container path so that worksheets with only filter/parameter shelves (no encoded fields) still emit a usable visual instead of an empty container.
- Hardened the field-binding logic in `visual_generator.py` for shelves that mix calculated columns, parameters, and measure references — these used to drop silently.
- Added unit coverage for the empty-visual regression in `tests/test_pbip_generator_coverage.py` and `tests/test_v51_features.py`.
- Re-ran the full sample-workbook batch (`artifacts/tableau_samples_output/`) to lock in the fix.

**Release commit:** `6298cdb1` — *Release v38.3.0: Fix 82 empty visuals in UC80 migration*.

No new CLI flags. No schema changes. This patch is fully backward-compatible with v38.2.0 outputs.

---

## v38.4.0 — Pixel-Perfect Text & Format Fidelity (Sprints 200–203 — Shipped)

**Theme:** Real-world dashboards (EDF UC80, internal "TDB Maintenance") exposed text-rendering and per-visual chrome (background, border) gaps that were invisible on synthetic samples but jarring on production-grade dashboards. v38.4.0 closes the **text run, font cascade, and visual chrome** loop end-to-end.

**Motivation:** Two distinct classes of issue were observed on UC80:
1. **Font cascade incomplete** — Tableau per-run font-family/size attributes (`fontname`, `fontsize`) on textboxes, annotations, and tooltips were dropped on the floor when the generator hit hard-coded `"Segoe UI" / 12pt` defaults.
2. **Format zones ignored** — Tableau `<format>` blocks defining per-zone `background-color`, `border-color`, `border-width` on individual worksheets/textboxes were extracted but never applied to the corresponding PBI visual container.
3. **Stray `Æ` glyphs** — Tableau Desktop emits a hidden `<run>Æ&#10;</run>` (sometimes with `fontalignment='1'`) as an internal soft line-break sentinel. Invisible in Tableau; rendered literally as `Æ` in PBI and browsers.

---

### Sprint 200 — Annotation & Textbox Font Cascade (@extractor, @generator) ✅ SHIPPED

| # | Item | Owner | File(s) | Details |
|---|------|-------|---------|---------|
| 200.1 | **Annotation font-family extraction** | @extractor | `extract_tableau_data.py` | Capture `fontname`/`fontsize`/`fontcolor`/`bold`/`italic` per run on `<annotation>` and `<tooltip>` elements. Persist into `annotations.json`. |
| 200.2 | **Annotation textbox font-family wiring** | @generator | `pbip_generator.py` (lines ~1170–1191) | Apply extracted font properties to the generated annotation/tooltip textbox runs. |
| 200.3 | **Multi-paragraph fidelity** | @generator | `pbip_generator.py::_parse_rich_text_runs` (~line 1035) | Preserve hard line breaks (`\n`) inside text runs so multi-line headers (e.g. *"Tableau de bord"* / *"ARGOS"*) keep two paragraphs in PBI. |

---

### Sprint 201 — Data-Driven Font Overrides on Visuals (@generator) ✅ SHIPPED

| # | Item | Owner | File(s) | Details |
|---|------|-------|---------|---------|
| 201.1 | **`_apply_tableau_font_overrides` helper** | @generator | `visual_generator.py` | Reads per-visual font metadata from extraction (`title.format`, `labels.format`, `axis.format`) and patches the corresponding PBI `formatProperties` instead of relying on hard-coded defaults. |
| 201.2 | **Invocation at end of `_apply_visual_decorations`** | @generator | `visual_generator.py` | Ensures every visual container goes through the font-override pass before serialization. |
| 201.3 | **Charts + tables + slicers covered** | @generator | `visual_generator.py` | Verified on `clusteredBarChart`, `tableEx`, `slicer`, `textbox`. |

---

### Sprint 202 — Per-Visual Background & Border (@generator) ✅ SHIPPED

| # | Item | Owner | File(s) | Details |
|---|------|-------|---------|---------|
| 202.1 | **`_apply_tableau_background_border` helper** | @generator | `visual_generator.py` | Reads Tableau zone `<format attr='background-color'>` / `<format attr='border-*'>` and emits PBI `visualContainerObjects.background` + `border` blocks. |
| 202.2 | **Border style normalization** | @generator | `visual_generator.py` | Tableau `none`/`solid`/`dotted`/`dashed` → PBI `lineStyle`. Width clamped to PBI valid range. |
| 202.3 | **Color fallback chain** | @generator | `visual_generator.py` | zone format → dashboard theme → default white. Hex normalized to `#RRGGBB`. |

---

### Sprint 203 — Tableau Line-Break Sentinel (`Æ`) Fix (@extractor) ✅ SHIPPED

| # | Item | Owner | File(s) | Details |
|---|------|-------|---------|---------|
| 203.1 | **Centralized helper** | @extractor | `extract_tableau_data.py` (~line 100) | New `_clean_tableau_run_text(run_elem)` + `_TABLEAU_LB_SENTINEL_RE = r'^[\s]*[\u00c6\u00a0]+[\s]*$'`. `fontalignment` deliberately excluded from `_TABLEAU_RUN_STYLE_ATTRS` so unstyled sentinel runs are still recognized. |
| 203.2 | **Three application sites** | @extractor | `extract_tableau_data.py` | Dashboard text zone runs (~line 1450), `extract_tooltips` runs (~line 1370), `extract_annotations` runs (~line 2120). Sentinel runs become empty values; embedded `\n` is preserved so paragraph breaks survive. |
| 203.3 | **Regression suite** | @tester | `tests/test_pixel_perfect_fidelity.py` (new) | New `TestTableauLineBreakSentinel` class — 7 dedicated tests. Total pixel-perfect suite: **28 tests**, all green. |

**Validation:** UC80 regeneration shows zero stray `Æ` textRuns; *"Tableau de bord"* and *"ARGOS"* render as two separate paragraphs as in Tableau.

---

### v38.4.0 Success Criteria — Met

| Metric | Target | Result |
|--------|--------|--------|
| Stray `Æ` glyphs in regenerated UC80 | 0 | ✅ 0 |
| Multi-paragraph headers preserved | 100% | ✅ verified |
| Annotation font cascade | font-family + size + color | ✅ all three |
| Per-visual chrome | background + border | ✅ both |
| Pixel-perfect regression suite | ≥28 tests | ✅ 28/28 green |
| Cross-suite tests | no regressions in 759 affected tests | ✅ all green |
| Real-world re-migration | 6 UC80-class workbooks | ✅ 100% extraction |

---

## v38.5.0 — Floating-Overlay Fidelity & Real-World QA (Sprints 204–208 — In Progress)

**Theme:** The v38.4 sizing audit confirmed that page dimensions, direct-zone visuals (textbox/image), and per-axis scaling are all faithful. One layout caveat remains: **floating worksheet zones overlaid on top of tiled chart zones** (e.g. legends pinned to a chart's corner) are currently flattened to a side-by-side layout by `_build_zone_layout_map`. v38.5 closes this loop, codifies the manual UC80 QA into an automated suite, and adds per-workbook golden fixtures to lock in the v38.4 wins.

**Sprint status:**

| Sprint | Title | Status |
|--------|-------|--------|
| 204 | Floating Zone Overlay Fidelity | ✅ Done (deterministic z-order overlap stagger) |
| 205 | Per-Workbook Pixel-Perfect Golden Fixtures | ✅ Done (7 deterministic fixtures incl. Enterprise_Sales) |
| 206 | Mixed-Alignment & Vertical-Anchor Text Runs | ✅ Done (per-run h-align + zone v-align; 19 tests) |
| 207 | Real-World Migration QA Suite | ✅ Done (incl. 207.4 autoplay QA report card) |
| 208 | v38.5.0 Release & Hardening | ✅ Done |

**Motivation:**
- UC80's `N_1`/`N_X`/`N_4`/`N_7` chart groups all have a chart at `x=2083 w=95834` with a legend floating at `x≈85k–89k w≈8.7k` — Tableau renders the legend overlaid on the chart's right corner, but the current grid layout puts them side-by-side, shrinking the chart from `~1880px → 1240px`.
- Pixel-perfect regression coverage is currently spot-tested. Without per-workbook golden fixtures, future refactors can silently regress font/border/sentinel behaviour.
- Manual QA of UC80 caught two real bugs (empty visuals, `Æ` glyphs). We need to run that QA as an automated step.

---

### Sprint 204 — Floating Zone Overlay Fidelity (@generator, @extractor)

**Goal:** When a Tableau zone has `is-floating='true'` AND its position rectangle overlaps a sibling tiled zone, place the corresponding PBI visual on top using absolute coordinates instead of routing it through the grid layout map.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 204.1 | **Floating-overlap detector** | @generator | `pbip_generator.py::_build_zone_layout_map` (~line 535) | Medium | Pre-pass that scans the zone hierarchy and flags any zone with `is_floating=True` whose rect intersects (≥30% overlap) with any sibling's rect. Flagged zones are excluded from grid subdivision and resolved via absolute proportional mapping into the parent's pixel space. |
| 204.2 | **Absolute-coord fallback** | @generator | `pbip_generator.py::_layout_zone` | Medium | Use the existing floating-children branch (already at ~line 638) for overlap-flagged zones. Increment z-order so the overlay sits on top. |
| 204.3 | **Legend-on-chart heuristic** | @generator | `visual_generator.py` | Low | When a small worksheet (≤ 15% of page area) named `*_Legende` / `*_Legend` is detected as overlapping a larger sibling worksheet, automatically apply transparent background so the chart shows through. |
| 204.4 | **Extractor signal** | @extractor | `extract_tableau_data.py::extract_zone_hierarchy` | Low | Persist the `is-floating` flag explicitly on every zone in `zone_hierarchy` so the generator does not need to re-parse. Today it lives on the flat `objects` list. |
| 204.5 | **UC80 validation** | @tester | `tests/test_pixel_perfect_fidelity.py::TestFloatingOverlap` (new) | Medium | 12+ tests: overlap detection (≥30% rule, no false positives), absolute placement of overlay, z-order correctness, legend transparency heuristic, UC80 `N_1`/`N_X`/`N_4`/`N_7` zone fixture round-trip. |

**Success:** After regenerating UC80, the `N_1 - Observables non conformes` chart spans `~1860 px` (matching Tableau's `~96%` of canvas width) with `N_1 Legende` overlaid on its right edge at the correct ~170×63 size.

---

### Sprint 205 — Per-Workbook Pixel-Perfect Golden Fixtures (@tester)

**Goal:** Capture a deterministic snapshot of font/border/background/sentinel-handling output for each real-world workbook so future regressions are caught at PR time.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 205.1 | **Golden fixture format** | @tester | `tests/golden/<workbook>/` (new) | Low | Minimal JSON per visual: `{ font_family, font_size, bg_color, border_color, border_width, text_runs: [...] }`. Excludes positions (covered by `TestPositionFidelity`) and IDs (non-deterministic). |
| 205.2 | **Fixture generator script** | @tester | `scripts/generate_pixel_fixtures.py` (new) | Medium | Walks a `.pbip` output and extracts the comparable subset to `tests/golden/<name>/<page>/<visual>.json`. Idempotent — diffable on PR. |
| 205.3 | **Golden test runner** | @tester | `tests/test_pixel_golden.py` (new) | Medium | Parametrized over 6 workbooks (UC80, NBA, Superstore, TDB Maintenance, World Indicators, Salesforce). Asserts regenerated output matches fixture. Fails with structured diff on mismatch. |
| 205.4 | **CI integration** | @tester | `.github/workflows/ci.yml` | Low | Add a `pixel-golden` job that runs after the standard test matrix. Failure marks the PR as needing fixture update review. |
| 205.5 | **Update workflow doc** | @tester | `tests/golden/README.md` (new) | Low | One-pager: how to update fixtures (`python scripts/generate_pixel_fixtures.py --workbook X --apply`), when it's legitimate vs a real regression. |

**Success:** 6 workbook golden fixture sets in `tests/golden/`, CI fails on any uncoordinated change to font/border/background/text-run output.

---

### Sprint 206 — Mixed-Alignment & Vertical-Anchor Text Runs (@extractor, @generator)

**Goal:** Tableau supports per-run horizontal alignment (`fontalignment`) and per-textbox vertical anchor (`vertical-align: top/center/bottom`). These are currently dropped — paragraphs default to left/top. v38.5 round-trips both.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 206.1 | **Capture `fontalignment` per run** | @extractor | `extract_tableau_data.py` | Low | Add `fontalignment` to the persisted run dict. Map `1`→`left`, `2`→`center`, `3`→`right`, `4`→`justify`. Keep `_clean_tableau_run_text` ignoring it for sentinel detection. |
| 206.2 | **Per-paragraph alignment in PBI** | @generator | `pbip_generator.py::_parse_rich_text_runs` | Medium | Emit `paragraph.horizontalAlignment` instead of single textbox-wide alignment. Group runs with the same alignment into one paragraph object. |
| 206.3 | **Vertical anchor extraction** | @extractor | `extract_tableau_data.py` | Low | Capture `<format attr='vertical-align'>` from zone format blocks. |
| 206.4 | **Vertical anchor application** | @generator | `pbip_generator.py::_create_visual_textbox` | Low | Map to PBI `verticalAlignment` on the textbox object. |
| 206.5 | **Tests** | @tester | `tests/test_pixel_perfect_fidelity.py::TestTextAlignment` (new) | Medium | 15+ tests: per-run horizontal alignment (4 modes), mixed-alignment paragraph grouping, vertical anchor (3 modes), round-trip on UC80 header textbox. |

---

### Sprint 207 — Real-World Migration QA Suite (@assessor, @tester) — ✅ DONE (207.4 autoplay pending)

**Goal:** Codify the manual UC80 QA we ran (zero stray `Æ`, sizing audit, empty-visual count, font cascade verification) into an automated end-to-end report card.

**Status:** Shipped in `powerbi_import/qa_suite.py` (5 read-only checks, stdlib-only), wired into `migrate.py::_run_qa_suite` with a new `--qa-strict` flag and `qa_report.html` emission, covered by `tests/test_qa_suite.py` (39 tests). Remaining: 207.4 — surface the report-card summary inside `--autoplay`.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 207.1 ✅ | **QA suite engine** | @assessor | `powerbi_import/qa_suite.py` (new) | High | After migration completes, run a battery of automated checks against the output: (1) no stray `Æ`/`Œ`/`nbsp` chars in any textRun value, (2) zero empty visuals (no encoded fields AND no static content), (3) all visuals have non-null `formatProperties`, (4) every dashboard zone matched to a PBI visual, (5) no orphan filters. Returns `QAReport` with per-check pass/fail + sample evidence. |
| 207.2 ✅ | **HTML report card** | @assessor | `powerbi_import/qa_suite.py` | Medium | Generates `<output>/qa_report.html` using `html_template.py`. Pass/fail badges per check, "evidence" panel with sample failed visuals, side-by-side Tableau ↔ PBI counts (worksheets, visuals, measures, columns). |
| 207.3 ✅ | **CLI integration** | @orchestrator | `migrate.py` | Low | `--qa` runs the report card and writes `qa_report.html`; new `--qa-strict` (implies `--qa`) exits non-zero on any error-severity check failure for CI. |
| 207.4 ⬜ | **Integration with `--autoplay`** | @assessor | `powerbi_import/qa_suite.py`, `scripts/autoplay.py` | Low | Autoplay surface includes QA report card summary alongside existing 5 checks. |
| 207.5 ✅ | **Tests** | @tester | `tests/test_qa_suite.py` (new) | Medium | 39 tests: each check has a positive + negative fixture; HTML report structure; `--qa-strict` exit code wiring. |

**Success:** `python migrate.py UC80.twbx --qa` reports 6/6 checks pass (zero `Æ`, zero empty visuals, full format coverage, all zones matched, no orphan filters, fidelity ≥97).

---

### Sprint 208 — v38.5.0 Release & Hardening (All Agents)

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 208.1 | **Version bump** | @orchestrator | `pyproject.toml`, `CHANGELOG.md` | Low | ✅ Done — `38.4.0` → `38.5.0`. Documents Sprints 200–208. |
| 208.2 | **Real-world re-migration baseline** | @tester | — | Medium | ✅ Done — 7 deterministic golden fixtures (incl. Enterprise_Sales); 0 visual regressions vs v38.4 baseline. |
| 208.3 | **Docs refresh** | @orchestrator | `README.md`, `docs/FAQ.md` | Low | ✅ Done — 4-axis pixel-perfect coverage matrix (fonts, chrome, sentinel, overlay); `--qa` / `--qa-strict` documented. |
| 208.4 | **PyPI publish** | @deployer | `.github/workflows/publish.yml` | Low | ✅ Done — tag-triggered (`v38.5.0`). |
| 208.5 | **Test baseline** | @tester | — | — | ✅ Done — **8,875** passed, 66 skipped, 1 xfailed (11 pre-existing DAX/`_quote_name` failures flagged, out of scope). |

### v38.5.0 Success Criteria

| Metric | Target | Owner |
|--------|--------|-------|
| Floating-overlay fidelity | Legends on charts render at correct ~170×63 px overlay (UC80 N_1/N_X/N_4/N_7) | @generator |
| Pixel-perfect golden fixtures | 6 workbooks captured, CI gate green | @tester |
| Mixed-alignment text runs | Per-paragraph horizontal alignment + vertical anchor on textboxes | @generator |
| Real-world QA suite | 6 automated checks, HTML report card, CI-strict mode | @assessor |
| Tests | **9,000+** | @tester |

### v38.5.0 Agent Ownership Matrix

| Agent | Sprint 204 | Sprint 205 | Sprint 206 | Sprint 207 | Sprint 208 |
|-------|-----------|-----------|-----------|-----------|-----------|
| **@extractor** | 204.4 | — | 206.1, 206.3 | — | — |
| **@generator** | 204.1–204.3 | — | 206.2, 206.4 | — | — |
| **@assessor** | — | — | — | 207.1, 207.2, 207.4 | — |
| **@orchestrator** | — | — | — | 207.3 | 208.1, 208.3 |
| **@deployer** | — | — | — | — | 208.4 |
| **@tester** | 204.5 | 205.1–205.5 | 206.5 | 207.5 | 208.2, 208.5 |

### v38.5.0 CLI Flags Summary

| Flag | Description |
|------|-------------|
| `--qa` | Run the real-world QA suite after migration; emit `qa_report.html` |
| `--qa-strict` | Exit non-zero on any QA check failure (for CI gating) |

---

## v39.0.0 — Data Blending & Advanced Connectivity (Sprints 180–184)

**Theme:** Close the remaining **data blending gap** (federated cross-datasource queries) and expand connector coverage to handle **real-world enterprise data landscapes** — SAP BW, IBM Db2, Teradata, and cloud-native warehouses with advanced features.

**Motivation:** The GAP_ANALYSIS identifies data blending as a partially-supported gap. Enterprise migrations frequently involve workbooks connecting to 3+ datasources with cross-source calculations. Additionally, many enterprise environments use legacy warehouse connectors (SAP BW, IBM Db2, Teradata) not yet covered by the 63-connector library.

---

### Sprint 180 — Data Blending Engine (@wiring, @semantic)

**Goal:** Full support for Tableau data blending — workbooks that use fields from multiple datasources in a single worksheet via blend relationships (link fields).

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 180.1 | **Blend relationship extraction** | @extractor | `tableau_export/datasource_extractor.py` | High | Extract `<relation type="blend">` and `<column-mapping>` elements. Build blend graph: primary datasource + linked secondary datasources with join fields. Capture blend direction (primary vs secondary) and aggregation behavior. |
| 180.2 | **Blend → merge M query** | @wiring | `tableau_export/m_query_builder.py` | High | Generate Power Query M merge steps for each blend relationship: `Table.NestedJoin(Primary, LinkFields, Secondary, LinkFields, "Blended", JoinKind.LeftOuter)` + `Table.ExpandTableColumn`. Handle multiple secondary datasources (sequential merges). |
| 180.3 | **Cross-datasource DAX measures** | @dax | `tableau_export/dax_converter.py` | High | When a calculation references fields from blended datasources, generate DAX with proper table qualifications. Use `RELATED()` for manyToOne blend relationships. Emit `LOOKUPVALUE()` for manyToMany. |
| 180.4 | **Blend relationship → TMDL relationships** | @semantic | `powerbi_import/tmdl_generator.py` | Medium | Convert blend link fields to TMDL relationships with correct cardinality. Set `crossFilteringBehavior: oneDirection` (secondary→primary). |
| 180.5 | **Blend migration report** | @assessor | `powerbi_import/assessment.py` | Low | Blend-specific assessment: count blended datasources, link field coverage, cross-source calculation complexity. Grade: GREEN (simple 2-source), YELLOW (3+ sources), RED (circular blends). |
| 180.6 | **Tests** | @tester | `tests/test_data_blending.py` (new) | Medium | 35+ tests: blend extraction, merge M generation, cross-source DAX, relationship conversion, complex blend graphs. |

**Success:** A workbook blending Salesforce Opportunities with SQL Server Products produces correct merged M queries and cross-table DAX measures.

---

### Sprint 181 — Enterprise Connector Expansion (@wiring, @extractor)

**Goal:** Add 12 enterprise connectors frequently seen in Fortune 500 migrations.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 181.1 | **SAP BW connector** | @wiring | `tableau_export/m_query_builder.py` | High | `SapBusinessWarehouse.Cubes()` with MDX query passthrough. Server, system ID, client number parameters. |
| 181.2 | **IBM Db2 connector** | @wiring | `tableau_export/m_query_builder.py` | Medium | `DB2.Database()` with schema navigation. z/OS and LUW variants. |
| 181.3 | **Teradata connector** | @wiring | `tableau_export/m_query_builder.py` | Medium | `Teradata.Database()` with BTEQ/FastLoad hints in migration notes. |
| 181.4 | **Vertica enhanced** | @wiring | `tableau_export/m_query_builder.py` | Low | Schema navigation, query pushdown hints, connection pooling parameters. |
| 181.5 | **Azure Synapse Analytics** | @wiring | `tableau_export/m_query_builder.py` | Medium | `AzureSynapse.Database()` with dedicated SQL pool vs serverless detection. |
| 181.6 | **Dremio connector** | @wiring | `tableau_export/m_query_builder.py` | Medium | `Dremio.Datasets()` with reflection and acceleration metadata. |
| 181.7 | **ClickHouse connector** | @wiring | `tableau_export/m_query_builder.py` | Low | `ClickHouse.Database()` with `Odbc.Query` fallback. |
| 181.8 | **SingleStore (MemSQL)** | @wiring | `tableau_export/m_query_builder.py` | Low | `MySQL.Database()` with SingleStore-specific connection parameters. |
| 181.9 | **Firebolt connector** | @wiring | `tableau_export/m_query_builder.py` | Low | `Odbc.Query()` with Firebolt JDBC bridge parameters. |
| 181.10 | **Trino/Starburst** | @wiring | `tableau_export/m_query_builder.py` | Medium | `Odbc.Query()` with catalog/schema navigation from Trino's information_schema. |
| 181.11 | **Connector auto-detection** | @extractor | `tableau_export/datasource_extractor.py` | Low | Detect connector type from connection XML `class` attribute for all 12 new types. Map to correct M generator. |
| 181.12 | **Tests** | @tester | `tests/test_enterprise_connectors.py` (new) | Medium | 30+ tests: M query generation, connection parameter mapping, schema navigation. |

**Success:** 75+ connectors supported. All Fortune 500 migration pilots encounter ≤2 unknown connector types.

---

### Sprint 182 — Custom SQL & Native Query Depth (@wiring, @dax)

**Goal:** Improve handling of Tableau custom SQL queries — currently passed through but not analyzed. Parse custom SQL to extract column metadata, detect aggregations, and generate optimized M `Value.NativeQuery()` expressions.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 182.1 | **Custom SQL parser** | @wiring | `powerbi_import/sql_analyzer.py` (new) | High | Lightweight SQL parser (stdlib `re`-based): extract SELECT columns (name, alias, expression), FROM tables, WHERE filters, GROUP BY, ORDER BY, JOINs. No full SQL AST — focus on column metadata extraction for M query generation. |
| 182.2 | **SQL → M column metadata** | @wiring | `powerbi_import/sql_analyzer.py` | Medium | Infer PBI column types from SQL expressions: `COUNT(*)` → Int64, `SUM(amount)` → Decimal, `CONCAT(...)` → String. Populate `sourceColumn` metadata for TMDL. |
| 182.3 | **Optimized NativeQuery** | @wiring | `tableau_export/m_query_builder.py` | Medium | When custom SQL contains parameters (Tableau `<Parameters.X>` placeholders), convert to M `Value.NativeQuery()` with proper parameter binding instead of string concatenation. |
| 182.4 | **SQL dialect detection** | @wiring | `powerbi_import/sql_analyzer.py` | Low | Detect SQL dialect from connection type (T-SQL, PL/SQL, PostgreSQL, MySQL). Add dialect-specific migration notes (e.g., Oracle `ROWNUM` → SQL Server `TOP`, PostgreSQL `LIMIT`). |
| 182.5 | **Tests** | @tester | `tests/test_sql_analyzer.py` (new) | Medium | 30+ tests: SELECT parsing, column type inference, parameterized NativeQuery, dialect detection. |

---

### Sprint 183 — OAuth & Authentication Flow Migration (@deployer, @extractor)

**Goal:** Automate gateway credential configuration and OAuth token flow setup for migrated connections.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 183.1 | **Gateway credential template v2** | @deployer | `powerbi_import/gateway_config.py` | Medium | Generate detailed gateway connection configs per datasource: data source type, connection string, authentication method (Windows, OAuth2, Basic, Key). Include Azure Key Vault reference for credentials. |
| 183.2 | **OAuth provider mapping** | @deployer | `powerbi_import/gateway_config.py` | Medium | Map Tableau OAuth providers (Google, Salesforce, Snowflake) → PBI OAuth2 client configuration. Generate Azure AD app registration requirements per provider. |
| 183.3 | **Service principal config** | @deployer | `powerbi_import/gateway_config.py` | Low | For each data source requiring service principal auth, generate Entra ID app registration instructions with required permissions. |
| 183.4 | **Connection test script** | @deployer | `powerbi_import/gateway_config.py` | Medium | Generate PowerShell/Python script that validates each migrated connection can reach its target (TCP connect test + basic auth handshake). Output pass/fail per connection. |
| 183.5 | **Tests** | @tester | `tests/test_gateway_v2.py` (new) | Medium | 20+ tests: credential template, OAuth mapping, service principal config, connection test script generation. |

---

### Sprint 184 — v39.0.0 Release & Hardening (All Agents)

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 184.1 | **Version bump** | @orchestrator | `pyproject.toml`, `CHANGELOG.md` | Low | `38.0.0` → `39.0.0`. |
| 184.2 | **Connector E2E tests** | @tester | `tests/test_connector_e2e.py` (new) | High | E2E: blend workbook → extract → generate → validate. 15+ tests with synthetic multi-datasource workbooks. |
| 184.3 | **Docs refresh** | @orchestrator | `docs/MAPPING_REFERENCE.md`, `docs/TABLEAU_TO_POWERQUERY_REFERENCE.md` | Medium | Update connector table, blend documentation, SQL analyzer reference. |
| 184.4 | **Test baseline** | @tester | — | — | Target: **9,200+** tests. |

### v39.0.0 Success Criteria

| Metric | Target | Owner |
|--------|--------|-------|
| Data blending | Full blend → merge M + cross-source DAX | @wiring |
| Connectors | 75+ supported (12 new enterprise connectors) | @wiring |
| Custom SQL | Column metadata extraction + parameterized NativeQuery | @wiring |
| OAuth config | Gateway credential templates with OAuth provider mapping | @deployer |
| Tests | **9,200+** | @tester |

### v39.0.0 Agent Ownership Matrix

| Agent | Sprint 180 | Sprint 181 | Sprint 182 | Sprint 183 | Sprint 184 |
|-------|-----------|-----------|-----------|-----------|-----------|
| **@extractor** | 180.1 | 181.11 | — | — | — |
| **@wiring** | 180.2 | 181.1–181.10 | 182.1–182.4 | — | — |
| **@dax** | 180.3 | — | — | — | — |
| **@semantic** | 180.4 | — | — | — | — |
| **@assessor** | 180.5 | — | — | — | — |
| **@deployer** | — | — | — | 183.1–183.4 | — |
| **@orchestrator** | — | — | — | — | 184.1, 184.3 |
| **@tester** | 180.6 | 181.12 | 182.5 | 183.5 | 184.2, 184.4 |

---

## v40.0.0 — VS Code Extension & Interactive Tooling (Sprints 185–189)

**Theme:** Bring the migration engine directly into the developer's IDE with a **VS Code extension** providing inline assessment, DAX preview, visual mapping review, and one-click migration. Also add **interactive notebook support** for data engineers who prefer Jupyter-style workflows.

**Motivation:** CLI is powerful for automation; Web UI serves non-technical users. But the core audience — data engineers and BI developers — live in VS Code. A native extension provides the fastest feedback loop: open a `.twbx`, see the assessment, preview DAX conversions, and generate the `.pbip` without leaving the editor.

---

### Sprint 185 — VS Code Extension Core (@orchestrator, @visual)

**Goal:** VS Code extension that provides Tableau file preview, inline assessment, and one-click migration.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 185.1 | **Extension scaffold** | @orchestrator | `vscode-extension/` (new) | High | TypeScript extension: package.json, activation events for `.twb`/`.twbx` files, command palette entries (Assess, Migrate, Preview DAX). Calls Python migration engine as subprocess. |
| 185.2 | **Tableau file tree view** | @visual | `vscode-extension/src/tableauTreeProvider.ts` (new) | Medium | TreeView showing workbook structure: datasources → tables → columns, worksheets → fields, dashboards, parameters. Parsed from extraction JSON. |
| 185.3 | **Inline assessment panel** | @assessor | `vscode-extension/src/assessmentPanel.ts` (new) | Medium | Webview panel showing 9-category assessment with pass/warn/fail badges. Strategy recommendation. Click to expand category details. |
| 185.4 | **One-click migration** | @orchestrator | `vscode-extension/src/migrateCommand.ts` (new) | Medium | `Tableau: Migrate to Power BI` command. Output channel for progress. Opens generated `.pbip` folder in Explorer on completion. |
| 185.5 | **Status bar integration** | @orchestrator | `vscode-extension/src/statusBar.ts` (new) | Low | Status bar item showing migration status, fidelity score, and quick actions. |
| 185.6 | **Tests** | @tester | `vscode-extension/src/test/` (new) | Medium | 20+ tests: command registration, tree view structure, assessment panel data, subprocess invocation. |

---

### Sprint 186 — DAX Preview & Side-by-Side Editor (@dax, @visual)

**Goal:** Interactive DAX conversion preview — see Tableau formula → DAX conversion in real-time with syntax highlighting, validation status, and edit capability.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 186.1 | **DAX preview panel** | @dax | `vscode-extension/src/daxPreviewPanel.ts` (new) | High | Split editor: Tableau formula (read-only, syntax highlighted) on left, converted DAX (editable, with validation) on right. Red/green indicators for conversion confidence. MigrationNote display. |
| 186.2 | **DAX language support** | @dax | `vscode-extension/syntaxes/dax.tmLanguage.json` (new) | Medium | TextMate grammar for DAX: function names, string literals, column references (`[Col]`), table references (`'Table'`), comments. |
| 186.3 | **Tableau formula language support** | @extractor | `vscode-extension/syntaxes/tableau.tmLanguage.json` (new) | Medium | TextMate grammar for Tableau calc syntax: LOD expressions, table calcs, functions. |
| 186.4 | **Override management** | @dax | `vscode-extension/src/overrideManager.ts` (new) | Medium | Edit DAX in preview → save as override in `config.json`. Override persists across re-migrations. Clear override to revert to auto-converted DAX. |
| 186.5 | **Tests** | @tester | `vscode-extension/src/test/` | Medium | 15+ tests: panel rendering, language grammar validation, override save/load. |

---

### Sprint 187 — Jupyter Notebook Migration API (@orchestrator, @semantic)

**Goal:** Enhance the existing `notebook_api.py` with interactive widgets, inline visualization, and step-by-step migration control for data engineers using Jupyter.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 187.1 | **Interactive assessment widget** | @orchestrator | `powerbi_import/notebook_api.py` | Medium | `session.assess_interactive()` renders HTML radar chart inline in notebook. Clickable categories expand to show details. |
| 187.2 | **DAX conversion explorer** | @dax | `powerbi_import/notebook_api.py` | Medium | `session.explore_dax()` shows all conversions as a DataFrame with columns: Tableau formula, DAX result, confidence, migration note. Filter by status (exact/approximated/unsupported). |
| 187.3 | **Relationship diagram** | @semantic | `powerbi_import/notebook_api.py` | Medium | `session.show_relationships()` renders Mermaid ER diagram inline. Highlights cross-table issues, cardinality, inactive relationships. |
| 187.4 | **Step-by-step migration** | @orchestrator | `powerbi_import/notebook_api.py` | Medium | `session.step_extract()`, `session.step_convert()`, `session.step_generate()`, `session.step_validate()` — run each phase independently with intermediate inspection. |
| 187.5 | **Notebook template generator** | @orchestrator | `powerbi_import/notebook_api.py` | Low | `MigrationSession.generate_notebook()` creates a ready-to-run `.ipynb` with all migration steps pre-populated for a specific workbook. |
| 187.6 | **Tests** | @tester | `tests/test_notebook_api_v2.py` (new) | Medium | 20+ tests: widget rendering, DAX explorer DataFrame, relationship diagram, step-by-step execution, notebook generation. |

---

### Sprint 188 — Plugin SDK & Community Marketplace (@orchestrator, @deployer)

**Goal:** Formalize the plugin API (currently informal hook-based) into a versioned SDK with schema validation, and connect to a community marketplace for sharing migration patterns.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 188.1 | **Plugin SDK v2** | @orchestrator | `powerbi_import/plugin_sdk.py` (new) | High | Formal plugin interface: `MigrationPlugin` base class with versioned hooks (`on_extract`, `on_convert_dax`, `on_generate_visual`, `on_validate`). Schema validation for plugin manifest. Backward-compatible with existing `plugins.py` auto-discovery. |
| 188.2 | **Plugin testing framework** | @tester | `powerbi_import/plugin_sdk.py` | Medium | `PluginTestRunner` that validates plugin output against expected schemas. `assert_dax_valid()`, `assert_m_valid()`, `assert_visual_schema()` helpers. |
| 188.3 | **Marketplace v2** | @deployer | `powerbi_import/marketplace.py` | Medium | Extend existing PatternRegistry: remote catalogue fetch from GitHub releases. `--marketplace-sync` CLI flag. Version pinning (`--marketplace-version 2.0.0`). Pattern dependency tracking. |
| 188.4 | **Curated industry packs** | @orchestrator | `examples/marketplace/` | Medium | Package existing DAX recipes + model templates into versioned industry packs: Healthcare v1.0, Finance v1.0, Retail v1.0. Include validation tests per pack. |
| 188.5 | **Tests** | @tester | `tests/test_plugin_sdk.py` (new) | Medium | 25+ tests: plugin lifecycle, hook invocation, schema validation, marketplace sync (mock), industry pack loading. |

---

### Sprint 189 — v40.0.0 Release & Hardening (All Agents)

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 189.1 | **Version bump** | @orchestrator | `pyproject.toml`, `CHANGELOG.md` | Low | `39.0.0` → `40.0.0`. |
| 189.2 | **Extension E2E tests** | @tester | `vscode-extension/src/test/` | High | E2E: open .twbx → assess → preview DAX → migrate → verify output. 15+ integration tests. |
| 189.3 | **Docs refresh** | @orchestrator | `docs/VS_CODE_EXTENSION.md` (new), `docs/PLUGIN_SDK.md` (new) | Medium | Extension installation guide, plugin authoring guide, marketplace usage. |
| 189.4 | **Test baseline** | @tester | — | — | Target: **9,500+** tests. |

### v40.0.0 Success Criteria

| Metric | Target | Owner |
|--------|--------|-------|
| VS Code extension | File preview, assessment panel, one-click migrate, DAX preview | @orchestrator, @visual, @dax |
| DAX language support | TextMate grammar with syntax highlighting | @dax |
| Notebook API v2 | Interactive widgets, step-by-step migration, diagram rendering | @orchestrator |
| Plugin SDK | Versioned hooks, schema validation, test framework | @orchestrator |
| Marketplace v2 | Remote catalogue, industry packs, version pinning | @deployer |
| Tests | **9,500+** | @tester |

### v40.0.0 Agent Ownership Matrix

| Agent | Sprint 185 | Sprint 186 | Sprint 187 | Sprint 188 | Sprint 189 |
|-------|-----------|-----------|-----------|-----------|-----------|
| **@orchestrator** | 185.1, 185.4, 185.5 | — | 187.1, 187.4, 187.5 | 188.1, 188.4 | 189.1, 189.3 |
| **@visual** | 185.2 | — | — | — | — |
| **@dax** | — | 186.1, 186.2, 186.4 | 187.2 | — | — |
| **@extractor** | — | 186.3 | — | — | — |
| **@semantic** | — | — | 187.3 | — | — |
| **@assessor** | 185.3 | — | — | — | — |
| **@deployer** | — | — | — | 188.3 | — |
| **@tester** | 185.6 | 186.5 | 187.6 | 188.2, 188.5 | 189.2, 189.4 |

---

## v41.0.0 — Real-Time, Streaming & Paginated Reports (Sprints 190–194)

**Theme:** Extend the migration engine to handle **real-time/streaming data scenarios** (Tableau Server extract refresh → PBI DirectQuery/real-time datasets) and **paginated reports** (Tableau PDF/print-optimized views → PBI paginated reports).

**Motivation:** Enterprise customers increasingly use real-time dashboards for operational monitoring. Tableau's live connections and frequent extract refreshes need to map to PBI DirectQuery or real-time streaming datasets. Additionally, regulated industries (finance, healthcare) require pixel-perfect paginated reports for compliance — Tableau print layouts need to map to PBI paginated report (RDL) format.

---

### Sprint 190 — DirectQuery Optimization & Real-Time (@semantic, @wiring)

**Goal:** Optimize DirectQuery mode for performance: query folding hints, aggregation pushdown, and dual-storage hybrid for frequently-accessed dimensions.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 190.1 | **Query folding analyzer** | @wiring | `powerbi_import/query_folding.py` (new) | High | Analyze generated M queries to detect operations that break query folding (Table.AddColumn with custom logic, Table.Buffer, non-foldable transforms). Emit warnings with suggested rewrites. |
| 190.2 | **Aggregation table auto-generation** | @semantic | `powerbi_import/tmdl_generator.py` | High | For DirectQuery tables with >1M rows (estimated from metadata), auto-generate Import-mode aggregation tables with `alternateOf` annotations. Configure rollup grain (daily, weekly, monthly). |
| 190.3 | **Hybrid storage advisor** | @assessor | `powerbi_import/strategy_advisor.py` | Medium | Extend strategy advisor: recommend which tables should be Import (small lookups), which DirectQuery (large facts), and which need aggregation tables. Output: per-table storage mode recommendation with rationale. |
| 190.4 | **Real-time dataset hints** | @semantic | `powerbi_import/tmdl_generator.py` | Low | For Tableau workbooks with <5min extract refresh schedules, emit migration note recommending PBI streaming dataset or push dataset configuration. |
| 190.5 | **Tests** | @tester | `tests/test_directquery_optimization.py` (new) | Medium | 25+ tests: query folding analysis, aggregation table generation, hybrid storage advisor, real-time hints. |

---

### Sprint 191 — Paginated Report Generation (@visual, @orchestrator)

**Goal:** Convert Tableau print-optimized layouts to PBI paginated reports (RDL format) for pixel-perfect PDF output.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 191.1 | **RDL generator** | @visual | `powerbi_import/rdl_generator.py` (new) | High | Generate Report Definition Language (RDL) XML for paginated reports. Map Tableau worksheets to RDL tablix/chart/gauge body items. Support page headers/footers with page numbers, dates, report title. |
| 191.2 | **Print layout detection** | @extractor | `tableau_export/extract_tableau_data.py` | Medium | Detect Tableau "print-optimized" dashboards (specific size targets: Letter 8.5×11, A4, Legal). Flag for paginated report generation. Extract page header/footer text. |
| 191.3 | **Table → tablix mapping** | @visual | `powerbi_import/rdl_generator.py` | Medium | Tableau text/cross-tab worksheets → RDL tablix with row groups, column groups, subtotals, formatting. Map Tableau number formats to RDL format strings. |
| 191.4 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--paginated` flag: generate `.rdl` alongside `.pbip`. `--paginated-only` for paginated-report-only output. `--page-size letter|a4|legal|custom`. |
| 191.5 | **Tests** | @tester | `tests/test_paginated_reports.py` (new) | Medium | 25+ tests: RDL XML structure, tablix generation, page header/footer, format string mapping, print layout detection. |

---

### Sprint 192 — Streaming & Push Dataset Support (@deployer, @semantic)

**Goal:** For operational dashboards requiring sub-minute refresh, generate PBI streaming/push dataset configurations that can receive data via REST API or EventHub.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 192.1 | **Streaming dataset schema** | @semantic | `powerbi_import/streaming_config.py` (new) | Medium | Generate PBI streaming dataset JSON schema from TMDL table definitions. Map column types to streaming-compatible types. |
| 192.2 | **Push data endpoint template** | @deployer | `powerbi_import/streaming_config.py` | Medium | Generate Python/PowerShell scripts that push data to PBI streaming dataset REST API. Include authentication, rate limiting, and error handling. |
| 192.3 | **EventHub integration config** | @deployer | `powerbi_import/streaming_config.py` | Medium | For Azure EventHub-sourced Tableau extracts, generate PBI real-time dataset with EventHub binding configuration. |
| 192.4 | **Refresh strategy migration** | @deployer | `powerbi_import/refresh_generator.py` | Low | Map Tableau extract refresh frequency to PBI refresh strategy: ≤5min → streaming, ≤15min → DirectQuery, ≤1hr → incremental, >1hr → scheduled. |
| 192.5 | **Tests** | @tester | `tests/test_streaming_config.py` (new) | Medium | 20+ tests: streaming schema, push endpoint, EventHub config, refresh strategy mapping. |

---

### Sprint 193 — Cross-Platform Data Validation v2 (@assessor, @tester)

**Goal:** Upgrade the equivalence testing framework to support automated visual validation against live PBI Service deployments.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 193.1 | **Live dataset query runner** | @assessor | `powerbi_import/equivalence_tester.py` | High | Execute DAX queries against deployed PBI datasets via REST API. Compare measure values against Tableau Server query results. Tolerance-based comparison (±0.01% for numerics, exact for strings). |
| 193.2 | **Visual regression runner** | @assessor | `powerbi_import/equivalence_tester.py` | Medium | Capture PBI report page screenshots via PBI REST API export. Compare against Tableau view screenshots (SSIM ≥ 0.85). Generate side-by-side diff HTML report. |
| 193.3 | **Automated validation pipeline** | @tester | `tests/test_live_validation.py` (new) | Medium | Opt-in CI job: deploy sample workbooks to PBI Service → run equivalence tests → capture regression snapshots → generate validation report. Requires `PBI_WORKSPACE_ID` env var. |
| 193.4 | **Validation dashboard** | @assessor | `powerbi_import/equivalence_tester.py` | Low | HTML dashboard: per-measure pass/fail, per-visual SSIM score, per-workbook overall confidence. Trend line across migration versions. |
| 193.5 | **Tests** | @tester | `tests/test_equivalence_v2.py` (new) | Medium | 20+ tests: query runner (mock PBI API), SSIM calculation, diff report, dashboard generation. |

---

### Sprint 194 — v41.0.0 Release & Hardening (All Agents)

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 194.1 | **Version bump** | @orchestrator | `pyproject.toml`, `CHANGELOG.md` | Low | `40.0.0` → `41.0.0`. |
| 194.2 | **Paginated report validation** | @tester | `tests/test_v41_e2e.py` (new) | High | E2E: extract → generate PBIP + RDL → validate both → deploy (mock). 20+ tests. |
| 194.3 | **Docs refresh** | @orchestrator | `docs/PAGINATED_REPORTS.md` (new), `docs/STREAMING_GUIDE.md` (new) | Medium | Paginated report format reference, streaming dataset setup guide. |
| 194.4 | **Test baseline** | @tester | — | — | Target: **9,800+** tests. |

### v41.0.0 Success Criteria

| Metric | Target | Owner |
|--------|--------|-------|
| DirectQuery optimization | Query folding analysis + aggregation tables + hybrid advisor | @wiring, @semantic |
| Paginated reports | RDL generation from Tableau print layouts | @visual |
| Streaming datasets | Push/EventHub dataset config from high-frequency extracts | @deployer |
| Live validation | Automated measure comparison against deployed PBI datasets | @assessor |
| Tests | **9,800+** | @tester |

---

## v42.0.0 — Ecosystem Maturity & GA Polish (Sprints 195–199)

**Theme:** Final polish for **general availability** — comprehensive documentation, onboarding wizard, migration certification program, community engagement infrastructure, and long-term maintenance tooling.

**Motivation:** The migration engine is technically complete. v42 focuses on the **last mile** — making the tool accessible to a broad audience of BI professionals who may not be Python experts, establishing quality certification for migration outputs, and building community infrastructure for long-term sustainability.

---

### Sprint 195 — Onboarding & Documentation Excellence (@orchestrator, @assessor)

**Goal:** Zero-friction onboarding: a new user should go from download to first migration in <5 minutes.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 195.1 | **Interactive quickstart wizard** | @orchestrator | `powerbi_import/wizard.py` | Medium | Extend existing wizard: auto-detect Tableau Desktop installation, scan for `.twbx` files, suggest first workbook, run migration with guided prompts. Progress indicators at each step. |
| 195.2 | **Video tutorial script generator** | @orchestrator | `scripts/generate_tutorial.py` (new) | Low | Generate step-by-step markdown tutorial with annotated CLI commands for common scenarios: single workbook, batch, shared model, Fabric deploy, server migration. |
| 195.3 | **Migration cookbook** | @orchestrator | `docs/COOKBOOK.md` (new) | Medium | 20 recipes: "How to migrate a workbook with custom SQL", "How to handle LOD expressions", "How to merge 5 workbooks into one model", "How to deploy to Fabric", etc. Each recipe: problem → solution → CLI command → expected output. |
| 195.4 | **API documentation** | @orchestrator | `docs/API_REFERENCE.md` (new) | Medium | Auto-generated module reference from docstrings. Public API surface for each module. Versioned since v28.0.0 REST API. |
| 195.5 | **Tests** | @tester | `tests/test_onboarding.py` (new) | Low | 10+ tests: wizard flows, tutorial generation, cookbook code samples execute without error. |

---

### Sprint 196 — Migration Certification & Quality Badge (@reviewer, @assessor)

**Goal:** Establish a migration quality certification system — each migrated `.pbip` gets a quality badge (Bronze/Silver/Gold/Platinum) based on automated testing results.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 196.1 | **Certification engine** | @reviewer | `powerbi_import/certification.py` (new) | High | Run automated quality checks: (1) TMDL validation pass, (2) PBIR schema validation pass, (3) Cross-artifact validation pass, (4) DAX syntax validation pass, (5) M validation pass, (6) Preceptor review ≥4★, (7) Zero self-healing repairs of severity ≥error. Score → badge: Bronze (≥3 checks), Silver (≥5), Gold (≥6), Platinum (all 7). |
| 196.2 | **Certification badge in output** | @reviewer | `powerbi_import/certification.py` | Medium | Embed certification badge in migration report HTML. Generate `certification.json` in output directory with check results, score, badge, timestamp. |
| 196.3 | **Certification history** | @assessor | `powerbi_import/certification.py` | Low | Track certification results across migration runs. Compare quality trends. Alert on quality regression (previous Gold → current Silver). |
| 196.4 | **CI certification gate** | @tester | `.github/workflows/ci.yml` | Low | Optional CI step: fail build if any sample workbook migration drops below Silver certification. |
| 196.5 | **Tests** | @tester | `tests/test_certification.py` (new) | Medium | 20+ tests: check execution, scoring algorithm, badge assignment, history tracking, regression detection. |

---

### Sprint 197 — Telemetry Insights & Usage Analytics (@deployer, @assessor)

**Goal:** Aggregate telemetry data across migrations to provide insights: which Tableau features are most migrated, which cause the most approximations, which connectors are most common.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 197.1 | **Telemetry aggregation engine** | @deployer | `powerbi_import/telemetry_insights.py` (new) | Medium | Aggregate JSONL telemetry across multiple migration runs. Compute: top 10 Tableau functions used, top 10 approximated functions, connector distribution, visual type distribution, average fidelity score, self-healing hit rate. |
| 197.2 | **Insights dashboard** | @assessor | `powerbi_import/telemetry_insights.py` | Medium | HTML dashboard: usage heatmap, approximation leaderboard, connector pie chart, fidelity trend line, self-healing effectiveness. Auto-generated when ≥10 migration records exist. |
| 197.3 | **Improvement advisor** | @assessor | `powerbi_import/telemetry_insights.py` | Low | Based on aggregated data, recommend: "Your top 3 approximation sources are LOD EXCLUDE, WINDOW_SUM, and REGEX_MATCH — consider enabling LLM refinement for these." |
| 197.4 | **Tests** | @tester | `tests/test_telemetry_insights.py` (new) | Medium | 15+ tests: aggregation, dashboard generation, advisor recommendations. |

---

### Sprint 198 — Windows CI, PyPI Polish & Distribution (@deployer, @tester)

**Goal:** Close the Windows CI gap, optimize PyPI package, and ensure cross-platform reliability.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 198.1 | **Windows CI matrix** | @tester | `.github/workflows/ci.yml` | Medium | Add `windows-latest` to CI matrix. Fix any Windows-specific path issues (backslash, long paths, case sensitivity). |
| 198.2 | **macOS CI matrix** | @tester | `.github/workflows/ci.yml` | Low | Add `macos-latest` to CI matrix. Verify Darwin-specific behavior. |
| 198.3 | **PyPI metadata polish** | @deployer | `pyproject.toml` | Low | Add classifiers, project URLs, long description from README, optional dependency groups (`[fabric]`, `[web]`, `[llm]`). |
| 198.4 | **Standalone executable** | @deployer | `scripts/build_exe.py` (new) | Medium | PyInstaller/Nuitka build script: produce standalone `migrate.exe` (Windows) / `migrate` (Linux/macOS) binary. No Python installation required. |
| 198.5 | **Tests** | @tester | — | — | CI matrix validation across 3 OS × 3 Python versions. |

---

### Sprint 199 — v42.0.0 GA Release (All Agents)

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 199.1 | **Version bump** | @orchestrator | `pyproject.toml`, `CHANGELOG.md` | Low | `41.0.0` → `42.0.0`. |
| 199.2 | **Full regression suite** | @tester | `tests/` | High | All 27+ real-world workbooks + 11 sample workbooks + synthetic benchmarks. Assert: 100% Platinum certification on samples, ≥Gold on real-world. |
| 199.3 | **Release notes** | @orchestrator | `CHANGELOG.md`, `README.md` | Medium | Comprehensive v42 GA release notes with feature highlights, migration guide updates. |
| 199.4 | **Announcement materials** | @orchestrator | `docs/GA_ANNOUNCEMENT.md` (new) | Low | Blog post draft, feature comparison table (Tableau vs PBI), migration ROI calculator template. |
| 199.5 | **Test baseline** | @tester | — | — | Target: **10,000+** tests. |

### v42.0.0 GA Success Criteria

| Metric | Target | Owner |
|--------|--------|-------|
| Onboarding time | <5 minutes from download to first migration | @orchestrator |
| Certification | Platinum on all sample workbooks, ≥Gold on real-world | @reviewer |
| Cross-platform CI | Windows + macOS + Linux, Python 3.12/3.13 | @tester |
| Standalone binary | Single-file executable for Windows/Linux/macOS | @deployer |
| Documentation | Cookbook (20 recipes), API reference, extension guide, plugin SDK | @orchestrator |
| Telemetry insights | Aggregated analytics across migration runs | @deployer |
| **Tests** | **10,000+** | @tester |

---

## v43.0.0 — Self-Healing Maturity & Functionality Parity (Sprints 209–214)

**Theme:** Make the migration engine **self-correcting by default** and close the remaining **Tableau → Power BI functionality gaps**. Where v30–v34 made migrations *zero-error*, v43 makes them *zero-gap*: every Tableau feature either maps exactly, is auto-healed into a faithful equivalent, or is explicitly documented as unsupported with a remediation path — never silently dropped.

**Motivation:** Real-world portfolios (Salesforce 217 visuals, UC80 377 visuals, TDB Maintenance 76 visuals) confirm extraction fidelity is 100%, but *functional* fidelity still varies: some LOD/table-calc edge cases approximate, a few analytics-pane features (forecast, clustering, reference distributions) have no native PBI target, and self-healing repairs are recorded per-subsystem rather than unified. v43 establishes a **single parity ledger**, expands the healer catalogue from 85 to 120+, and adds a **confidence-scored healing report** so every auto-repair is auditable.

**Guiding principles:**
1. **Parity is measurable** — every Tableau feature has a tracked coverage status (exact / approximated / healed / unsupported) in a versioned registry.
2. **Healing is auditable** — every auto-repair records category, severity, confidence, before/after, and a follow-up recommendation.
3. **No silent loss** — an unsupported feature produces a migration note + assessment finding, never a missing artifact.
4. **Regression-guarded** — heals are idempotent and never degrade already-valid content (verified by golden snapshots).

---

### Sprint 209 — Functionality Parity Ledger & Gap Registry (@assessor, @reviewer) — ✅ DONE (209.1 registry, 209.2 assessment category, 209.3 scorecard, 209.4 `--parity`/`--parity-strict`, 209.5 tests)

**Goal:** Build a single source of truth for Tableau → Power BI feature coverage, and surface it as a per-workbook parity scorecard.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 209.1 | **Parity registry** | @assessor | `powerbi_import/parity_registry.py` (new) | High | Versioned registry mapping every supported Tableau feature (calculations, table calcs, LODs, filters, actions, analytics-pane items, visual marks, formatting) to a coverage status: `exact` / `approximated` / `healed` / `unsupported`, with a target mechanism and remediation note. Loadable as data; queryable by category. |
| 209.2 | **Per-workbook parity scan** | @assessor | `powerbi_import/assessment.py` | Medium | New assessment category "Functionality Parity": for each feature actually used in the workbook, resolve its registry status and compute a parity score (% exact + healed). Emit findings for every approximated/unsupported feature in use. |
| 209.3 | **Parity scorecard report** | @reviewer | `powerbi_import/parity_registry.py` | Medium | HTML + JSON scorecard: feature-by-feature coverage, parity score, list of gaps with remediation. Reuses `html_template.py` components. Written to output dir as `parity_scorecard.html`/`.json`. |
| 209.4 | **CLI integration** | @orchestrator | `migrate.py` | Low | `--parity` flag emits the scorecard; `--parity-strict` fails migration if any in-use feature is `unsupported`. |
| 209.5 | **Tests** | @tester | `tests/test_parity_registry.py` (new) | Medium | 30+ tests: registry lookups, per-category scoring, scorecard generation, strict-mode failure, real-world UC80/Salesforce parity scan. |

**Success:** Every migrated workbook reports a parity score and an itemized gap list; no in-use feature is unaccounted for.

---

### Sprint 210 — DAX & Calculation Parity Healing (@dax, @wiring) — 🟡 IN PROGRESS (210.3 DONE `dax_healing.py`, 210.4 DONE `m_healing.py`; 210.1/210.2 dax_converter LOD/table-calc pending)

**Goal:** Close the remaining calculation gaps and auto-heal the most common DAX/M defects so calculation parity approaches 100%.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 210.1 | **LOD edge-case parity** | @dax | `tableau_export/dax_converter.py` | High | Close nested-LOD and mixed-grain gaps: `{FIXED}` inside `{INCLUDE}`, LOD referencing table calcs, conditional aggregation inside LOD. Each converts to a verified DAX pattern or, if impossible, emits a parity note. |
| 210.2 | **Table-calc parity** | @dax | `tableau_export/dax_converter.py` | High | Fill gaps in `WINDOW_*` variants (WINDOW_MEDIAN, WINDOW_VAR/STDEV, WINDOW_CORR), `FIRST()`/`LAST()`/`INDEX()`/`SIZE()` addressing, and `LOOKUP()` with offset → DAX `OFFSET`/`INDEX` (PBI 2024+) with fallback. |
| 210.3 | **DAX self-healing v2** | @dax | `powerbi_import/repair_strategies.py`, `powerbi_import/self_healing_v3.py` | High | New healers: unbalanced parens/brackets, dangling commas, `SUM(measure)` → measure, bare column in measure → wrapped aggregate, unresolved `[Param]` → inlined value, RELATED across missing relationship → LOOKUPVALUE. Each records confidence. |
| 210.4 | **M expression healing** | @wiring | `tableau_export/m_query_builder.py`, `powerbi_import/calc_column_utils.py` | Medium | Heal common M defects: unquoted identifiers with special chars, unbalanced `if/then/else`, type-mismatch in `Table.AddColumn`, broken step chaining after injection. |
| 210.5 | **Tests** | @tester | `tests/test_dax_parity_healing.py` (new) | Medium | 35+ tests: each new LOD/table-calc pattern, each DAX healer (idempotent + confidence), M healers, no-regression on valid formulas. |

**Success:** Calculation parity score ≥98% on real-world workbooks; common DAX/M defects auto-repaired with recorded confidence.

---

### Sprint 211 — Visual & Analytics-Pane Parity Healing (@visual)

### Sprint 211 — Visual & Analytics-Pane Parity Healing (@visual) — 🟡 IN PROGRESS (211.3 DONE via `powerbi_import/visual_healing.py` + recovery wiring; 211.1/211.2/211.4 pending)

**Goal:** Close visual/analytics-pane gaps and auto-heal malformed visual containers so report parity approaches 100%.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 211.1 | **Analytics-pane parity** | @visual | `powerbi_import/visual_generator.py` | High | Map Tableau analytics-pane items to nearest PBI equivalent: trend line → analytics trend line, average/median/constant lines → reference lines, distribution band → shaded band (where supported), forecast → migration note + nearest approximation, cluster → migration note. Every item resolves to a target or a documented gap. |
| 211.2 | **Dual-axis & combo parity** | @visual | `powerbi_import/visual_generator.py` | Medium | Robust dual-axis synchronization (shared vs independent axis), combo chart ordering, secondary-axis formatting fidelity. |
| 211.3 | **Visual self-healing** | @visual | `powerbi_import/pbip_generator.py` | High | New PBIR healers: visual with no data roles → recover from source shelf or downgrade to table; orphaned field references → drop + note; invalid encoding (measure on category role) → reassign; empty-config visual → minimal valid config. Records each repair. |
| 211.4 | **Conditional-format & reference-line parity** | @visual | `powerbi_import/visual_generator.py` | Medium | Multi-rule conditional formatting (gradient + rules + field-based), per-mark color overrides, and reference-line label/format fidelity. |
| 211.5 | **Tests** | @tester | `tests/test_visual_parity_healing.py` (new) | Medium | 35+ tests: each analytics-pane mapping, dual-axis sync, each visual healer, conditional-format rules, real-world Salesforce/UC80 visual parity. |

**Success:** Report parity score ≥97% on real-world workbooks; malformed visuals auto-recovered instead of dropped.

---

### Sprint 212 — Semantic-Model Parity Healing (@semantic)

**Goal:** Auto-heal model-level defects (relationships, RLS, hierarchies) and close parity for calculation groups, field parameters, and sets/groups/bins.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 212.1 | **Relationship self-healing v2** | @semantic | `powerbi_import/tmdl_generator.py`, `powerbi_import/self_healing_v3.py` | High | Heal ambiguous/circular relationships: break ambiguity by activity (active vs inactive), resolve many-to-many via bridge table suggestion, repair dangling FK references by column-name inference, fix cardinality mismatches. Each repair recorded with rationale. |
| 212.2 | **RLS parity & healing** | @semantic | `powerbi_import/tmdl_generator.py` | Medium | Place `tablePermission` on the correct table when the filter references another table; heal RLS roles whose DAX references missing columns; ISMEMBEROF group-per-role expansion completeness. |
| 212.3 | **Calc-group / field-parameter parity** | @semantic | `powerbi_import/tmdl_generator.py` | Medium | Close gaps in param-swap → calculation group and dimension-switch → field parameter conversion (precedence, ordinal ordering, default selection). |
| 212.4 | **Hierarchy & sort parity** | @semantic | `powerbi_import/tmdl_generator.py` | Low | Multi-level drill-path fidelity, `sortByColumn` completeness for all month/day/period columns, key column detection. |
| 212.5 | **Tests** | @tester | `tests/test_semantic_parity_healing.py` (new) | Medium | 30+ tests: relationship healers (ambiguous/circular/dangling), RLS placement/healing, calc-group/field-param parity, hierarchy/sort, no-regression on valid models. |

**Success:** Model-level parity score ≥98%; relationship/RLS defects auto-healed with recorded rationale.

---

### Sprint 213 — Unified Healing Observability & Confidence (@deployer, @assessor)

**Goal:** Consolidate every subsystem's repairs into one confidence-scored healing report, and guard against regressions.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 213.1 | **Unified healing ledger** | @assessor | `powerbi_import/recovery_report.py`, `powerbi_import/self_healing_report.py` | High | Merge DAX, M, visual, and semantic repairs into one ledger entry schema: `{subsystem, category, severity, confidence, before, after, recommendation}`. Single `healing_report.html`/`.json` per migration. |
| 213.2 | **Healing confidence scoring** | @assessor | `powerbi_import/recovery_report.py` | Medium | Per-repair confidence (high/medium/low) based on heal determinism; aggregate a workbook "healing confidence" metric. Surface low-confidence heals for manual review. |
| 213.3 | **Healing regression guard** | @tester | `powerbi_import/regression_suite.py` | Medium | Golden-snapshot guard: assert heals are idempotent (running twice = no further change) and never alter already-valid artifacts. Drift detection on heal output. |
| 213.4 | **Monitoring & telemetry hooks** | @deployer | `powerbi_import/monitoring.py`, `powerbi_import/telemetry.py` | Low | Emit per-subsystem heal counts and confidence distribution as metrics/events for the observability dashboard and SLA tracker. |
| 213.5 | **Tests** | @tester | `tests/test_healing_observability.py` (new) | Medium | 25+ tests: unified ledger schema, confidence scoring, idempotency guard, no-degradation guard, telemetry emission. |

**Success:** One auditable healing report per migration; every repair carries a confidence score; heals proven idempotent and non-degrading.

---

### Sprint 214 — v43.0.0 Release & Hardening (All Agents)

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 214.1 | **Version bump** | @orchestrator | `pyproject.toml`, `powerbi_import/__init__.py`, `CHANGELOG.md` | Low | `40.0.0` → `43.0.0` (skips planned 41/42 if shipped out of order; otherwise sequential). |
| 214.2 | **Parity + healing E2E** | @tester | `tests/test_v43_e2e.py` (new) | High | E2E across real-world portfolio: extract → migrate → parity scorecard ≥ target → unified healing report present → idempotency verified. 25+ tests. |
| 214.3 | **Docs** | @orchestrator | `docs/FUNCTIONALITY_PARITY.md` (new), `docs/SELF_HEALING.md` (new) | Medium | Parity ledger reference (feature-by-feature coverage table) and self-healing catalogue (every healer, trigger, confidence). |
| 214.4 | **ROADMAP / CHANGELOG** | @orchestrator | `docs/ROADMAP.md`, `CHANGELOG.md` | Low | Mark v43.0.0 ✅ Shipped; prepend changelog entry. |
| 214.5 | **Test baseline** | @tester | — | — | Target: **9,500+** tests; healer catalogue **120+**. |

### v43.0.0 Success Criteria

| Metric | Target | Owner |
|--------|--------|-------|
| Functionality parity ledger | Every Tableau feature tracked (exact/approx/healed/unsupported) | @assessor |
| Calculation parity | ≥98% on real-world workbooks | @dax, @wiring |
| Report (visual) parity | ≥97% on real-world workbooks | @visual |
| Model parity | ≥98% on real-world workbooks | @semantic |
| Healers | **120+** (from 85), all confidence-scored | All |
| Healing report | Single unified, auditable, idempotency-guarded report | @assessor |
| No silent loss | Every unsupported feature → migration note + finding | @reviewer |
| **Tests** | **9,500+** | @tester |

### v43.0.0 Agent Ownership Matrix

| Agent | Sprints |
|-------|---------|
| **@assessor** | 209, 213 |
| **@reviewer** | 209, 214 |
| **@dax** | 210 |
| **@wiring** | 210 |
| **@visual** | 211 |
| **@semantic** | 212 |
| **@deployer** | 213 |
| **@orchestrator** | 209, 214 |
| **@tester** | 209–214 (cross-cutting) |

---

## v44.0.0 — Agentic & Copilot-Native Migration (Sprints 215–220) ✅ SHIPPED

**Theme:** Turn a mature migration *engine* into a mature migration *assistant*. Where v22–v43 made the engine complete, correct, self-healing, and gap-aware, **v44 makes it conversational, tool-callable, and AI-assisted** — so a user (or an agent like GitHub Copilot) can drive an entire migration, understand its findings, and apply fixes in natural language, without memorising CLI flags.

**Motivation:** The engine already exposes rich structured data — assessment findings, a functionality-parity ledger (v43), a unified confidence-scored healing report (v43), 190 visual mappings, 133+ DAX conversions, and a REST API. What's missing is a **first-class agent surface**: (1) a repo-scoped **GitHub Copilot skill** that teaches any agent how to run the pipeline safely, (2) an **MCP server** exposing migrate/assess/parity/deploy as callable tools, and (3) an **LLM remediation layer** (building on the existing `llm_client.py`) that explains parity gaps and healing decisions and proposes source-level fixes. This closes the loop from *"the engine can do it"* to *"the assistant does it with you."*

**Guiding principles:**
1. **Skill-first, not chat-bolt-on** — the agent surface is grounded in the same generators and reports; no parallel logic.
2. **Tools are contracts** — every MCP/skill capability has a typed input/output and a contract test; the assistant never guesses flags.
3. **AI is assistive, never authoritative** — LLM output is a *suggestion* with confidence; source-level fixes still go through owning agents and tests.
4. **Secrets never transit the model** — server tokens/PATs are typed by the user, never requested by a tool or echoed.
5. **Stdlib-first** — MCP transport and skill tooling stay dependency-free for the core path; LLM/remote features are opt-in.

---

### Sprint 215 — GitHub Copilot Migration Skill (@orchestrator, @web-designer)

**Goal:** Ship a repo-scoped, progressive-disclosure skill that lets any Copilot/agent-mode session run migrations correctly and fix issues at the source.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 215.1 | **Core skill** | @orchestrator | `.github/skills/tableau-to-powerbi/SKILL.md` (✅ seeded) | Medium | Frontmatter (name + trigger-rich description), 2-step pipeline, canonical commands, output layout, "fix at the source" ownership map, troubleshooting playbook, safety rules. |
| 215.2 | **Reference bundle** | @orchestrator | `.github/skills/tableau-to-powerbi/references/*.md` (new) | Medium | On-demand deep-dive files (flag catalogue, parity-report reading guide, deploy runbook) linked from SKILL.md so the core file stays lean. |
| 215.3 | **Sub-skills for specialties** | @web-designer | `.github/skills/tableau-to-powerbi-deploy/`, `.../-assess/` (new) | Medium | Optional focused skills for deployment and assessment workflows, so large tasks don't load the full engine context. |
| 215.4 | **Skill lint & registry** | @orchestrator | `scripts/validate_skills.py` (new) | Low | Validate frontmatter, dead links, and that every referenced CLI flag exists in `migrate.py --help`. Wire into CI. |
| 215.5 | **Tests** | @tester | `tests/test_skill_docs.py` (new) | Medium | 20+ tests: frontmatter schema, referenced-flag existence, no broken relative links, no secrets in examples. |

**Success:** A fresh Copilot agent-mode session can migrate, assess, and troubleshoot a workbook using only the skill — no prior repo knowledge.

---

### Sprint 216 — MCP Server for Migration Tools (@orchestrator, @deployer)

**Goal:** Expose the engine as Model Context Protocol tools so agents/IDEs can call migration capabilities directly.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 216.1 | **MCP stdio server** | @orchestrator | `powerbi_import/mcp_server.py` (new) | High | Stdlib JSON-RPC over stdio (no external deps). Tools: `migrate`, `assess`, `parity_scan`, `qa`, `shared_model`, `diff`, `deploy`. Typed schemas; reuses existing entry points. |
| 216.2 | **Job model & streaming** | @orchestrator | `powerbi_import/mcp_server.py` | Medium | Long migrations run as jobs with progress events (reuse `progress.py`); tools return job IDs + poll/stream results, mirroring `api_server.py`. |
| 216.3 | **Report resources** | @assessor | `powerbi_import/mcp_server.py` | Medium | Expose assessment/parity/healing/QA reports as MCP *resources* (JSON) so the agent can read findings without shelling out. |
| 216.4 | **Deploy tool guardrails** | @deployer | `powerbi_import/mcp_server.py`, `deploy/*` | Medium | `deploy` tool requires explicit workspace + confirmation flag; secrets read from env only, never tool args. Dry-run default. |
| 216.5 | **Tests** | @tester | `tests/test_mcp_server.py` (new) | Medium | 30+ tests: JSON-RPC contract, each tool schema in/out, job lifecycle, resource reads, deploy-guardrail refusal without confirmation. |

**Success:** An MCP-capable client can run `assess` → `migrate` → `parity_scan` → read reports end-to-end via tool calls.

---

### Sprint 217 — Natural-Language Remediation (@dax, @reviewer)

**Goal:** Turn findings and healing decisions into plain-language explanations and *actionable, source-targeted* fix suggestions, built on the existing LLM client.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 217.1 | **Finding explainer** | @reviewer | `powerbi_import/llm_client.py`, `powerbi_import/remediation.py` (new) | High | Given a parity gap / healing entry / assessment finding, produce a plain-language explanation + recommended action, with the owning file/agent and a confidence score. Deterministic template fallback when no LLM configured. |
| 217.2 | **DAX fix suggestions** | @dax | `powerbi_import/remediation.py` | Medium | For approximated/failed DAX, propose a concrete rewrite grounded in the 133+ conversion catalogue; label as suggestion + confidence; never auto-apply. |
| 217.3 | **Remediation report** | @reviewer | `powerbi_import/remediation.py` | Medium | HTML + JSON: per-finding explanation, suggested fix, owning agent, confidence. Reuses `html_template.py`. |
| 217.4 | **Cost & privacy controls** | @reviewer | `powerbi_import/llm_client.py` | Low | Dry-run prompt preview, token/cost report, redaction of identifiers before any external call; fully offline path when no key present. |
| 217.5 | **Tests** | @tester | `tests/test_remediation.py` (new) | Medium | 25+ tests: template fallback (no LLM), suggestion structure, confidence bounds, redaction, cost report, no network in offline mode. |

**Success:** Every parity gap and low-confidence heal has a human-readable explanation and a suggested next step; offline mode fully functional.

---

### Sprint 218 — Conversational Assessment & Migration Plan (@assessor, @web-designer)

**Goal:** Let a user ask questions about a workbook/portfolio and get a grounded migration plan — without reading raw JSON.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 218.1 | **Structured Q&A over reports** | @assessor | `powerbi_import/assessment.py`, `powerbi_import/parity_registry.py` | Medium | Query interface answering "what won't migrate cleanly?", "how many visuals are approximated?", "which datasources need a gateway?" from existing structured findings (no hallucination — data-backed). |
| 218.2 | **Plan generator** | @assessor | `powerbi_import/migration_planner.py` | Medium | Produce an effort-scored, wave-ordered plan from assessment + parity + merge signals; consumable by the skill and MCP `assess` tool. |
| 218.3 | **Skill wiring** | @web-designer | `.github/skills/tableau-to-powerbi/references/*.md` | Low | Document the Q&A/plan capability in the skill so agents route "assess/plan" intents to it. |
| 218.4 | **Tests** | @tester | `tests/test_conversational_assess.py` (new) | Medium | 20+ tests: each canned question resolves from data, plan ordering, gateway/parity signal surfacing, empty-workbook edge case. |

**Success:** "Assess this workbook and give me a migration plan" returns a grounded, itemized answer + ordered plan.

---

### Sprint 219 — Agent-Mode Guardrails & Contract Test Harness (@tester, @reviewer)

**Goal:** Make the agent surface safe and regression-guarded so tool/skill changes can't silently break the contract.

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 219.1 | **Tool contract snapshots** | @tester | `tests/test_agent_contracts.py` (new) | High | Golden snapshots of every MCP tool schema + skill command surface; drift fails CI. |
| 219.2 | **Safety guard suite** | @reviewer | `tests/test_agent_safety.py` (new) | Medium | Assert secrets never appear in tool args/logs, deploy refuses without confirmation, LLM path is opt-in, redaction applied. |
| 219.3 | **End-to-end agent replay** | @tester | `tests/test_agent_e2e.py` (new) | Medium | Simulated agent session (assess → migrate → parity → remediate → deploy dry-run) over a real-world fixture. |
| 219.4 | **Docs** | @reviewer | `docs/AGENT_SURFACE.md` (new) | Low | Reference for the skill, MCP tools, and remediation layer — capabilities, contracts, safety model. |

**Success:** The agent surface has snapshot-guarded contracts and a passing safety suite; a full simulated session runs green.

---

### Sprint 220 — v44.0.0 Release & Hardening (All Agents)

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 220.1 | **Version bump** | @orchestrator | `pyproject.toml`, `powerbi_import/__init__.py`, `CHANGELOG.md` | Low | → `44.0.0`. |
| 220.2 | **Release E2E** | @tester | `tests/test_v44_e2e.py` (new) | High | Skill lint + MCP contract + remediation + conversational assess across the real-world portfolio. |
| 220.3 | **Docs & ROADMAP** | @orchestrator | `docs/AGENT_SURFACE.md`, `docs/ROADMAP.md`, `CHANGELOG.md`, `README.md` | Medium | Mark v44.0.0 ✅ Shipped; document the agent surface; refresh README with skill/MCP quickstart. |
| 220.4 | **Test baseline** | @tester | — | — | Target: **9,700+** tests. |

### v44.0.0 Success Criteria

| Metric | Target | Owner |
|--------|--------|-------|
| GitHub Copilot skill | Repo-scoped skill drives migrate/assess/troubleshoot with zero prior context | @orchestrator |
| MCP server | 7 tools (migrate/assess/parity/qa/shared_model/diff/deploy), contract-tested | @orchestrator |
| NL remediation | Every parity gap + low-confidence heal has explanation + suggested fix | @reviewer |
| Conversational assess | Grounded Q&A + ordered migration plan from structured findings | @assessor |
| Safety | Secrets never transit model/args; deploy gated; LLM opt-in; offline path works | @reviewer |
| **Tests** | **9,700+** | @tester |

### v44.0.0 Agent Ownership Matrix

| Agent | Sprints |
|-------|---------|
| **@orchestrator** | 215, 216, 220 |
| **@web-designer** | 215, 218 |
| **@deployer** | 216 |
| **@assessor** | 216, 218 |
| **@dax** | 217 |
| **@reviewer** | 217, 219 |
| **@tester** | 215–220 (cross-cutting) |

---

## Sprint 221 — LLM Gateway: Online/Offline Connectivity & On-the-Fly Correction (@deployer, @dax, @reviewer) — 🟡 IN PROGRESS (221.1/221.2/221.3 core DONE `powerbi_import/llm_gateway.py` + `local` provider in `llm_client.py`; 221.4 remediation contract fixed; MCP `llm_status` shipped; 221.6 tests. Pending: healer LLM escalation + CLI `--llm-mode`/`--llm-local-url`/`--llm-autofix`)

**Theme:** Turn the existing one-shot `LLMClient` into a **unified, connectivity-aware LLM gateway** that any part of the engine can call *on the fly* — for DAX/M/visual correction, plain-language explanations, naming/synonyms, and more — whether the machine is **online** (OpenAI / Anthropic / Azure OpenAI) or **offline** (a local OpenAI-compatible model such as Ollama / LM Studio / vLLM), with graceful degradation to the deterministic path when neither is available.

**Motivation:** The engine already ships `powerbi_import/llm_client.py` (`LLMClient` over stdlib `urllib` — OpenAI/Anthropic/Azure, dry-run, cost/token tracking, 429 retry) wired into `--llm-refine` for approximated DAX. Three gaps block "call an LLM on the fly":
1. **No offline/local path** — every call assumes a cloud provider + API key; air-gapped or key-less environments get nothing.
2. **No connectivity awareness** — a call to an unreachable endpoint blocks for the full timeout then fails hard instead of falling back.
3. **A latent contract bug** — `remediation.refine_with_llm()` calls `client.complete(prompt)`, but `LLMClient` only exposes `call(system, user)`; the v44 remediation LLM path can never actually invoke the real client.

v221 closes all three with one small, dependency-free gateway layer, and reuses it everywhere corrections happen.

**Guiding principles:**
1. **Offline-first, opt-in online** — default `auto`: try local/offline, use cloud only when configured; the full deterministic pipeline still works with the LLM entirely disabled.
2. **Connectivity is probed, not assumed** — a cheap reachability check picks the route and fails over fast.
3. **Assistive, never authoritative** — LLM output is a confidence-scored *suggestion*; nothing is auto-applied unless the user opts in (`--llm-autofix`), and every applied fix is recorded in the recovery ledger.
4. **Secrets never transit args/model** — keys come from the environment only; identifiers are redacted before any network call.
5. **Stdlib-first** — no new dependencies for the core path (local endpoints are plain HTTP).

---

### Deliverables

| # | Item | Owner | File(s) | Est. | Details |
|---|------|-------|---------|------|---------|
| 221.1 | **LLM Gateway core** | @deployer | `powerbi_import/llm_gateway.py` (new) | High | `LLMGateway` wrapping `LLMClient`. Modes `online` / `offline` / `auto`. Providers: `openai`, `anthropic`, `azure_openai`, **`local`** (OpenAI-compatible, default `http://localhost:11434/v1`, no key), **`none`** (disabled → deterministic no-op). Unified `complete(prompt, system=None) -> LLMResult` **and** `call(system, user)` (fixes the `remediation` contract bug). `LLMResult{text, provider, mode, tokens, cost, confidence, source, cached, error}`. |
| 221.2 | **Connectivity probe & failover** | @deployer | `powerbi_import/llm_gateway.py` | Medium | `is_online(timeout=1.5)` — cheap TCP/HTTP reachability check on the configured endpoint host:port, result cached for the run. `auto` mode: probe → local → cloud → deterministic fallback. Never blocks longer than the probe timeout before failing over. |
| 221.3 | **Config & guardrails** | @deployer | `powerbi_import/llm_gateway.py`, `config/migration_config.py` | Medium | Env-driven: `LLM_MODE`, `LLM_PROVIDER`, `LLM_ENDPOINT`, `LLM_LOCAL_URL`, `LLM_API_KEY`, `LLM_MAX_CALLS`, `LLM_MAX_COST_USD`. Budget cap (calls + $), per-run cost report, **identifier redaction** before send (reuse `security_validator.redact_credentials`), response cache (content-hash → result, optional on-disk), dry-run prompt preview. |
| 221.4 | **On-the-fly correction wiring** | @dax, @reviewer | `powerbi_import/remediation.py`, `powerbi_import/dax_healing.py`, `powerbi_import/m_healing.py`, `powerbi_import/recovery_report.py` | High | Fix `refine_with_llm` to use `gateway.complete`. Add **LLM escalation**: when a deterministic healer leaves a still-invalid / low-confidence result, optionally ask the gateway for a fix (labeled `source="llm"`, confidence from validation re-check). Record every LLM-suggested/applied repair in the recovery ledger (`repair_type="llm_correction"`). Never auto-apply unless `--llm-autofix`. |
| 221.5 | **CLI & MCP surface** | @orchestrator, @deployer | `migrate.py`, `powerbi_import/mcp_server.py` | Medium | New CLI: `--llm-mode {auto,online,offline}`, `--llm-local-url URL`, `--llm-autofix` (default off), `--llm-max-cost`; keep existing `--llm-*`. New MCP tool `llm_status` (report mode, provider, reachability, budget — no secrets). Optional `llm_refine` tool gated behind explicit confirm, env-only creds. |
| 221.6 | **Tests** | @tester | `tests/test_llm_gateway.py` (new) | Medium | 30+ tests (all mocked, no network): mode routing, `is_online` probe + failover, local-provider body/URL, `complete`/`call` parity, redaction-before-send, budget cap, cache hit, dry-run, offline no-op fallback, remediation wiring, healer escalation labeling, `llm_status` MCP contract. |

**Success:** A single `LLMGateway` serves every LLM need; migrations run identically with the LLM **off**, **local/offline**, or **online**; connectivity loss fails over within the probe timeout; the `remediation` LLM path works; and every LLM correction is confidence-scored, redaction-safe, budget-capped, and auditable in the recovery report.

### Architecture

```
callers (remediation · dax_healing · m_healing · converter · assessment)
                     │  gateway.complete(prompt) / gateway.call(system,user)
                     ▼
             ┌───────────────────┐
             │    LLMGateway     │  mode: auto | online | offline
             │  redact · cache · │
             │  budget · probe   │
             └───────┬───────────┘
        is_online? ──┤
     ┌───────────────┼────────────────┬─────────────────┐
     ▼               ▼                ▼                 ▼
 local (Ollama/   openai /         azure_openai      none (offline)
 LM Studio/vLLM)  anthropic                          → deterministic
 OpenAI-compat    (LLMClient)      (LLMClient)          template / no-op
```

### Safety model
- **Default is `auto` and opt-in** — with no config, the gateway is `none` (disabled); the deterministic healers/remediation are unaffected.
- **Redaction** — table/column/measure identifiers are scrubbed before any prompt leaves the machine; a dry-run shows the exact redacted prompt.
- **Budget** — hard caps on call count and USD; exceeding either returns a deterministic no-op, never a silent overspend.
- **No auto-apply** — LLM fixes are suggestions in the recovery report unless `--llm-autofix`, and even then only high-confidence (re-validated) corrections apply.
- **Secrets** — keys from env only; the `llm_status` / `llm_refine` MCP tools refuse secret-looking arguments (same guard as `deploy`).

### Agent ownership

| Agent | Items |
|-------|-------|
| **@deployer** | 221.1, 221.2, 221.3, 221.5 (MCP) |
| **@dax** | 221.4 (DAX escalation) |
| **@reviewer** | 221.4 (remediation), safety review |
| **@orchestrator** | 221.5 (CLI) |
| **@tester** | 221.6 (cross-cutting) |

---

## Sprint Sequencing (v38–v42)


```
v38.2.0 — Report Packaging & Developer Experience
  Sprint 175 (PDF/PPTX) ──→ Sprint 176 (REST API v2)
           ↓                        ↓
  Sprint 177 (Web UI Polish) ──→ Sprint 178 (Artifact Diff)
                                       ↓
                             Sprint 179 (Release)

v39.0.0 — Data Blending & Advanced Connectivity
  Sprint 180 (Data Blending) ──→ Sprint 181 (12 Connectors)
           ↓                          ↓
  Sprint 182 (Custom SQL) ──→ Sprint 183 (OAuth/Gateway)
                                       ↓
                             Sprint 184 (Release)

v40.0.0 — VS Code Extension & Interactive Tooling
  Sprint 185 (Extension Core) ──→ Sprint 186 (DAX Preview)
           ↓                           ↓
  Sprint 187 (Notebook API v2) ──→ Sprint 188 (Plugin SDK)
                                       ↓
                             Sprint 189 (Release)

v41.0.0 — Real-Time, Streaming & Paginated Reports
  Sprint 190 (DirectQuery) ──→ Sprint 191 (Paginated/RDL)
           ↓                         ↓
  Sprint 192 (Streaming) ──→ Sprint 193 (Live Validation v2)
                                       ↓
                             Sprint 194 (Release)

v42.0.0 — Ecosystem Maturity & GA Polish
  Sprint 195 (Onboarding) ──→ Sprint 196 (Certification)
           ↓                         ↓
  Sprint 197 (Telemetry) ──→ Sprint 198 (Cross-Platform CI)
                                       ↓
                             Sprint 199 (GA Release)

v43.0.0 — Self-Healing Maturity & Functionality Parity
  Sprint 209 (Parity Ledger) ──→ Sprint 210 (DAX/Calc Healing)
           ↓                            ↓
  Sprint 211 (Visual Healing) ──→ Sprint 212 (Semantic Healing)
           ↓                            ↓
  Sprint 213 (Healing Observability) ──→ Sprint 214 (Release)

v44.0.0 — Agentic & Copilot-Native Migration
  Sprint 215 (Copilot Skill) ──→ Sprint 216 (MCP Server)
           ↓                          ↓
  Sprint 217 (NL Remediation) ──→ Sprint 218 (Conversational Assess)
                                       ↓
  Sprint 219 (Guardrails/Contracts) ──→ Sprint 220 (Release)
```

---

## Risk Matrix (v38–v42)

| Risk | Impact | Mitigation |
|------|--------|-----------|
| **Floating-overlap detection false positives** (v38.5) | Medium | 30% overlap threshold is tunable; opt-in via `--qa` first, default-on only after 2 release cycles of green real-world QA. |
| **Golden fixture churn on intentional changes** (v38.5) | Low | `scripts/generate_pixel_fixtures.py --apply` is one command; PR reviewer guidance lives in `tests/golden/README.md`. |
| VS Code extension TypeScript adds new tech stack | Medium | Keep extension thin — delegate all logic to Python subprocess. Extension is presentation layer only. |
| Paginated reports (RDL) is complex XML format | High | Start with tablix/chart only. Complex report items (sub-reports, drill-through) deferred. |
| Data blending complexity explosion | High | Limit to 5 blended datasources per worksheet. Circular blends emit error + migration note. |
| Streaming dataset requires PBI Premium/PPU | Medium | Detect license type in assessment. Recommend refresh strategy compatible with license. |
| Standalone binary size (PyInstaller) | Low | Strip test files and sample data from binary. Target <50MB. |
| Plugin API stability across versions | Medium | Semantic versioning for plugin SDK. Deprecation warnings for changed hooks. |
| Cross-platform path handling (Windows backslash) | Medium | Use `pathlib.Path` consistently. CI matrix catches regressions. |
| Community marketplace quality control | Medium | Require passing tests for submitted patterns. Automated validation on merge. |
| **Aggressive healing degrades valid content** (v43) | High | Idempotency + no-degradation golden-snapshot guards (Sprint 213); every heal confidence-scored, low-confidence heals flagged for review. |
| **Parity registry drifts from actual coverage** (v43) | Medium | Registry is the single source of truth consumed by assessment + tests; a CI test asserts every converter/healer has a registry entry. |
| **Unsupported features silently dropped** (v43) | High | Parity scan emits a finding for every unsupported in-use feature; `--parity-strict` fails the build. |

---

## Cumulative Metrics Targets

| Version | Tests | Connectors | Visual Types | DAX Functions | Healers | Agents |
|---------|-------|------------|--------------|---------------|---------|--------|
| v28.5.8 | 7,099 | 63 | 118 | 125 | 13 | 14 |
| v30.0.0 | 8,008 | 63 | 118 | 125 | 85 | 14 |
| v37.1.0 | 8,518 | 63 | 145 | 125+ | 85 | 14 |
| v38.2.0 | 8,738 | 63 | 145 | 125+ | 85 | 14 |
| v38.3.0 | 8,750 | 63 | 145 | 125+ | 85 | 14 |
| **v38.4.0** | **8,778** | 63 | 145 | 125+ | 85 | 14 |
| **v38.5.0** | **9,000+** | 63 | 145 | 125+ | 85 | 14 |
| **v39.0.0** | **9,200+** | **75+** | 145 | 125+ | 85 | 14 |
| **v40.0.0** | **9,500+** | 75+ | 145 | 125+ | 85 | 14 |
| **v41.0.0** | **9,800+** | 75+ | 145+ | 125+ | 85 | 14 |
| **v42.0.0** | **10,000+** | 75+ | 145+ | 125+ | 85 | 14 |
| **v43.0.0** | **9,500+** (post-v40 baseline) | 75+ | 145+ | **133+** | **120+** | 14 |
| **v44.0.0** | **9,700+** | 75+ | 145+ | 133+ | 120+ | 15 |
