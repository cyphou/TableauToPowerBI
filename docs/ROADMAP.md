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
- **Carry the preceptor's coaching feedback into the consolidated report.** It
  is the only surface that says *how* to fix something, and it is currently
  reachable only via `--preceptor`.
- **Extract conditional formatting.** Assessment counts it, but the extractor
  never produces it, so every workbook reports zero rules. Either extract it or
  stop claiming a count.

### P3 — Runtime evidence

Every runtime signal is still `not_run`: Desktop open, semantic execution,
refresh, deployment. Static validation has taken the corpus as far as it can.

- Desktop probe is best-effort and opt-in; decide whether an authorized
  environment exists to make it a gate, or state plainly that it never will be.
- Semantic execution against a real model would turn parity scores from
  structural coverage into verified behaviour.

### P4 — Fidelity where the corpus is weakest

Parity floors sit at 91.7% (`feedback_dashboard`) and ~96% for three others.
Those four workbooks are the honest backlog: work the specific approximations
they report rather than chasing an aggregate.

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
