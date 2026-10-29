"""Plan which Tableau calculations become Delta columns, and in which table.

The Fabric PySpark notebook and the Lakehouse definition must agree on which
calculated columns exist, how they are named and which table holds them.  This
module derives that once from the extracted workbook so both generators read
the same plan.  It performs no I/O and does not raise on malformed input: a
calculation that cannot be planned is reported with a reason instead.

Every calculation ends up as one of:

* a *materialised* column: a PySpark ``withColumn`` statement that passed the
  safety gate, placed in the one Delta table that holds every field it reads;
* a *pending* column: row-level in nature but not converted, with the reason
  (unsupported function, unresolved field, fields spread over several tables,
  a type that contradicts the declared one, a circular reference, ...);
* a *measure*: kept in the semantic model because it is not row-level
  (aggregates, level-of-detail expressions, table calculations, user functions,
  formulas that read no field, formulas that read a measure);
* inlined: parameters and constant calculations are substituted where they are
  referenced, frozen at their migration-time value.

Nothing in the plan was executed on Spark.
"""

import heapq
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from .calc_column_utils import (
    calc_spark_type,
    is_calc_column_candidate,
    sanitize_calc_col_name,
)
from .fabric_naming import sanitize_column_name, sanitize_table_name
from .pyspark_converter import (
    T_BOOLEAN,
    T_DATE,
    T_DATETIME,
    T_INTEGER,
    T_REAL,
    T_STRING,
    T_UNKNOWN,
    Ref,
    collect_field_refs,
    contains_lod,
    convert_to_pyspark,
    literal_ref,
    normalize_datatype,
    validate_statement,
    with_column_statement,
)

logger = logging.getLogger(__name__)

__all__ = ['CalcColumnPlan', 'PlannedColumn', 'PlannedMeasure', 'plan_calculated_columns']

# Declared and inferred types are only compared by family: Spark infers its own
# result type, so integer versus real is not a contradiction.
_FAMILY = {
    T_INTEGER: 'number', T_REAL: 'number', T_STRING: 'string',
    T_BOOLEAN: 'boolean', T_DATE: 'datetime', T_DATETIME: 'datetime',
}


def _items(value):
    """Return ``value`` when it is a list, otherwise an empty list (malformed input is ignored)."""
    return value if isinstance(value, (list, tuple)) else []


def _text(value):
    """Return ``value`` when it is a string, otherwise an empty string."""
    return value if isinstance(value, str) else ''


# ── Plan data ──────────────────────────────────────────────────────

@dataclass
class PlannedColumn:
    """A row-level calculation: materialised when ``statement`` is set."""

    caption: str
    internal: str
    datasource: str
    table: str
    delta_table: str
    delta_name: str
    datatype: str
    spark_type: str
    formula: str
    statement: str | None = None
    reason: str | None = None
    notes: list = field(default_factory=list)
    inferred_type: str = ''

    @property
    def materialised(self):
        return self.statement is not None


@dataclass
class PlannedMeasure:
    """A calculation kept in the semantic model rather than materialised."""

    caption: str
    internal: str
    formula: str
    reason: str


@dataclass
class CalcColumnPlan:
    """Result of :func:`plan_calculated_columns`.

    ``columns`` is in dependency order: a column always follows the calculated
    columns it reads.  ``table_order`` lists the Delta tables in the order the
    Lakehouse generator creates them.
    """

    columns: list = field(default_factory=list)
    measures: list = field(default_factory=list)
    table_order: list = field(default_factory=list)

    @property
    def materialised(self):
        return [col for col in self.columns if col.materialised]

    @property
    def pending(self):
        return [col for col in self.columns if not col.materialised]

    def by_table(self):
        """Return ``[(delta_table, [PlannedColumn, ...])]`` in table order."""
        groups = {}
        for col in self.columns:
            if col.delta_table:
                groups.setdefault(col.delta_table, []).append(col)
        return [(name, groups[name]) for name in self.table_order if name in groups]

    def unplaced(self):
        """Pending columns that could not be assigned to any table."""
        return [col for col in self.columns if not col.delta_table]


