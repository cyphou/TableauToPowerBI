<p align="center">
  <a href="../README.md"><img src="images/logo-migration-dashboards.png" alt="Tableau to Power BI project overview" width="104"/></a>
</p>

# Documentation

**Find the right guide, run locally, and review the evidence.** This hub groups
the maintained Tableau to Power BI documentation by the task you want to finish.

[🏠 Project overview](../README.md) · [🌐 Local browser](LOCAL_WEB_UI.md) · [✅ Review checklist](MIGRATION_CHECKLIST.md) · [⚠️ Limitations](KNOWN_LIMITATIONS.md) · [📈 Roadmap](ROADMAP.md)

![Extract metadata, convert formulas, generate editable projects, run locally, review evidence and publish separately](images/features.svg)

## Start here

| Your goal | Read first | Continue with |
|---|---|---|
| 🌐 Migrate with the guided interface | [Local browser UI](LOCAL_WEB_UI.md) | Start `.\run_web_ui.ps1`; enter separate local source/output folder paths |
| 💻 Automate a folder of workbooks | [Operator manual](USER_MANUAL.md) | `python migrate.py batch .\workbooks --output-dir .\output`; a one-workbook folder is valid |
| 🗄️ Choose Import or DirectQuery | [Source-mode decision diagram](ARCHITECTURE.md#automatic-storage-mode-and-hyper-export) | Review Hyper export rules and the explicit Import override |
| 🔎 Check the generated result | [Review checklist](MIGRATION_CHECKLIST.md) | Configure sources, verify refresh, save/reopen, then compare values and visuals |
| 🧰 Add Microsoft's static PBIR check | [Optional authoring validation](POWERBI_PROJECT_GUIDE.md#optional-microsoft-pbir-validation) | Validate the generated `.Report` separately; no Node/MCP dependency is added to core migration |
| 🚀 Publish with authorization | [Deployment guide](DEPLOYMENT_GUIDE.md) | Preview separately; generation does not deploy or prove runtime readiness |

The local browser runner, the optional Streamlit/upload interface, and the REST
upload API are separate surfaces. The standalone Windows desktop application
and executable packaging have been removed; PowerShell remains a browser
launcher, and Power BI/Tableau Desktop remain optional diagnostic prerequisites.

## Operator guides

| Guide | What it covers |
|---|---|
| 📘 [User manual](USER_MANUAL.md) | Workflows, validation states and Tableau ↔ Power BI correspondence |
| 🌐 [Local browser UI](LOCAL_WEB_UI.md) | Launcher, folder paths, diagnostics and troubleshooting |
| ✅ [Review checklist](MIGRATION_CHECKLIST.md) | Post-generation refresh, security and release checks |
| 📦 [Power BI project guide](POWERBI_PROJECT_GUIDE.md) | PBIP layout, source configuration and save/reopen |
| ❓ [FAQ](FAQ.md) | Common questions and actionable refresh guidance |
| ⚠️ [Known limitations](KNOWN_LIMITATIONS.md) | Unsupported features, approximations and runtime boundaries |
| 🧩 [Tableau versions](TABLEAU_VERSION_COMPATIBILITY.md) | File formats and version-validation scope |
| 🚀 [Deployment guide](DEPLOYMENT_GUIDE.md) | Separately authorized publishing and client-side native checks |
| ☁️ [Tableau Server/Cloud](SERVER_MIGRATION_GUIDE.md) | Server migration and optional official Tableau MCP evidence |
| 🏢 [Enterprise guide](ENTERPRISE_GUIDE.md) | Portfolio assessment, migration waves, governance and handoff |
| 🔐 [Upload security](UPLOAD_SECURITY.md) | Uploaded-source confinement, distinct from trusted local paths |

### Source strategy at a glance

| Source | PBIP behavior | Data handling |
|---|---|---|
| Hyper + eligible explicit original database in the same datasource | **Original database**, DirectQuery where supported or Import | No Hyper data read/export; complete native binding required; no runtime or snapshot-parity guarantee |
| Retained Hyper at most 10 MiB, uncompressed | **Import + CSV** | Export the Hyper snapshot |
| Retained Hyper strictly above 10 MiB, uncompressed | **Import + native Parquet** | Requires the optional Hyper SDK; failures stay explicit |
| No Hyper | **DirectQuery where supported**, otherwise Import | No native row export; explicitly packaged Excel is copied unchanged into `Data/` and bound by absolute path. Loose native files and packaged non-Excel files are not copied |

The user can always select **Import** instead of automatic DirectQuery.
Power BI reads native Import rows at refresh, after source access is configured.
The packaged-Excel exception covers trusted local per-workbook batch generation,
not uploaded jobs or shared-model/add-to-model workflows.
Fabric keeps its separate staging contract. See the
[decision diagram and exceptions](ARCHITECTURE.md#automatic-storage-mode-and-hyper-export).

## Presentations: architecture and business value

Two French decks, edition **26 October 2026**, use editable PowerPoint diagrams
and speaker notes with source references. Their source-policy slide includes
database-first selection and the packaged-Excel exception; source eligibility,
snapshot-parity and absolute-path/privacy limitations are in the
[source architecture](ARCHITECTURE.md#automatic-storage-mode-and-hyper-export).

| Deck | Audience and content |
|---|---|
| [Vision and business case — 15 slides](https://github.com/cyphou/TableauToPowerBI/blob/main/docs/Tableau_to_PowerBI_Migration.pptx) | Value proposition, workflow, source policy, dated evidence, illustrative time savings, TCO and pilot acceptance |
| [Architecture and workflows — 23 slides](https://github.com/cyphou/TableauToPowerBI/blob/main/docs/Tableau_to_PowerBI_Migration_Guide.pptx) | Two-step pipeline, PBIP/TMDL/PBIR, Hyper decision diagram, shared models, Prep, Fabric, quality gates, agent ownership and optional Microsoft tooling |

The links target the repository's published `main` branch; local regenerated
copies live alongside this guide. The minimal HTML publisher does not copy
PowerPoint packages. Until a refresh is committed and pushed, the local files
are the updated edition, not the published branch.

### Regenerate the decks

Presentation authoring requires the **optional** `python-pptx` package, tested
with **1.0.2**. It is not a core migration dependency.
From the repository root:

```powershell
python -m pip install "python-pptx==1.0.2"
python docs\generate_pptx.py --deck both

# One deck, custom destination:
python docs\generate_pptx.py --deck overview --output .\output\business-case.pptx

# Compatible technical-guide entrypoint:
python scripts\generate_presentation.py --output .\output\technical-guide.pptx
```

The [canonical generator](../docs/generate_pptx.py) owns both decks; the
[compatibility entrypoint](../scripts/generate_presentation.py) defaults to the
technical guide. `--output-dir` changes the destination for the two standard
filenames; `--output` selects one file and cannot be combined with `--deck both`.

### Commercial assumptions, not a productivity benchmark

The model assumes **100 reports**, **16 manual hours/report**, **80 hours of
setup**, a loaded rate of **€100/hour**, and eight-hour person-days. Assisted
effort includes human review, remediation and validation.

| Illustrative scenario | Assisted hours/report | Total hours, including setup | Hours saved | Effort reduction | Labor-equivalent value |
|---|---:|---:|---:|---:|---:|
| Prudent | 8 | 880 | 720 | 45% | €72,000 |
| Central | 5 | 580 | 1,020 | 63.75% | €102,000 |
| Ambitious | 3 | 380 | 1,220 | 76.25% | €122,000 |

`Hours saved = reports × manual hours − (setup + reports × assisted hours)`.
These are **illustrative capacity values**, not measured time savings,
guaranteed cash savings, elapsed delivery times or net ROI. Deduct additional
costs not already counted in assisted labor: licensing/capacity, storage,
gateways, integration, training and operations. Do not subtract the same setup
or labor twice. A comparative ROI uses
`(manual cost − total assisted cost) / total assisted cost`, with the same
scope and time horizon. Calibrate the assumptions on a representative pilot.

The decks separately label the **23 October 2026** local tests and ten-report
Microsoft static-validation results. Those observations do not measure
productivity or establish model execution, Desktop opening, refresh, rendered
parity or deployment.

## Conversion references

| Reference | What to look up |
|---|---|
| 🗺️ [Mapping reference](MAPPING_REFERENCE.md) | 164 visual mapping records, formulas and interactions |
| 🧮 [Tableau → DAX](TABLEAU_TO_DAX_REFERENCE.md) | Conversion patterns, approximations and manual cases |
| 🔌 [Tableau → Power Query M](TABLEAU_TO_POWERQUERY_REFERENCE.md) | Source properties and connectors; 97 dispatch keys route to 55 generators |
| 🔄 [Tableau Prep → M](TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md) | Prep patterns and the bounded producer-dialect/parser contract |

## Developer and project references

| Guide | What it covers |
|---|---|
| 🏗️ [Architecture](ARCHITECTURE.md) | Extraction/generation, entry points and validation layers |
| 🤖 [Agent architecture](AGENTS.md) | Specialist ownership, handoffs and coordination |
| 🛠️ [Agent-facing tools](AGENT_SURFACE.md) | MCP, evidence, remediation and owner-pull handoff |
| 🧩 [Plugin SDK](PLUGIN_SDK.md) | Versioned API and legacy-hook adapters |
| 💻 [VS Code extension](VS_CODE_EXTENSION.md) | Setup and current CLI integration limits |
| 📈 [Roadmap](ROADMAP.md) | Current priorities, release gates and dated evidence |
| 🧭 [Evolution plan](FULL_MIGRATION_EVOLUTION_PLAN.md) | Long-term direction, not a declaration of delivered parity |
| 🤝 [Contributing](../CONTRIBUTING.md) | Development setup and review workflow |
| 🧪 [Test guide](../tests/README.md) | Test commands and evidence boundaries |
| 📝 [Changelog](../CHANGELOG.md) | Unreleased changes and historical release notes |

- Module guides: [extraction](../tableau_export/README.md) and [generation](../powerbi_import/README.md)
- Public fixtures: [sample workbooks](../examples/tableau_samples/README.md), [real-world sources](../examples/real_world/SOURCES.md) and [golden snapshots](../tests/golden/README.md)
- Plugin examples: [legacy hooks](../examples/plugins/README.md)

Package metadata currently declares `44.0.0`; the roadmap's v48 track and
historical changelog entries are not package-release or runtime certification.
Use [CI](https://github.com/cyphou/TableauToPowerBI/actions) for current test
results rather than treating historical totals as live counts.
Local badges describe metadata and formats, not a release, refresh, parity or
deployment verdict. The root README's CI badge reports the published `main`
branch, not uncommitted local changes.

### Build the documentation locally

From the repository root:

```powershell
python .github\scripts\build_docs.py docs .\build\docs
python -m pytest tests\test_build_docs.py -q
```

Open `build\docs\index.html` to inspect the generated pages. The standard-library
publisher copies only PNG, JPEG, GIF, WebP and strictly validated passive SVG
assets from `docs\images`. It rejects symlinks, Windows junctions, unsupported
file types and unsafe SVG content with a file-specific build error. SVGs may
use static geometry, text, accessibility metadata, gradients and local
`url(#id)` references, but not scripts, event handlers, stylesheets, animation,
external resources or embedded HTML. The maintained hero, feature, pipeline,
architecture and badge illustrations retain their authored metadata and local
references. The publisher preserves bounded presentation HTML and rewrites
sibling guide links to HTML. Links to repository source files outside
the documentation point to the published `main` branch on GitHub. This is a
minimal Markdown publisher, not a complete GitHub Flavored Markdown renderer;
Mermaid blocks remain code in these pages. GitHub renders the Markdown sources
and Mermaid separately.

> [!NOTE]
> Static quality, native refresh, saved/reopened state, value/visual comparison
> and deployment each need their own evidence. Unexecuted runtime checks remain
> `not_run`; see the [review checklist](MIGRATION_CHECKLIST.md).

## Quick Reference

### CLI Commands

```bash
python migrate.py batch ./workbooks --output-dir /tmp/out
python migrate.py assess file.twbx
python migrate.py batch dir/ --output-dir /tmp/out
python migrate.py server https://tableau.example "Sales Dashboard"
python migrate.py merge wb1.twbx wb2.twbx
python migrate.py fabric dir/ --output-dir /tmp/fabric-out
# Deploy generated projects separately with the MCP deploy tool (preview first).
python migrate.py qa ./workbooks
python migrate.py quality file.twbx
python migrate.py parity file.twbx
python migrate.py portfolio ./workbooks
python migrate.py plan https://tableau.example
python migrate.py lineage ./prep_flows
python migrate.py package ./workbooks
```

Use `python migrate.py quality file.twbx` after migrating the workbook to
inspect the combined assessment, parity, data, interface, and openability report.
`plan` requires a Tableau Server/Cloud URL. Add `--quality-policy report|enterprise|production`
to control escalation of static semantic diagnostics and unresolved lineage.
Use `python migrate.py --help` for the
concise command list. Supported flag-based automation remains available through
batch mode; direct single-file project generation has been retired.
`python migrate.py --advanced-help` lists advanced and legacy options.

Semantic-context checks for converted LOD expressions are available through
`powerbi_import.semantic_execution_validator`. They are static diagnostics only;
the optional `powerbi_import.semantic_runtime` adapter accepts an injected
executor and records bounded DAX query evidence as `not_run`, `passed`, or
`failed`. The unified quality report continues to label live semantic execution
as `not_run` until an authorized execution environment supplies evidence.

Quality-surface coverage is explicit: the file-based diagnostic CLI and MCP
`quality_report` tool run the unified report. The `qa FOLDER` shortcut follows
batch migration and does not invoke the legacy specialized QA report card; the
legacy `--qa` flag is retained but does not invoke that report card in batch mode.
Generation is batch-only; one-workbook migrations use a folder containing just
that workbook. Batch migration writes unified quality reports by default.
Notebook workflows expose their own migration/assessment operations and do not
implicitly run the unified quality report for every item.

In an MCP session, an owning agent can then call `agent_handoff` with its own
agent name to pull only its matching quality priorities. The packet is
read-only. The separate `agent_handoff_ack` tool accepts `not_applicable` with
a rationale, or `applied` only after PBIP definition files change and a fresh
quality report no longer contains the same finding. ACKs remain in the MCP
process memory; they are not durable resolution history.

Notebook sessions can run the same report explicitly after generation:
`session.quality_report()`. It writes JSON and HTML evidence beside the generated
project and keeps live semantic execution marked `not_run` until an authorized
runtime is used.

For local browser setup and launch options, see the
[Local browser UI guide](LOCAL_WEB_UI.md).

Desktop probe results are evidence gates: a successful launch can produce
`DESKTOP_SMOKE_PASS`, while a crash, timeout, or probe error downgrades the
confidence level to `UNVERIFIED` even when static validation passed. Two
successful consecutive launches produce `DESKTOP_REOPEN_PASS`; this still does
not claim that Desktop saved project changes. The batch opening service also
records `OPENED`/`CRASHED` and a separate model data status; only `OPENED` plus
`verified` qualifies as a data-bearing screenshot. Its HTML/JSON/PNG evidence
must remain outside the repository for customer reports.

The migration-level zero-touch tracker (`ZeroTouchTracker` in
`powerbi_import/feedback_loop.py`) uses the unified quality status when recording
history: blockers are `quality_blocker` and cannot count as successful
migrations. This is distinct from preceptor coaching: that review writes a
report, but does not dispatch fixes to owning agents. Batch runs use the same
zero-touch classification for each workbook.

Authenticated Tableau Server evidence also includes an operational risk level
and reasons, so dependency and refresh complexity can influence migration
planning without turning unavailable API data into a false pass.

The control-plane foundations are available as `evidence_manifest` for generated
Fabric metadata, `probe_desktop_reopen()` for repeated Desktop loading, and
`build_server_lineage_graph()` for normalized portfolio dependencies.

Unified quality reports also include the versioned `evidence_manifest`, keeping
CLI, batch, MCP, Notebook, and Fabric outputs on the same provenance contract.

### Project Structure

| Module | Purpose |
|--------|---------|
| `migrate.py` | CLI entry point, batch support, logging |
| `tableau_export/` | Tableau XML parsing, DAX conversion, Power Query M generation |
| `powerbi_import/` | .pbip generation, TMDL, visuals, validation, deployment |
| `web/`, `run_web_ui.ps1` | Local browser assets, loopback runner and launcher; separate optional upload UI |
| `tests/` | Unit, integration, and regression tests; see CI for current results |
| `artifacts/` | Generated .pbip projects |
| `.github/workflows/` | CI lint/test/validation and GitHub Pages documentation publishing; service deployment jobs in `ci.yml` are disabled |
