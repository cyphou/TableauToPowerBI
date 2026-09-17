"""One grade vocabulary for reports that aggregate several scoring models.

The pipeline scores quality four different ways: migration_quality yields
PASS/WARN/FAIL, assessment yields GREEN/YELLOW/RED, parity_registry yields
FULL/HIGH/PARTIAL, and preceptor yields approved/coaching/escalated_* on a 0-5
scale. Renderers used to translate only the first two, so parity grades and
every preceptor status rendered as unstyled text in the consolidated report.

Only unambiguous tokens are mapped. ``HIGH`` is deliberately absent: it means
"good" for parity coverage but "severe" for an issue severity, and this helper
has no context to tell them apart.
"""

PASS = 'pass'
WARN = 'warn'
FAIL = 'fail'
NEUTRAL = 'neutral'

#: Exact-match tokens per canonical level, upper-cased.
_DIALECT = {
    PASS: {
        'PASS', 'PASSED', 'GREEN', 'OK', 'TRUE', 'YES',
        'FULL',                 # parity_registry
        'APPROVED',             # preceptor
        'OPENED', 'REOPENED', 'COMPLETED', 'SUCCESS',
    },
    WARN: {
        'WARN', 'WARNING', 'YELLOW',
        'PARTIAL',              # parity_registry
        'COACHING', 'ESCALATED_WARN',   # preceptor
        'DEGRADED',
    },
    FAIL: {
        'FAIL', 'FAILED', 'RED', 'ERROR', 'CRASHED', 'TIMED_OUT',
        'ESCALATED_BLOCK',      # preceptor
        'INVALID', 'BLOCKED', 'FALSE', 'NO',
    },
    NEUTRAL: {
        'NOT_RUN', 'UNVERIFIED', 'SKIPPED', 'NONE', 'N/A', 'NA',
        'NOT_AVAILABLE', 'NOT_FOUND', 'PENDING', 'UNKNOWN',
    },
}

_LOOKUP = {token: level for level, tokens in _DIALECT.items() for token in tokens}

#: Substring fallbacks, applied only when no exact token matches. Kept so that
#: composite values such as "static_diagnostics PASS" keep their old styling.
_SUBSTRING = ((PASS, 'PASS'), (FAIL, 'FAIL'))

_COLOR = {PASS: 'green', WARN: 'yellow', FAIL: 'red', NEUTRAL: 'gray'}


def normalize(value):
    """Return the canonical level for a status-like value, or None if unknown."""
    if isinstance(value, bool):
        return PASS if value else FAIL
    if value is None:
        return NEUTRAL
    token = str(value).strip().upper()
    if not token:
        return NEUTRAL
    level = _LOOKUP.get(token)
    if level is not None:
        return level
    for candidate, needle in _SUBSTRING:
        if needle in token:
            return candidate
    return None


def color(value, default=None):
    """Badge colour for a status-like value, or ``default`` when unrecognised."""
    level = normalize(value)
    return _COLOR.get(level, default) if level else default
