# Demo Studio integration

<!-- demo-studio:integration:start -->
## Demo and presentation workflow

Project: **TableauToPowerBI**.
Repository identity: `cyphou/tableautopowerbi`. This does not certify public visibility or capabilities.

This is an optional presentation handoff, not a replacement for the project's domain Skills,
tool restrictions, branding, ownership rules, data handling or acceptance gates.
Use it only when a demo, workshop or presentation of this project is requested.

### 1. Qualify this project

Read its README and relevant domain Skills. Identify documented inputs, operations, outputs,
prerequisites, limitations and the actual evidence available. Do not invent capabilities,
commands, screenshots, success rates, cost savings or execution results.
CRM/customer data, credentials and private code must not be sent to a public catalog or external model.

### 2. Connect Demo Studio

Install or locate the workspace containing `demo-studio` and its `demo-catalog`,
`demo-storyboard` and `demo-prep` Skills. Configure its DemoVideoOnTheFly engine with
`DEMO_VIDEO_ROOT` if needed. The domain repository does not bundle these tools.

```powershell
$env:DEMO_STUDIO_ROOT = 'C:\path\to\SyncAll'
npm --prefix "$env:DEMO_STUDIO_ROOT" run demo:sync -- --sources cyphou/tableautopowerbi
node "$env:DEMO_STUDIO_ROOT\demo-studio\cli.cjs" inspect --repo cyphou/tableautopowerbi
npm --prefix "$env:DEMO_STUDIO_ROOT" run demo:studio
```

Replace the example installation path. Verify public visibility and redistribution rights first.
A private or non-GitHub project requires a separately approved workflow; do not make it public
or upload its content to bypass this restriction. Synchronization replaces the Studio catalog
configuration, not project code or existing presentations.

### 3. Author the scenario

Select the repository by its complete owner/repository identity, and retain the verified commit SHA.
Choose demo, presentation, workshop or migration according to the request. Set the client context,
audience, objective, title and permitted brand. Add project-specific actions, expected results
and proof/limitation notes; do not force a migration narrative onto a different use case.
Reuse existing approved artifacts rather than altering the domain pipeline for a slide.

The Studio generates standalone HTML and keeps a brief, pinned sources, storyboard and
presenter notes. Its gallery editor creates a new validated version without replacing the original.
If this project's branding or approval rules differ from the default, follow the stricter local rules.

### 4. Verify and hand off

Separate proposed steps, documented resources, saved captures and verified live execution.
For migrations, include source/target parity controls; for other demos, use the relevant observable checks.
Obtain authorization before installing tools, running project code, writing to services or incurring costs.
Preserve failed checks and blockers; no mock or saved capture may be described as a successful live run.
Verify offline HTML, navigation, notes, sources, themes and complete printing, then rehearse timing.
Store project artifacts only in locations allowed by this repository. Do not commit generated
client briefs, data, credentials or demo outputs unless explicitly permitted.

The Studio's source ZIP and project Skills are preparation resources, not an already deployed environment.
<!-- demo-studio:integration:end -->
