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

- **Contract tests between stages.** For each hand-off (extractor → inventory,
  parity → renderer, validator → preceptor → healer), assert that the keys and
  vocabularies one side emits are the ones the other side reads. The existing
  `test_quality_grades` and `test_agent_ownership` guards are the shape to
  follow: they fail the build on drift rather than reporting it.
- **A key-name lint.** Every consumer that reads a field by name from extracted
  JSON should be checked against the extractor's actual output on the corpus.
  `source_worksheets` vs `worksheet` was invisible for as long as nobody
  compared the two.
- **Corpus assertion in CI.** The migration of `examples/` currently proves
  itself only when run by hand. Wire the 44-artefact run into CI with a floor on
  openability (27/27) and blockers (0) so a regression cannot merge.

### P2 — Make the quality report answer "what do I fix first?"

The report is now accurate — every verdict is coloured and no category is
mistaken for one — but it still lists findings rather than ranking them.

- Rank warnings by remediation cost and blast radius, not by category.
- Distinguish "needs a human decision" (unrecognised connector) from "we could
  not convert this" (unsupported table calculation). Today they read alike.
- Carry the preceptor's coaching feedback into the consolidated report; it is
  the only surface that says *how* to fix something, and it is currently
  reachable only via `--preceptor`.

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
