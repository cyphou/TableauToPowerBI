# Development Roadmap

The engine migrates Tableau workbooks to Power BI (PBIP/PBIR v4.0 + TMDL) or
Fabric-native artifacts. Static local validation is the release gate. Semantic
execution, refresh and deployment evidence remain `not_run` unless an
authorized environment proves otherwise.

## Where we stand

Measured on the committed example corpus, not estimated:

| Signal | Value |
|---|---|
| Migration success | 44/44 artefacts, 0 failures |
| Static openability gate | 27/27 projects, 0 blockers |
| Quality verdicts | 17 PASS, 10 WARN, 0 FAIL |
| Outstanding repairs | **0** — nothing measurably broken remains |
| Functional parity | mean **99.7%**, lowest **96.4%**, 20 of 26 at full parity |
| Lineage coverage | **99.9%**, 0 unresolved source records |
| Evidence level | `STATIC_PASS` on 26/26 |
| Test suite | 10,097 passed, 67 skipped, 1 xfailed, across 288 files |
| Agent ownership | 0 unowned modules, 0 asymmetric declarations |

The remediation queue now contains **no `repair` actions at all**: 10 `decide`,
12 `note`, 2 `verify`. Everything left either needs a human judgement or is
evidence with nothing to act on. That is the milestone this cycle reached, and
it changes what the next one should be about.

## Convergence to 100% and Resilience

The current **99.7% functional parity** is not the release target by itself.
The target is the contract in [FULL_MIGRATION_EVOLUTION_PLAN.md](FULL_MIGRATION_EVOLUTION_PLAN.md):
no silent source-object drops, an explicit target or remediation for every
in-use object, evidence for every claimed conversion, and a separate status for
runtime checks that cannot run locally.

### The five 100% gates

Every migration must pass these gates independently. A numerical score cannot
compensate for a missing object, an unverified interaction, or a recovery path
that loses state.

| Gate | 100% condition | Required evidence |
|---|---|---|
| Inventory | All 23 extracted object types are counted and assigned stable source identity | Source inventory plus extraction contract report |
| Conversion | Every in-use object is `exact`, `healed`, `approximated`, or `unsupported`; nothing is silently skipped | Parity registry, migration ledger, explicit remediation |
| Target | Every convertible object has a PBIR, TMDL, M, Fabric, or handoff artifact | Target evidence probe and source-to-target lineage |
| Behavior | Calculations, filters, visuals, actions, security, geometry, refresh strategy, and packaging have applicable checks | Equivalence, openability, QA, and review reports |
| Resilience | A failed stage can resume or roll back without corrupting prior evidence or changing unrelated output | Checkpoint, recovery ledger, idempotence, fault-injection, and rollback evidence |

`STATIC_100` means the first four gates pass locally. `OPERATIONAL_100` additionally
requires authorized Desktop, semantic execution, refresh, deployment, and
post-deployment evidence. Those states must never be inferred from static files.

### Closure matrix for all migratable objects

This is the inventory that future roadmap work must close. A row is complete only
when its producer, target consumer, evidence probe, negative control, and owner
are all documented. The list is intentionally broader than the parity score.

| Source object | Target / closure mechanism | 100% proof | Owner |
|---|---|---|---|
| Worksheets | PBIR visual or explicit unplaced-sheet record | Worksheet lineage and visual binding | Extractor / Visual |
| Dashboards | PBIR report page | Page inventory and layout comparison | Visual |
| Datasources | M, Import, DirectQuery, Dataflow, or handoff connection | Connector strategy and sanitized prerequisite report | Wiring / Deployer |
| Calculations | DAX measure, calculated column, or M transformation | DAX/M validation and value checks | DAX / Wiring |
| Parameters | What-If, field parameter, or documented runtime binding | Parameter table/control and selected-value evidence | Semantic / Visual |
| Filters | Report, page, visual filter, slicer, or explicit no-op reason | Filter condition comparison and negative control | Visual |
| Stories | PBIR bookmarks and navigation | Bookmark inventory and state comparison | Visual |
| Actions | Cross-filter, navigation, WebUrl, bookmark, or remediation | Action ownership and target evidence | Visual |
| Sets | Boolean calculated column or DAX equivalent | Membership comparison | Semantic / Wiring |
| Groups | SWITCH/Mapped display column | Value-to-group comparison | Semantic / Wiring |
| Bins | FLOOR/bin column or equivalent grouping | Boundary and membership comparison | Semantic |
| Hierarchies | TMDL hierarchy | Level order and drill-path evidence | Semantic |
| Sort orders | Sort-by-column or visual sort state | Source order versus target order | Semantic / Visual |
| Aliases | Named measure or generated display column | Alias evidence and value comparison | Semantic |
| Custom SQL | Value.NativeQuery or approved connector query | Query text, binding, and credential prerequisite | Wiring / Deployer |
| User filters | RLS roles and membership handoff | Role expression and assignment state | Semantic / Deployer |
| Hyper files | Imported/staged table or explicit unsupported-format finding | Row/schema evidence and source hash | Extractor / Fabric |
| Datasource filters | Report/data-scope filter | Scope-level filter evidence | Visual / Wiring |
| Custom geocoding | Geographic category or registered shape resource | Category/resource presence and map review | Visual / Extractor |
| Published datasources | Bound semantic source or connection handoff | Published-source identity and binding evidence | Tableau / Deployer |
| Data blending | Relationship, merge, or documented virtual-source decision | Blend graph, cardinality, and direction check | Semantic / Merger |
| Table extensions | M/Power Query extension source | Endpoint/schema/authentication readiness | Wiring / Deployer |
| Linguistic schema | Q&A synonyms and Copilot metadata | Synonym/schema presence and semantic review | Semantic / Evidence |

