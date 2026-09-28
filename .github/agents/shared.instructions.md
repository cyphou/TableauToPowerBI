---
description: "Shared rules for all agents in the Tableau to Power BI migration project. USE FOR: enforcing project-wide constraints, coding standards, and safety rules."
---

# Shared Project Rules — Tableau to Power BI Migration

All agents MUST follow these rules. They apply to every file in the project.

## Pipeline Architecture

```
.twbx → [Extraction] → 23 JSON files → [Generation] → .pbip (PBIR v4.0 + TMDL)
                                                      → Fabric-native (Lakehouse + Dataflow + Notebook + SemanticModel + Pipeline)
```

- **Source**: `tableau_export/` — extraction + DAX converter + M query builder
- **Target**: `powerbi_import/` — TMDL generator + PBIR report + visual generator + Fabric generators
- **Tests**: `tests/` — latest full run: 10,553 passed, 66 skipped, 1 xfailed (10,620 collected across 312 test files)
- **Docs**: `docs/` — architecture, known limitations, deployment, agent surface, references, roadmap
- **Release gates**: `docs/ROADMAP.md` is authoritative for current scope and gates.

## Hard Constraints

1. **No external dependencies** — Python standard library only for core migration
2. **No duplicate functions** — always `grep_search` for an existing name before creating one
3. **Read before write** — never assume file contents from memory
4. **Test after every change** — run `pytest tests/ --tb=short -q`
5. **Git hygiene** — commit only when tests pass, conventional messages (`feat:`, `fix:`, `test:`, `docs:`)
6. **Identifier safety (Unicode + special chars)** — always preserve and validate field/table identifiers with accents, spaces, and symbols (for example `réalisé`, `%`, `/`, parentheses). Never assume ASCII-only names.
7. **Pre-push privacy and provenance audit** — before pushing to any remote, audit the exact staged/committed scope and all changed documentation, tests, examples, and generated fixtures for personal data, customer/account data, tenant/subscription IDs, private endpoints, credentials, tokens, and unverified third-party content. Do not push while any finding is unresolved.
8. **Generic code only — no report-specific logic** — engine behaviour MUST derive from extracted Tableau metadata/structure, never from matching a specific migrated workbook, report, worksheet, dashboard, field, measure, or datasource by name. Forbidden: `if name == 'Player Stats'`, `worksheet_key in ('teams', ...)`, hardcoded field tokens (`'player'`, `'yr'`), hardcoded filter values, hardcoded connection strings/hostnames/servers. If a heuristic is needed, base it on structural signals (aggregation, role, data type, shelf, cardinality), not on literal names from a migrated report. Tests and fixtures use synthetic or public placeholder data only.
9. **Raise domain errors** — new failure paths raise a `powerbi_import.errors.MigrationError` subclass (`ExtractionError`, `ConversionError`, `GenerationError`, `ValidationError`, `ConfigurationError`, `DeploymentError`) instead of a bare `ValueError`/`RuntimeError`. Resilience boundaries that must never abort a migration should catch `MigrationError` (plus the specific stdlib errors they expect) rather than `Exception`, so genuine defects still surface. When a broad `except Exception` is genuinely required, keep it and state why on the same line.
10. **Declare file ownership** — every module under `powerbi_import/` and `tableau_export/` must be listed by exactly one agent before that agent's `## Constraints` heading. To point at another owner write "owned by **@agent**"; to declare intentional sharing write "co-owned with @agent". `python scripts/check_agent_ownership.py` reports the current state and `tests/test_agent_ownership.py` fails the build on drift.

### Documentation Gate

Before every implementation, release, or documentation update, consult
`@readme` (the Documentation Guardian). It checks `README.md`, `docs/ROADMAP.md`,
`CHANGELOG.md`, relevant technical documentation, links, counts, release claims,
and evidence status. After the change, run its post-update review again. A stale
claim, broken reference, invented metric, or unverified readiness claim blocks the
update until the owning agent reconciles it.

## Python Conventions

- Python 3.12+ compatible
- `unittest.TestCase` for all test classes
- No type annotations on code you didn't write
- No docstrings on code you didn't write
- Prefer smallest change that solves the problem

## Learned Pitfalls (Global)

- Use `elem is not None` instead of `if elem` (Python 3.14 `Element.__bool__()` change)
- `replace_string_in_file` fails on duplicate matches — use unique surrounding context
- Never weaken test assertions to make tests pass
- Stage only files related to the current task
- M `if...then` without `else` causes Power BI M engine error "Token 'else' expected" — always emit `else null`
- M single-quoted strings in `IN {…}` sets must be converted to double-quoted
- `inject_m_steps()` can produce duplicate step names when called multiple times — use dedup suffix
- Calendar `Date.MonthName()`/`Date.DayOfWeekName()` must pass explicit culture parameter
- Connection string values must be escaped with `_m_escape_string()` before M injection
- Regex/parsing for table/field refs must support Unicode identifiers and quoted names; include edge-case tests when touching ref parsing