# ── Physical tables ────────────────────────────────────────────────

class _Table:
    """A Delta table as the Lakehouse generator creates it."""

    def __init__(self, delta, original, order, columns):
        self.delta = delta
        self.original = original
        self.order = order
        self.columns = columns
        self.by_fold = defaultdict(list)
        self.taken = set()
        self.created = []
        for display, datatype in columns:
            self.by_fold[display.casefold()].append((display, datatype))
            self.taken.add(display.casefold())
            self.taken.add(sanitize_column_name(display).casefold())

    def available(self):
        """Every column a statement placed in this table may read."""
        return [display for display, _ in self.columns] + self.created


class _Registry:
    """Delta tables of the workbook; the first datasource to declare a table wins."""

    def __init__(self, datasources):
        self.tables = {}
        self.by_datasource = {}
        self._scopes = {}
        for ds in _items(datasources):
            if not isinstance(ds, dict):
                continue
            deltas = self.by_datasource.setdefault(_text(ds.get('name')), [])
            for table in _items(ds.get('tables')):
                if not isinstance(table, dict):
                    continue
                original = _text(table.get('name'))
                delta = sanitize_table_name(original)
                if delta not in self.tables:
                    columns = [
                        (col['name'], normalize_datatype(col.get('datatype')))
                        for col in _items(table.get('columns'))
                        if isinstance(col, dict) and isinstance(col.get('name'), str) and col['name']
                    ]
                    self.tables[delta] = _Table(delta, original, len(self.tables), columns)
                if delta not in deltas:
                    deltas.append(delta)

    def scope_tables(self, scope):
        """Tables a calculation of datasource ``scope`` may read; '' means every table."""
        cached = self._scopes.get(scope)
        if cached is None:
            names = self.by_datasource.get(scope, []) if scope else list(self.tables)
            cached = [self.tables[name] for name in names if self.tables[name].columns]
            self._scopes[scope] = cached
        return cached


# ── Parameters ─────────────────────────────────────────────────────

def _strip_brackets(name):
    name = (name or '').strip() if isinstance(name, str) else ''
    if len(name) >= 2 and name.startswith('[') and name.endswith(']'):
        return name[1:-1]
    return name


def _parameter_value(typ, raw):
    """Return the Python value of a parameter default, or ``None`` when unusable."""
    if isinstance(raw, bool):
        return raw if typ == T_BOOLEAN else None
    if isinstance(raw, int):
        return raw if typ == T_INTEGER else (float(raw) if typ == T_REAL else None)
    if isinstance(raw, float):
        return raw if typ == T_REAL else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    parsed = literal_ref(text)
    if typ == T_STRING:
        if parsed is not None and parsed.datatype == T_STRING:
            return parsed.value
        return None if text[0] in '"\'' else text
    if parsed is None:
        return None
    if typ == T_INTEGER:
        return parsed.value if parsed.datatype == T_INTEGER else None
    if typ == T_REAL:
        return float(parsed.value) if parsed.datatype in (T_INTEGER, T_REAL) else None
    if typ == T_BOOLEAN:
        return parsed.value if parsed.datatype == T_BOOLEAN else None
    return None


def _parameter_ref(caption, datatype, raw):
    typ = normalize_datatype(datatype)
    label = f"parameter '{caption}'"
    if typ in (T_DATE, T_DATETIME):
        return Ref('missing', reason=f'{label} of type {typ} is not inlined')
    value = _parameter_value(typ, raw)
    if value is None:
        return Ref('missing', reason=f'{label} has no usable default value')
    return Ref('literal', datatype=typ, value=value,
               note=f'{label} is frozen at its default value {value!r}')