### Resilience workstream

Resilience is a correctness requirement, not an operational extra. The route to
100% is staged as follows:

1. **Fail closed.** Any unreadable source, missing required artifact, invalid
  DAX/M/TMDL/PBIR, unresolved reference, or missing prerequisite becomes a
  structured blocker or `not_run`; it must not become a pass through a fallback.
2. **Checkpoint every boundary.** Persist source hash, configuration hash,
  stage, output manifest, and evidence references after extraction, generation,
  validation, packaging, and deployment handoff. Resume from the last valid
  boundary.
3. **Make reruns idempotent.** Same source and configuration produce the same
  semantic names, lineage, evidence, and repaired output aside from declared
  nondeterministic IDs. A second run must not duplicate measures, columns,
  visuals, relationships, or recovery entries.
4. **Repair deterministically first.** Every automatic repair must declare its
  confidence, before/after state, affected object, and follow-up check. Low
  confidence or ambiguous repairs stop for review; they are never guessed.
5. **Rollback on regression.** If a repair lowers parity, creates a blocker,
  changes unrelated objects, or fails post-repair validation, restore the last
  valid checkpoint and retain the failed attempt in the recovery ledger.
6. **Bound external failure.** Connector, Desktop, Fabric, gateway, and LLM
  calls use bounded timeout/retry policy, redacted diagnostics, and explicit
  `not_run`/`blocked` states. Credentials and private payloads never enter
  fixtures, prompts, or reports.
7. **Prove failure paths.** Each gate needs a positive control and a negative
  control: corrupt a source, delete a target table, break a reference, interrupt
  a stage, and replay a checkpoint. A detector that cannot fail is not evidence.

### Roadmap sequence to 100%

| Wave | Outcome | Exit gate |
|---|---|---|
| R1 — Inventory closure | **Done.** The extractor guarantees all 23 canonical JSON outputs, including empty collections; producer-consumer field lint is strict | 17 contract/lint tests pass; 0 unknown consumer fields. The corpus populates 21 types, while 2 rare types remain represented by empty canonical outputs |
| R2 — Target evidence closure | **Done.** Specific probes cover filters, actions, parameters, hierarchies, sort order, RLS, SQL, schedules, subscriptions, aliases, groups, bins, reference lines, Hyper staging, linguistic cultures, and the three calculation families | Measured across the 16-workbook corpus: 18 features in use, 24 probed, **0 in-use features without a target probe** |
| R3 — Fidelity closure | Resolve remaining approximations or publish equivalent/unsupported decisions | Every in-use item has accepted behavior evidence or explicit remediation |
| R4 — Resilience closure | Resume, idempotence, rollback, timeout, and fault-injection coverage | Interrupted and replayed migrations preserve evidence and do not duplicate output |
| R5 — Runtime closure | Desktop save/reopen, semantic execution, refresh, deployment, and post-deploy checks | `OPERATIONAL_100` only in an authorized environment |

The next implementation issue must be selected by the first failing gate in this
sequence, not by the headline parity percentage. This prevents a 100% score from
masking an untested object type or a migration that cannot recover safely.

## Measured architecture baseline

Behaviour is mature; the remaining debt is structural. These numbers are
measured from the tree, not estimated, and they set the agenda below.

| Signal | Value |
|---|---|
| Source modules | 161 (149 `powerbi_import`, 12 `tableau_export`) |
| Source lines | 96,538 |
| Test files / lines | 288 / 119,084 (1.23x test-to-source) |
| CLI surface | 14 public commands over 145 flags in a 6,795-line `migrate.py` |
| Concentration | Top 6 modules hold 28,143 lines — **29% of all source** |
| Production reachability | 132 modules reachable, **23 not reachable**, 1 genuinely dead |
| Healing layering | 1,203 lines inside the documented facade, **3,995 lines outside it** |
| Reporting surface | 29 modules, ~14,000 lines, 21 HTML generators |

Four findings follow from that table:

1. **Concentration risk.** `tmdl_generator` (6,151), `pbip_generator` (5,468),
   `extract_tableau_data` (4,071), `shared_model` (3,219), `dax_converter`
   (3,147) and `visual_generator` (3,053) are also the files most often recorded
   as regression-prone. Two prior extractions proved the seams exist.
2. **Shipped but unwired.** Only `api_server` (Dockerfile) and `mcp_server`
   (MCP stdio) have a non-CLI entry point. The rest — including `dax_optimizer`,
   `marketplace`, `model_templates`, `dax_recipes`, `plugin_sdk`,
   `geo_passthrough`, `gateway_config`, `alerts_generator`, `visual_diff` and
   `regression_suite` — are complete and tested with no user path. This is the
   `visual_generator` finding generalised: a module reads as covered because a
   test imports it.
3. **The documented healing architecture describes a third of the code.**
   `self_healing_v3`, `self_healing_report` and `tmdl_self_heal` sit outside the
   `healing_core` contract, and `healing.py` is not reachable from `migrate.py`.