## Mandatory Pre-Push Privacy Audit

This check is required before every `git push`, including documentation-only
changes and agent/customization changes. It is a publication check, not a
replacement for the security test suite.

1. Inspect `git status`, the staged diff, and the complete list of staged paths.
2. Scan staged text and binary-adjacent metadata for high-confidence secrets:
     API keys, passwords, bearer/JWT tokens, PATs, private URLs, emails, phone
     numbers, tenant/subscription/directory IDs, TPIDs, and connection strings.
3. Review `tests/`, `examples/`, documentation, fixtures, screenshots, and
     generated assets for personal names combined with location/contact data,
     customer or account information, business transactions, or private
     environment metadata.
4. Verify public/example provenance and redistribution rights. A public URL is
     not automatically a redistribution license; record source and license in
     `examples/real_world/SOURCES.md` or remove the asset.
5. Classify every finding as `synthetic`, `public with verified license`,
     `provenance-required`, or `sensitive`. Treat unresolved `provenance-required`
     or `sensitive` findings as a push blocker.
6. Report the scan result in the final response. If a finding is ambiguous,
     stop and ask for confirmation or sanitize/remove the asset before pushing.

Minimum evidence for a clean push:

- staged paths reviewed;
- high-confidence secret scan clean;
- tests/examples/docs reviewed for personal, customer, and business data;
- public provenance checked for newly added assets; and
- the user is told about any remaining provenance or sample-data caveat.

## Preceptorship Report and Agent Handoff

The CLI runs the Python preceptor by default after generation. This scores the
artifact and writes `preceptor_report.json`; it does **not** invoke the Copilot
`@reviewer` agent, dispatch coaching to an owning agent, or apply a fix. The
quality report can carry those coaching items into its finding queue.

For an MCP-connected agent handling an existing migration, pull only your own
items by calling `agent_handoff` with your agent name (for example,
`{"agent":"@dax"}`). The MCP process must first have a current `quality_report`
for the source workbook and project; if its report cache is empty or stale, run
`quality_report` again in that session. Handle only findings returned for your
owner token. The packet is read-only and is not itself an ACK or a resolution.

When an agent or operator receives a review report:
- Check the dimension, evidence, location, and suggested fix.
- Apply an in-scope fix or explain why the finding is not applicable; hand off
     changes outside your ownership boundary.
- Request a fresh review after an actual artifact change. A repeated score of
     unchanged output is not remediation evidence.
- In an MCP session, rerun `quality_report` after a change and compare the new
     findings. For an actual repair, call `agent_handoff_ack` with the handoff ID,
     your agent name, `outcome="applied"`, and a rationale only after the PBIP
     definition changed and the same finding is absent from the fresh report. For
     a non-applicable item, call it with `outcome="not_applicable"` and a rationale.
     ACKs live only in the current MCP process (`persisted: false`); they are not
     durable resolution history or automatic agent dispatch.
- Treat escalation as report/CLI state, not an automatic user interaction.

`--no-preceptor` opts out. `--preceptor-block` opts into a hard failure after
the configured review cycles fail to meet the score threshold. Without an
external artifact change, those cycles can re-score the same output.

### Scoring Dimensions (6)
1. **Completeness** — all source objects mapped to output
2. **DAX Correctness** — valid syntax, no Tableau leakage
3. **M Query Validity** — balanced if/else, proper quoting
4. **TMDL Structure** — relationships, Calendar, RLS
5. **PBIR Fidelity** — visual types, filters, layout
6. **Visual Equivalence** — SSIM screenshot comparison (source vs output)

## Cross-Agent Handoff Protocol

When your task requires work outside your domain:
1. Complete your part fully (including tests for your domain)
2. State clearly what the next agent needs to do
3. List the exact files and functions involved
4. Provide any intermediate artifacts (JSON, dict structures)

## Key References

- Project rules: `.github/copilot-instructions.md`
- Known limitations: `docs/KNOWN_LIMITATIONS.md`
- Roadmap: `docs/ROADMAP.md`
- Deployment guide: `docs/DEPLOYMENT_GUIDE.md`
- Agent architecture: `docs/AGENTS.md`
- Public fixtures only: examples and test fixtures must use public sources or reserved placeholders; never add customer, tenant, account, or private-environment data.

## Cross-Cutting Utilities

- `powerbi_import/security_validator.py` — Shared security module (path validation, ZIP slip defense, XXE protection, credential redaction). Used by Extractor, Orchestrator, Deployer.
- `powerbi_import/recovery_report.py` — Self-healing recovery tracker. Used by Generator (TMDL self-repair, visual fallback).