class _Parameters:
    """Workbook parameters, found by internal name or caption."""

    def __init__(self, data):
        self._by_internal = {}
        self._by_caption = {}
        for param in _items(data.get('parameters')):
            if isinstance(param, dict):
                self._add(param.get('name'), param.get('caption'),
                          param.get('datatype'), param.get('value'))
        for calc in _items(data.get('calculations')):
            if isinstance(calc, dict) and calc.get('datasource_name') == 'Parameters':
                self._add(calc.get('name'), calc.get('caption'),
                          calc.get('datatype'), calc.get('formula'))

    def _add(self, name, caption, datatype, raw):
        internal = _strip_brackets(name)
        caption = (caption or '').strip() if isinstance(caption, str) else ''
        caption = caption or internal
        key = (internal or caption).casefold()
        if not key or key in self._by_internal:
            return
        ref = _parameter_ref(caption, datatype, raw)
        self._by_internal[key] = ref
        self._by_caption.setdefault(caption.casefold(), ref)

    def get(self, name):
        fold = name.casefold()
        return self._by_internal.get(fold) or self._by_caption.get(fold)


# ── Calculations ───────────────────────────────────────────────────

@dataclass
class _Calc:
    index: int
    caption: str
    internal: str
    scope: str
    datatype: str
    formula: str
    state: str
    reason: str = ''
    literal: Ref | None = None
    planned: PlannedColumn | None = None


class _Hit(NamedTuple):
    """Outcome of looking a field reference up (``kind`` names the target)."""

    kind: str
    tables: tuple = ()
    calc: Any = None
    ref: Any = None


class _Resolver:
    """Resolves the field references of one calculation for the converter."""

    def __init__(self, planner, calc, table):
        self.planner = planner
        self.calc = calc
        self.table = table
        self.measure_reason = None

    def __call__(self, qualifier, name):
        hit = self.planner.lookup(self.calc.scope, qualifier, name)
        if hit.kind == 'column':
            delta = self.table or hit.tables[0]
            display, datatype = self.planner.registry.tables[delta].by_fold[name.casefold()][0]
            return Ref('column', name=display, datatype=datatype)
        if hit.kind == 'calc':
            return self._nested(hit.calc)
        if hit.kind == 'measure':
            reason = f"references the measure '{hit.calc.caption}' (kept as a DAX measure)"
            self.measure_reason = self.measure_reason or reason
            return Ref('missing', reason=reason)
        return hit.ref

    @staticmethod
    def _nested(calc):
        planned = calc.planned
        if calc.state == 'column':
            datatype = planned.inferred_type
            if datatype in ('', T_UNKNOWN, 'null'):
                datatype = planned.datatype
            return Ref('column', name=planned.delta_name, datatype=datatype)
        if calc.state == 'pending':
            return Ref('missing', reason=(
                f"depends on '{calc.caption}', which is not materialised: {planned.reason}"))
        return Ref('missing', reason=f"depends on '{calc.caption}', which was not planned")


