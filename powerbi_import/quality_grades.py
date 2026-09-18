"""One grade vocabulary for reports that aggregate several scoring models.

The pipeline scores quality several different ways: migration_quality yields
PASS/WARN/FAIL, assessment yields GREEN/YELLOW/RED, parity_registry yields
FULL/HIGH/PARTIAL for a scan and exact/healed/approximated/unsupported per
feature, and preceptor yields approved/coaching/escalated_* on a 0-5 scale.
Renderers used to translate only the first two, so parity grades and every
preceptor status rendered as unstyled text in the consolidated report.

Some tokens only mean something next to their key: ``HIGH`` is good parity
coverage but a severe issue severity. Those are resolved through ``key``;
without one they stay unmapped rather than being guessed at.

``status`` is deliberately overloaded in the reports — it carries both grades
(``exact``) and descriptive categories (``native``, ``generated``,
``static_evidence``). Only the grade values are mapped, so a category keeps
rendering as plain text instead of being coloured as if it were a verdict.
"""

PASS = 'pass'
WARN = 'warn'
FAIL = 'fail'
NEUTRAL = 'neutral'

#: Exact-match tokens per canonical level, upper-cased.
_DIALECT = {
    PASS: {
        'PASS', 'PASSED', 'GREEN', 'OK', 'TRUE', 'YES',
        'FULL',                 # parity_registry scan grade
        'EXACT', 'HEALED',      # parity_registry feature status
        'APPROVED',             # preceptor
        'VALID', 'EVIDENCED',
        'OPENED', 'REOPENED', 'COMPLETED', 'SUCCESS',
    },
    WARN: {
        'WARN', 'WARNING', 'YELLOW',
        'PARTIAL',              # parity_registry scan grade
        'APPROXIMATED',         # parity_registry feature status
        'COACHING', 'ESCALATED_WARN',   # preceptor
        'NEEDS_REVIEW', 'FALLBACK', 'SOURCE_ONLY', 'DEGRADED',
    },
    FAIL: {
        'FAIL', 'FAILED', 'RED', 'ERROR', 'CRASHED', 'TIMED_OUT',
        'UNSUPPORTED',          # parity_registry feature status
        'ESCALATED_BLOCK',      # preceptor
        'INVALID', 'BLOCKED', 'FALSE', 'NO',
    },
    NEUTRAL: {
        'NOT_RUN', 'UNVERIFIED', 'SKIPPED', 'NONE', 'N/A', 'NA',
        'NOT_AVAILABLE', 'NOT_FOUND', 'NOT_CHECKED', 'PENDING', 'UNKNOWN',
    },
}

_LOOKUP = {token: level for level, tokens in _DIALECT.items() for token in tokens}

#: Tokens whose meaning flips with the field they describe.
_BY_KEY = {
    'grade': {'HIGH': PASS, 'MEDIUM': WARN, 'LOW': WARN},
    'parity_grade': {'HIGH': PASS, 'MEDIUM': WARN, 'LOW': WARN},
    'severity': {'HIGH': FAIL, 'CRITICAL': FAIL, 'MEDIUM': WARN,
                 'LOW': NEUTRAL, 'INFO': NEUTRAL},
    'confidence': {'HIGH': PASS, 'MEDIUM': WARN, 'LOW': WARN},
}

#: Substring fallbacks, applied only when no exact token matches. Kept so that
#: composite values such as "static_diagnostics PASS" keep their old styling.
_SUBSTRING = ((PASS, 'PASS'), (FAIL, 'FAIL'))

_COLOR = {PASS: 'green', WARN: 'yellow', FAIL: 'red', NEUTRAL: 'gray'}


def normalize(value, key=None):
    """Return the canonical level for a status-like value, or None if unknown.

    ``key`` disambiguates tokens that only mean something in context.
    """
    if isinstance(value, bool):
        return PASS if value else FAIL
    if value is None:
        return NEUTRAL
    token = str(value).strip().upper()
    if not token:
        return NEUTRAL

    if key:
        contextual = _BY_KEY.get(str(key).strip().lower())
        if contextual and token in contextual:
            return contextual[token]

    level = _LOOKUP.get(token)
    if level is not None:
        return level
    for candidate, needle in _SUBSTRING:
        if needle in token:
            return candidate
    return None


def color(value, default=None, key=None):
    """Badge colour for a status-like value, or ``default`` when unrecognised."""
    level = normalize(value, key=key)
    return _COLOR.get(level, default) if level else default