4. **Reporting producers were never consolidated.** `html_template` (fan-in 32)
   unified presentation; `migration_quality` still carries fan-out 29.

## Next cycle — structural closure

Ordered so that no effort is spent refactoring code that should not exist.

| Wave | Outcome | Exit gate |
|---|---|---|
| A1 — Calculation evidence | **Done.** Each family is keyed on its distinguishing DAX artifact — LOD by `ALLEXCEPT`/`REMOVEFILTERS`, table calc by `RANKX`/`OFFSET`/`ALLSELECTED`/`WINDOW`/`INDEX`, basic by an aggregation with neither — and every declaration is gated per-measure by its migration provenance annotation, so a generated helper is never attributed to a source calculation | 0 in-use features report `not_checked`; window semantics are tested before grain override so a `WINDOW_*` expression using `ALLEXCEPT` is not misread as a LOD; a generated R² measure using `RANKX` evidences nothing |
| A2 — Reachability contract | **In progress.** Measured: **22 modules / 9,099 lines** unreachable from any production entry point, and **15 of 142 CLI flags declared but never read** — now 11, with `--agg-tables`, `--composite-threshold`, `--optimize-dax` and `--time-intelligence` wired and proven. `scripts/check_cli_flags.py` plus a ratcheting baseline prevent both a new inert flag and a stale entry | Guard proven to fail on an injected inert flag, and twice on real fixes. Remaining: a wire/declare/retire decision per module and per remaining inert flag |
| A3 — Healing consolidation | Bring the three outside modules under the `healing_core` contract and one recovery ledger, or document the exemption | One healing contract, no undocumented stage, `RepairAttempt` stays distinct from `HealAction` |
| A4 — Decompose by seam | Split the top-6 along existing seams (page/layout vs visual, relationship inference, parameter tables, per-type extractors) with the proven extraction recipe | No module above ~2,500 lines; `tmdl_generator` reduced to one owner |
| A5 — Resilience baseline (R4) | Measure what survives interruption *before* building: interrupt a batch, replay a checkpoint, corrupt a source, delete a target table | Interrupted and replayed migrations preserve evidence and never duplicate output |
| A6 — Runtime closure (R5) | Unchanged: an authorized environment, or the roadmap states plainly that these signals stay `not_run` | `OPERATIONAL_100` only with real environment evidence |

Non-goals for this cycle, recorded so they are not re-proposed:

- Rewriting the 145-flag surface. The 14-command layer already solved
  discoverability, and flag-based automation is a compatibility contract.
- Merging the 21 HTML generators. `html_template` already unified presentation;
  merging producers would risk the reporting contract for cosmetic gain.

### A2 findings — the inert surface

Two measurements, both the same defect class the last cycle kept finding: a
surface that reads as working because a test imports it or `--help` lists it.

**Unreachable modules — 22, totalling 9,099 lines.** Reachability was computed
with full dotted-path resolution *and* the rule that importing a submodule
executes its package `__init__`; that rule alone reclassified `deploy.utils`
and `deploy.config.environments` as genuinely reachable, so the number is a
floor, not a guess. Three groups need different answers:

- **Declare** — real entry points that simply are not CLI-reachable:
  `api_server` (Dockerfile `CMD`), `mcp_server` (MCP stdio), `notebook_api`
  (Jupyter `MigrationSession`), `plugin_sdk` and `plugins` (authored against,
  not called by us).
- **Wire** — complete, tested, and wanted, but with no user path:
  `dax_optimizer`, `gateway_config`, `alerts_generator`, `visual_diff`,
  `geo_passthrough`, `marketplace`, `model_templates`, `dax_recipes`,
  `regression_suite`, `remediation`, `conversational`,
  `deploy.multi_tenant`, `deploy.credential_vault`, `subscription_migrator`,
  `dax_query_generator`.
- **Retire** — `connection_rewriter` (407 lines, zero tests, zero references).
- **Special case** — `healing.py` is the documented facade, yet `migrate.py`
  imports `autoheal` directly and bypasses it. That is A3's problem, recorded
  here because it is why the facade looks unused.

**Inert CLI flags — 5 of 142.** Declared, accepted by the parser, never read:
`--parallel-run`, `--prep-to-dataflow`, `--skip-conversion`, `--sync`,
`--validate-data`.

`--agg-tables` and `--composite-threshold` **are now wired** and were the first
two removed from that set. The generator already accepted both; only the
CLI-to-pipeline handoff was missing. Verified with a positive control in
composite mode: `--agg-tables auto` emits `Agg_Commandes.tmdl` and registers it
in `model.tmdl`, and `--composite-threshold 3` moves tables from `import` to
`directQuery`. In the default import mode they correctly change nothing, which
is why a first comparison showed no difference until GUID churn was filtered
out and the mode condition was found.

`--optimize-dax` and `--time-intelligence` followed. Both now reach the
generator; time intelligence binds to the model's own date table rather than a
hardcoded `'Calendar'[Date]` that the generator suppresses when the source
ships its own date dimension.

`--server-assess` carried a second defect on top of being inert: it was
declared `store_true`, but README, `server_assessment.py` and the project
instructions all document it taking a project name. Run as documented, the
parser bound `Marketing` to the workbook positional, so the command failed for
a reason unrelated to the flag. It now takes an optional project name — absent
means the whole site — and delegates to `run_bulk_assessment_mode` so server
and local assessment cannot drift apart.

