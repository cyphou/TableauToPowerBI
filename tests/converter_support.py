"""Shared fixtures for the Tableau -> PySpark converter tests (not a test module)."""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from powerbi_import.pyspark_converter import Ref, convert_to_pyspark  # noqa: E402
from powerbi_import.pyspark_gate import validate_statement  # noqa: E402

# Field name -> Tableau datatype.  ``u`` has no usable type on purpose.
TYPES = {
    'a': 'integer', 'b': 'integer', 'c': 'integer', 'n': 'integer',
    'x': 'real', 'y': 'real',
    's': 'string', 't': 'string',
    'flag': 'boolean', 'g': 'boolean',
    'd': 'date', 'e': 'date', 'ts': 'datetime',
    'u': 'unknown',
}


def resolve(qualifier, name):
    """Resolver used by the tests: the fields above, nothing qualified."""
    if qualifier is not None:
        return Ref('missing', reason=f'qualified {qualifier}.{name}')
    if name not in TYPES:
        return Ref('missing', reason=f'unknown field {name}')
    return Ref('column', name=name, datatype=TYPES[name])


def convert(formula, resolver=resolve):
    return convert_to_pyspark(formula, resolver)


def expression_of(formula, resolver=resolve):
    """Return the generated expression, failing loudly when the formula is refused."""
    conversion = convert(formula, resolver)
    assert conversion.ok, f'{formula!r} was refused: {conversion.reason}'
    return conversion.expression


def gate_reason(expression, name='c'):
    """Return the gate's refusal of ``withColumn(name, expression)``, or None when accepted."""
    from powerbi_import.pyspark_converter import with_column_statement
    return validate_statement(with_column_statement(name, expression), list(TYPES))


def fields_in(formula):
    """Distinct simple ``[field]`` names of ``formula``, in order of appearance.

    An independent oracle for ``Conversion.columns``: a plain regular expression over
    the Tableau text (string literals removed first), not the converter's tokenizer.
    """
    seen = []
    text = re.sub(r'"[^"]*"|\'[^\']*\'', '', formula)
    for name in re.findall(r'\[(\w+)\]', text):
        if name not in seen:
            seen.append(name)
    return seen
