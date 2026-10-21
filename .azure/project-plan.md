# Project Plan

**Status**: Planning
**Created**: 2026-10-21
**Mode**: AUGMENT

---

## 1. Project Overview

**Goal**: Finish and validate the existing Tableau-to-Power BI website so users can select local folders, run the existing migration engine, follow real job status, and open generated projects and evidence entirely on their own computer. Each module remains independently testable.

**App Type**: Static + API, served together by one local Python process.

**API Login**: No

**Mode**: AUGMENT

**Deployment Plan**: No deployment plan. Local execution only; no Azure or other cloud services.

The user confirmed **offline localhost**, not a backend-free static page. The browser is the interface; the existing Python engine performs conversion. Opening the production HTML directly with `file://` is not the supported launch method. The `.azure` directory is only the requested planning-artifact location.

**Existing baseline to reuse**

- [Browser interface](../web/index.html), [client behavior](../web/app.js), and [stylesheet](../web/style.css): French-language configuration, migration, optional native diagnostics, job history, logs, artifact links, and ZIP downloads.
- [Local runner](../web/local_server.py): standard-library HTTP server, loopback binding, fixed migration commands, job tracking, artifact confinement, and a native folder-picker endpoint.
- [Local web launcher](../run_web_ui.ps1): starts the browser-based runner at `http://127.0.0.1:8765/`, optionally without opening the browser.
- [Focused tests](../tests/test_local_web_ui.py): command construction and real loopback HTTP coverage without launching Desktop applications.
- [Migration engine](../migrate.py): folder-based generation, including folders containing only one workbook.

The browser, runner, launcher, and tests currently exist as working-tree files. Preserve and integrate them; do not overwrite them or create a second server. The standalone desktop application and its Windows executable packaging are not part of this product. Preserve the migration CLI, browser interface, local folder picker, and optional Streamlit interface.

**Included scope**

1. One local, French-language migration workspace using the existing HTML/CSS/JavaScript and Python architecture.
2. Source and output folder fields, with visible manual entry and Browse buttons wired to the existing local folder picker.
3. Batch-only generation using existing options: Power BI or Fabric artifact output, model culture, calendar years, quality policy, and resume.
4. Real queued/running/completed/error states, bounded technical logs, job history, generated report links, and output ZIP download.
5. Clear startup, connection, input-validation, unavailable-feature, and subprocess-error messages.
6. Separate, explicitly confirmed native diagnostics; never launch Tableau or Power BI as a side effect of ordinary migration.
7. Offline launch and usage instructions, plus focused regression and browser acceptance checks.
8. Keep the local site browser-first: do not add a standalone desktop UI or packaged executable. Retain the PowerShell script only as an optional shortcut for starting the website.

**Offline boundary**

- Website assets, API traffic, input files, output files, and normal migration processing stay local. No CDN, remote font, account, cloud provisioning, online AI, deployment, or remote Tableau source is required.
- Browser requests are same-origin to the loopback runner. No LAN binding or remote hosting is introduced.
- Generating M queries and connection metadata for an external datasource does not refresh that datasource or prove its availability. Live connection, refresh, and semantic-runtime checks remain explicitly `not_run` unless separately and deliberately executed.
- Native diagnostics can interact with installed applications and their connections. They are outside the guaranteed offline generation path, remain opt-in, and must not be described as offline-safe refresh operations.
- Hyper readers and Desktop applications are optional capabilities, not hidden installation steps. If a required local reader is unavailable, report that limitation explicitly; do not substitute fake data or claim successful extraction.
- Core startup and generation must not install packages, download resources, send telemetry, or invoke optional online services. Verify the real execution path with external networking unavailable; resolve any discovered dependency before claiming offline readiness.

**Out of scope**

- Porting the converter to browser JavaScript or WebAssembly.
- Azure, Fabric/Power BI Service deployment, server-source migrations, cloud credentials, online AI, or scheduled refresh.
- A new frontend framework, npm toolchain, database, Docker environment, service worker, or second migration backend.
- New per-job cancellation or permanent job-history storage. Closing a browser tab does not cancel a job; stopping the launcher stops the local runner and its managed processes.

---

## 2. Backend — Offline Local Runner

| Component | Technology |
|-----------|-----------|
| **Language** | Python |
| **Runtime** | CPython 3.12+, matching the existing migration engine |
| **Package Manager** | None for core runtime; optional development dependencies remain repository-managed |
| **Test Runner** | Existing pytest runner with unittest-based local UI tests |
| **Mocking Library** | unittest.mock |
| **Test Command** | `python -m pytest tests\test_local_web_ui.py -q` |
| **Orchestration** | Existing PowerShell launcher; one Python process, no containers |