`--gateway-bind` was *suspected* inert and is not: it is read through
`getattr(args, 'gateway_bind')`, so the detector accounts for attribute,
`getattr`, and config-dictionary access. `gateway_config.py` is separately
unused because binding runs through `pbi_deployer` instead.

`--live-connection` was the purest case of the pattern: `ThinReportGenerator`
has always accepted a `live_connection` argument and writes a `byConnection`
`datasetReference` when given one, but none of its three call sites passed it,
so the documented shared-model command silently produced `byPath`. Threading
it through `run_shared_model_migration` → `import_shared_model` → the generator
is the whole fix. Positive control: with the flag, both thin reports emit
`byConnection` with `Data Source=…/myorg/ws-1234;Initial Catalog=LCModel`,
while the model-explorer report correctly stays `byPath` — it exists to open
the *local* model, so flipping it would make the project unopenable.

Writing the test for it exposed a gap in the test itself. Constructing
`ThinReportGenerator` directly proves the generator works and says nothing
about the caller: removing the forwarding left every assertion green while the
flag went inert again. The handoff is now asserted directly, and the control
that previously passed 5/5 now fails.

`--resolve-published-ds` and `--no-ds-cache` were wired together, since one
entry point serves both. `resolve_all_published()` was complete and already
had passing tests, but no production caller: a published datasource carries no
tables or columns in the workbook XML, so the generator saw an empty source
while the flag meant to fix that did nothing. Resolution now runs after
extraction, reads the cache and the server when credentials exist, and
rewrites `datasources.json` in place.

`--no-ds-cache` means something narrower than its help text suggests. It
disables the cache as a *primary source*, but the offline fallback still
applies when no server is reachable, so the only observable difference is the
provenance tag (`cache` vs `cache_fallback`) — which is what the test asserts
rather than a naive "nothing was resolved".

Two method notes from this wave, both from controls that lied. A test asserting
`"_resolve_published_datasources(args)" in source` passes with no call site at
all, because the `def` line contains the same text. And a PowerShell control
harness silently failed to write its mutations — `Copy-Item` reported
`PathNotFound`, every control "passed", and the passes meant nothing. Controls
now verify the mutation is on disk before judging the result.

`--merge-preview` was inert while `merge_preview()` was complete and tested.
Its help promises a dry run "without writing any files", so the branch returns
before `import_shared_model` and the test asserts the output directory stays
empty. Positive control on two unrelated sample workbooks: score 20/100,
recommendation `separate`, 0 tables saved, `FILES_WRITTEN=0` — the right answer
for workbooks that should not be merged.

The reporter shipped with the very defect this wave is about. It read
`overall_score`, the assessment exposes `merge_score`, and the `if score is not
None` guard turned a wrong key into a silently missing line rather than an
error. A report that looks complete and has quietly dropped its headline number
is the same failure as a flag that parses and does nothing. There is now a test
asserting the score is actually printed, and a control that reproduces the bug.

A third bad test surfaced here too: locating `import_shared_model` by substring
matched the *docstring* before the call, so the ordering assertion failed
against correct code. Matching prose is not matching behaviour.

### Preceptorship now runs on every migration

The loop existed, was reachable, scored six dimensions, and was **off unless
asked for** — so the default migration shipped with no review at all. The
mechanism was there; the cadence was not.

Measured before changing it: median 9.2s without, 8.6s with, i.e. overhead
inside run-to-run noise, one extra file (`preceptor_report.json`), verdict
`approved 5.0/5 in 1 cycle`. A first measurement showed the reviewed run
*faster by 71%*, which was cold-start cost on the unreviewed run rather than a
result; discarding the warm-up run removed the artifact.

Default-on is safe because the escalation contract already made it advisory:
a warn never fails the migration, a reviewer exception never fails the
migration, and blocking still requires `--preceptor-block`. `--no-preceptor`
opts out, mirroring `--no-compare`.

`docs/AGENTS.md` claimed the loop also triggered "on `--review`". That flag has
never existed. The section now describes the two switches that do.

### Documented flags must exist

`--review` was not an isolated slip. The inert-flag guard covers flags the CLI
accepts and ignores; nothing covered flags the *documentation* promises and the
CLI rejects. That failure is worse to diagnose, because argparse fails for a
reason unrelated to the flag — a documented `--server-assess PROJECT` bound the
project name to the workbook positional and reported a missing file.

`scripts/check_doc_claims.py` closes it. Precision mattered more than coverage:
judging every `--word` in the docs produced 20 hits, almost all pytest's
`--cov`, git's `--oneline`, VS Code's `--install-extension`. Judging only lines
that invoke `migrate.py` produced 2 — and one of those, `--advanced-help`, was
a blind spot in the guard rather than a defect in the docs, since it is
compared against `argv` before the parser sees it. With intercept detection the
count is **1 real phantom**: `docs/ENTERPRISE_GUIDE.md` documented
`--visual-diff`, whose module `visual_diff.py` is itself on the unreachable
list. The example now shows the comparison report that does exist.

`--multi-tenant` was inert while `deploy_multi_tenant()` was complete: it
validates tenants, substitutes per-tenant connection strings, deploys each
copy, and supports `dry_run`. The documented
`--shared-model ... --multi-tenant tenants.json` built a model and stopped.
Positive control (dry run, two tenants): 2/2 succeed and
`multi_tenant_deployment.json` is written; without the flag, neither happens.

