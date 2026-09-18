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
| Quality verdicts | 13 PASS, 14 WARN, 0 FAIL |
| Outstanding repairs | **0** — nothing measurably broken remains |
| Functional parity | mean **99.7%**, lowest **96.4%**, 20 of 26 at full parity |
| Evidence level | `STATIC_PASS` on 26/26 |
| Test suite | 9,988 passed, 67 skipped, 1 xfailed, across 274 files |
| Agent ownership | 0 unowned modules, 0 asymmetric declarations |

The remediation queue now contains **no `repair` actions at all**: 10 `decide`,
12 `note`, 2 `verify`. Everything left either needs a human judgement or is
evidence with nothing to act on. That is the milestone this cycle reached, and
it changes what the next one should be about.

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

- **Unresolved lineage — 120 records across 12 of 26 workbooks**, one workbook
  alone accounting for 65. Sampling them shows the resolver compares targets
  against the wrong source category: the 19 unresolved `tables` are generated
  **parameter** tables (`Base Salary`, `Last x Days`) whose source is a
  parameter, not a table, and the 101 unresolved `columns` are **calculated**
  columns whose source is a calculation, not a column. This is the same
  category mismatch as the defects above, one level up. Either resolve across
  source categories or stop reporting generated artifacts as orphans — but
  decide on evidence, not by suppressing the count.
- **"Pre-migration assessment contains warnings" — 10 of 26 workbooks.** One
  line covers unrecognised connectors, wide schemas and LOD complexity, all
  owned by "Assessor" and all `decide`. A reader cannot act on it. Split it by
  what the warning actually is, so each carries its own action and owner, the
  way findings already do elsewhere.

### P2 — Audit the detectors that cannot fail

Five of the nine defects were checks that could not report failure, or statuses
asserted rather than measured. That is now a known failure mode, so look for it
deliberately rather than waiting to trip over the next one.

- Enumerate every status the pipeline reports and classify it: **measured**,
  **asserted** (a fixed capability claim), or **unfalsifiable** (no input could
  make it fail). The `alias` status was asserted; the Desktop probe is
  unfalsifiable for content.
- For each check, write the negative control first: what input *should* make
  this fail? If none exists, the check is documentation, and should say so in
  its payload the way the Desktop probe now does.
- The parity registry is the densest concentration of asserted statuses — every
  `Feature` carries a fixed verdict. Some are genuine capability statements;
  others, as `alias` was, are measurable per workbook.

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

### P4 — The last two parity gaps

Both are genuine limits rather than measurement errors — the first real feature
work on this list.

- **Per-value aliases (4 workbooks).** Power BI has no per-value alias, but the
  renaming is expressible as a Power Query value replacement or a lookup
  column. Worth scoping against how often it changes meaning rather than
  cosmetics.
- **URL actions (2 workbooks).** Maps to a Power BI action button or a
  conditional URL measure.

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
