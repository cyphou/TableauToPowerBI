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
- **Tests**: `tests/` — 9,500+ tests across the current test suites
- **Docs**: `docs/` — architecture, known limitations, deployment, agent surface, references, roadmap
- **Current release**: v44.0.0. Next gates are v45 Fabric contract/performance and v46 Desktop reliability/release discipline; `docs/ROADMAP.md` is authoritative.

## Hard Constraints

1. **No external dependencies** — Python standard library only for core migration
2. **No duplicate functions** — always `grep_search` for an existing name before creating one
3. **Read before write** — never assume file contents from memory
4. **Test after every change** — run `pytest tests/ --tb=short -q`
5. **Git hygiene** — commit only when tests pass, conventional messages (`feat:`, `fix:`, `test:`, `docs:`)
6. **Identifier safety (Unicode + special chars)** — always preserve and validate field/table identifiers with accents, spaces, and symbols (for example `réalisé`, `%`, `/`, parentheses). Never assume ASCII-only names.

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

## Preceptorship Loop — Quality Gate

All generation agents participate in the **preceptorship loop** before artifacts are finalized:

```
DRAFT (Agent) → REVIEW (@reviewer) → APPROVE? (≥ 4★?)
     ↑                                    │
     │              YES ──────────────────→ DONE
     │               NO ──────────────────→ COACH (feedback)
     │                                        │
     └────────────────────────────────────────┘
                   (max 3 cycles, then escalate)
```

### Rules
- After generating artifacts, the pipeline invokes `@reviewer` for quality scoring
- If scored < 4★, read the coaching feedback and apply fixes within your domain
- Do NOT ignore coaching items — address each one or explain why it's not applicable
- After 3 cycles, the reviewer escalates to the user (accept-with-warnings or block)
- The review is read-only — `@reviewer` never modifies your files directly

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