Wiring it exposed something larger. Both post-shared-model deployment steps
are gated on `exit_code == ExitCode.SUCCESS`, and `--shared-model` returns
`VALIDATION_FAILED` because of the openability defect below — so
**`--deploy-bundle` and `--multi-tenant` are both unreachable in a default
run**. The first positive control failed for exactly this reason and only
passed once `--no-verify-open` was added. The gate check is correct; what it
depends on is broken. `TestDeploymentGating` pins the coupling so it is
visible rather than rediscovered.

Two test-data defects during the same wiring, both caught by validation doing
its job: workspace ids that merely looked plausible (`ws-contoso-0001`) are
rejected as non-GUIDs, and connection overrides must be `${UPPER_NAME}`
placeholders rather than bare keys like `server`.

### Resolved — the openability gate assumed one report per project

`--shared-model` exited `5` (`VALIDATION_FAILED`) on every run, and because
both post-shared-model deploy steps are gated on a `SUCCESS` exit code, that
single defect kept `--deploy-bundle` and `--multi-tenant` unreachable.

Three separate faults, and the diagnosis mattered more than the fix. The `.pbip`
manifests were **correct** — `DiagModel.pbip` declares `DiagModel_Model.Report`,
which exists — so the output was right and the gate was wrong:

* `pbip_contract` required every `.Report` and `.SemanticModel` to share one
  name. A bundle is one model plus *N* thin reports, so it never can.
* `manifest_coherence` derived the report folder from the model name instead of
  reading the path each `.pbip` declares.
* `visual_bindings` validated `report_dirs[0]` only.

Both checks now read the declared path, which is what Desktop opens and is
correct for either layout.

That third fault was the dangerous one, and it produced **false passes**. The
verdict depended on glob order: with the model named `DiagModel` the empty
model-explorer report sorted first, was checked, and passed while the thin
reports went unexamined; renaming it to `MTFinal` moved `Financial_Report.Report`
first and the same project reported 5 blockers. Deterministic, reproducible,
and entirely an artefact of which report happened to sort first. Every report
is now checked.

One genuine generator defect surfaced underneath: the model-explorer report was
written without `definition/pages/pages.json`, so it was an incomplete PBIR
report. The gate was right to demand it.

**Deployment is still blocked, now for a true reason.** With the false
failures and false passes gone, the gate reports 10 real orphaned visual
bindings — thin-report visuals referencing columns that do not survive the
merge (`'transactions'.'Revenue'`, `'Net Income'`, and others). That is a merge
defect, not a gate defect, and it is the next thing standing between
`--multi-tenant` and a default run. Two measurement harnesses lied on the way
here: one reused output directories so blocker counts accumulated 5→10, and an
earlier control passed only because it inherited a model from a previous run.

### A2 findings — the advisory boundary

Determinism is the product: the same workbook must yield the same project, or
the golden fixtures, the parity registry and the regression suite stop meaning
anything. Non-deterministic code earns its freedom by staying out of the
artifact path.

Measured, that split is **already intact**. `llm_client` writes only a report
JSON, `remediation` only its own JSON and HTML, and none of the nine advisory
modules imports an artifact generator — 0 breaches. It held by habit, though,
not by rule, so writing refined DAX straight into the model would have looked
like an improvement while silently destroying reproducibility.

`scripts/check_advisory_boundary.py` now enforces two statically checkable
rules: an advisory module may not import a generator, and may not name a
`.tmdl`/`.pbir`/`.pbip`/`.bim`/`.pq` path. Proven by injecting a real breach
into `remediation.py`: the detector reports it and `TestRealBoundary` fails.
The rule is stated in `SKILL.md` as golden rule 5, because the agent reading
that file is the most likely thing to break it.

This is also the answer to where Skills fit. A Skill is a routing and knowledge
layer, not an execution layer, which makes it the right host for judgement:
triaging a 200-workbook portfolio, or explaining which of forty warnings matter,
is interpretation over deterministic evidence and writes nothing.

**`--optimize-dax` is now wired, and the contradiction behind it is resolved.**
It declared `default=True` ("enabled by default") while `KNOWN_LIMITATIONS.md`
stated optimization is opt-in to preserve original semantics — and the code did
neither, because the value was never read. Honouring the declared default would
have changed generated DAX for every migration; the flag is therefore genuinely
opt-in now, which keeps existing output byte-identical while making the feature
reachable. Optimization runs *before* self-healing, so rewritten DAX passes the
same validation gates as directly converted DAX, and the direct conversion is
preserved as a `MigrationNote` annotation.

Measured on `Complex_Enterprise`: 2 measures rewritten
(`IF(ISBLANK(x), 0, x)` → `COALESCE(x, 0)`), one file changed, the project
still openable with 0 blockers, and parity unchanged at 99.7% `FULL` with no
feature reporting `not_checked`.

The guard is a ratchet, and it proved itself on this change rather than on a
fixture: wiring the flag made `test_the_baseline_only_shrinks` fail until
`optimize_dax` was removed from `KNOWN_INERT`.