Reuse [the local runner](../web/local_server.py), not the optional Streamlit wizard or the upload API. The browser sends local folder paths; it does not upload workbook folders.

Preserve the existing API contract, loopback and same-origin request guards, fixed argument lists, `shell=False` execution, input validation, output-directory confinement, and sandboxing of generated HTML reports.

Keep existing limits rather than introducing new capacity policy: one active job or folder dialog, 30 retained jobs, 2,000 retained log lines per job, 1,000 listed artifacts, a 64 KiB request limit, and the existing ZIP byte/file limits. Display truncation and artifact-enumeration errors explicitly.

The folder picker must not start while a job is active. User cancellation returns `path: null`; picker unavailability produces a visible message and leaves manual path entry available.

Use actual exit status and errors for completion. Static validation, Desktop opening, datasource refresh, screenshot comparison, and semantic execution must remain distinct evidence states; a generated ZIP is not proof of runtime parity.

---

## 3. Frontend — Local Browser Workspace

| Component | Technology |
|-----------|-----------|
| **Language** | JavaScript |
| **Framework** | Existing semantic HTML5, CSS3, and vanilla JavaScript |
| **Package Manager** | None |
| **Test Runner** | Existing pytest HTTP coverage plus real-browser acceptance checks |
| **Mocking Library** | unittest.mock for runner boundaries; controlled local responses for browser failure scenarios |
| **Test Command** | `python -m pytest tests\test_local_web_ui.py -q` plus the browser acceptance checklist below |

Serve the current frontend and API from the same loopback origin. Preserve existing labels, field identifiers, quality options, non-overlapping polling/submission, and the rule that an ambiguous POST is never automatically retried.

Wire Browse controls to `POST /api/pick-folder`; keep editable absolute-path fields and do not persist folder paths or session values in browser storage. Validate required paths and calendar bounds before submission and again on the server.

Represent progress using real job states and log events. Do not invent a completion percentage, a successful Desktop check, or a completed comparison.

Retain a keyboard-accessible native-diagnostics confirmation dialog. Missing applications, unsupported actions, and offline-only limitations must produce an explanation and `not_run` where appropriate, rather than a success-shaped fallback.

---

## 4. Services Required

**Cloud services: none.** The cloud-service table is intentionally empty.

| Azure Service | Role in App | Environment Variable | Default Value (Local) | Classification |
|---------------|-------------|----------------------|-----------------------|----------------|

Local components are:

- **Essential:** CPython, the loopback runner, the browser, the existing migration engine, and readable/writable local folders.
- **Enhancement:** native folder browsing; manual path entry remains available if its GUI dependency is unavailable.
- **Enhancement:** separately confirmed Desktop diagnostics and optional installed Hyper support. Their absence must not prevent startup or ordinary supported local-file workflows.

No cloud environment variables, datastore, credentials, emulators, or provisioning steps are required.

---

## 5. Prerequisites

Inventory performed on 2026-10-21. These are detected tools, not application-validation results.

### Run

| Tool | Service(s) | Installed | Version |
|------|------------|-----------|---------|
| CPython 3.12+ | Backend | ✅ | 3.13.15 |
| Microsoft Edge | Frontend | ✅ | 155.0.4283.62 |
| PowerShell, for the Windows launcher | Backend | ✅ | 7.6.7 |

Users can instead start the runner directly with Python. Node.js, npm, Docker, Azure CLI, and an internet connection are not runtime prerequisites.

### Debug

| Tool | Service(s) | Installed | Version |
|------|------------|-----------|---------|
| Existing pytest installation | Backend, frontend HTTP contracts | ✅ | 9.1.1 |
| Microsoft Edge DevTools | Frontend | ✅ | Bundled with Edge 155.0.4283.62 |

Use the current development environment. No new debugger extension, package, container, or tooling installation is part of this plan.

---

## 6. Design System & UI

