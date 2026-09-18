# Development Roadmap

The engine migrates Tableau workbooks to Power BI (PBIP/PBIR v4.0 + TMDL) or
Fabric-native artifacts. Static local validation is the release gate; Desktop,
semantic execution, refresh, and deployment evidence remain `not_run` unless an
authorized environment proves otherwise.

## Where we stand

Measured on the committed example corpus (44 artefacts: 27 workbooks, 17 prep
flows), not estimated:

| Signal | Value |
|---|---|
| Migration success | 44/44, 0 failures |
| Opens in PBI Desktop (static gate) | 27/27 |
| Quality verdicts | 13 PASS, 14 WARN, 0 FAIL, 0 blockers |
| Functional parity | 18 workbooks FULL (≥99%), 9 HIGH (≥90%), lowest 91.7% |
| Test suite | 9,875 passed, 67 skipped, 1 xfailed |
| Agent ownership | 132 modules, 0 unowned, 0 asymmetric declarations |

Residual warnings are legitimate and documented: unrecognised connectors to
verify by hand, wide schemas, LOD complexity, and explicit visual
approximations. They are advisory, not defects.

## What the last cycle proved about method

Four defects were found by **probing generated output**, not by reading code,
and each was a vocabulary mismatch between a producer and a consumer:

- the preceptor scored DAX clean that the validator rejected, because the two
  kept separate lists of Tableau functions;
- the healer repaired syntax but left `COUNTD` in place, so autoheal could
  report success on DAX that will not load;
- the shared badge helper knew `APPROXIMATE` while the parity registry emits
  `APPROXIMATED`, so two of four parity statuses rendered as neutral grey in
  all nine HTML reports;
- the lineage inventory looked for `worksheet` while the extractor emits
  `source_worksheets`, reporting 216 healthy objects as orphans.

The lesson drives the next phase: the same value must mean the same thing at
every hop, and the only reliable way to find out is to run the pipeline and
read what it wrote.

## Next phase

### P1 — Close the producer/consumer contract gap

The four defects above were all found by accident. They should be impossible to
reintroduce silently.

- **Contract tests between stages.** *Started.*
  `tests/test_extraction_contract.py` extracts genuine workbooks, derives the
  field names actually emitted per object type, and asserts the constants
  consumers depend on intersect them. It found three gaps on its first run:
  filters, Hyper extracts and blending links exposed no name-like key, so the
  inventory listed them as `item-0`. Extend the same shape to the remaining
  hand-offs (parity → renderer, validator → preceptor → healer).
- **A key-name lint.** *Done.* `scripts/check_field_names.py` tracks which
  variables hold extracted objects of which type, groups fallback chains into
  one logical value, and compares against both the vocabulary a real extraction
  produces and the keys the extractor assigns anywhere. Observation proves a key
  is emitted but never that it is absent, so the declared-key cross-check
  matters: `hyper_files['tables']` is set on a branch the sample corpus does not
  take. It reported 50 candidates before those refinements and 3 after.

  It surfaced a blind spot worth naming: **tests fabricate keys the extractor
  never produces**. `test_conditional_formatting` builds a worksheet carrying
  `conditionalFormatting` and `test_v51_features` one carrying
  `dynamic_visibility`; both pass, while on a real workbook neither code path
  fires. Fixed where the data exists — dynamic zone visibility is now read from
  the dashboard, where the extractor records it, and the comparison report now
  reads `chart_type` rather than falling through to the mark-encoding type.
  Conditional formatting is genuinely not extracted, so counting it reports zero
  on every workbook; extracting it is a feature, left open deliberately.
- **Corpus assertion in CI.** *Done.* `scripts/check_corpus_gate.py` migrates
  all three batches and enforces the floors static validation can prove — every
  artefact migrates, every project passes the openability preflight, no
  blockers. Warnings stay advisory. Wired as the `corpus-gate` job;
  `tests/test_corpus_gate.py` proves the gate fails when a project will not
  open, when a blocker appears, and when a verdict is missing or unreadable.

### P2 — Make the quality report answer "what do I fix first?"

The report is now accurate — every verdict is coloured and no category is
mistaken for one — but it still lists findings rather than ranking them.

- **Rank warnings by what they require.** *Done.* Every finding records the
  action it needs — `repair` (measurably wrong, the pipeline can fix it),
  `decide` (a human judgement is required first), `verify` (we approximated;
  confirm it matches intent) or `note` (evidence, nothing to act on) — at the
  point it is raised. The queue sorts blockers first, then repair before decide
  before verify before note. Owners come from the same record; they used to be
  guessed by searching the message text for "DAX" or "table", the same brittle
  pattern that let producers and consumers drift apart elsewhere.