**`--time-intelligence auto` is now wired**, and it exposed a trap worth
recording. `generate_time_intelligence_measures` defaults to
`'Calendar'[Date]`, but the generator deliberately suppresses its Calendar when
the source ships its own date dimension. Binding to that default would have
emitted measures against a column that does not exist. The date reference is
now resolved from the model's own date table through the existing
`_is_date_table` detector — no second heuristic — and generation is skipped
entirely when no date table is present, rather than producing invalid DAX.

Measured on `Complex_Enterprise`: 27 measures added against
`'dim_date'[full_date]`, not `'Calendar'[Date]`, and the project stays openable
with 0 blockers. Groups are added all-or-nothing, so `YoY %` can never
reference a `PY` measure that was skipped for a name collision.

The end-to-end run earned its place here: `import_all` had been given the new
argument at its call site but not in its signature, and only a real migration
surfaced the `TypeError`.

## What the last cycle proved about method

Nine defects were found, and **not one was a missing feature**. Every single
one was a detector that counted something other than what it claimed:

- the preceptorship reviewer failed six checks against shapes the generator
  never writes — it read documentation annotations as DAX, and its M dimension
  scanned fenced blocks that hold *calculated-table DAX*, so it scored every
  workbook 5/5 while reading no M at all;
- `conditionalFormatting` was a key no extractor could ever fill, and beneath
  it sat a real loss: **72 colour-encoded worksheets produced 0 coloured
  visuals**, because a colour encoding is stored in two places and only one was
  read;
- the `alias` parity status was **hardcoded**, not measured, so every workbook
  using aliases was docked — while 0 of 17 measure-name aliases actually
  reached the model;
- all 15 "data blending" records in the corpus were **parameter usage**; not
  one genuine blend existed, yet five workbooks were penalised;
- the Desktop probe reported `opened` for a project whose **semantic model had
  been deleted**.

Three rules earned their place, and the next phase is built on them:

1. **Measure before building.** Twice, measuring changed what the work was: the
   "conditional formatting" item was really colour loss, and the "data
   blending" floor was really parameter usage.
2. **Prove the detector can fail.** A check that only ever passes is a rubber
   stamp. The Desktop probe passed 26/26 — and then passed four deliberately
   broken projects too.
3. **Build cross-module fixtures from the producer.** Hand-written JSON encodes
   the consumer's assumption. Serialising through the producer's own classes
   caught a wrong key immediately.

## Next phase

### P1 — Make the remaining warnings mean something

Two motifs account for almost every warning in the corpus, and neither is
currently actionable.

- **Unresolved lineage.** *Done — 120 records across 12 of 26 workbooks, now
  0.* Not one was a missing source. The resolver compared every target against
  source *tables* and *columns* only, so three whole categories could never
  match: generated What-If tables (named after their parameter, `Base Salary`,
  so a `startswith("parameter")` test never fired), calculated columns (whose
  source is a calculation), and columns a join merged in from another Tableau
  table (looked up only in the table sharing the target's name). Each is now
  resolved against the category it actually comes from, and `generated` counts
  as resolved for tables and columns as it already did for calculations —
  "I made it" is an answer to "where did this come from?".

  Lineage coverage 99.7% → **99.9%**, and corpus verdicts moved from
  13 PASS / 14 WARN to **17 PASS / 10 WARN**. Six raw records remain genuinely
  unattributable — an escaped name (`Probability \%`), two columns renamed
  during generation, one malformed (`-2,0`) — and the report already excludes
  them because they are not source objects.
- **"Pre-migration assessment contains warnings" — 10 of 26 workbooks.**
  *Done.* One line covered unrecognised connectors, wide schemas and licensing
  limits alike, all owned by "Assessor" and all `decide`. Behind it sat **19
  warnings across 6 categories and 13 distinct checks**, each already carrying
  its own detail and recommendation — text the queue was discarding.

  Each warning is now its own finding, routed by assessment *category* rather
  than by matching words in a check name, which would drift the moment a new
  connector appeared. The queue reads 9 `decide` / 6 `verify` / 4 `note` across
  four owners: connector, volume and licensing warnings go to Deployer as
  environment decisions nobody else can make; conversion warnings ask DAX and
  Visual to confirm their approximations; the rest is context for Semantic.
  Every entry carries the check's own recommendation as its *How* line. Blocking
  failures are now named rather than merely counted.

  Verdicts are deliberately unchanged — this makes the queue readable, it does
  not move a score.

### P2 — Audit the detectors that cannot fail

Five of the nine defects were checks that could not report failure, or statuses
asserted rather than measured. That is now a known failure mode, so look for it
deliberately rather than waiting to trip over the next one.

- **The parity registry, audited.** *Done for the evidence layer.* All 32
  feature statuses are fixed by construction — not one varied across the 27
  corpus workbooks, and 15 features were never exercised by any of them. The
  registry does carry a second, genuinely measurable signal: whether the
  generated project contains the target it claims. That signal was reported as
  `source_only`, a single word covering both "we looked and found nothing" and
  "nothing ever looked" — and only 8 of 32 features had a probe at all, so the
  headline read 36% when 24 features simply had no check.

  Evidence is now three-valued: `evidenced`, `not_found`, `not_checked`, with
  the probed features declared explicitly and unprobed ones kept out of the
  coverage denominator. Coverage reads **77.2%** over what is actually checked,
  with the unchecked count reported beside it instead of hidden inside it.

  The audit found a live mis-count, the same shape as the others: *any* TMDL
  table containing the word "measure" was recorded as evidence of a
  **parameter**. Parameters are now evidenced by the table shapes the generator
  really emits for them (`GENERATESERIES` / `DATATABLE` / `NAMEOF`), and
  hierarchies, sort-by-column and native SQL queries gained probes of their own.
  A test fixture had pinned the wrong behaviour in place, so it was corrected to
  a shape the generator would actually produce.

  Probes were added only where they are *specific*. A measure in TMDL proves
  some calculation converted but not which kind, so no calculation feature was
  given one — claiming otherwise would repeat the mistake being fixed.

- **Newly visible, not yet explained:** three workbooks (Complex_Enterprise,
  vishnu_dashboard, feedback_dashboard) report source filters while their
  generated reports contain no filter at any level and no slicer. The old
  vocabulary could not distinguish this from "not checked". Owner: Visual.
  *Resolved — see below. Making the miss visible is what found it.*

- Still open: statuses outside the parity registry have not been enumerated.
  For each, write the negative control first — what input *should* make this
  fail? If none exists, the check is documentation, and should say so in its
  payload the way the Desktop probe and parity evidence now do.

### P3 — The runtime ceiling

Every workbook sits at `STATIC_PASS` and nothing in the engine can raise it.

- **Desktop is measured and capped.** All 26 projects launch and survive, but
  the probe watches the process, not the document. Raising it needs UI
  automation to read the error dialog, or a headless engine that loads the
  model — a Tabular/AMO path is worth scoping, since those libraries ship with
  Desktop and need no tenant.
- **Semantic execution, refresh and deployment need an authorized
  environment.** This is a decision, not an engineering task: either a
  workspace and credentials exist, or the roadmap should say plainly that these
  signals will stay `not_run` and stop listing them as pending.

### Visual fidelity — sheets that arrive as tables

Raised from reading the generated reports rather than from a report figure: a
large share of visuals were plain tables.

- **Marks-card KPI sheets.** *Done — 28 table-like worksheets, now 12.* A sheet
  that leaves Rows and Columns empty and puts its measure on the Text shelf is
  Tableau's "big number" card. Both the `Text` mark (mapped straight to
  `tableEx`) and the `Automatic` mark (whose inference returns `table` when it
  finds nothing on either shelf) sent them to a grid. They now become a `card`,
  or a `multiRowCard` when several measures share the Text shelf.