class _Planner:
    def __init__(self, data):
        self.registry = _Registry(data.get('datasources'))
        self.params = _Parameters(data)
        self.calcs = []
        self._by_internal = defaultdict(dict)
        self._by_caption = defaultdict(dict)
        self._collect(data.get('calculations'))

    # collection and first classification
    def _collect(self, calculations):
        seen = set()
        for raw in _items(calculations):
            if not isinstance(raw, dict):
                continue
            formula = raw.get('formula')
            formula = formula.strip() if isinstance(formula, str) else ''
            scope = _text(raw.get('datasource_name'))
            if not formula or scope == 'Parameters':
                continue
            internal = _strip_brackets(raw.get('name'))
            caption = raw.get('caption')
            caption = (caption.strip() if isinstance(caption, str) else '') or internal or 'calculation'
            key = (scope, internal.casefold())
            if internal and key in seen:
                continue
            seen.add(key)
            state, reason, literal = self._first_state(raw, formula, caption)
            calc = _Calc(len(self.calcs), caption, internal, scope,
                         _text(raw.get('datatype')) or 'string', formula, state, reason, literal)
            self.calcs.append(calc)
            if internal:
                self._by_internal[scope].setdefault(internal.casefold(), calc)
            self._by_caption[scope].setdefault(caption.casefold(), calc)

    @staticmethod
    def _first_state(raw, formula, caption):
        if contains_lod(formula):
            return 'measure', 'level-of-detail expression (kept as a DAX measure)', None
        if is_calc_column_candidate(raw):
            return 'candidate', '', None
        if '[' not in formula:
            lit = literal_ref(formula)
            if lit is not None:
                note = f"calculation '{caption}' is inlined as the constant {lit.value!r}"
                return 'literal', '', Ref('literal', datatype=lit.datatype, value=lit.value, note=note)
            return 'measure', 'references no field (kept as a DAX measure)', None
        return 'measure', 'aggregate expression (kept as a DAX measure)', None

    # field lookup
    def lookup(self, scope, qualifier, name):
        if qualifier is not None:
            if qualifier.casefold() != 'parameters':
                return _Hit('missing', ref=Ref(
                    'missing', reason=f'qualified field reference [{qualifier}].[{name}]'))
            ref = self.params.get(name)
            if ref is None:
                return _Hit('missing', ref=Ref('missing', reason=f'parameter [{name}] was not found'))
            return _Hit(ref.kind, ref=ref)
        fold = name.casefold()
        holders = []
        ambiguous = False
        for table in self.registry.scope_tables(scope):
            matches = table.by_fold.get(fold)
            if matches:
                holders.append(table.delta)
                ambiguous = ambiguous or len(matches) > 1
        if ambiguous:
            return _Hit('missing', ref=Ref(
                'missing', reason=f'column [{name}] is ambiguous: several columns differ only by case'))
        if holders:
            return _Hit('column', tables=tuple(holders))
        calc = self._by_internal[scope].get(fold) or self._by_caption[scope].get(fold)
        if calc is not None:
            if calc.state == 'literal':
                return _Hit('literal', ref=calc.literal)
            return _Hit('measure' if calc.state == 'measure' else 'calc', calc=calc)
        ref = self.params.get(name)
        if ref is not None:
            return _Hit(ref.kind, ref=ref)
        return _Hit('missing', ref=Ref(
            'missing', reason=f"field [{name}] was not found among the datasource's tables"))

    # ordering
    def _dependency_order(self, candidates):
        by_index = {calc.index: calc for calc in candidates}
        waiting = {}
        dependents = defaultdict(list)
        for calc in candidates:
            needs = set()
            for qualifier, name in collect_field_refs(calc.formula):
                if qualifier is None:
                    hit = self.lookup(calc.scope, None, name)
                    if hit.kind == 'calc' and hit.calc.state == 'candidate':
                        needs.add(hit.calc.index)
            waiting[calc.index] = len(needs)
            for dep in needs:
                dependents[dep].append(calc.index)
        ready = [index for index, count in waiting.items() if count == 0]
        heapq.heapify(ready)
        ordered = []
        while ready:
            index = heapq.heappop(ready)
            ordered.append(by_index[index])
            for nxt in dependents[index]:
                waiting[nxt] -= 1
                if waiting[nxt] == 0:
                    heapq.heappush(ready, nxt)
        done = {calc.index for calc in ordered}
        return ordered, [calc for calc in candidates if calc.index not in done]

    # planning of one calculation
    def run(self):
        candidates = [calc for calc in self.calcs if calc.state == 'candidate']
        ordered, cyclic = self._dependency_order(candidates)
        for calc in cyclic:
            self._pending(calc, 'part of, or depends on, a circular reference', None)
        for calc in ordered:
            try:
                self._plan_one(calc)
            except Exception as exc:  # one bad calculation must not abort the migration
                logger.warning('Planning calculation %r failed: %s: %s',
                               calc.caption, type(exc).__name__, exc)
                self._pending(calc, f'internal planner error: {type(exc).__name__}: {exc}', None)
        planned = [calc.planned for calc in ordered + cyclic if calc.planned is not None]
        measures = [PlannedMeasure(calc.caption, calc.internal, calc.formula, calc.reason)
                    for calc in self.calcs if calc.state == 'measure']
        return CalcColumnPlan(planned, measures, list(self.registry.tables))

    def _best_table(self, common):
        if not common:
            return None
        tables = self.registry.tables
        return min(common, key=lambda delta: (-len(tables[delta].columns), tables[delta].order))

    def _plan_one(self, calc):
        sets = []
        for qualifier, name in collect_field_refs(calc.formula):
            if qualifier is not None:
                continue
            hit = self.lookup(calc.scope, None, name)
            if hit.kind == 'column':
                sets.append(set(hit.tables))
            elif hit.kind == 'calc' and hit.calc.planned is not None and hit.calc.planned.delta_table:
                sets.append({hit.calc.planned.delta_table})
        table = self._best_table(set.intersection(*sets) if sets else set())
        resolver = _Resolver(self, calc, table)
        conv = convert_to_pyspark(calc.formula, resolver)
        if not conv.ok:
            if conv.model_only or (resolver.measure_reason
                                   and conv.reason == resolver.measure_reason):
                self._measure(calc, conv.reason)
            else:
                self._pending(calc, conv.reason, table)
            return
        if not conv.columns:
            self._measure(calc, 'references no row-level field (kept as a DAX measure)')
            return
        if table is None:
            involved = ', '.join(sorted(set().union(*sets)))
            self._pending(calc, f'needs columns from several tables ({involved}): join them first',
                          None, conv)
            return
        declared = _FAMILY.get(normalize_datatype(calc.datatype))
        inferred = _FAMILY.get(conv.datatype)
        if declared and inferred and declared != inferred:
            self._pending(calc, (f'inferred type {conv.datatype} does not match '
                                 f'the declared type {calc.datatype}'), table, conv)
            return
        target = self.registry.tables[table]
        name = self._free_name(target, calc.caption)
        statement = with_column_statement(name, conv.expression)
        rejected = validate_statement(statement, target.available())
        if rejected:
            self._pending(calc, f'generated code rejected by validation: {rejected}', table, conv)
            return
        target.taken.add(name.casefold())
        target.created.append(name)
        calc.state = 'column'
        calc.planned = self._planned(calc, target, name, statement, None, conv)

    @staticmethod
    def _free_name(table, caption):
        base = sanitize_calc_col_name(caption)
        name, attempt = base, 1
        while name.casefold() in table.taken:
            attempt += 1
            name = f'{base}_{attempt}'
        return name

    @staticmethod
    def _planned(calc, table, name, statement, reason, conv):
        return PlannedColumn(
            caption=calc.caption,
            internal=calc.internal,
            datasource=calc.scope,
            table=table.original if table else '',
            delta_table=table.delta if table else '',
            delta_name=name,
            datatype=calc.datatype,
            spark_type=calc_spark_type(calc.datatype),
            formula=calc.formula,
            statement=statement,
            reason=reason,
            notes=list(conv.notes) if conv else [],
            inferred_type=conv.datatype if conv and conv.ok else '',
        )

    def _pending(self, calc, reason, table_name, conv=None):
        table = self.registry.tables.get(table_name) if table_name else None
        calc.state = 'pending'
        calc.planned = self._planned(
            calc, table, sanitize_calc_col_name(calc.caption), None, reason, conv)

    @staticmethod
    def _measure(calc, reason):
        calc.state = 'measure'
        calc.reason = reason


def plan_calculated_columns(extracted_data):
    """Return the :class:`CalcColumnPlan` for the extracted workbook data."""
    return _Planner(extracted_data if isinstance(extracted_data, dict) else {}).run()