**Component Library**: Native semantic HTML controls and the existing local stylesheet; no external component library.
**Style Direction**: A calm, task-focused French migration console. Preserve the existing navy identity, give folder selection and the primary migration action clear priority, and separate optional diagnostics from ordinary generation.
**Typography**: Segoe UI, system-ui, sans-serif; system monospace for paths and logs.
**Brand identity & links**: Reuse the existing local conversion logo from [docs/images/logo.svg](../docs/images/logo.svg). Add a compact, text-labelled icon strip for Tableau, Power BI, Fabric, the Python engine, and the project’s GitHub repository. Use accessible inline SVG icons and existing project assets, not copied vendor artwork, remote icon packages, or CDN assets. The GitHub link opens [the project repository](https://github.com/cyphou/TableauToPowerBI) only when the user activates it; it never loads during offline operation.

### Color Palette

| Token | Hex | Usage |
|-------|-----|-------|
| `primary` | `#0F4C81` | Primary migration action, selected navigation, and local workflow emphasis |
| `accent` | `#147D64` | Connected loopback state and explicit successful generation states |
| `surface` | `#F4F7FB` | Migration-workspace background behind white configuration and result panels |
| `text` | `#13233A` | Folder labels, job names, settings, and report titles |
| `muted` | `#52647C` | Path help, optional-diagnostic explanations, timestamps, and secondary metadata |
| `border` | `#D2DCE8` | Field outlines, panel divisions, artifact rows, and log boundaries |

### Pages

| Page | Route | Purpose | Layout |
|------|-------|---------|--------|
| Migration locale | `/` | Configure a local batch, see its Tableau/Power BI/Fabric/Python ecosystem, run migration, and inspect results | `sidebar + header + hero + form + action-bar + two-column(list+main) + card-list + footer` |

Keep one page with the existing `#configuration`, `#migration`, `#diagnostics`, and `#results` sections. Do not introduce a router or extra backend page routes.

### Sample Content

These records are **illustrative preview content**, not measurements or jobs executed during planning. Production starts with the real session state and an empty history when no jobs exist.

Migration locale — configuration:

| Field | Example/default | Behavior |
|-------|-----------------|----------|
| Dossier source Tableau | `C:\Tableau\Sources` | Required local folder; Browse or manual entry |
| Dossier de sortie | `C:\MigrationOutput` | Required local output path; Browse or manual entry |
| Format | Projet Power BI (.pbip) | Fabric remains file generation only |
| Culture | `fr-FR` | Preserve the existing locale choices |
| Calendrier | 2020 to 2030 | Reject reversed bounds |
| Politique de qualité | `report` | Preserve `report`, `enterprise`, and `production` |

Migration locale — services représentés:

| Service | Rôle affiché | Icône et comportement |
|---------|--------------|-----------------------|
| Tableau | Classeur source | Icône de graphique locale, libellée Tableau |
| Power BI | Projet de sortie | Icône de colonnes locale, libellée Power BI |
| Microsoft Fabric | Format de sortie facultatif | Icône de couches locale, libellée Fabric |
| Python | Moteur local | Icône de code locale, libellée Python |
| GitHub | Code source du projet | Lien explicite vers le dépôt `cyphou/TableauToPowerBI`, ouvert uniquement après clic |

Migration locale — example job/evidence states:

| Record | Display state | Required distinction |
|--------|---------------|----------------------|
| Migration Power BI | Terminé | Generation succeeded; no inferred Desktop opening or refresh |
| Actualisation des résultats | En cours | Loading treatment retains the last known data without fabricating results |
| Probe Power BI | Non exécuté | No diagnostic evidence exists until explicitly requested |
| Comparaison Tableau/Power BI | Non exécuté | An unavailable Desktop application produces an explanation, not a parity pass |

Representative artifacts are labelled **Projet Power BI**, **Rapport de qualité**, and **Paquet de preuves**. Actual names and URLs come only from the runner's artifact inventory.

Show populated results, a loading state, a useful empty diagnostic state, and an explicit unavailable-application error in the visual preview. Do not ship those demonstrations as live results.

Accessibility requirements: labelled controls, visible keyboard focus, skip link, live status/error regions, text alongside status colors, reduced-motion support, and responsive panels without page-level horizontal scrolling at a 390 px viewport.

---

## 7. Project Structure

Retain the current layout rather than generating a new application tree:

```text
TableauToPowerBI\
  .azure\
    project-plan.md
    .preview-temp\
      manifest.json
      theme.css
      migration.html
  run_web_ui.ps1
  migrate.py
  web\
    index.html
    app.js
    style.css
    local_server.py
    app.py                  existing optional interface; preserve
  tests\
    test_local_web_ui.py
  README.md
  docs\
    LOCAL_WEB_UI.md
    images\
      logo.svg             existing local Tableau-to-Power BI product logo
```

Planning writes only this plan and its visual-preview assets. Implementation, after approval, should modify only the directly related runner, launcher, frontend, tests, and offline-launch documentation as needed.

The preview is a design reference, not an iframe to embed in the application or a replacement for the current frontend.

---

## 8. Route Definitions

Preserve these existing routes. No cloud endpoint, generic command-execution endpoint, or invented upload workflow is required.

| # | Method | Path | Description | Request Body | Response Body | Status Codes |
|---|--------|------|-------------|--------------|---------------|--------------|
| 1 | GET | `/` | Local migration workspace | None | HTML | 200, 403, 404 |
| 2 | GET | `/app.js` | Existing local browser behavior | None | JavaScript | 200, 403, 404 |
| 3 | GET | `/style.css` | Existing local styles | None | CSS | 200, 403, 404 |
| 4 | GET | `/api/session` | Establish the existing local browser session | None | Existing session object with local origin | 200, 403 |
| 5 | GET | `/api/jobs` | Read job states, bounded logs, and artifact inventory | None | `{ jobs: [...] }` | 200, 403 |
| 6 | POST | `/api/jobs` | Submit a supported migration or explicitly confirmed diagnostic action | `{ action, source_dir, output_dir, opts, confirm_native? }` | `{ job: {...} }` or `{ error }` | 202, 400, 403, 409, 411, 413, 415, 500 |
| 7 | POST | `/api/pick-folder` | Browse for a local folder | `{ title? }` | `{ path: string or null }` or `{ error }` | 200, 400, 403, 409, 411, 413, 415, 503 |
| 8 | GET | `/api/artifacts/{job_id}/{relative_path}` | Open an allowed generated artifact within that job's output root | None | Artifact bytes or `{ error }` | 200, 403, 404 |
| 9 | GET | `/api/download/{job_id}` | Download the bounded generated-output ZIP after the job finishes | None | ZIP bytes or `{ error }` | 200, 403, 404, 409, 413 |

Unknown routes remain explicit errors. Unavailable native checks can be accepted as jobs and then resolve to `not_run`; never reinterpret that state as a successful check.

---

## 9. Next Steps

1. Review this local-only plan and its [visual preview](.preview-temp/migration.html). Keep status **Planning** until the user approves implementation.
2. Preserve the existing working tree and finish the current frontend/runner integration, especially Browse controls, offline explanations, and visible failure states. Do not scaffold another backend or frontend framework.
3. Verify the core generation path is offline, including optional environment-driven integrations. Do not invent an unsupported `--offline` CLI flag; use existing controls or make a narrowly scoped, tested change if an actual dependency is discovered.
4. Extend the focused tests only for changed behavior, then run `python -m pytest tests\test_local_web_ui.py -q`. Use browser checks for JavaScript interactions; HTTP tests alone do not verify them.
5. Run a public/local sample batch and browser acceptance checks with external network access unavailable. Record actual evidence and fix blockers before claiming the website works end-to-end offline.
6. Update the README and local web-interface documentation with offline startup, prerequisites, optional readers, diagnostics limitations, output access, and shutdown behavior. Remove the standalone desktop application, executable packaging, launcher, app icon, desktop screenshot, and app-only tests.

**Launch contract after implementation**

```powershell
.\run_web_ui.ps1
# Alternative without automatic browser opening:
python web\local_server.py --port 8765 --no-browser
```

Open `http://127.0.0.1:8765/`. Exit the launcher with Ctrl+C. A busy port or missing Python must produce an actionable error, not a false ready message.

**Acceptance criteria**

- A clean local launch serves the page and API with all assets available while external networking is unavailable. Browser requests target loopback only; the real generation process must also complete without outbound network access.
- The source/output Browse buttons populate the correct fields. Cancellation leaves the prior value intact; an unavailable picker visibly explains manual entry.
- A folder containing one public workbook uses batch generation and produces a valid Power BI project plus applicable quality/evidence output. A standalone Prep flow retains its existing Power Query/source/assessment output semantics.
- The default migration does not start Desktop applications, refresh live sources, deploy artifacts, or invoke online AI. Fabric output means local artifact generation, not deployment.
- One active job blocks duplicate submissions and conflicting folder dialogs. Polling remains bounded and does not overlap submission; ambiguous submissions are never retried automatically.
- Validation errors, failed subprocesses, unavailable native applications, dropped runner connections, artifact errors, and ZIP limits are visible and do not appear as successful migration or parity evidence.
- Reports open in isolated tabs, ZIP contents remain confined to allowed generated output, and malicious or out-of-root paths remain rejected.
- At 1440 px and 390 px widths, keyboard navigation works, all controls remain reachable, and the page has no unintended horizontal overflow.
- Existing focused tests pass, and real-browser checks cover folder browsing, submit/poll behavior, reconnection, optional-diagnostic confirmation/cancellation, and result links. Production never displays the preview's illustrative jobs as real results.
- The migration CLI, browser-based local UI, optional Streamlit/upload interfaces, PowerShell web launcher, and unrelated working-tree changes remain intact; only the standalone desktop application and its packaging are removed.