- **Packed-bubble sheets.** *Done — four sheets, now treemaps.* With empty
  shelves, a measure on Size and a dimension on Colour is one shape per
  category sized by the measure. The registry's nominal mapping for a packed
  bubble is a scatter chart, but a scatter needs an X and a Y these sheets do
  not have, so it would have degraded straight back to a table; a treemap
  draws exactly what the sheet says. Measured before choosing: all four carry
  one measure on Size, one dimension on Colour, and nothing on Rows or Columns.

- **Still tables, each for its own reason:** four sheets have a genuine
  dimension on Rows and a measure on Columns, so a text table is right; two
  Salesforce sheets mix Text and Columns; one is empty; one is a Heat Map,
  correctly a matrix.

- **Found while looking, then measured properly.** `visual_generator.py` reads
  as the visual builder; it is consumed as a lookup registry. Production
  imports **ten names** from it — three maps and seven helpers — and builds its
  visuals in `pbip_generator`. The other **48 of its 55 top-level functions are
  referenced by no production code at all**, including `_build_visual_filters`
  (measured at zero calls across four migrations), `create_visual_container`,
  `build_query_state` and the tooltip, sparkline, small-multiples and
  sync-group builders. **38 of them are named by a test**, so the module reads
  as covered. `tests/test_visual_generator_surface.py` now pins the ten, so
  wiring a builder in becomes a decision rather than an accident.

  What this does *not* show: that the unreached features are broken or missing.
  Absence in the output only means the corpus never asked. Checking the source
  side, SCRIPT_\* calls and synchronised filter zones are **0 across all 27
  workbooks**, so those two are simply unexercised. Sparklines, small multiples
  and conditional icons remain unverified in either direction.

- **A hypothesis that measurement killed:** visual sort state looked missing —
  11 worksheets carry a sort order and only 2 generated visuals have a
  `sortDefinition`. Nine of those eleven sheets are on no dashboard, so they
  never become visuals at all. Two placed, two emitted: the sort path is
  correct end to end and needed no change.

- **A trap removed:** `pbip_generator.py` was the only file in the repository
  carrying a UTF-8 BOM, which makes `ast.parse` fail on an otherwise valid
  file. It silently excluded the largest generator from this audit's first
  pass. Stripped, and a test now refuses a BOM anywhere in the source tree.

### Filters — the reports that had none

Found by the P2 evidence work, once `not_found` became distinguishable from
`not_checked`.

- **Filters carried no values at all.** *Done — 167 corpus filters reporting
  zero values, now 67 with values.* The workbook-level reader looked for
  `<value>` children; Tableau writes members as the `member` attribute of a
  `<groupfilter>`, with the parent's `function` saying whether they are kept,
  excluded or merely enumerated. The worksheet-level reader already knew this,
  so two functions in one file disagreed about the same XML — the motif this
  cycle keeps finding. They now share one reader. 19 report-level filters are
  generated where there were none.