- **Make the preceptor read what the generator writes.** *Done.* Before wiring
  its coaching into the report, the reviewer itself had to be trusted — and it
  was not. Six of its checks consumed a shape the generator never produces, so
  it penalised correct output while leaving real defects unseen:
  - It read *annotations* as DAX. `Copilot_Description` preserves the original
    Tableau formula on purpose, so every `COUNTD` recorded there was reported
    as a leak, and every truncated annotation as an unbalanced parenthesis.
  - It looked for `definition.pbir` inside `definition/`; the generator writes
    it beside that folder.
  - It read report-level filters from a bare `filters` key; PBIR nests them
    under `filterConfig`.
  - It demanded a table literally named `Calendar`, though the generator
    deliberately skips auto-Calendar when the source already ships a date
    dimension such as `dim_date`.
  - It expected a visual for *every* worksheet, including those never placed on
    a dashboard, which map to no PBI artifact.
  - **The M dimension inspected no M at all.** It scanned fenced ``` blocks,
    but M partitions are written as a bare `source =` with an indented body —
    fences hold calculated-table *DAX*. It therefore scored 5/5 by looking at
    nothing, and its one finding was valid `NAMEOF('Table'[Col])` DAX misread
    as an M string literal.

  Each check now reuses the producer's own vocabulary (`_DATE_TABLE_NAMES`,
  `_is_non_restrictive`, `_dashboard_worksheet_names`) instead of restating it.
  Across the real-world corpus the mean rose from 4.92 to 5.00 with every
  finding eliminated — and a negative-control suite proves injected leaks,
  paren imbalances, M `if`/`else` gaps, single-quoted M sets, missing PBIR and
  absent date tables are all still caught.
- **Carry the preceptor's coaching feedback into the consolidated report.** *Done.*
  The review was the only surface that said *how* to fix something, and it was
  reachable only via `--preceptor`. The quality report now reads
  `preceptor_report.json` when it is present beside the project — the review
  stays opt-in, and its absence reads as `not_run` rather than as a failure.
  Each coaching item enters the remediation queue as a `repair` owned by the
  agent that owns the artifact (DAX, Wiring, Semantic, Visual, Orchestrator)
  instead of the generic "Assessor", carries its `fix` text and the file it
  was raised against, and renders as a "How:" line in the HTML. Visual
  equivalence asks to `verify` rather than `repair`, because screenshot
  similarity is a judgement and not a measurable defect. An escalated review
  becomes a blocker (`escalated_block`) or a warning (`escalated_warn`).
- **Extract conditional formatting.** *Done, and the defect was not the one
  recorded here.* Tableau has no separate conditional-formatting object: the
  rules are the per-value colours and stepped thresholds of a colour encoding,
  so the `conditionalFormatting` key the assessment counted was a vocabulary no
  extractor could ever fill.

  Measuring the corpus found a larger loss underneath. Tableau splits a colour
  encoding in two — the worksheet names the field and the palette, while the
  per-value colours are stored once per datasource as
  `<map to="#hex"><bucket>value</bucket></map>` — and only the worksheet half
  was read. Of 114 `<color>` elements across the corpus **none** carries a
  palette attribute and **no** `<bucket>` carries a colour attribute, so the
  palette and threshold paths were both unreachable: 72 colour-encoded
  worksheets produced 0 coloured visuals.

  The halves are now joined and custom palettes resolve against their
  document-level `<color-palette>` definitions. 50 worksheets gained per-value
  colours and **46 of 284 visuals now carry colour that was silently dropped**.
  The assessment counts those rules instead of a key nobody emits. Corpus gate
  unchanged: 27 openable, 0 blockers.

### P3 — Runtime evidence

Every runtime signal is still `not_run`: Desktop open, semantic execution,
refresh, deployment. Static validation has taken the corpus as far as it can.

- Desktop probe is best-effort and opt-in; decide whether an authorized
  environment exists to make it a gate, or state plainly that it never will be.
- Semantic execution against a real model would turn parity scores from
  structural coverage into verified behaviour.

### P4 — Fidelity where the corpus is weakest

- **Field aliases are no longer dropped.** *Done.* All four floor workbooks
  reported the same single gap, and its status was hardcoded `APPROXIMATED` in
  the feature table rather than measured — so any workbook using aliases was
  docked regardless of the result.

  Measuring showed the family covers two capabilities with different fidelity.
  Aliases that rename an *aggregated field* (`sum:F: GDP (curr $)` →
  "GDP (US $'s)") were simply lost: **0 of 17 reached the model**, because
  Tableau aggregates implicitly and there was no named object to carry the
  caption. An explicit measure is now generated — added, never renamed, so no
  reference can break — and `rank:` prefixes wrap it in `RANKX(ALL(...))`,
  the convention the DAX converter already uses. All 17 are now applied.
  Aliases that rename *individual values* (`%null%` → " ") have no Power BI
  equivalent and stay approximated.

  The registry feature is split accordingly (`alias_measure_name` healed,
  `alias_value` approximated, registry 1.2.0). Effect on the floors:
  `feedback_dashboard` 91.7% → **100%**, `RESTAPISample` and `SampleWB`
  96.7% → 98.6%, `World Indicators` 97.8% → 99.0%. Corpus mean 98.4% → 98.9%,
  workbooks at full parity 16 → 17 of 26. Corpus gate unchanged.

- **The remaining floor is data blending**, not aliases: `shapes_test` 90.9%,
  `nba_player_stats` 93.3%, `vishnu_dashboard` 94.7%. Two `URL action` gaps
  (`Enterprise_Sales`, `Complex_Enterprise`) are the only other family left.

## Open decisions

These need a product call, not an engineering one. They are listed because they
have been deferred more than once.

- **Workbook names in test content assertions.** Fixtures are name-free, but
  ~100 occurrences remain in `tests/` inside layout and regression assertions
  (`test_layout_regression`, `test_non_regression`,
  `test_performance_regression`). They are page and visual names — report
  *content*. Replacing them would destroy the point of those tests. Either
  accept them or accept weaker tests.
- **Unfaithful sample fixtures.** Three hand-written samples declare dashboard
  actions as `<action source=...>` attributes; no genuine Tableau file in the
  corpus uses that shape. The engine is correct and the fixtures are wrong.
  Either regenerate them from real Tableau or accept that they exercise a
  shape the engine will never see in production.

## Non-goals

Recorded so they are not re-proposed:

- Pre-write TMDL leak healing. Measured: zero Tableau function leaks across 211
  measures in 96 generated TMDL files. The converter already handles them and
  the openability gate blocks anything that slips through.
- Teaching the extractor the attribute-form action shape. That would be fitting
  production code to a bad test input.
- Widening the grade vocabulary to descriptive categories (`native`,
  `generated`, `static_evidence`). Colouring a category as if it were a verdict
  is worse than leaving it plain.