- **A top-N filter was migrated as a bound on the measure.** `class="topn"
  direction="top" max="10"` means "the ten largest"; its cut-off was read as a
  range and shipped as `amount <= 10`. Now classified as top-N, so the filter
  is *absent* rather than wrong. Expressing it as a Power BI TopN filter is
  open work. The current example corpus contains **0 real Top-N filters**, and
  the repository has no verified PBIR TopN fixture, so this remains deliberately
  unimplemented until a positive producer-to-output test exists.

- **The two filters still reporting `not_found` are honest.** Financial_Report
  holds three "all members selected" filters, which have no condition to
  express, plus the top-N above; feedback_dashboard filters on `:Measure
  Names`, a Tableau virtual field with no Power BI column. Both are reported
  rather than hidden, which is the point.

### Actions — every real one was invisible

- **A real `<action>` has no `type` attribute.** *Done — 29 of 45 corpus
  actions were dropped, now 0.* The kind is a child: `<command>` names brushing
  or filtering, `<link>` is a URL unless its expression is an internal `tsl:`
  link. The reader matched only `@type`, which is the dialect the hand-written
  sample files use — so the 16 typed actions it saw were all synthetic, and
  every action in every genuine workbook was invisible.

- **URL targets were read from the wrong place, then written anyway.** The
  target is the `<url>` element's *text* or `link/@expression`; `@url` returned
  nothing, so buttons shipped with `webUrl: ''`. Reading it correctly then
  exposed the real problem: Tableau interpolates a field
  (`https://crm/customer/<customer_id>`), which a button cannot hold. A button
  is now created only for a static link, so the corpus emits none — an honest
  zero, where before there were 24 buttons pointing at a field reference and
  4 pointing at nothing.

- **One action became one button per page.** *Done — three buttons for one
  action, now one.* The call site filtered on `source_worksheet`, singular,
  which the extractor never emits, so every action passed on every page.
  Tableau names the endpoint on `<source>`/`<target>` as `dashboard`, and only
  sometimes as `worksheet` — all six `<target>` elements in the corpus carry
  `dashboard` alone, which is why `target_worksheets` was empty everywhere and
  no drill-through page was ever built. Both endpoints are now captured, and
  one function decides page ownership. Verified end to end on a workbook whose
  URL was made static: three buttons before, one after, on the page the action
  names.

### P4 — The last two parity gaps

Both are genuine limits rather than measurement errors — the first real feature
work on this list.

- **Per-value aliases.** *Done — value aliases now migrate as a generated
  `'<field> (Display)'` calculated column that maps each value to its Tableau
  label with a `SWITCH`, leaving the source column intact for filtering and
  joins.* Measuring the corpus first changed the shape of the work: of the three
  workbooks that appeared to use value aliases, two were **parameter** value
  labels (`Parameter 1`, `pLastxDays`) already carried by the parameter table's
  Name column, and only one was a genuine column alias. The `alias_value`
  detector was counting the parameter cases too — the same over-count as the old
  `data_blending` floor — so it now excludes parameter names. The status moves
  from `approximated` to `healed`, backed by an evidence probe on the generated
  `displayFolder: Aliases` column. SampleWB scores 100% parity with the alias
  `evidenced`.
- **URL actions.** *Partially done for direct field targets.* A Tableau action
  whose target is exactly `<[datasource].[field]>` now marks the generated
  source column as `dataCategory: WebUrl`, which is the Power BI representation
  for a row-level link. Salesforce was verified end to end: its calculated URL
  field is emitted as `WebUrl`, and the parity evidence is `evidenced` for all
  three source actions. Static URLs still become action buttons. URLs composed
  from free-form placeholders remain suppressed rather than emitted as a wrong
  static button, and their report-level presentation is still open.

## Open decisions

These need a product call, not an engineering one.

- **An authorized environment for runtime evidence** (P3). Everything else on
  this roadmap can be done locally; this cannot.
- **Workbook names in test content assertions.** Fixtures are name-free, but
  ~100 occurrences remain in `tests/` inside layout and regression assertions.
  They are page and visual names — report *content*. Replacing them would
  destroy the point of those tests. Either accept them or accept weaker tests.
- **Unfaithful sample fixtures.** Three hand-written samples declare dashboard
  actions as `<action source=...>` attributes; no genuine Tableau file in the
  corpus uses that shape. The engine is correct and the fixtures are wrong.
  Either regenerate them from real Tableau or accept that they exercise a shape
  the engine will never see in production.
- **Orphaned documentation assets.** `docs/images/logo.svg`,
  `conversions.svg`, `features.svg`, `pipeline.svg` and `light_ui_batch.png`
  are referenced by nothing. Keep for future use or delete.

## Non-goals

Recorded so they are not re-proposed:

- Pre-write TMDL leak healing. Measured: zero Tableau function leaks across 211
  measures in 96 generated TMDL files. The converter already handles them and
  the openability gate blocks anything that slips through.
- Teaching the extractor the attribute-form action shape. That would be fitting
  production code to a bad test input.
- Stripping parameter pseudo-blends at extraction. `blend_graph` deliberately
  receives the raw records and filters them itself; removing them earlier would
  break that contract and its `skip_virtual` option.
- Widening the grade vocabulary to descriptive categories (`native`,
  `generated`, `static_evidence`). Colouring a category as if it were a verdict
  is worse than leaving it plain.
- Failing the corpus gate on warnings. Unrecognised connectors and documented
  approximations are advisory; gating on them would train people to ignore the
  gate.
