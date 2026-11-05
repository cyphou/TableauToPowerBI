"""
Power BI Project (.pbip) generator from converted Tableau objects

This module automatically creates the complete structure of a Power BI Project,
including all the files needed to open the project in Power BI Desktop.
"""

import copy
import json
import logging
import math
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Pre-compiled patterns for field name cleaning
_RE_DERIVATION_PREFIX = re.compile(
    r'^(none|sum|avg|count|cnt|ctd|countd|min|max|usr|yr|mn|dy|qr|wk|attr|md|mdy|hms|hr|mt|sc|thr|trunc|tyr|tqr|tmn|tdy|twk):'
)
_RE_TABLE_CALC_PREFIX = re.compile(
    r'^(pcto|pctd|diff|running_sum|running_avg|running_count|running_min|running_max|rank_unique|rank_dense|rank):(sum|avg|countd|ctd|count|cnt|min|max)?:?'
)
_RE_TYPE_SUFFIX = re.compile(r':(nk|qk|ok|fn|tn)$')

# Tableau shelf aggregation prefix â†’ PBI Aggregation Function ID
# Semantic-query enum (not model summarizeBy): 2=Count (distinct values),
# 5=CountNonNull (populated rows), 6=Median.
_TABLEAU_AGG_TO_PBI_FUNC = {
    'sum': 0, 'avg': 1, 'cnt': 5, 'count': 5,
    'min': 3, 'max': 4, 'ctd': 2, 'countd': 2,
    'median': 6, 'attr': 0,
}
_NUMERIC_MODEL_TYPES = frozenset({
    'int64', 'double', 'decimal', 'integer', 'real', 'number', 'currency',
})


def _disambiguate_aggregation_query_refs(projections):
    names = {0: 'Sum', 1: 'Avg', 2: 'Count', 3: 'Min', 4: 'Max',
             5: 'CountNonNull', 6: 'Median'}
    for projection in projections:
        aggregate = projection['field'].get('Aggregation')
        if aggregate and any(
                other is not projection
                and other['queryRef'] == projection['queryRef']
                and other['field'] != projection['field'] for other in projections):
            column = aggregate['Expression']['Column']
            entity = column['Expression']['SourceRef']['Entity']
            function = names.get(aggregate['Function'], f"Aggregate{aggregate['Function']}")
            projection['queryRef'] = f"{function}({entity}.{column['Property']})"


_LATITUDE_ROLES = frozenset({'latitude', 'lat'})
_LONGITUDE_ROLES = frozenset({'longitude', 'lon', 'lng'})
_GEOGRAPHIC_ROLES = frozenset({
    'address', 'city', 'continent', 'country', 'county', 'geography',
    'place', 'postalcode', 'postcode', 'region', 'state', 'stateorprovince',
    'territory', 'zip', 'zipcode',
})
_GEOGRAPHIC_FIELD_WORDS = frozenset({
    'address', 'city', 'continent', 'country', 'county', 'geography',
    'location', 'place', 'postal', 'postcode', 'province', 'region', 'state',
    'territory', 'zip',
})

# Role contracts from the Power BI visual capability catalog. A visual missing
# a required role renders nothing, and a measure-only role rejects a bare
# column reference, so both have to be enforced before the query is emitted.
_REQUIRED_VISUAL_ROLES = {
    'areaChart': ('Category', 'Y'),
    'azureMap': ('Category',),
    'barChart': ('Category', 'Y'),
    'card': ('Values',),
    'clusteredBarChart': ('Category', 'Y'),
    'clusteredColumnChart': ('Category', 'Y'),
    'columnChart': ('Category', 'Y'),
    'donutChart': ('Category', 'Y'),
    'filledMap': ('Category',),
    'funnel': ('Category', 'Y'),
    'gauge': ('Y',),
    'hundredPercentStackedBarChart': ('Y',),
    'lineChart': ('Category', 'Y'),
    'lineClusteredColumnComboChart': ('Category',),
    'matrix': ('Values',),
    'multiRowCard': ('Values',),
    'pieChart': ('Category', 'Y'),
    'pivotTable': ('Values',),
    'ribbonChart': ('Category', 'Y'),
    'scatterChart': ('X', 'Y'),
    'shapeMap': ('Category',),
    'tableEx': ('Values',),
    'treemap': ('Values',),
}
_MEASURE_ONLY_ROLES = {
    'areaChart': ('Y', 'Y2'),
    'azureMap': ('Size',),
    'barChart': ('Y',),
    'card': ('Values',),
    'clusteredBarChart': ('Y',),
    'clusteredColumnChart': ('Y',),
    'columnChart': ('Y',),
    'donutChart': ('Y',),
    'filledMap': ('Y', 'X'),
    'funnel': ('Y',),
    'gauge': ('Y', 'MinValue', 'MaxValue', 'TargetValue'),
    'hundredPercentStackedBarChart': ('Y',),
    'lineChart': ('Y', 'Y2'),
    'lineClusteredColumnComboChart': ('Y', 'Y2'),
    'matrix': ('Values',),
    'pieChart': ('Y',),
    'pivotTable': ('Values',),
    'ribbonChart': ('Y',),
    'scatterChart': ('Size',),
    'shapeMap': ('Value',),
    'treemap': ('Values',),
    'waterfallChart': ('Y',),
}
#: Visuals exposing a Tooltips role, per the Power BI visual catalog. Card,
#: matrix, multiRowCard, pivotTable and tableEx deliberately have none.
_TOOLTIP_CAPABLE_VISUALS = frozenset({
    'areaChart', 'azureMap', 'barChart', 'clusteredBarChart', 'clusteredColumnChart',
    'columnChart',
    'donutChart', 'filledMap', 'funnel', 'gauge',
    'hundredPercentStackedBarChart', 'lineChart',
    'lineClusteredColumnComboChart', 'map', 'pieChart', 'ribbonChart',
    'scatterChart', 'shapeMap', 'treemap', 'waterfallChart',
})


def _is_bindable_coordinate(field):
    """Tableau's own geocoding output is not a model column, so it can never
    reach a Latitude/Longitude well."""
    return '(generated)' not in (field.get('name') or '').lower()


def _is_latitude_field(field):
    """True when *field* carries a bindable latitude, by role or by name."""
    if not _is_bindable_coordinate(field):
        return False
    role = (field.get('semantic_role') or '').strip('[]').lower()
    return role in _LATITUDE_ROLES or 'latitude' in (field.get('name') or '').lower()


def _is_longitude_field(field):
    """True when *field* carries a bindable longitude, by role or by name."""
    if not _is_bindable_coordinate(field):
        return False
    role = (field.get('semantic_role') or '').strip('[]').lower()
    return role in _LONGITUDE_ROLES or 'longitude' in (field.get('name') or '').lower()


def _is_geographic_dimension(field):
    """True when a non-coordinate Tableau field can populate Azure Maps Location."""
    if _is_latitude_field(field) or _is_longitude_field(field):
        return False
    # The generated model already resolved Tableau's geographic role into a
    # dataCategory, and it survives fields whose worksheet entry carries none.
    if (field.get('_data_category') or '').lower() in _GEOGRAPHIC_ROLES:
        return True
    role = (field.get('semantic_role') or '').strip('[]').lower()
    if role in _GEOGRAPHIC_ROLES:
        return True
    import unicodedata
    plain = unicodedata.normalize('NFKD', field.get('name') or '').encode('ascii', 'ignore').decode('ascii')
    field_words = re.findall(r'[a-z]+', plain.lower())
    return any(word in _GEOGRAPHIC_FIELD_WORDS for word in field_words)


# â”€â”€ PBIR schema constants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
SCHEMA_REPORT = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/2.0.0/schema.json"
SCHEMA_PAGE = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json"
SCHEMA_VISUAL = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.7.0/schema.json"
SCHEMA_BOOKMARK = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmark/2.1.0/schema.json"
SCHEMA_BOOKMARKS_METADATA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmarksMetadata/1.0.0/schema.json"
SCHEMA_PAGES_METADATA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json"
SCHEMA_VERSION = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json"
SCHEMA_DEFINITION_PBIR = "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json"
SCHEMA_PLATFORM = "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json"
SCHEMA_PBIP = "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json"

# Theme baseline â€” updated when PBI Desktop ships new monthly theme
PBI_BASE_THEME_NAME = "CY26SU04"
PBI_REPORT_VERSION_AT_IMPORT = "5.58"

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Generator imports
import m_query_generator
import tmdl_generator

from powerbi_import.atomic_file import atomic_write_text


def _write_json(filepath, data, ensure_ascii=True):
    """Write a JSON file, creating parent directories as needed."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=ensure_ascii)


def _rmtree_with_retry(path, attempts=3, delay=0.5):
    """Remove a directory tree with retry and exponential backoff.

    OneDrive and other sync tools may hold brief locks on files,
    causing ``PermissionError`` during cleanup.  This function
    retries up to *attempts* times with increasing delay.

    Returns:
        True if successfully removed, False otherwise.
    """
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except PermissionError:
            if attempt < attempts - 1:
                time.sleep(delay * (2 ** attempt))
                logger.debug(
                    "Retry %d/%d removing %s (PermissionError)",
                    attempt + 1, attempts, path,
                )
            else:
                logger.warning("Cannot remove %s after %d attempts (locked)", path, attempts)
                return False
        except OSError as exc:
            logger.debug("Cannot remove %s: %s", path, exc)
            return False
    return False


def _L(v):
    """PBIR expression literal wrapper.

    Over a hundred call sites build their own quoted literal with
    ``f"'{value}'"``. That is correct until the value carries an
    apostrophe -- a French title such as ``Taux d'utilisation`` then
    closes the literal early and Power BI cannot parse the expression.
    Rather than trust each site to escape, normalise here: re-escape the
    body of an already-quoted literal so every caller is right by
    construction. Collapsing before doubling keeps this idempotent, so a
    site that *did* escape correctly is left alone.
    """
    if isinstance(v, str) and len(v) >= 2 and v.startswith("'") and v.endswith("'"):
        body = v[1:-1].replace("''", "'").replace("'", "''")
        v = f"'{body}'"
    return {"expr": {"Literal": {"Value": v}}}


#: Schemes a Power BI action button will actually navigate to.
_STATIC_URL_SCHEMES = ('http://', 'https://', 'mailto:', '//')
#: Power BI's own padding above and below a textbox's text.
_TEXTBOX_VERTICAL_PADDING = 16

#: A Tableau URL action interpolates fields as ``<field>`` or ``<[ds].[col]>``.
_URL_FIELD_PLACEHOLDER = re.compile(r'<[^>]+>')


def _is_static_url(value):
    """True when a Tableau URL action target is a literal a button can hold.

    Tableau usually builds the target from a field — on its own or embedded in
    a query string — which is a per-row value, not a button target. Writing it
    as a literal would ship a link that goes nowhere.
    """
    url = (value or '').strip()
    if not url or _URL_FIELD_PLACEHOLDER.search(url):
        return False
    return url.lower().startswith(_STATIC_URL_SCHEMES)


def _action_belongs_to_page(action, page_display_name, page_worksheets=()):
    """Whether an action's button belongs on this page.

    Action buttons are built per page from the whole workbook list, so an
    action that names where it lives would otherwise appear on every page.
    Tableau names either the source dashboard or the source worksheets; an
    action that names neither is left on all pages rather than silently
    dropped.
    """
    dashboards = action.get('source_dashboards') or []
    if dashboards:
        return page_display_name in dashboards
    worksheets = action.get('source_worksheets') or []
    if worksheets:
        return any(name in set(page_worksheets) for name in worksheets)
    return True


def _numeric_pbi_literal(v_str, prefer_integer=None):
    """Return a typed numeric literal, or None when *v_str* is not numeric.

    A bare number is never a valid Power BI literal: it crashes
    ``SQExprValidationVisitor.visitIn`` with ``e.accept is not a function``
    and the report refuses to render. DAX types it with a suffix -- ``L``
    for a whole number, ``D`` for a real one.
    """
    text = str(v_str).strip()
    try:
        value = float(text)
    except (ValueError, TypeError):
        return None
    if value != value or value in (float('inf'), float('-inf')):
        return None          # NaN and infinity have no literal form
    looks_integer = ('.' not in text and 'e' not in text.lower())
    as_integer = looks_integer if prefer_integer is None else prefer_integer
    if as_integer:
        try:
            return f"{int(float(text))}L"
        except (ValueError, OverflowError):
            return None
    return f"{text}D"


def _pbi_literal(v, column_type=None):
    """Convert a filter value to a PBI literal string.

    PBI PBIR filter JSON uses different formats for different types:
    - Strings:  ``"'some text'"`` (single-quoted)
    - Booleans: ``"true"`` / ``"false"`` (unquoted)
    - Numbers:  ``"123"`` or ``"1.5"`` (unquoted)

    Without this, boolean filter values like ``true``/``false`` are
    wrapped as strings ``'true'``/``'false'`` causing a type mismatch
    with boolean columns (``Broken_Filters`` error in PBI Desktop).

    Tableau XML stores filter values wrapped in double-quotes
    (e.g. ``"EC"``, ``"false"``, ``"123"``).  These must be stripped
    before type detection so that ``"false"`` is recognised as boolean
    and ``"EC"`` becomes the string literal ``'EC'`` (not ``'"EC"'``).

    ``column_type`` (optional, lowercased semantic-model dataType) forces
    the literal format to match the column's declared type.  This is
    critical for string columns that happen to hold digit-only values
    (e.g. ``Theme = "1"..."7"``): without the hint, ``_pbi_literal('1')``
    returns the unquoted integer literal ``'1'`` which causes PBI's
    ``SQExprValidationVisitor.visitIn`` to crash with
    ``e.accept is not a function`` due to the type mismatch.
    """
    v_str = str(v)
    # Strip Tableau's outer double-quotes from values
    if v_str.startswith('"') and v_str.endswith('"') and len(v_str) >= 2:
        v_str = v_str[1:-1]
    v_lower = v_str.lower().strip()

    # Type-aware formatting (column type takes precedence over auto-detection)
    if column_type:
        ct = column_type.lower()
        if ct == 'string':
            # Always wrap as quoted string for string columns, even if the
            # value looks like a number or boolean.  Otherwise PBI's visitIn
            # crashes on the column-vs-literal type mismatch.
            return f"'{v_str.replace(chr(39), chr(39) * 2)}'"
        if ct == 'boolean':
            if v_lower in ('true', 'vrai', '1'):
                return 'true'
            if v_lower in ('false', 'faux', '0'):
                return 'false'
            # Unknown boolean value — fall through to default detection
        elif ct in ('int64', 'integer', 'double', 'decimal', 'currency', 'number'):
            literal = _numeric_pbi_literal(
                v_str, prefer_integer=ct in ('int64', 'integer'))
            if literal is not None:
                return literal
            # Numeric column but value is non-numeric — wrap as string
            # to avoid crashing the visitor (PBI will report a type
            # mismatch warning but won't crash).
            return f"'{v_str}'"

    # Auto-detect (no column type hint, or unknown type)
    # Boolean
    if v_lower in ('true', 'false', 'vrai', 'faux'):
        return v_lower.replace('vrai', 'true').replace('faux', 'false')
    # Numeric
    literal = _numeric_pbi_literal(v_str)
    if literal is not None:
        return literal
    # String (default)
    return f"'{v_str.replace(chr(39), chr(39) * 2)}'"


def _filter_literal(v, date_part_prefix='', boundary='min'):
    """Convert a filter min/max value to a PBI literal.

    Handles three cases:
    - **Year-part prefix** (``yr:``): integer year → PBI datetime literal
      for the start (Jan 1) or end (Dec 31) of that year.
    - **Tableau date hash** (``#2001-12-07#``): → PBI datetime literal.
    - **Other**: fall through to ``_pbi_literal()``.
    """
    v_str = str(v).strip()

    # Tableau date literal: #YYYY-MM-DD# → datetime'...'
    if v_str.startswith('#') and v_str.endswith('#'):
        date_str = v_str.strip('#')
        # Normalize to ISO datetime
        if len(date_str) == 10:  # YYYY-MM-DD
            return f"datetime'{date_str}T00:00:00'"
        return f"datetime'{date_str}'"

    # Year-part prefix (yr:) — convert integer year to date boundary
    if date_part_prefix == 'yr':
        try:
            year = int(float(v_str))
            if boundary == 'max':
                return f"datetime'{year}-12-31T23:59:59'"
            return f"datetime'{year}-01-01T00:00:00'"
        except (ValueError, TypeError):
            pass

    # Numeric — PBI expects 123L (integer) or 1.5D (decimal)
    literal = _numeric_pbi_literal(v_str)
    if literal is not None:
        return literal

    # String literal — double apostrophes so PBI can parse the PBIR JSON.
    return f"'{v_str.replace(chr(39), chr(39) * 2)}'"


def _date_part_interval(value, date_part):
    """Return a half-open datetime interval for an unambiguous date-part value."""
    raw = str(value).strip().strip('#')
    part = str(date_part or '').lower()
    try:
        if part in ('year', 'yr') and re.fullmatch(r'\d{4}', raw):
            start = datetime(int(raw), 1, 1)
            end = datetime(int(raw) + 1, 1, 1)
        elif part in ('month', 'mn') and re.fullmatch(r'\d{4}-\d{2}', raw):
            start = datetime.strptime(raw, '%Y-%m')
            end = datetime(start.year + (start.month == 12),
                           1 if start.month == 12 else start.month + 1, 1)
        elif part in ('day', 'dy') and re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            start = datetime.strptime(raw, '%Y-%m-%d')
            end = start + timedelta(days=1)
        else:
            # A month number or day-of-month without a year/month is a repeating
            # date-part selection, not a contiguous range on the timestamp column.
            return None
    except (ValueError, OverflowError):
        return None
    return (
        f"datetime'{start:%Y-%m-%dT%H:%M:%S}'",
        f"datetime'{end:%Y-%m-%dT%H:%M:%S}'",
    )


class PowerBIProjectGenerator:
    """Generates Power BI Project (.pbip) files"""

    def __init__(self, output_dir='artifacts/powerbi_projects/'):
        from powerbi_import.desktop_paths import validate_desktop_root
        self.output_dir = validate_desktop_root(output_dir)
        self._relationship_inference_cache = {}
        self._fidelity_preprocess_summary = {}
        self._equivalence_report = None
        self.last_phase_timings = None

    def generate_project(self, report_name, converted_objects, calendar_start=None,
                         calendar_end=None, culture=None, model_mode='auto',
                         output_format='pbip', paginated=False, languages=None,
                         composite_threshold=None, agg_tables='none',
                         incremental_refresh=False, incremental_refresh_months=12,
                         parameterize=True, optimize_dax=False, time_intelligence='none',
                         strict_generation=False):
        self._partial_staging_dir = None
        try:
            return self._generate_project_impl(
                report_name, converted_objects, calendar_start, calendar_end,
                culture, model_mode, output_format, paginated, languages,
                composite_threshold, agg_tables, incremental_refresh,
                incremental_refresh_months, parameterize, optimize_dax,
                time_intelligence, strict_generation)
        finally:
            staging = self._partial_staging_dir
            if staging and os.path.isdir(staging):
                shutil.rmtree(staging)

    def _generate_project_impl(self, report_name, converted_objects, calendar_start=None,
                              calendar_end=None, culture=None, model_mode='auto',
                              output_format='pbip', paginated=False, languages=None,
                              composite_threshold=None, agg_tables='none',
                              incremental_refresh=False, incremental_refresh_months=12,
                              parameterize=True, optimize_dax=False,
                              time_intelligence='none', strict_generation=False):
        """
        Generates a complete Power BI Project

        Args:
            report_name: Report name
            converted_objects: Dict containing all converted objects
            calendar_start: Start year for Calendar table (default: 2020)
            calendar_end: End year for Calendar table (default: 2030)
            culture: Override culture/locale for semantic model
            paginated: If True, generate a paginated report layout alongside interactive report

        Returns:
            str: Path to the generated project
        """

        from powerbi_import.telemetry import PhaseTimingCollector

        phase_timings = PhaseTimingCollector().start()
        from powerbi_import.desktop_paths import validate_desktop_project
        validate_desktop_project(self.output_dir, report_name)
        project_dir = os.path.join(self.output_dir, report_name)
        original_project_dir = project_dir
        os.makedirs(project_dir, exist_ok=True)
        pbip_file = os.path.join(project_dir, f"{report_name}.pbip")
        previous_marker = None
        if os.path.isfile(pbip_file):
            with open(pbip_file, 'rb') as handle:
                previous_marker = handle.read()
            os.remove(pbip_file)
        print(f"\nðŸ”¨ Generating Power BI Project: {report_name}")

        # Store options for downstream use
        self._calendar_start = calendar_start
        self._calendar_end = calendar_end
        self._culture = culture
        self._model_mode = model_mode or 'auto'
        self._output_format = output_format or 'pbip'
        self._paginated = paginated
        self._languages = languages
        self._composite_threshold = composite_threshold
        self._agg_tables = agg_tables
        self._optimize_dax = optimize_dax
        self._time_intelligence = time_intelligence
        self._incremental_refresh = incremental_refresh
        self._incremental_refresh_months = incremental_refresh_months
        self._parameterize = parameterize
        self._partial_generation = False

        # Preprocess model metadata using optional fidelity enhancers.
        with phase_timings.phase('fidelity_preprocess'):
            converted_objects = self._preprocess_for_fidelity(
                report_name, converted_objects,
            )

        # Detect datasource-only mode (.tds â€” no worksheets/dashboards)
        self._datasource_only = not bool(
            converted_objects.get('worksheets') or converted_objects.get('dashboards')
        )

        # 2. Create the SemanticModel structure
        if self._output_format in ('pbip', 'tmdl'):
            with phase_timings.phase('semantic_model'):
                from powerbi_import.errors import PartialGenerationError
                try:
                    sm_dir = self.create_semantic_model_structure(
                        project_dir, report_name, converted_objects,
                    )
                except PartialGenerationError as exc:
                    if strict_generation or self._output_format != 'pbip':
                        raise
                    original_project_dir = project_dir
                    original_sm = os.path.join(project_dir, f'{report_name}.SemanticModel')
                    if os.path.exists(os.path.join(original_sm, '.pbi')):
                        raise
                    project_dir = self._allocate_partial_staging()
                    self._partial_staging_dir = project_dir
                    staged_sm = os.path.join(project_dir, f'{report_name}.SemanticModel')
                    os.makedirs(staged_sm)
                    for filename in ('.platform', 'definition.pbism'):
                        shutil.copy2(os.path.join(original_sm, filename),
                                     os.path.join(staged_sm, filename))
                    repaired = self._prepare_selective_group_repair(converted_objects, exc)
                    if repaired is not None:
                        source_data = os.path.join(original_project_dir, 'Data')
                        if os.path.isdir(source_data):
                            shutil.copytree(source_data, os.path.join(project_dir, 'Data'))
                        try:
                            self.create_tmdl_model(staged_sm, report_name, repaired)
                        except PartialGenerationError:
                            repaired = None
                    if repaired is None:
                        staged_data = os.path.join(project_dir, 'Data')
                        if os.path.isdir(staged_data):
                            shutil.rmtree(staged_data)
                        converted_objects = self._create_partial_repair_project(
                            project_dir, report_name, converted_objects, exc)
                    else:
                        from powerbi_import.security_validator import redact_credentials
                        diagnostic = {
                            'schema_version': '1.0', 'status': 'partial',
                            'validation_status': 'FAIL', 'data_retained': True,
                            'recovery_scope': 'unaffected_model_and_visuals',
                            'affected_objects': repaired.pop('_partial_affected_objects'),
                            'errors': [{
                                **exc.to_dict(), 'message': redact_credentials(str(exc)),
                                'impact': 'The unresolved group and its directly dependent '
                                          'worksheets are excluded. Other model objects '
                                          'and visuals are retained.',
                                'fix': 'Restore or map the missing group input, then '
                                       'regenerate with --strict-generation and verify '
                                       'the affected worksheet bindings and refresh.',
                            }],
                        }
                        _write_json(os.path.join(project_dir, 'partial_generation.json'), diagnostic)
                        self._partial_generation = True
                        converted_objects = repaired
                    sm_dir = os.path.join(project_dir, f'{report_name}.SemanticModel')
            print(f"  âœ“ SemanticModel created: {sm_dir}")

        # 3. Create the Report structure only when the source has visuals.
        has_visuals = bool(converted_objects.get('worksheets') or converted_objects.get('dashboards'))
        if self._output_format in ('pbip', 'pbir') and has_visuals:
            with phase_timings.phase('report'):
                report_dir = self.create_report_structure(
                    project_dir, report_name, converted_objects,
                )
            print(f"  âœ“ Report created: {report_dir}")
        elif not has_visuals:
            print("  â„¹ Datasource-only mode: SemanticModel generated (no Report)")

        # 4. Create metadata
        with phase_timings.phase('metadata'):
            self.create_metadata(project_dir, report_name, converted_objects)
            metadata_path = os.path.join(project_dir, 'migration_metadata.json')
            with open(metadata_path, encoding='utf-8') as handle:
                metadata = json.load(handle)
            metadata['generation_status'] = (
                'partial' if self._partial_generation else 'complete')
            if self._partial_generation:
                metadata['partial_diagnostic'] = 'partial_generation.json'
                metadata['validation_status'] = 'FAIL'
            _write_json(metadata_path, metadata)
        print("  âœ“ Metadata created")

        # 5. Create paginated report layout (if requested)
        if self._paginated:
            with phase_timings.phase('paginated_report'):
                pag_dir = self._create_paginated_report(
                    project_dir, report_name, converted_objects,
                )
            print(f"  âœ“ Paginated report layout created: {pag_dir}")

        # 6. Generate post-migration automation artifacts
        with phase_timings.phase('automation_artifacts'):
            self._generate_automation_artifacts(
                project_dir, report_name, converted_objects,
            )

        # 7. Equivalence report (best-effort, non-blocking)
        with phase_timings.phase('equivalence_validation'):
            self._run_equivalence_checks(
                project_dir, report_name, converted_objects,
            )
        from powerbi_import.desktop_paths import validate_desktop_tree
        validate_desktop_tree(project_dir)
        with phase_timings.phase('project_manifest'):
            partial_path = os.path.join(project_dir, 'partial_generation.json')
            if not self._partial_generation and os.path.isfile(partial_path):
                os.remove(partial_path)
            pbip_file = self.create_pbip_file(project_dir, report_name)
        if self._partial_generation:
            previous_dir = self._allocate_partial_staging()
            os.rmdir(previous_dir)
            os.replace(original_project_dir, previous_dir)
            try:
                if previous_marker is not None:
                    with open(os.path.join(previous_dir, f'{report_name}.pbip'), 'wb') as handle:
                        handle.write(previous_marker)
                diagnostic_path = os.path.join(project_dir, 'partial_generation.json')
                with open(diagnostic_path, encoding='utf-8') as handle:
                    diagnostic = json.load(handle)
                diagnostic['previous_output'] = {
                    'status': 'preserved',
                    'path': os.path.relpath(previous_dir, original_project_dir),
                    'not_part_of_current_project': True,
                }
                _write_json(diagnostic_path, diagnostic)
                os.replace(project_dir, original_project_dir)
            except BaseException:
                os.replace(previous_dir, original_project_dir)
                raise
            project_dir = original_project_dir
            pbip_file = os.path.join(project_dir, f'{report_name}.pbip')
        print(f"  ✓ .pbip file created: {pbip_file}")
        self.last_phase_timings = phase_timings.finish()

        print(f"\nâœ… Power BI Project generated: {project_dir}")
        print(f"   ðŸ“‚ Open in Power BI Desktop: {pbip_file}")

        return project_dir

    def _allocate_partial_staging(self):
        for attempt in range(20):
            directory = os.path.join(self.output_dir, '.p' + uuid.uuid4().hex[:3])
            try:
                os.mkdir(directory)
                return directory
            except FileExistsError:
                continue
        raise FileExistsError('Cannot allocate an isolated partial output directory')

    @staticmethod
    def _prepare_selective_group_repair(objects, error):
        if (getattr(error, 'recovery_kind', None) != 'unresolved_combined_group'
                or not objects.get('dashboards') or objects.get('user_filters')):
            return None
        groups = [group for group in objects.get('groups', [])
                  if (group.get('raw_name') or group.get('name')) == error.item
                  and (not error.source or group.get('datasource') == error.source)]
        if len(groups) != 1:
            return None
        group = groups[0]
        aliases = {str(value).strip('[]') for value in (
            group.get('name'), group.get('raw_name')) if value}

        def references(value):
            if isinstance(value, str):
                return any(alias.casefold() in value.casefold() for alias in aliases)
            if isinstance(value, dict):
                return any(references(item) for item in value.values())
            if isinstance(value, list):
                return any(references(item) for item in value)
            return False

        for key in ('calculations', 'filters', 'actions', 'sets', 'bins',
                    'parameters', 'hierarchies', 'datasource_filters', 'aliases',
                    'sort_orders', 'stories', 'data_blending', 'table_extensions'):
            if references(objects.get(key, [])):
                return None
        if any(references(other) for other in objects.get('groups', []) if other is not group):
            return None
        for datasource in objects.get('datasources', []):
            if datasource.get('user_filters') or references(datasource):
                return None
        affected = set()
        for worksheet in objects.get('worksheets', []):
            if references(worksheet.get('filters', [])):
                return None
            if references(worksheet):
                name = worksheet.get('name')
                if not isinstance(name, str) or not name:
                    return None
                affected.add(name)
        if not affected:
            return None
        for dashboard in objects.get('dashboards', []):
            if references({key: value for key, value in dashboard.items() if key != 'objects'}):
                return None
            for obj in dashboard.get('objects', []):
                if obj.get('type') != 'worksheetReference' and references(obj):
                    return None
        repaired = copy.deepcopy(objects)
        repaired['groups'] = [other for other in repaired.get('groups', [])
                              if (other.get('raw_name') or other.get('name')) != error.item
                              or (error.source and other.get('datasource') != error.source)]
        repaired['worksheets'] = [worksheet for worksheet in repaired.get('worksheets', [])
                                  if worksheet.get('name') not in affected]
        for dashboard in repaired['dashboards']:
            for index, obj in enumerate(dashboard.get('objects', [])):
                if obj.get('worksheetName') in affected:
                    dashboard['objects'][index] = {
                        'type': 'text', 'position': obj.get('position', {}),
                        'content': str(obj['worksheetName'])
                                   + ' [PARTIAL: unresolved group; see migration report]',
                    }
        repaired['_partial_affected_objects'] = [
            {'type': 'group', 'name': group.get('name'), 'raw_name': group.get('raw_name'),
             'source_fields': group.get('source_fields', []), 'status': 'excluded'},
            *[{'type': 'worksheet', 'name': name, 'status': 'placeholder'}
              for name in sorted(affected)],
        ]
        return repaired

    def _create_partial_repair_project(self, project_dir, report_name, objects, error):
        """Keep page outlines, never source rows or executable ambiguous semantics."""
        from powerbi_import.security_validator import redact_credentials
        from powerbi_import.tmdl_writers import _write_tmdl_files

        sm_dir = os.path.join(project_dir, f'{report_name}.SemanticModel')
        if os.path.exists(os.path.join(sm_dir, '.pbi')):
            raise error
        definition_dir = os.path.join(sm_dir, 'definition')
        if os.path.isdir(definition_dir):
            shutil.rmtree(definition_dir)
        model = {'model': {
            'culture': self._culture or 'en-US',
            'tables': [{
                'name': 'MigrationRepair',
                'columns': [{'name': 'Status', 'dataType': 'string',
                             'sourceColumn': 'Status'}],
                'partitions': [{'name': 'MigrationRepair', 'mode': 'import',
                                'source': {'type': 'm', 'expression':
                                           '#table(type table [Status = text], {})'}}],
            }],
            'relationships': [], 'roles': [],
        }}
        _write_tmdl_files(model, sm_dir)
        self.apply_model_context({})
        message = redact_credentials(str(error))
        dashboards = []
        source_pages = objects.get('dashboards') or objects.get('worksheets') or [
            {'name': report_name}]
        for page in source_pages:
            page_objects = []
            for obj in page.get('objects', []):
                if obj.get('type') == 'text':
                    page_objects.append(copy.deepcopy(obj))
                else:
                    page_objects.append({
                        'type': 'text', 'position': copy.deepcopy(obj.get('position', {})),
                        'content': str(obj.get('worksheetName') or obj.get('name') or '')
                                   + ' [PARTIAL: unavailable]',
                    })
            dashboards.append({'name': 'PARTIAL - ' + page.get('name', report_name),
                               'size': copy.deepcopy(page.get('size') or
                                                     {'width': 1280, 'height': 720}),
                               'objects': page_objects})
        dashboards.insert(0, {
            'name': 'Migration Repair', 'size': {'width': 1280, 'height': 720},
            'objects': [{'type': 'text',
                         'position': {'x': 40, 'y': 40, 'w': 1100, 'h': 200},
                         'content': 'PARTIAL / NOT VALIDATED - semantic model unavailable. '
                                    'No source data retained. See partial_generation.json '
                                    'and the migration quality report.'}],
        })
        self._datasource_only = False
        diagnostic = {
            'schema_version': '1.0', 'status': 'partial',
            'validation_status': 'FAIL', 'data_retained': False,
            'recovery_scope': 'data_free_page_outlines',
            'errors': [{**error.to_dict(), 'stage': 'semantic_model',
                        'message': message,
                        'impact': 'All source data bindings, calculations, filters, '
                                  'relationships, actions and security roles are excluded. '
                                  'Page objects are non-querying text placeholders.',
                        'fix': 'Correct the source semantics or conversion, regenerate '
                               'with --strict-generation, and verify security, refresh '
                               'and dependent visuals before use.'}],
            'source_datasource_count': len(objects.get('datasources', [])),
            'source_page_count': len(source_pages),
            'affected_objects': [
                {'type': key, 'name': item.get('caption') or item.get('name') or item.get('field'),
                 'status': 'excluded'}
                for key in ('datasources', 'worksheets', 'calculations', 'groups',
                            'filters', 'parameters', 'actions', 'user_filters')
                for item in objects.get(key, []) if isinstance(item, dict)
            ],
        }
        _write_json(os.path.join(project_dir, 'partial_generation.json'), diagnostic)
        self._partial_generation = True
        logger.error('Partial repair project generated; no source data retained: %s', message)
        return {'datasources': [], 'worksheets': [], 'dashboards': dashboards}

    @staticmethod
    def _has_explicit_dual_axis(ws_data):
        dual = ws_data.get('dual_axis')
        axes = ws_data.get('axes')
        axis_dual = axes.get('dual_axis') if isinstance(axes, dict) else None
        return any(
            value is True or (isinstance(value, dict) and value.get('enabled') is True)
            for value in (dual, axis_dual)
        )

    def _preprocess_for_fidelity(self, report_name, converted_objects):
        """Apply optional fidelity preprocessors before generation.

        This step is intentionally best-effort: failures are logged and ignored
        so core generation remains stable.
        """
        summary = {
            'dual_axis_upgrades': 0,
            'reference_lines_normalized': 0,
            'relationships_inferred': 0,
        }

        try:
            from powerbi_import.dual_axis_and_reference_lines import (
                DualAxisBuilder,
                ReferenceLineBuilder,
            )
        except Exception:
            DualAxisBuilder = None
            ReferenceLineBuilder = None

        try:
            from powerbi_import.relationship_inference_v2 import (
                RelationshipInferenceEngine,
            )
        except Exception:
            RelationshipInferenceEngine = None

        worksheets = converted_objects.get('worksheets', []) or []
        if DualAxisBuilder and ReferenceLineBuilder:
            for ws in worksheets:
                try:
                    if (self._has_explicit_dual_axis(ws)
                            and DualAxisBuilder.detect_dual_axis_worksheet(ws)):
                        if ws.get('chart_type') not in (
                            'lineClusteredColumnComboChart',
                            'lineStackedColumnComboChart',
                        ):
                            ws['chart_type'] = 'lineClusteredColumnComboChart'
                            summary['dual_axis_upgrades'] += 1

                        combo = DualAxisBuilder.build_combo_chart_config(ws)
                        ws['_dual_axis_combo'] = DualAxisBuilder.inject_combo_axis_formatting(
                            combo,
                            DualAxisBuilder.extract_axis_config(ws, 0),
                            DualAxisBuilder.extract_axis_config(ws, 1),
                        )

                    refs = ReferenceLineBuilder.extract_reference_lines(ws)
                    if refs:
                        ws['_pbi_reference_lines'] = [
                            ReferenceLineBuilder.build_pbi_reference_line_config(ref)
                            for ref in refs
                        ]
                        summary['reference_lines_normalized'] += len(refs)
                except Exception:
                    continue

        datasources = converted_objects.get('datasources', []) or []
        if RelationshipInferenceEngine:
            for ds in datasources:
                tables = ds.get('tables', []) or []
                if len(tables) < 2:
                    continue

                sig = tuple(
                    sorted(
                        (
                            t.get('name', ''),
                            tuple(sorted(c.get('name', '') for c in t.get('columns', []) or [])),
                        )
                        for t in tables
                    )
                )

                if sig in self._relationship_inference_cache:
                    inferred = self._relationship_inference_cache[sig]
                else:
                    engine = RelationshipInferenceEngine(verbose=False)
                    inferred = engine.infer_relationships(tables)
                    self._relationship_inference_cache[sig] = inferred

                existing = ds.get('relationships', []) or []
                existing_keys = set()
                for rel in existing:
                    left = rel.get('left', {})
                    right = rel.get('right', {})
                    from_t = rel.get('fromTable') or left.get('table')
                    from_c = rel.get('fromColumn') or left.get('column')
                    to_t = rel.get('toTable') or right.get('table')
                    to_c = rel.get('toColumn') or right.get('column')
                    if from_t and from_c and to_t and to_c:
                        existing_keys.add((from_t, from_c, to_t, to_c))

                added = 0
                for rel in inferred:
                    key = (rel.get('fromTable'), rel.get('fromColumn'), rel.get('toTable'), rel.get('toColumn'))
                    if key in existing_keys:
                        continue
                    existing.append(
                        {
                            'left': {'table': rel.get('fromTable'), 'column': rel.get('fromColumn')},
                            'right': {'table': rel.get('toTable'), 'column': rel.get('toColumn')},
                            'type': 'left',
                            'inferred': True,
                            'confidence': rel.get('confidence', 0.0),
                        }
                    )
                    existing_keys.add(key)
                    added += 1

                if added:
                    ds['relationships'] = existing
                    summary['relationships_inferred'] += added

        self._fidelity_preprocess_summary = summary
        if any(summary.values()):
            print(
                "  ✓ Fidelity preprocess: "
                f"{summary['dual_axis_upgrades']} dual-axis upgrades, "
                f"{summary['reference_lines_normalized']} reference lines normalized, "
                f"{summary['relationships_inferred']} inferred relationships"
            )

        return converted_objects

    def _run_equivalence_checks(self, project_dir, report_name, converted_objects):
        """Run v2 equivalence checks and persist a JSON report (best-effort)."""
        try:
            from powerbi_import.equivalence_tester_v2 import run_full_equivalence_suite
        except ImportError as exc:
            logger.warning("Equivalence v2 unavailable: %s", exc)
            return

        try:
            report = run_full_equivalence_suite(
                converted_objects,
                {'project_dir': project_dir, 'report_name': report_name},
                verbose=False,
            )
            self._equivalence_report = report
            output_path = os.path.join(project_dir, 'equivalence_report_v2.json')
            _write_json(output_path, report, ensure_ascii=False)
            print(
                "  ✓ Equivalence v2 (offline model-name checks): "
                f"{report.get('fidelity_percent', 0):.1f}% "
                f"({report.get('passed', 0)}/{report.get('compared', 0)} checks; "
                f"{report.get('not_run', 0)} not run)"
            )
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Equivalence v2 could not be generated: %s", exc)

    def create_pbip_file(self, project_dir, report_name):
        """Creates the main .pbip file â€” format identical to PBI Hero reference"""

        pbip_content = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
            "version": "1.0",
            "artifacts": [
                {
                    "report": {
                        "path": f"{report_name}.Report"
                    }
                }
            ],
            "settings": {
                "enableAutoRecovery": True
            }
        }

        # Datasource-only migrations contain a semantic model but no report.
        if hasattr(self, '_datasource_only') and self._datasource_only:
            pbip_content["artifacts"] = [
                {
                    "semanticModel": {
                        "path": f"{report_name}.SemanticModel"
                    }
                }
            ]

        pbip_file = os.path.join(project_dir, f"{report_name}.pbip")

        gitignore = os.path.join(project_dir, '.gitignore')
        with open(gitignore, 'w', encoding='utf-8') as f:
            f.write(".pbi/\n")

        atomic_write_text(pbip_file, json.dumps(pbip_content, indent=2))
        return pbip_file

    def create_semantic_model_structure(self, project_dir, report_name, converted_objects):
        """Creates the SemanticModel structure (format identical to PBI Hero reference)"""

        sm_dir = os.path.join(project_dir, f"{report_name}.SemanticModel")
        os.makedirs(sm_dir, exist_ok=True)

        # 1. Create .platform
        platform = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
            "metadata": {
                "type": "SemanticModel",
                "displayName": report_name
            },
            "config": {
                "version": "2.0",
                "logicalId": str(uuid.uuid4())
            }
        }
        _write_json(os.path.join(sm_dir, '.platform'), platform)

        # 2. Create definition.pbism
        pbism_definition = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
            "version": "4.2",
            "settings": {
                "qnaEnabled": True
            }
        }
        pbism_file = os.path.join(sm_dir, 'definition.pbism')
        _write_json(pbism_file, pbism_definition)

        # 3. Create SemanticModel in TMDL (format identical to PBI Hero reference)
        self.create_tmdl_model(sm_dir, report_name, converted_objects)

        return sm_dir

    def create_tmdl_model(self, sm_dir, report_name, converted_objects):
        """Creates the semantic model in TMDL format (Tabular Model Definition Language)

        Directly converts extracted Tableau data to TMDL files.
        """

        datasources = converted_objects.get('datasources', [])

        # Collect additional objects
        extra_objects = {
            'hierarchies': converted_objects.get('hierarchies', []),
            'sets': converted_objects.get('sets', []),
            'groups': converted_objects.get('groups', []),
            'bins': converted_objects.get('bins', []),
            'aliases': converted_objects.get('aliases', {}),
            'parameters': converted_objects.get('parameters', []),
            'filters': converted_objects.get('filters', []),
            'actions': converted_objects.get('actions', []),
            'user_filters': converted_objects.get('user_filters', []),
            '_datasources': converted_objects.get('datasources', []),
            '_worksheets': converted_objects.get('worksheets', []),
            'calculations': converted_objects.get('calculations', []),
            'hyper_files': converted_objects.get('hyper_files', []),
        }

        try:
            # Direct Tableau -> TMDL generation (no intermediate BIM layer)
            stats = tmdl_generator.generate_tmdl(
                datasources=datasources,
                report_name=report_name,
                extra_objects=extra_objects,
                output_dir=sm_dir,
                calendar_start=getattr(self, '_calendar_start', None),
                calendar_end=getattr(self, '_calendar_end', None),
                culture=getattr(self, '_culture', None),
                model_mode=getattr(self, '_model_mode', 'auto'),
                languages=getattr(self, '_languages', None),
                composite_threshold=getattr(self, '_composite_threshold', None),
                agg_tables=getattr(self, '_agg_tables', 'none'),
                incremental_refresh=getattr(self, '_incremental_refresh', False),
                incremental_refresh_months=getattr(self, '_incremental_refresh_months', 12),
                parameterize=getattr(self, '_parameterize', True),
                optimize_dax=getattr(self, '_optimize_dax', False),
                time_intelligence=getattr(self, '_time_intelligence', 'none'),
            )

            print("  \u2713 TMDL model created with:")
            print(f"    - {stats['tables']} tables")
            print(f"    - {stats['columns']} columns")
            print(f"    - {stats['measures']} DAX measures")
            print(f"    - {stats['relationships']} relationships")
            if stats['hierarchies']:
                print(f"    - {stats['hierarchies']} hierarchies")
            if stats['roles']:
                print(f"    - {stats['roles']} RLS roles")
            if stats.get('incremental_refresh'):
                ir = stats['incremental_refresh']
                print(f"    - {len(ir.get('tables_configured', []))} incremental refresh table(s)")

            # Store actual BIM measure names for report visual generation.
            # This set reflects the TMDL generator's 3-factor classification
            # (aggregation, column refs, role) and may differ from Tableau's
            # role='measure' metadata â€” a calculated column like DATEDIFF()
            # has role='measure' in Tableau but is a column in the BIM model.
            self.apply_model_context(stats)
            self._related_month_bindings = self._read_related_month_bindings(sm_dir)

            # Write lineage map alongside the project for traceability
            lineage = stats.get('lineage')
            if lineage:
                lineage_path = os.path.join(os.path.dirname(sm_dir), 'lineage_map.json')
                _write_json(lineage_path, lineage)

        except Exception as e:
            print(f"  \u26a0 Error during TMDL generation: {e}")
            import traceback
            traceback.print_exc()
            raise

    def apply_model_context(self, context):
        """Copy the generated model's bindings for a report using that model."""
        self._actual_bim_measure_names = set(context.get('actual_bim_measures') or ())
        self._actual_bim_symbols = set(context.get('actual_bim_symbols') or ())
        measure_symbols = context.get('actual_bim_measure_symbols')
        if measure_symbols is None:
            self.__dict__.pop('_actual_bim_measure_symbols', None)
        else:
            self._actual_bim_measure_symbols = set(measure_symbols)
        group_symbols = context.get('actual_bim_group_symbols')
        if group_symbols is None:
            self.__dict__.pop('_actual_bim_group_symbols', None)
        else:
            self._actual_bim_group_symbols = set(group_symbols)
        for key in (
                'actual_bim_column_types', 'actual_bim_column_categories',
                'actual_bim_measure_types', 'measure_rename_map',
                'table_rename_map', 'column_rename_map',
                'countd_measure_bindings', 'quick_table_calc_bindings',
                'unsupported_quick_table_calcs',
                'related_month_bindings', 'related_continuous_month_bindings',
                'related_year_bindings'):
            setattr(self, '_' + key, copy.deepcopy(context.get(key) or {}))

    def export_model_context(self):
        """Return independent model bindings for a thin report."""
        context: dict[str, object] = {
            'actual_bim_measures': set(getattr(self, '_actual_bim_measure_names', ())),
            'actual_bim_symbols': set(getattr(self, '_actual_bim_symbols', ())),
        }
        measure_symbols = getattr(self, '_actual_bim_measure_symbols', None)
        if measure_symbols is not None:
            context['actual_bim_measure_symbols'] = set(measure_symbols)
        group_symbols = getattr(self, '_actual_bim_group_symbols', None)
        if group_symbols is not None:
            context['actual_bim_group_symbols'] = set(group_symbols)
        for key in (
                'actual_bim_column_types', 'actual_bim_column_categories',
                'actual_bim_measure_types', 'measure_rename_map',
                'table_rename_map', 'column_rename_map',
                'countd_measure_bindings', 'quick_table_calc_bindings',
                'unsupported_quick_table_calcs',
                'related_month_bindings', 'related_continuous_month_bindings'):
            context[key] = copy.deepcopy(getattr(self, '_' + key, {}))
        year_bindings = getattr(self, '_related_year_bindings', {})
        if year_bindings:
            context['related_year_bindings'] = copy.deepcopy(year_bindings)
        return context

    def _create_basic_model_bim(self, report_name, datasources):
        """Basic BIM generation in case of error (fallback)"""

        tables = []

        # Create a simple sample table
        m_expression = m_query_generator.generate_sample_data_query('SampleData', None)

        tables.append({
            "name": "SampleData",
            "columns": [
                {"name": "ID", "dataType": "int64", "sourceColumn": "ID"},
                {"name": "Name", "dataType": "string", "sourceColumn": "Name"},
                {"name": "Value", "dataType": "int64", "sourceColumn": "Value"}
            ],
            "partitions": [{
                "name": "SampleData",
                "mode": "import",
                "source": {
                    "type": "m",
                    "expression": m_expression
                }
            }]
        })

        return {
            "name": report_name,
            "compatibilityLevel": 1567,
            "model": {
                "culture": "en-US",
                "defaultPowerBIDataSourceVersion": "powerBI_V3",
                "tables": tables
            }
        }

    # â”€â”€ Visual creation helpers (extracted from create_report_structure) â”€â”€â”€â”€â”€

    # Layout constants
    MIN_VISUAL_WIDTH = 60
    MIN_VISUAL_HEIGHT = 40
    MIN_GAP = 4  # pixels between adjacent visuals

    def _make_visual_position(self, pos, scale_x, scale_y, z_index,
                               page_width=None, page_height=None):
        """Create a standard PBIR position dict from Tableau coordinates.

        Applies minimum size constraints, clamps to page bounds, and enforces
        a minimum gap between the visual edge and the page boundary.
        """
        if page_width is None:
            page_width = getattr(self, '_current_page_width', 1280)
        if page_height is None:
            page_height = getattr(self, '_current_page_height', 720)
        x = round(pos.get('x', 0) * scale_x)
        y = round(pos.get('y', 0) * scale_y)
        w = max(round(pos.get('w', 300) * scale_x), self.MIN_VISUAL_WIDTH)
        h = max(round(pos.get('h', 200) * scale_y), self.MIN_VISUAL_HEIGHT)

        # Clamp within page bounds
        if x + w > page_width:
            w = max(page_width - x, self.MIN_VISUAL_WIDTH)
        if y + h > page_height:
            h = max(page_height - y, self.MIN_VISUAL_HEIGHT)
        x = max(x, 0)
        y = max(y, 0)

        return {
            "x": x,
            "y": y,
            "z": z_index * 1000,
            "height": h,
            "width": w,
            "tabOrder": z_index * 1000
        }

    # â”€â”€ Grid-snapping layout engine (Sprint 76) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _build_zone_layout_map(self, zone_hierarchy, page_width, page_height):
        """Build a flat map of zone-name/zone-id â†’ PBI pixel rect from zone hierarchy.

        Recursively subdivides the page according to container orientation
        (horizontal splits width, vertical splits height) and proportional
        zone sizes from Tableau's 0-100 000 coordinate system.

        Returns:
            dict mapping zone ``name`` (or ``id`` when unnamed) to
            ``{'x': int, 'y': int, 'w': int, 'h': int}``.
        """
        layout_map = {}
        if not zone_hierarchy:
            return layout_map
        root_pos = zone_hierarchy.get('position', {})
        root_w = max(root_pos.get('w', 0), root_pos.get('x', 0) + root_pos.get('w', 0))
        root_h = max(root_pos.get('h', 0), root_pos.get('y', 0) + root_pos.get('h', 0))
        if root_w == 0:
            root_w = 100000
        if root_h == 0:
            root_h = 100000
        self._layout_zone(zone_hierarchy, 0, 0, page_width, page_height,
                          root_w, root_h, layout_map)
        return layout_map

    def _layout_zone(self, zone, px_x, px_y, px_w, px_h,
                     coord_w, coord_h, layout_map):
        """Recursively lay out a zone and its children into PBI pixel space.

        For leaf zones the full allocated rectangle is recorded.
        For container zones with explicit orientation (horz/vert), children
        are subdivided along the orientation axis.  For containers without
        explicit orientation (common for layout-flow with 2-D grids), each
        child's Tableau coordinates are mapped proportionally into the
        parent's pixel space so side-by-side layouts are preserved.
        """
        key = zone.get('name') or zone.get('id', '')
        children = zone.get('children', [])

        if not children:
            # Leaf zone â€” record its pixel rectangle.
            # Only record zones that correspond to actual content objects.
            # Filter, paramctrl, color (legend), title, and size zones share
            # the parent worksheet's name and would overwrite its position.
            zone_type = zone.get('zone_type', '')
            if key and zone_type not in ('filter', 'paramctrl', 'color',
                                         'title', 'size'):
                layout_map[key] = {
                    'x': round(px_x), 'y': round(px_y),
                    'w': max(round(px_w), self.MIN_VISUAL_WIDTH),
                    'h': max(round(px_h), self.MIN_VISUAL_HEIGHT),
                }
            return

        orientation = zone.get('orientation', '')

        # Separate floating children (absolute position) from tiled children
        tiled = [c for c in children if not c.get('is_floating', False)]
        floating = [c for c in children if c.get('is_floating', False)]

        if tiled:
            if orientation == 'horz':
                total = sum(c.get('position', {}).get('w', 1) for c in tiled) or 1
                cursor = px_x
                for child in tiled:
                    cw = child.get('position', {}).get('w', 1)
                    alloc_w = px_w * cw / total
                    self._layout_zone(child, cursor, px_y, alloc_w, px_h,
                                      coord_w, coord_h, layout_map)
                    cursor += alloc_w
            elif orientation == 'vert':
                total = sum(c.get('position', {}).get('h', 1) for c in tiled) or 1
                cursor = px_y
                for child in tiled:
                    ch = child.get('position', {}).get('h', 1)
                    alloc_h = px_h * ch / total
                    self._layout_zone(child, px_x, cursor, px_w, alloc_h,
                                      coord_w, coord_h, layout_map)
                    cursor += alloc_h
            else:
                # No explicit orientation â€” use proportional coordinate mapping
                # to preserve 2-D grid layouts (e.g. side-by-side + stacked).
                child_max_x = max((c.get('position', {}).get('x', 0)
                                   + c.get('position', {}).get('w', 1)
                                   for c in tiled), default=1) or 1
                child_max_y = max((c.get('position', {}).get('y', 0)
                                   + c.get('position', {}).get('h', 1)
                                   for c in tiled), default=1) or 1
                for child in tiled:
                    cpos = child.get('position', {})
                    cx = px_x + (cpos.get('x', 0) / child_max_x) * px_w
                    cy = px_y + (cpos.get('y', 0) / child_max_y) * px_h
                    cw = (cpos.get('w', 1) / child_max_x) * px_w
                    ch = (cpos.get('h', 1) / child_max_y) * px_h
                    self._layout_zone(child, cx, cy, cw, ch,
                                      coord_w, coord_h, layout_map)

        # Floating children get absolute-scaled positions
        for child in floating:
            cpos = child.get('position', {})
            sx = px_w / max(coord_w, 1)
            sy = px_h / max(coord_h, 1)
            fx = px_x + cpos.get('x', 0) * sx
            fy = px_y + cpos.get('y', 0) * sy
            fw = cpos.get('w', 300) * sx
            fh = cpos.get('h', 200) * sy
            self._layout_zone(child, fx, fy, fw, fh,
                              coord_w, coord_h, layout_map)

        # Record the container itself (useful for padding propagation).
        # Only if the key is NOT already in the map — child worksheet
        # entries take priority over their parent container position.
        if key and key not in layout_map:
            layout_map[key] = {
                'x': round(px_x), 'y': round(px_y),
                'w': max(round(px_w), self.MIN_VISUAL_WIDTH),
                'h': max(round(px_h), self.MIN_VISUAL_HEIGHT),
            }

    def _resolve_visual_position(self, obj, layout_map, scale_x, scale_y,
                                  z_index, page_width, page_height):
        """Resolve visual position using the grid layout map if available.

        Falls back to proportional scaling when the object is not found
        in the layout map (preserves backward compatibility).
        """
        ws_name = obj.get('worksheetName', '') or obj.get('name', '')
        mapped = layout_map.get(ws_name) if layout_map else None
        if mapped:
            x = max(mapped['x'], 0)
            y = max(mapped['y'], 0)
            w = max(mapped['w'], self.MIN_VISUAL_WIDTH)
            h = max(mapped['h'], self.MIN_VISUAL_HEIGHT)
            if x + w > page_width:
                w = max(page_width - x, self.MIN_VISUAL_WIDTH)
            if y + h > page_height:
                h = max(page_height - y, self.MIN_VISUAL_HEIGHT)
            return {
                "x": x, "y": y,
                "z": z_index * 1000,
                "height": h, "width": w,
                "tabOrder": z_index * 1000,
            }
        # Fallback to proportional scaling
        pos = obj.get('position', {})
        return self._make_visual_position(pos, scale_x, scale_y, z_index,
                                          page_width, page_height)

    def _apply_padding_to_visual(self, visual_json, zone_hierarchy, obj_name):
        """Add padding properties to visual from zone hierarchy padding data."""
        if not zone_hierarchy:
            return
        padding = self._find_zone_padding(zone_hierarchy, obj_name)
        if not padding:
            return
        general_props = visual_json.setdefault("visual", {}).setdefault(
            "objects", {}).setdefault("general", [{}])[0].setdefault("properties", {})
        for side in ('top', 'bottom', 'left', 'right'):
            if side in padding:
                general_props[f"padding{side.capitalize()}"] = padding[side]

    def _find_zone_padding(self, zone, name):
        """Recursively find padding for a named zone in the hierarchy."""
        zone_name = zone.get('name', '') or zone.get('id', '')
        if zone_name == name and zone.get('padding'):
            return zone['padding']
        for child in zone.get('children', []):
            result = self._find_zone_padding(child, name)
            if result:
                return result
        return None

    @staticmethod
    def _append_visual_query_notes(visual_json, worksheet):
        mapping_note = worksheet.pop('_visual_mapping_note', None)
        if mapping_note:
            visual_json.setdefault('annotations', []).append({
                'name': 'MigrationNote', 'value': mapping_note,
            })
        notes = (
            ('_unresolved_source_fields', 'Unresolved Tableau datasource fields: ', ''),
            ('_countd_unresolved', 'COUNTD without a verified nonblank measure: ',
             '; Power BI distinct count may include BLANK.'),
            ('_quick_countd_unresolved',
             'Quick COUNTD omitted because its source or calculation could not be verified: ', ''),
            ('_quick_calc_unresolved', 'Quick table calculation omitted: ', ''),
        )
        for key, prefix, suffix in notes:
            fields = worksheet.pop(key, [])
            if fields:
                visual_json.setdefault('annotations', []).append({
                    'name': 'MigrationNote',
                    'value': prefix + ', '.join(fields) + suffix,
                })

    def _worksheet_title_properties(self, ws_name, ws_data, converted_objects):
        display_title = (ws_data.get('title', '') if ws_data else '') or ws_name
        if display_title.strip().casefold() == '<sheet name>':
            display_title = ws_name
        display_title = self._resolve_parameter_title(display_title, converted_objects)
        display_title = display_title.replace("'", "''")
        properties: dict[str, object] = {
            'show': _L('true'), 'text': _L(f"'{display_title}'")}
        title_format = ws_data.get('title_format', {}) if ws_data else {}
        if title_format.get('font_size'):
            try:
                properties['fontSize'] = _L(f"{int(title_format['font_size'])}D")
            except (ValueError, TypeError):
                logger.warning("Invalid worksheet title font size for %r", ws_name)
        if title_format.get('font_family'):
            properties['fontFamily'] = _L(f"'{title_format['font_family']}'")
        if title_format.get('font_color'):
            properties['fontColor'] = {
                'solid': {'color': _L(f"'{title_format['font_color']}'")}}
        for key in ('bold', 'italic', 'underline'):
            if title_format.get(key):
                properties[key] = _L('true')
        if title_format.get('alignment'):
            alignment = {'0': 'left', '1': 'center', '2': 'right'}.get(
                str(title_format['alignment']), 'left')
            properties['alignment'] = _L(f"'{alignment}'")
        return properties

    def _apply_worksheet_sort(self, visual_json, worksheet, query):
        """Apply representable Tableau shelf sorts using verified bindings."""
        sorts = worksheet.get('sort_orders', [])
        if query.get('sortDefinition') or not isinstance(sorts, list) or not sorts:
            return

        def parse_reference(reference):
            raw = str(reference or '').replace('[', '').replace(']', '')
            sources = {
                source.get('name') for source in getattr(self, '_datasources_ref', [])
                if source.get('name')
            } | {
                field.get('datasource') for field in worksheet.get('fields', [])
                if field.get('datasource')
            }
            matches = [source for source in sources if raw.startswith(source + '.')]
            source = max(matches, key=len) if matches else ''
            if source:
                raw = raw[len(source) + 1:]
            agg_match = _RE_DERIVATION_PREFIX.match(raw)
            name = self._clean_field_name(raw)
            if not source:
                field_sources = {
                    field.get('datasource') for field in worksheet.get('fields', [])
                    if field.get('datasource')
                    and self._clean_field_name(field.get('name', '')) == name
                }
                if len(field_sources) == 1:
                    source = next(iter(field_sources))
            return name, source, agg_match.group(1) if agg_match else ''

        sort_state = []
        unsupported = []
        for sort in sorts:
            if not isinstance(sort, dict) or not sort.get('field'):
                continue
            sort_type = str(sort.get('sort_type') or '').lower()
            if sort_type == 'natural' or sort.get('source_element') == 'natural-sort':
                unsupported.append('natural ordering is not representable in PBIR sortDefinition')
                continue
            if sort_type not in ('', 'field', 'computed', 'alphabetic', 'alphabetical'):
                unsupported.append(f"sort type {sort_type!r} is not representable")
                continue
            reference = sort.get('sort_by') or sort['field']
            countd = tmdl_generator._parse_countd_reference(reference) if sort.get('sort_by') else None
            if countd:
                binding = self._resolve_countd_sort_binding(worksheet, countd)
                field = ({'Measure': {
                    'Expression': {'SourceRef': {'Entity': binding[0]}},
                    'Property': binding[1],
                }} if binding else None)
            else:
                name, source, aggregation = parse_reference(reference)
                model_sources = getattr(self, '_datasources_ref', [])
                if source and model_sources and not any(
                        item.get('name') == source for item in model_sources):
                    field = None
                else:
                    entry = self._make_projection_entry(
                        {'name': name, 'datasource': source,
                         'aggregation': aggregation or ('sum' if sort.get('sort_by') else '')},
                        force_column=not sort.get('sort_by'))
                    field = entry.get('field') if entry else None

            if field is None:
                logger.warning("Worksheet sort %r has no verified model binding", reference)
                unsupported.append(f"sort {reference!r} has no verified model binding")
                continue
            direction = 'Descending' if str(sort.get('direction', 'ASC')).upper() == 'DESC' else 'Ascending'
            sort_state.append({'field': field, 'direction': direction})

        if sort_state:
            query['sortDefinition'] = {'sort': sort_state}
        if unsupported:
            visual_json.setdefault('annotations', []).append({
                'name': 'MigrationNote',
                'value': 'Tableau sort not applied: ' + '; '.join(unsupported) + '.',
            })

    def _create_visual_worksheet(self, visuals_dir, ws_data, obj, scale_x, scale_y,
                                  visual_count, worksheets, converted_objects,
                                  tooltip_page_map=None):
        """Create a worksheet-type visual (chart, table, etc.).

        If the worksheet uses SCRIPT_* analytics extensions, generates a
        PBI Python or R script visual instead of a standard chart.
        """
        visual_id = uuid.uuid4().hex[:8]
        visual_dir = os.path.join(visuals_dir, visual_id)

        pos = obj.get('position', {})
        ws_name = obj.get('worksheetName', '')

        # â”€â”€ SCRIPT_* detection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        script_info = self._detect_script_visual(ws_data, converted_objects)
        if script_info:
            from visual_generator import generate_script_visual
            position = self._make_visual_position(pos, scale_x, scale_y, visual_count)
            field_names = [self._clean_field_name(f.get('name', ''))
                           for f in (ws_data or {}).get('fields', [])
                           if f.get('name')]
            container = generate_script_visual(
                visual_name=ws_name,
                script_info=script_info,
                fields=field_names,
                x=position.get('x', 10),
                y=position.get('y', 10),
                width=position.get('width', 400),
                height=position.get('height', 300),
                z_index=visual_count,
            )
            os.makedirs(visual_dir, exist_ok=True)
            _write_json(os.path.join(visual_dir, 'visual.json'), container, ensure_ascii=False)
            return visual_id

        # Defensive normalization: convert raw Tableau mark names like
        # ``'bar'`` to valid PBI visualTypes (``'clusteredBarChart'``).
        # The extractor *should* already produce valid types, but this
        # guards against future regressions and external JSON inputs.
        visual_type = self._resolve_worksheet_visual_type(ws_data or {})

        # Check for custom visual GUID (higher-fidelity AppSource visual)
        guid_info = None
        mark_type = ws_data.get('original_mark_class') if ws_data else None
        if mark_type and hasattr(self, '_used_custom_guids'):
            from visual_generator import resolve_custom_visual_type
            custom_vtype, guid_info = resolve_custom_visual_type(mark_type)
            if guid_info:
                visual_type = custom_vtype
                key = mark_type.lower().replace(' ', '').replace('_', '')
                self._used_custom_guids[key] = guid_info

        if visual_type == 'ganttChart':
            visual_type = 'clusteredBarChart'

        # Validate scatter chart: needs at least one measure for X/Y axes.
        # Circle/Shape marks sometimes produce scatterChart but lack measures.
        if visual_type == 'scatterChart' and ws_data:
            skip_names = {'Measure Names', 'Measure Values', 'Multiple Values',
                          ':Measure Names', ':Measure Values'}
            fields = ws_data.get('fields', [])
            has_measure = any(
                (f.get('aggregation') in ('ctd', 'countd')
                 or self._is_measure_field(self._clean_field_name(f.get('name', ''))))
                for f in fields
                if self._clean_field_name(f.get('name', '')) not in skip_names
            )
            if not has_measure:
                visual_type = 'tableEx'
            else:
                # Scatter X/Y require numeric measures. If every measure on
                # the worksheet is a BIM measure that returns a string,
                # boolean, or other non-numeric type (Tableau icon/badge
                # marks using string-literal measures like `"i"` or
                # `IF(..., "Erreur", "OK")`), PBI Desktop rejects the
                # scatter with DataViewMappingError_ScatterXIncorrectAggregate.
                # Downgrade to multiRowCard (multiple values) or card (single).
                _bim_meas_types = getattr(self, '_actual_bim_measure_types', {}) or {}
                _non_numeric = {'string', 'boolean', 'text', 'datetime', 'date', 'binary'}
                if _bim_meas_types:
                    measure_fields = [
                        f for f in fields
                        if self._clean_field_name(f.get('name', '')) not in skip_names
                        and self._is_measure_field(
                            self._clean_field_name(f.get('name', '')))
                    ]
                    if measure_fields:
                        def _measure_is_numeric(mf):
                            clean = self._clean_field_name(mf.get('name', ''))
                            # Look up (entity, prop) via field map / resolver
                            entity = None
                            prop = clean
                            if hasattr(self, '_field_map') and clean in self._field_map:
                                entity, prop = self._field_map[clean]
                            # When we can't resolve, assume numeric (don't downgrade)
                            if not entity:
                                return True
                            t = _bim_meas_types.get((entity, prop))
                            if t is None:
                                t = _bim_meas_types.get((entity, prop.strip()))
                            # Unknown type → assume numeric (safe default)
                            if t is None:
                                return True
                            return t.lower() not in _non_numeric
                        if not any(_measure_is_numeric(mf) for mf in measure_fields):
                            visual_type = (
                                'multiRowCard' if len(measure_fields) > 1
                                else 'card'
                            )
                # Detect :Measure Names + Multiple Values pattern (strip/dot plot)
                # â†’ clusteredBarChart shows multiple measures per category better
                has_measure_names = any(
                    f.get('name', '') in (':Measure Names', 'Measure Names')
                    for f in fields
                )
                has_multiple_values = any(
                    f.get('name', '') == 'Multiple Values'
                    for f in fields
                )
                if has_measure_names and has_multiple_values:
                    visual_type = 'clusteredBarChart'

                # Detect placeholder scatter: all row measures are min(1) dummies
                # (Tableau shape marks use min(1) for positioning, no real data)
                if visual_type == 'scatterChart':
                    calc_formulas = {}
                    for c in converted_objects.get('calculations', []):
                        cname = self._clean_field_name(c.get('name', '')).strip('[]')
                        calc_formulas[cname] = (c.get('formula', '') or '').strip().lower()
                    row_meas = [
                        f for f in fields
                        if f.get('shelf') == 'rows'
                        and self._is_measure_field(self._clean_field_name(f.get('name', '')))
                    ]
                    if row_meas and all(
                        calc_formulas.get(self._clean_field_name(f.get('name', '')).strip('[]'), '') == 'min(1)'
                        for f in row_meas
                    ):
                        visual_type = 'multiRowCard'

                if (str(mark_type).lower() == 'bar'
                    and visual_type == 'clusteredColumnChart'):
                    visual_type = 'clusteredBarChart'

        # Spatial detection: map visuals with lat/lon fields â†’ azureMap
        if visual_type in ('map', 'filledMap'):
            visual_type = 'azureMap'
        if visual_type in ('map', 'scatterChart') and ws_data:
            fields = ws_data.get('fields', [])
            has_lat = any(_is_latitude_field(f) for f in fields)
            has_lon = any(_is_longitude_field(f) for f in fields)
            if has_lat and has_lon:
                visual_type = 'azureMap'

        # Sync overridden visual type back to ws_data so _build_visual_query
        # generates the correct data-role assignments.
        if ws_data and ws_data.get('chart_type') != visual_type:
            ws_data['chart_type'] = visual_type

        visual_json = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json",
            "name": visual_id,
            "position": self._make_visual_position(pos, scale_x, scale_y, visual_count),
            "visual": {
                "visualType": visual_type,
                "drillFilterOtherVisuals": True
            }
        }

        # Add query if fields are available
        if ws_data and ws_data.get('fields'):
            query = self._build_visual_query(ws_data)
            self._append_visual_query_notes(visual_json, ws_data)
            if query:
                # If _build_visual_query detected an all-measures worksheet
                # and set an override visual type, apply it
                override_vt = ws_data.pop('_override_visual_type', None)
                if override_vt:
                    visual_type = override_vt
                    visual_json["visual"]["visualType"] = visual_type
                    fallback_note = ws_data.pop('_visual_fallback_note', None)
                    if fallback_note:
                        visual_json.setdefault('annotations', []).append({
                            'name': 'MigrationNote', 'value': fallback_note,
                        })
                    if (override_vt == 'map' and ws_data.get('chart_type') == 'azureMap'
                            and any('(generated)' in f.get('name', '').lower()
                                    for f in ws_data.get('fields', []))):
                        visual_json.setdefault('annotations', []).append({
                            'name': 'MigrationNote',
                            'value': 'Tableau-generated map coordinates require spatial geometry; no geocodable Location is bound.',
                        })
                visual_json["visual"]["query"] = query
                self._apply_worksheet_sort(visual_json, ws_data, query)

        # Visual container title (visualContainerObjects per PBIR schema)
        # Prefer the worksheet's extracted title (from Tableau <title><run>)
        # over the raw worksheet name.
        title_props = self._worksheet_title_properties(
            ws_name, ws_data, converted_objects)
        visual_json["visual"]["visualContainerObjects"] = {
            "title": [{"properties": title_props}]
        }

        # Visual objects: encodings (labels, legend, axes, colors)
        self._apply_visual_formatting(visual_json, ws_name, ws_data)

        # Visual filters — only emit if the visual has a query (From clause)
        # otherwise Source alias refs in Where conditions can't resolve → Broken_Filters
        if ws_data and ws_data.get('filters') and "query" in visual_json.get("visual", {}):
            filter_notes = []
            visual_filters = self._create_visual_filters(
                ws_data['filters'], notes=filter_notes)
            if visual_filters:
                visual_json["filterConfig"] = {"filters": visual_filters}
            for note in filter_notes:
                visual_json.setdefault('annotations', []).append({
                    'name': 'MigrationNote', 'value': note,
                })

        # Tooltip page binding (viz-in-tooltip â†’ Power BI Report Page tooltip)
        if tooltip_page_map and ws_data:
            # Check if this worksheet has a viz-in-tooltip reference
            tooltips = ws_data.get('tooltips', [])
            if isinstance(tooltips, list):
                for tip in tooltips:
                    if isinstance(tip, dict) and tip.get('type') == 'viz_in_tooltip':
                        tip_ws_name = tip.get('worksheet', '')
                        tip_page_name = tooltip_page_map.get(tip_ws_name)
                        if tip_page_name:
                            visual_json["visual"].setdefault("objects", {})
                            visual_json["visual"]["objects"]["tooltips"] = [{
                                "properties": {
                                    "type": _L("'ReportPage'"),
                                    "page": _L(f"'{tip_page_name}'")
                                }
                            }]
                            break

        # Apply padding from dashboard zone
        obj_padding = obj.get('padding', {})
        if obj_padding:
            pad_props = {}
            for side in ('left', 'right', 'top', 'bottom'):
                pad_key = f'padding-{side}'
                if pad_key in obj_padding:
                    pad_props[side] = _L(f"{obj_padding[pad_key]}D")
            if pad_props:
                visual_json["visual"]["visualContainerObjects"]["padding"] = [{"properties": pad_props}]
            # Apply border if extracted
            if obj_padding.get('border_style') and obj_padding['border_style'] != 'none':
                border_props: dict[str, object] = {
                    "show": _L("true")
                }
                if obj_padding.get('border_color'):
                    border_props["color"] = {
                        "solid": {"color": _L(f"'{obj_padding['border_color']}'")}
                    }
                visual_json["visual"]["visualContainerObjects"]["border"] = [{"properties": border_props}]

        if visual_json.get('visual', {}).get('visualType') == 'ganttChart':
            visual_json['visual']['visualType'] = 'clusteredBarChart'
        vis = visual_json["visual"]
        if 'query' not in vis and vis.get('visualType') in _REQUIRED_VISUAL_ROLES:
            label = (ws_data or {}).get('name') or 'Worksheet'
            missing_type = vis['visualType']
            vis["visualType"] = "textbox"
            vis["objects"] = {"general": [{"properties": {
                "paragraphs": [{"textRuns": [{"value": label}]}]
            }}]}
            visual_json.setdefault('annotations', []).append({
                'name': 'MigrationNote',
                'value': f'No source field resolved for "{label}"; '
                         f'the {missing_type} has no data to bind.',
            })
        _write_json(os.path.join(visual_dir, 'visual.json'), visual_json, ensure_ascii=False)
        return visual_id

    def _expose_occluded_card_values(self, visuals_dir, worksheet_visuals):
        """Shorten a covered parent card only when a clear top band exists."""
        cards = []
        for worksheet_name, visual_id in worksheet_visuals:
            path = os.path.join(visuals_dir, visual_id, 'visual.json')
            with open(path, encoding='utf-8') as stream:
                visual = json.load(stream)
            if visual.get('visual', {}).get('visualType') not in ('card', 'multiRowCard'):
                continue
            position = visual.get('position', {})
            cards.append((worksheet_name, path, visual, position))

        for name, path, visual, position in cards:
            if visual.get('visual', {}).get('visualType') != 'card':
                continue
            x, y = position.get('x', 0), position.get('y', 0)
            width, height = position.get('width', 0), position.get('height', 0)
            z = position.get('z', 0)
            if width <= 0 or height <= 0:
                continue
            center_x, center_y = x + width / 2, y + height / 2
            blockers = []
            for _, _, _, child in cards:
                cx, cy = child.get('x', 0), child.get('y', 0)
                cw, ch = child.get('width', 0), child.get('height', 0)
                if (child.get('z', 0) <= z or cw <= 0 or ch <= 0
                        or cx < x or cy < y or cx + cw > x + width
                        or cy + ch > y + height
                        or (cx == x and cy == y and cw == width and ch == height)
                        or not cx <= center_x <= cx + cw):
                    continue
                blockers.append(child)
            if not any(child['y'] <= center_y < child['y'] + child['height']
                       for child in blockers):
                continue
            free_height = min(child['y'] for child in blockers) - y
            if free_height >= self.MIN_VISUAL_HEIGHT:
                position['height'] = free_height
                note = (
                    f"Card height reduced from {height} to {free_height} pixels "
                    "to expose its value above contained higher cards; review the layout."
                )
                logger.info("Adjusted covered card %r: %s", name, note)
            else:
                note = (
                    "Card value may be obscured by contained higher cards; "
                    "no sufficiently tall free top band was available."
                )
                logger.warning("Covered card %r: %s", name, note)
            visual.setdefault('annotations', []).append({
                'name': 'MigrationNote', 'value': note,
            })
            _write_json(path, visual, ensure_ascii=False)

    @staticmethod
    def _parse_rich_text_runs(obj):
        """Parse Tableau text runs into PBI paragraphs with formatting.

        Converts Tableau <formatted-text><run> elements (bold, italic, color,
        font_size, url) into PBI textRuns with textStyle properties.

        Args:
            obj: Dashboard text object with 'text_runs' and 'content' fields

        Returns:
            list: PBI paragraphs array for textbox visual
        """
        text_runs = obj.get('text_runs', [])
        zone_align = obj.get('text_align', '')
        if not text_runs:
            # Fallback: single plain run from content
            para = {"textRuns": [{"value": obj.get('content', '')}]}
            if zone_align:
                para["horizontalTextAlignment"] = zone_align
            return [para]

        # Group runs into paragraphs by newline characters
        paragraphs = []
        current_runs = []
        current_align = ''

        def _flush(runs, align):
            para = {"textRuns": runs if runs else [{"value": ""}]}
            resolved = align or zone_align
            if resolved:
                para["horizontalTextAlignment"] = resolved
            paragraphs.append(para)

        for run in text_runs:
            text = run.get('text', '')
            run_align = run.get('alignment', '')
            # Split on newlines to create separate paragraphs
            lines = text.split('\n')
            for i, line in enumerate(lines):
                if i > 0:
                    # Newline = new paragraph
                    _flush(current_runs, current_align)
                    current_runs = []
                    current_align = ''
                if run_align and not current_align:
                    current_align = run_align
                if line or i == 0:
                    pbi_run = {"value": line}
                    # Build textStyle from Tableau run formatting
                    style = {}
                    if run.get('bold'):
                        style['fontWeight'] = 'bold'
                    if run.get('italic'):
                        style['fontStyle'] = 'italic'
                    if run.get('color'):
                        color = run['color']
                        if color.startswith('#') and len(color) == 7:
                            style['color'] = color
                        elif color.startswith('#') and len(color) == 9:
                            # Tableau uses #AARRGGBB, PBI uses #RRGGBB
                            style['color'] = '#' + color[3:]
                    if run.get('font_size'):
                        try:
                            pts = float(run['font_size'])
                            style['fontSize'] = f"{pts}pt"
                        except (ValueError, TypeError):
                            logger.debug("Could not parse font_size: %s", run.get('font_size'))
                    if style:
                        pbi_run['textStyle'] = style
                    # URL â†’ hyperlink
                    if run.get('url'):
                        pbi_run['url'] = run['url']
                    current_runs.append(pbi_run)

        # Flush last paragraph
        if current_runs:
            _flush(current_runs, current_align)

        if not paragraphs:
            para = {"textRuns": [{"value": ""}]}
            if zone_align:
                para["horizontalTextAlignment"] = zone_align
            paragraphs.append(para)
        return paragraphs

    def _textbox_height_floor(self, paragraphs):
        """Smallest height that renders the largest run without a scrollbar.

        A point is 4/3 px at 96 dpi and Power BI leaves 8px of padding above and
        below; below this the textbox clips its own text.
        """
        largest = 0.0
        for paragraph in paragraphs or []:
            for run in paragraph.get('textRuns') or []:
                size = (run.get('textStyle') or {}).get('fontSize')
                if not size:
                    continue
                try:
                    largest = max(largest, float(str(size).rstrip('pt')))
                except ValueError:
                    continue
        if largest <= 0:
            return 0
        return int(math.ceil(largest * 4 / 3 * 1.2)) + _TEXTBOX_VERTICAL_PADDING

    def _create_visual_textbox(self, visuals_dir, obj, scale_x, scale_y, visual_count):
        """Create a textbox visual from a Tableau text object.

        Supports rich text with bold, italic, color, font_size, and URLs
        from Tableau <formatted-text><run> elements.
        """
        visual_id = uuid.uuid4().hex[:8]
        visual_dir = os.path.join(visuals_dir, visual_id)

        pos = obj.get('position', {})
        paragraphs = self._parse_rich_text_runs(obj)

        general_props = {"paragraphs": paragraphs}
        vertical = obj.get('vertical_align', '')
        if vertical:
            general_props["verticalAlignment"] = vertical

        position = self._make_visual_position(pos, scale_x, scale_y, visual_count)
        floor = self._textbox_height_floor(paragraphs)
        if floor and position.get('height', 0) < floor:
            position['height'] = floor

        visual_json = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json",
            "name": visual_id,
            "position": position,
            "visual": {
                "visualType": "textbox",
                "objects": {
                    "general": [{
                        "properties": general_props
                    }]
                }
            }
        }
        _write_json(os.path.join(visual_dir, 'visual.json'), visual_json, ensure_ascii=False)

    def _create_annotation_overlay(self, visuals_dir, annotation, parent_obj,
                                    scale_x, scale_y, visual_count):
        """Create a textbox overlay visual from a Tableau annotation.

        Positions the textbox relative to the parent chart visual using the
        annotation's position data.  Annotation formatting (font size, color,
        bold/italic) is converted to PBI textStyle properties.

        Args:
            visuals_dir: Directory for visual containers.
            annotation: Annotation dict from extraction (text, position, formatting).
            parent_obj: Parent dashboard object (for base position reference).
            scale_x: Horizontal scale factor.
            scale_y: Vertical scale factor.
            visual_count: Z-index / tab order value.
        """
        visual_id = uuid.uuid4().hex[:8]
        visual_dir = os.path.join(visuals_dir, visual_id)

        text = annotation.get('text', '')
        if not text:
            return

        # Build position: offset within the parent chart area
        parent_pos = parent_obj.get('position', {})
        ann_pos = annotation.get('position', {})
        try:
            px = float(parent_pos.get('x', 0))
            py = float(parent_pos.get('y', 0))
            ax = float(ann_pos.get('x', 0))
            ay = float(ann_pos.get('y', 0))
        except (ValueError, TypeError):
            px, py, ax, ay = 0, 0, 0, 0

        overlay_pos = {
            'x': px + ax,
            'y': py + ay,
            'w': float(ann_pos.get('w', 150)),
            'h': float(ann_pos.get('h', 40)),
        }

        position = self._make_visual_position(overlay_pos, scale_x, scale_y, visual_count)

        # Build textRun with formatting
        text_run = {"value": text}
        fmt = annotation.get('formatting', {})
        style = {}
        if fmt.get('font_size'):
            try:
                style['fontSize'] = f"{float(fmt['font_size'])}pt"
            except (ValueError, TypeError):
                pass
        if fmt.get('font_family'):
            style['fontFamily'] = fmt['font_family']
        if fmt.get('font_color'):
            color = fmt['font_color']
            if color.startswith('#') and len(color) == 9:
                color = '#' + color[3:]  # #AARRGGBB → #RRGGBB
            style['color'] = color
        if fmt.get('bold'):
            style['fontWeight'] = 'bold'
        if fmt.get('italic'):
            style['fontStyle'] = 'italic'
        if style:
            text_run['textStyle'] = style

        paragraphs = [{"textRuns": [text_run]}]

        visual_json = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json",
            "name": visual_id,
            "position": position,
            "visual": {
                "visualType": "textbox",
                "objects": {
                    "general": [{
                        "properties": {
                            "paragraphs": paragraphs
                        }
                    }]
                }
            },
            "annotations": [{
                "name": "MigrationNote",
                "value": "Converted from Tableau annotation"
            }]
        }
        _write_json(os.path.join(visual_dir, 'visual.json'), visual_json, ensure_ascii=False)

    @staticmethod
    def _embed_local_image(path, resource_root=None):
        import base64
        import stat
        from pathlib import Path
        from powerbi_import.security_validator import SecurityError, is_non_local_windows_path

        if is_non_local_windows_path(path):
            raise ValueError('Non-local image paths are not read automatically')
        candidate = Path(os.path.abspath(path))
        for component in [*reversed(candidate.parents), candidate]:
            if (os.path.islink(component)
                    or getattr(os.path, 'isjunction', lambda value: False)(component)):
                raise ValueError('Linked image paths are not read automatically')
        upload_root = os.environ.get('TTPBI_UPLOAD_ROOT')
        resolved = os.path.realpath(path)
        if is_non_local_windows_path(resolved):
            raise ValueError('Non-local image paths are not read automatically')
        for root in (upload_root, resource_root):
            if not root:
                continue
            try:
                confined = (not is_non_local_windows_path(root)
                            and os.path.commonpath([os.path.realpath(root), resolved])
                            == os.path.realpath(root))
            except ValueError:
                confined = False
            if not confined:
                if upload_root:
                    sys.audit('ttpbi.upload_denied', 'Image resource escapes private input scope')
                    raise SecurityError('Image resource escapes private input scope')
                raise ValueError('Image resource escapes its workbook directory')
        mime_types = {
            '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.gif': 'image/gif', '.svg': 'image/svg+xml', '.bmp': 'image/bmp',
            '.webp': 'image/webp', '.tif': 'image/tiff', '.tiff': 'image/tiff',
            '.ico': 'image/x-icon',
        }
        mime = mime_types.get(os.path.splitext(resolved)[1].lower())
        if not mime:
            raise ValueError('Unsupported local image format')
        limit = 20 * 1024 * 1024
        info = os.stat(resolved)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('Local image must be a regular file no larger than 20 MiB')
        with open(resolved, 'rb') as handle:
            payload = handle.read(limit + 1)
        if not payload or len(payload) > limit:
            raise ValueError('Local image is empty or larger than 20 MiB')
        return f'data:{mime};base64,' + base64.b64encode(payload).decode('ascii')

    def _create_visual_image(self, visuals_dir, obj, scale_x, scale_y, visual_count):
        """Create an image visual from a Tableau image object."""
        visual_id = uuid.uuid4().hex[:8]
        visual_dir = os.path.join(visuals_dir, visual_id)

        pos = obj.get('position', {})
        img_source = obj.get('source') or obj.get('url') or ''
        missing_resource = (obj.get('resource_missing') is True
                            or obj.get('source_exists') is False
                            or not img_source)
        resource_issue = ''
        local_path = obj.get('resolved_source') or (
            img_source if os.path.isabs(img_source) else None)
        if local_path and not missing_resource:
            try:
                img_source = self._embed_local_image(local_path, obj.get('resource_root'))
            except (OSError, ValueError) as exc:
                missing_resource = True
                resource_issue = type(exc).__name__
        if missing_resource:
            filename = os.path.basename(img_source.replace('\\', '/'))
            note = 'Source image unavailable'
            if filename:
                note += ': ' + filename
            logger.warning('%s', note)
            visual_json = {
                "$schema": SCHEMA_VISUAL,
                "name": visual_id,
                "position": self._make_visual_position(pos, scale_x, scale_y, visual_count),
                "visual": {
                    "visualType": "textbox",
                    "objects": {"general": [{"properties": {
                        "paragraphs": [{"textRuns": [{"value": note}]}]
                    }}]},
                },
                "annotations": [{
                    "name": "MigrationNote",
                    "value": note + '. No replacement image was generated.'
                             + (f' Local resource could not be embedded ({resource_issue}).'
                                if resource_issue else ''),
                }],
            }
            _write_json(os.path.join(visual_dir, 'visual.json'), visual_json, ensure_ascii=False)
            return

        # For embedded TWBX images, convert to base64 data URI
        if img_source and not img_source.startswith(('http://', 'https://', 'data:')):
            # Track for later resolution by _extract_twbx_data_files
            if not hasattr(self, '_embedded_image_paths'):
                self._embedded_image_paths = {}
            self._embedded_image_paths[visual_id] = {
                'relative_path': img_source,
                'visual_dir': visual_dir,
            }

        visual_json = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json",
            "name": visual_id,
            "position": self._make_visual_position(pos, scale_x, scale_y, visual_count),
            "visual": {
                "visualType": "image",
                "objects": {
                    "general": [{
                        "properties": {
                            "imageUrl": _L(f"'{img_source}'")
                        }
                    }]
                }
            }
        }
        _write_json(os.path.join(visual_dir, 'visual.json'), visual_json, ensure_ascii=False)

    def _create_visual_filter_control(self, visuals_dir, obj, scale_x, scale_y,
                                       visual_count, calc_id_to_caption, converted_objects):
        """Create a slicer visual from a Tableau filter control."""
        visual_id = uuid.uuid4().hex[:8]
        visual_dir = os.path.join(visuals_dir, visual_id)

        pos = obj.get('position', {})
        calc_col_id = obj.get('calc_column_id') or obj.get('calc_col_id', '')
        param_ref = obj.get('param', '')
        reference = re.search(r'\[([^\]]+)\]\.\[([^\]]+)\]', param_ref)
        datasource = reference.group(1) if reference else ''
        reference_field = self._clean_field_name(reference.group(2)) if reference else ''
        column_name = calc_id_to_caption.get(calc_col_id, '')
        if not column_name and reference_field:
            column_name = calc_id_to_caption.get(reference_field, reference_field)
        if not column_name:
            column_name = calc_col_id if calc_col_id else obj.get('field', obj.get('name', ''))

        _bim_sym = getattr(self, '_actual_bim_symbols', None) or set()
        sources = getattr(self, '_datasources_ref', [])
        if datasource and sources:
            if not any(source.get('name') == datasource for source in sources):
                logger.warning(
                    "Filter control %r references unknown datasource %r; it is not emitted.",
                    column_name, datasource,
                )
                return
            names = {name for name in (reference_field, column_name) if name}
            candidates = {
                self._resolve_field_entity(name, datasource=datasource) for name in names
            }
            if _bim_sym:
                candidates &= _bim_sym
            if len(candidates) != 1:
                logger.warning(
                    "Filter control %r has no unique binding in datasource %r; "
                    "it is not emitted.", column_name, datasource,
                )
                return
            table_name, column_name = next(iter(candidates))
        elif _bim_sym:
            candidates = {(table, prop) for table, prop in _bim_sym if prop == column_name}
            if len(candidates) != 1:
                logger.warning(
                    "Filter control %r has no unique model binding; it is not emitted.",
                    column_name,
                )
                return
            table_name, column_name = next(iter(candidates))
        elif column_name and column_name in getattr(self, '_field_map', {}):
            table_name, column_name = self._field_map[column_name]
        else:
            logger.warning("Filter control %r has no model binding; it is not emitted.", column_name)
            return

        measure_symbols = getattr(self, '_actual_bim_measure_symbols', None)
        is_measure = ((table_name, column_name) in measure_symbols
                      if measure_symbols is not None
                      else column_name in (getattr(self, '_actual_bim_measure_names', None) or set()))
        if is_measure:
            logger.warning("Filter control %s.%s is a measure and cannot be a slicer",
                           table_name, column_name)
            return

        vpos = self._make_visual_position(pos, scale_x, scale_y, visual_count)
        vx, vy, vw, vh = vpos['x'], vpos['y'], vpos['width'], vpos['height']

        # Determine slicer mode from parameter/field data type
        slicer_mode = self._detect_slicer_mode(
            obj, column_name, converted_objects, resolved_pair=(table_name, column_name))

        slicer_json = self._create_slicer_visual(visual_id, vx, vy, vw, vh,
                                                  column_name, table_name, visual_count,
                                                  slicer_mode=slicer_mode,
                                                  title=column_name)
        slicer_json.setdefault('annotations', []).append({
            'name': 'MigrationNote',
            'value': (
                'Review slicer interactions and existing worksheet filters: '
                'Power BI defaults may affect other visuals or intersect the '
                'saved Tableau filter selection.'
            ),
        })
        logger.warning(
            "Filter control %s.%s requires review of slicer interactions and worksheet filters",
            table_name, column_name,
        )
        _write_json(os.path.join(visual_dir, 'visual.json'), slicer_json, ensure_ascii=False)

    def _create_visual_parameter_control(self, visuals_dir, obj, scale_x, scale_y,
                                          visual_count, converted_objects):
        """Create a slicer visual from a Tableau parameter control (paramctrl zone).

        Maps the parameter to its What-If table in the semantic model.  The
        slicer references the parameter table's Value column so users can pick
        a parameter value from a dropdown.
        """
        visual_id = uuid.uuid4().hex[:8]
        visual_dir = os.path.join(visuals_dir, visual_id)

        pos = obj.get('position', {})
        param_name = obj.get('param_name', '')

        # Resolve param_name (e.g., "Parameter 1") to caption via parameters list
        param_caption = param_name
        has_aliases = False
        for p in converted_objects.get('parameters', []):
            pname = p.get('name', '').strip('[]')
            if pname == param_name:
                param_caption = p.get('caption', param_name)
                # Check if aliases exist (for display name column)
                avs = p.get('allowable_values', [])
                has_aliases = any(
                    v.get('alias') and str(v.get('alias')) != str(v.get('value', ''))
                    for v in avs if v.get('type') != 'range'
                )
                break

        # The What-If table uses the caption as table name
        # Use "Name" column when aliases exist, "Value" otherwise
        table_name = param_caption
        column_name = "Name" if has_aliases else "Value"

        # Skip slicer if the parameter table doesn't exist in the semantic model
        _bim_sym = getattr(self, '_actual_bim_symbols', None) or set()
        if _bim_sym:
            _bim_tables = {tbl for (tbl, _) in _bim_sym}
            if table_name not in _bim_tables:
                return
        elif hasattr(self, '_field_map') and not any(
                entity == table_name for entity, _ in self._field_map.values()):
            return

        vpos = self._make_visual_position(pos, scale_x, scale_y, visual_count)
        vx, vy, vw, vh = vpos['x'], vpos['y'], vpos['width'], vpos['height']

        slicer_json = self._create_slicer_visual(visual_id, vx, vy, vw, vh,
                                                  column_name, table_name, visual_count,
                                                  slicer_mode='Dropdown',
                                                  title=param_caption)
        _write_json(os.path.join(visual_dir, 'visual.json'), slicer_json, ensure_ascii=False)

    @staticmethod
    def _normalize_action_visual(container):
        visual = container['visual']
        objects = visual.get('objects', {})
        actions = objects.pop('action', [])
        if actions:
            for action in actions:
                properties = action.get('properties', {})
                if 'destinationPage' in properties:
                    properties['navigationSection'] = properties.pop('destinationPage')
                properties.setdefault('show', _L('true'))
            visual.setdefault('visualContainerObjects', {})['visualLink'] = actions
        for icon in objects.get('icon', []):
            literal = icon.get('properties', {}).get('shapeType', {}).get('expr', {}).get('Literal')
            if literal and literal.get('Value') == "'ArrowRight'":
                literal['Value'] = "'rightArrow'"
            elif literal and literal.get('Value') == "'Filter'":
                literal['Value'] = "'bookmarks'"

    def _create_action_visuals(self, visuals_dir, actions, scale_x, scale_y,
                                visual_count, page_display_name, *,
                                page_name_map=None):
        """Create action button visuals from Tableau actions.

        Generates:
        - URL actions â†’ actionButton visuals with WebUrl type
        - sheet-navigate actions â†’ actionButton visuals with PageNavigation type

        Returns the number of visuals created.
        """
        created = 0
        for action in actions:
            action_type = action.get('type', '')

            if action_type == 'url':
                url = action.get('url', '')
                if not _is_static_url(url):
                    # Tableau URL actions are usually built from a field, which
                    # a button cannot hold; a data-bound URL column carries it.
                    continue
                visual_id = uuid.uuid4().hex[:8]
                visual_dir = os.path.join(visuals_dir, visual_id)

                action_name = action.get('name', 'URL Action')

                btn_json = {
                    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json",
                    "name": visual_id,
                    "position": {
                        "x": 10, "y": 10 + created * 50,
                        "z": (visual_count + created) * 1000,
                        "height": 40, "width": 200,
                        "tabOrder": (visual_count + created) * 1000
                    },
                    "visual": {
                        "visualType": "actionButton",
                        "objects": {
                            "icon": [{"properties": {"shapeType": _L("'ArrowRight'")}}],
                            "outline": [{"properties": {"show": _L("false")}}],
                            "text": [{"properties": {
                                "show": _L("true"),
                                "text": _L(f"'{action_name}'")
                            }}],
                            "action": [{"properties": {
                                "type": _L("'WebUrl'"),
                                "webUrl": _L(f"'{url}'")
                            }}]
                        }
                    }
                }
                self._normalize_action_visual(btn_json)
                _write_json(os.path.join(visual_dir, 'visual.json'), btn_json, ensure_ascii=False)
                created += 1

            elif action_type == 'sheet-navigate':
                created += self._generate_navigation_buttons(
                    visuals_dir, [action], visual_count + created,
                    page_name_map or {}, button_offset=created)

        return created

    # ── Sprint 122: Set Actions & Interactive Parity ────────────────

    def _generate_set_action_artifacts(self, visuals_dir, actions, visual_count,
                                       page_display_name, converted_objects):
        """Generate hidden slicers + bookmarks + action buttons for set-value actions.

        For each set-value action:
        1. Creates a hidden slicer bound to the set's source field.
        2. Creates two bookmarks (set-active / set-clear) that toggle
           the slicer's filter state.
        3. Creates an action button that triggers the set-active bookmark.

        Returns ``(created_count, bookmarks_list)`` where *bookmarks_list*
        contains the bookmark dicts ready for ``_write_bookmark_files``.
        """
        created = 0
        bookmarks = []
        set_actions = [a for a in actions if a.get('type') == 'set-value']
        for action in set_actions:
            set_name = action.get('set_name') or action.get('target_set', '')
            source_field = action.get('source_field') or action.get('target_field', '')
            action_name = action.get('name', set_name or 'Set Action')
            if not source_field:
                continue

            # Resolve table for the source field
            # _field_map normally stores tuples (table, prop) but callers
            # may also pass dicts with a 'table' key.
            field_map = getattr(self, '_field_map', {})
            entry = field_map.get(source_field)
            if entry is None:
                table_name = getattr(self, '_main_table', 'Table')
            elif isinstance(entry, dict):
                table_name = entry.get('table', getattr(self, '_main_table', 'Table'))
            else:
                table_name = entry[0]

            # 1. Hidden slicer visual
            slicer_id = uuid.uuid4().hex[:8]
            slicer_dir = os.path.join(visuals_dir, slicer_id)
            slicer_json = {
                "$schema": SCHEMA_VISUAL,
                "name": slicer_id,
                "position": {
                    "x": 0, "y": 0,
                    "z": (visual_count + created) * 1000,
                    "height": 0, "width": 0,
                    "tabOrder": (visual_count + created) * 1000
                },
                "visual": {
                    "visualType": "slicer",
                    "query": {
                        "queryState": {
                            "Values": {
                                "projections": [{"field": {
                                    "Column": {
                                        "Expression": {"SourceRef": {"Entity": table_name}},
                                        "Property": source_field
                                    }
                                }}]
                            }
                        }
                    },
                    "objects": {
                        "data": [{"properties": {"mode": _L("'Dropdown'")}}],
                        "header": [{"properties": {"show": _L("false")}}]
                    }
                },
                "filters": [],
                "annotations": [{
                    "name": "MigrationNote",
                    "value": f"Hidden slicer for set action '{action_name}' (set: {set_name})"
                }],
                "isHidden": True
            }
            _write_json(os.path.join(slicer_dir, 'visual.json'), slicer_json, ensure_ascii=False)
            created += 1

            # 2. Bookmarks: set-active and set-clear
            assign = action.get('assign_behavior', 'assign')
            for suffix, bm_label in [('active', f'{action_name} (Apply)'),
                                      ('clear', f'{action_name} (Clear)')]:
                bm_name = uuid.uuid4().hex[:8]
                bm = {
                    'name': bm_name,
                    'displayName': bm_label,
                    'explorationState': {
                        'version': '1.0',
                        'activeSection': page_display_name,
                        'sections': {}
                    },
                    'options': {'targetVisualIds': [slicer_id]},
                    'annotations': [{
                        'name': 'MigrationNote',
                        'value': f"Set action bookmark ({suffix}): "
                                 f"set={set_name}, behavior={assign}"
                    }]
                }
                bookmarks.append(bm)

            # 3. Action button that triggers the first (active) bookmark
            btn_id = uuid.uuid4().hex[:8]
            btn_dir = os.path.join(visuals_dir, btn_id)
            btn_json = {
                "$schema": SCHEMA_VISUAL,
                "name": btn_id,
                "position": {
                    "x": 10, "y": 10 + created * 50,
                    "z": (visual_count + created) * 1000,
                    "height": 40, "width": 200,
                    "tabOrder": (visual_count + created) * 1000
                },
                "visual": {
                    "visualType": "actionButton",
                    "objects": {
                        "icon": [{"properties": {"shapeType": _L("'Filter'")}}],
                        "outline": [{"properties": {"show": _L("false")}}],
                        "text": [{"properties": {
                            "show": _L("true"),
                            "text": _L(f"'{action_name}'")
                        }}],
                        "action": [{"properties": {
                            "type": _L("'Bookmark'"),
                            "bookmark": _L(f"'{bookmarks[-2]['name']}'")
                        }}]
                    }
                },
                "annotations": [{
                    "name": "MigrationNote",
                    "value": f"Set action button: {action_name} → slicer {slicer_id}"
                }]
            }
            self._normalize_action_visual(btn_json)
            _write_json(os.path.join(btn_dir, 'visual.json'), btn_json, ensure_ascii=False)
            created += 1

        return created, bookmarks

    def _generate_navigation_buttons(self, visuals_dir, actions, visual_count,
                                      page_name_map, *, button_offset=0):
        """Create navigation action buttons with drill-through filter support.

        *page_name_map* maps Tableau view names to PBIR page IDs.

        Returns the number of visuals created.
        """
        created = 0
        nav_actions = [a for a in actions if a.get('type') == 'sheet-navigate']
        for action in nav_actions:
            target_ws = action.get('target_sheet') or action.get('target_worksheet', '')
            if not target_ws and action.get('target_worksheets'):
                target_ws = action['target_worksheets'][0]
            action_name = action.get('name', target_ws or 'Navigate')
            dest_page = page_name_map.get(target_ws, '') if target_ws else ''
            if not dest_page:
                logger.warning("Navigation action %r has no generated page for target %r",
                               action_name, target_ws)

            visual_id = uuid.uuid4().hex[:8]
            visual_dir = os.path.join(visuals_dir, visual_id)

            action_props = {"type": _L("'PageNavigation'")}
            if dest_page:
                action_props["destinationPage"] = _L(f"'{dest_page}'")

            btn_json = {
                "$schema": SCHEMA_VISUAL,
                "name": visual_id,
                "position": {
                    "x": 10, "y": 10 + (button_offset + created) * 50,
                    "z": (visual_count + created) * 1000,
                    "height": 40, "width": 200,
                    "tabOrder": (visual_count + created) * 1000
                },
                "visual": {
                    "visualType": "actionButton",
                    "objects": {
                        "icon": [{"properties": {"shapeType": _L("'ArrowRight'")}}],
                        "outline": [{"properties": {"show": _L("false")}}],
                        "text": [{"properties": {
                            "show": _L("true"),
                            "text": _L(f"'{action_name}'")
                        }}],
                        "action": [{"properties": action_props}]
                    }
                },
                "annotations": [{
                    "name": "MigrationNote",
                    "value": f"Navigation action → {dest_page or target_ws}"
                }]
            }

            # If the action has field mappings, annotate drill-through filters
            field_mappings = action.get('field_mappings', [])
            if field_mappings:
                mapping_desc = '; '.join(f"{fm['source']}→{fm['target']}" for fm in field_mappings)
                btn_json['annotations'].append({
                    'name': 'DrillThroughFields',
                    'value': mapping_desc
                })

            self._normalize_action_visual(btn_json)
            _write_json(os.path.join(visual_dir, 'visual.json'), btn_json, ensure_ascii=False)
            created += 1

        return created

    def _generate_parameter_action_slicers(self, visuals_dir, actions,
                                            visual_count, converted_objects):
        """Create slicer visuals for Tableau parameter-change actions.

        Maps each ``param`` action to a slicer visual bound to the
        corresponding PBI What-If parameter table, so users can
        interactively change parameter values.

        Returns the number of visuals created.
        """
        created = 0
        param_actions = [a for a in actions if a.get('type') == 'param']
        parameters = converted_objects.get('parameters', [])
        param_by_name = {p.get('name', ''): p for p in parameters}

        for action in param_actions:
            param_name = action.get('target_parameter') or action.get('parameter', '')
            if not param_name:
                continue
            action_name = action.get('name', f'Parameter: {param_name}')

            # Resolve the parameter table name — PBI convention
            table_name = param_name
            param_def = param_by_name.get(param_name, {})

            # Determine slicer mode from parameter definition
            domain_type = param_def.get('domain_type', '')
            if domain_type == 'range':
                slicer_mode = 'Between'
            elif domain_type == 'list':
                slicer_mode = 'Dropdown'
            else:
                slicer_mode = 'Dropdown'

            visual_id = uuid.uuid4().hex[:8]
            visual_dir = os.path.join(visuals_dir, visual_id)

            slicer_json = {
                "$schema": SCHEMA_VISUAL,
                "name": visual_id,
                "position": {
                    "x": 10, "y": 10 + (visual_count + created) * 60,
                    "z": (visual_count + created) * 1000,
                    "height": 50, "width": 250,
                    "tabOrder": (visual_count + created) * 1000
                },
                "visual": {
                    "visualType": "slicer",
                    "query": {
                        "queryState": {
                            "Values": {
                                "projections": [{"field": {
                                    "Column": {
                                        "Expression": {"SourceRef": {"Entity": table_name}},
                                        "Property": param_name
                                    }
                                }}]
                            }
                        }
                    },
                    "objects": {
                        "data": [{"properties": {"mode": _L(f"'{slicer_mode}'")}}],
                        "header": [{"properties": {"show": _L("true")}}]
                    },
                    "title": action_name
                },
                "annotations": [{
                    "name": "MigrationNote",
                    "value": f"Parameter action slicer: {action_name} → parameter '{param_name}'"
                }]
            }
            _write_json(os.path.join(visual_dir, 'visual.json'), slicer_json, ensure_ascii=False)
            created += 1

        return created

    def create_report_structure(self, project_dir, report_name, converted_objects):
        """Creates the Report structure in PBIR v4.0 format (identical to PBI Hero reference)

        Structure:
          Report/
            .platform
            definition.pbir
            definition/
              version.json
              report.json
              pages/
                pages.json
                {pageName}/
                  page.json
                  visuals/
                    {visualId}/
                      visual.json
        """
        import shutil
        import time

        # Clear auto-generated measures from previous run (batch mode safety)
        from powerbi_import.visual_generator import clear_auto_generated_measures
        clear_auto_generated_measures()

        # Build field mapping from Tableau to Power BI model
        self._build_field_mapping(converted_objects)

        # Initialize motion chart bookmarks list (may be populated by dashboard pages)
        self._motion_chart_bookmarks = []
        # Initialize set action bookmarks list (Sprint 122)
        self._set_action_bookmarks = []

        report_dir = os.path.join(project_dir, f"{report_name}.Report")

        # Clean previous content (with retries for OneDrive sync locks)
        if os.path.exists(report_dir):
            for attempt in range(5):
                try:
                    shutil.rmtree(report_dir)
                    break
                except PermissionError:
                    if attempt < 4:
                        time.sleep(0.5 * (attempt + 1))
                    else:
                        # Last resort: remove files individually, skip locked ones
                        for root, dirs, files in os.walk(report_dir, topdown=False):
                            for name in files:
                                try:
                                    os.remove(os.path.join(root, name))
                                except PermissionError as exc:
                                    logger.debug("Could not remove locked file %s: %s", os.path.join(root, name), exc)
                            for name in dirs:
                                try:
                                    os.rmdir(os.path.join(root, name))
                                except (PermissionError, OSError) as exc:
                                    logger.debug("Could not remove directory %s: %s", os.path.join(root, name), exc)
        os.makedirs(report_dir, exist_ok=True)

        # 1. .platform
        platform = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
            "metadata": {
                "type": "Report",
                "displayName": report_name
            },
            "config": {
                "version": "2.0",
                "logicalId": str(uuid.uuid4())
            }
        }
        _write_json(os.path.join(report_dir, '.platform'), platform)

        # 2. definition.pbir (PBIR v4.0, schema 2.0.0, points to SemanticModel)
        report_definition = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
            "version": "4.0",
            "datasetReference": {
                "byPath": {
                    "path": f"../{report_name}.SemanticModel"
                }
            }
        }
        _write_json(os.path.join(report_dir, 'definition.pbir'), report_definition)

        # 3. definition/ folder
        def_dir = os.path.join(report_dir, 'definition')
        os.makedirs(def_dir, exist_ok=True)

        # 3a. version.json
        version_json = {
            "$schema": SCHEMA_VERSION,
            "version": "2.0.0"
        }
        _write_json(os.path.join(def_dir, 'version.json'), version_json)

        # 3b. report.json â€” generate custom theme from extracted Tableau dashboard colors
        theme_data = None
        dashboards = converted_objects.get('dashboards', [])
        for db in dashboards:
            t = db.get('theme')
            if t and t.get('colors'):
                theme_data = t
                break

        custom_theme = tmdl_generator.generate_theme_json(theme_data)

        report_json = {
            "$schema": SCHEMA_REPORT,
            "themeCollection": {
                "baseTheme": {
                    "name": PBI_BASE_THEME_NAME,
                    "reportVersionAtImport": PBI_REPORT_VERSION_AT_IMPORT,
                    "type": "SharedResources"
                }
            },
            "resourcePackages": [
                {
                    "name": "SharedResources",
                    "type": "SharedResources",
                    "items": [
                        {
                            "name": PBI_BASE_THEME_NAME,
                            "path": f"BaseThemes/{PBI_BASE_THEME_NAME}.json",
                            "type": "BaseTheme"
                        }
                    ]
                }
            ],
            "settings": {
                "hideVisualContainerHeader": True,
                "useStylableVisualContainerHeader": True,
                "exportDataMode": "None",
                "defaultDrillFilterOtherVisuals": True,
                "allowChangeFilterTypes": True,
                "useEnhancedTooltips": True
            }
        }

        # Add custom theme reference if theme data was found
        if theme_data:
            report_json["resourcePackages"].append({
                "name": "MigrationTheme",
                "type": "RegisteredResources",
                "items": [
                    {
                        "name": "TableauMigrationTheme",
                        "path": "RegisteredResources/TableauMigrationTheme.json",
                        "type": "CustomTheme"
                    }
                ]
            })
            report_json["themeCollection"]["customTheme"] = {
                "name": "TableauMigrationTheme",
                "reportVersionAtImport": PBI_REPORT_VERSION_AT_IMPORT,
                "type": "RegisteredResources"
            }

        # Worksheet filters are emitted on their owning visual. Only
        # datasource-level filters belong at report scope.
        all_report_filters = self._collect_report_scope_filters(converted_objects)
        if all_report_filters:
            report_level_filters = self._create_visual_filters(all_report_filters)
            if report_level_filters:
                report_json["filterConfig"] = {"filters": report_level_filters}

        # Defer writing report.json until after pages â€” custom visual
        # GUIDs are discovered during visual creation and must be added
        # to report.json before it is persisted.
        self._used_custom_guids = {}  # key â†’ guid_info dict

        # Write custom theme file if theme data was found
        if theme_data:
            res_dir = os.path.join(def_dir, 'RegisteredResources')
            _write_json(os.path.join(res_dir, 'TableauMigrationTheme.json'), custom_theme)

        # Copy custom shapes from extraction output to RegisteredResources
        self._copy_custom_shapes(def_dir, converted_objects)

        # 4. Create pages with visuals
        pages_dir = os.path.join(def_dir, 'pages')
        os.makedirs(pages_dir, exist_ok=True)

        worksheets = converted_objects.get('worksheets', [])

        page_names = []
        page_name_map = {}

        # Pre-build tooltip page mapping for viz-in-tooltip binding
        # tooltip_page_map: worksheet_name â†’ tooltip_page_name
        tooltip_page_map = {}

        if dashboards:
            page_names = self._create_dashboard_pages(
                pages_dir, dashboards, worksheets, converted_objects, tooltip_page_map,
                standalone_worksheets=self._get_standalone_worksheets(dashboards, worksheets),
                page_name_map=page_name_map)
        else:
            page_names = self._create_fallback_page(pages_dir, worksheets, converted_objects)
            page_name_map.update(
                (ws['name'], page_name)
                for ws, page_name in zip(worksheets, page_names)
                if isinstance(ws.get('name'), str) and ws['name'])

        # Collect bookmarks: from stories + from dynamic zone visibility (sheet-swap)
        all_bookmarks = []
        stories = converted_objects.get('stories', [])
        if stories:
            all_bookmarks.extend(self._create_bookmarks(
                stories, page_names, page_name_map=page_name_map))
        # Dynamic zone visibility â†’ swap bookmarks (per dashboard)
        if dashboards:
            for db_idx, db in enumerate(dashboards):
                dz_vis = db.get('dynamic_zone_visibility', [])
                if dz_vis:
                    pg_name = page_names[db_idx] if db_idx < len(page_names) else ''
                    all_bookmarks.extend(self._create_swap_bookmarks(dz_vis, pg_name))
        # Motion chart bookmarks from Pages shelf
        if self._motion_chart_bookmarks:
            all_bookmarks.extend(self._motion_chart_bookmarks)
        # Set action bookmarks (Sprint 122)
        if self._set_action_bookmarks:
            all_bookmarks.extend(self._set_action_bookmarks)
        if all_bookmarks:
            self._write_bookmark_files(def_dir, all_bookmarks)

        # 5b. Tooltip pages
        self._create_tooltip_pages(pages_dir, page_names, worksheets, converted_objects)

        # 5c. Mobile layout pages
        if dashboards:
            self._create_mobile_pages(pages_dir, page_names, dashboards, worksheets, converted_objects)

        # 5d. Drill-through pages â€” from filter actions targeting specific worksheets
        self._create_drillthrough_pages(pages_dir, page_names, worksheets,
                                        converted_objects)
        pages_metadata = {
            "$schema": SCHEMA_PAGES_METADATA,
            "pageOrder": page_names,
            "activePageName": page_names[0] if page_names else ""
        }
        _write_json(os.path.join(pages_dir, 'pages.json'), pages_metadata)

        # Post-generation cleanup: remove stale page and visual directories
        # (from previous migration runs or OneDrive lock leftovers)
        valid_page_set = set(page_names)
        stale_count = 0
        if os.path.isdir(pages_dir):
            for entry in os.listdir(pages_dir):
                entry_path = os.path.join(pages_dir, entry)
                if not os.path.isdir(entry_path):
                    continue
                # Remove entire stale page directories that are not in current
                # page_names AND don't have a page.json (i.e., truly orphaned)
                if (entry.startswith('ReportSection')
                        and entry not in valid_page_set
                        and not os.path.isfile(os.path.join(entry_path, 'page.json'))):
                    if not _rmtree_with_retry(entry_path):
                        stale_count += 1
                    continue
                # For valid pages, remove stale visual subdirs without visual.json
                visuals_dir = os.path.join(entry_path, 'visuals')
                if os.path.isdir(visuals_dir):
                    for vdir in os.listdir(visuals_dir):
                        vpath = os.path.join(visuals_dir, vdir)
                        if os.path.isdir(vpath) and not os.path.exists(os.path.join(vpath, 'visual.json')):
                            _rmtree_with_retry(vpath)
        if stale_count:
            logger.warning(
                "%d stale page directories could not be removed "
                "(OneDrive lock or permission issue)", stale_count
            )
            print(f"  âš  {stale_count} stale page directories could not be removed (OneDrive lock)")

        # Inject publicCustomVisuals for any AppSource custom visuals used
        if self._used_custom_guids:
            cv_classes = sorted({info.get('class', k) for k, info in self._used_custom_guids.items()})
            report_json["publicCustomVisuals"] = cv_classes

        _write_json(os.path.join(def_dir, 'report.json'), report_json)

        # Sprint 140 — Self-Healing v3.4: scan the freshly written .Report
        # tree for layout/visual/filter/bookmark issues and patch in place.
        try:
            from powerbi_import.self_healing_report import heal_report
            repaired = heal_report(report_dir)
            if repaired:
                logger.info(
                    "Self-Healing v3.4 applied %d report-side repair(s)",
                    repaired,
                )
        except Exception:  # never block migration
            pass

        return report_dir

    def _generate_report_definition_content(self, report_dir, report_name,
                                              converted_objects):
        """Generate report definition content (pages, visuals, theme) inside an
        existing report directory.

        This is the content-only portion of ``create_report_structure`` â€” it does
        **not** write ``.platform`` or ``definition.pbir``, making it suitable for
        thin reports that already have those files pointing to a shared semantic model.
        """
        # Ensure field mapping is initialized (thin reports call this directly)
        if not hasattr(self, '_field_map'):
            self._build_field_mapping(converted_objects)

        def_dir = os.path.join(report_dir, 'definition')
        os.makedirs(def_dir, exist_ok=True)

        # version.json
        version_json = {
            "$schema": SCHEMA_VERSION,
            "version": "2.0.0",
        }
        _write_json(os.path.join(def_dir, 'version.json'), version_json)

        # Theme extraction
        theme_data = None
        dashboards = converted_objects.get('dashboards', [])
        for db in dashboards:
            t = db.get('theme')
            if t and t.get('colors'):
                theme_data = t
                break

        custom_theme = tmdl_generator.generate_theme_json(theme_data)

        report_json = {
            "$schema": SCHEMA_REPORT,
            "themeCollection": {
                "baseTheme": {
                    "name": PBI_BASE_THEME_NAME,
                    "reportVersionAtImport": PBI_REPORT_VERSION_AT_IMPORT,
                    "type": "SharedResources",
                },
            },
            "resourcePackages": [
                {
                    "name": "SharedResources",
                    "type": "SharedResources",
                    "items": [{"name": PBI_BASE_THEME_NAME, "path": f"BaseThemes/{PBI_BASE_THEME_NAME}.json", "type": "BaseTheme"}],
                },
            ],
            "settings": {
                "hideVisualContainerHeader": True,
                "useStylableVisualContainerHeader": True,
                "exportDataMode": "None",
                "defaultDrillFilterOtherVisuals": True,
                "allowChangeFilterTypes": True,
                "useEnhancedTooltips": True,
            },
        }

        if theme_data:
            report_json["resourcePackages"].append({
                "name": "MigrationTheme", "type": "RegisteredResources",
                "items": [{"name": "TableauMigrationTheme",
                           "path": "RegisteredResources/TableauMigrationTheme.json",
                           "type": "CustomTheme"}],
            })
            report_json["themeCollection"]["customTheme"] = {
                "name": "TableauMigrationTheme",
                "reportVersionAtImport": PBI_REPORT_VERSION_AT_IMPORT,
                "type": "RegisteredResources",
            }
            res_dir = os.path.join(def_dir, 'RegisteredResources')
            _write_json(os.path.join(res_dir, 'TableauMigrationTheme.json'), custom_theme)

        # Worksheet filters are emitted on their owning visual. Only
        # datasource-level filters belong at report scope.
        all_report_filters = self._collect_report_scope_filters(converted_objects)
        if all_report_filters:
            report_level_filters = self._create_visual_filters(all_report_filters)
            if report_level_filters:
                report_json["filterConfig"] = {"filters": report_level_filters}

        self._used_custom_guids = {}

        # Pages
        pages_dir = os.path.join(def_dir, 'pages')
        os.makedirs(pages_dir, exist_ok=True)
        worksheets = converted_objects.get('worksheets', [])
        page_names = []
        page_name_map = {}
        tooltip_page_map = {}

        self._motion_chart_bookmarks = []
        self._set_action_bookmarks = []
        if dashboards:
            page_names = self._create_dashboard_pages(
                pages_dir, dashboards, worksheets, converted_objects, tooltip_page_map,
                standalone_worksheets=self._get_standalone_worksheets(dashboards, worksheets),
                page_name_map=page_name_map)
        else:
            page_names = self._create_fallback_page(pages_dir, worksheets, converted_objects)
            page_name_map.update(
                (ws['name'], page_name)
                for ws, page_name in zip(worksheets, page_names)
                if isinstance(ws.get('name'), str) and ws['name'])

        # Bookmarks
        all_bookmarks = []
        stories = converted_objects.get('stories', [])
        if stories:
            all_bookmarks.extend(self._create_bookmarks(
                stories, page_names, page_name_map=page_name_map))
        if dashboards:
            for db_idx, db in enumerate(dashboards):
                dz_vis = db.get('dynamic_zone_visibility', [])
                if dz_vis:
                    pg_name = page_names[db_idx] if db_idx < len(page_names) else ''
                    all_bookmarks.extend(self._create_swap_bookmarks(dz_vis, pg_name))
        # Motion chart bookmarks from Pages shelf
        if self._motion_chart_bookmarks:
            all_bookmarks.extend(self._motion_chart_bookmarks)
        # Set action bookmarks (Sprint 122)
        if self._set_action_bookmarks:
            all_bookmarks.extend(self._set_action_bookmarks)
        if all_bookmarks:
            self._write_bookmark_files(def_dir, all_bookmarks)

        self._create_tooltip_pages(pages_dir, page_names, worksheets, converted_objects)
        if dashboards:
            self._create_mobile_pages(pages_dir, page_names, dashboards, worksheets, converted_objects)
        self._create_drillthrough_pages(pages_dir, page_names, worksheets, converted_objects)

        pages_metadata = {
            "$schema": SCHEMA_PAGES_METADATA,
            "pageOrder": page_names,
            "activePageName": page_names[0] if page_names else "",
        }
        _write_json(os.path.join(pages_dir, 'pages.json'), pages_metadata)

        # Inject publicCustomVisuals for any AppSource custom visuals used
        if self._used_custom_guids:
            cv_classes = sorted({info.get('class', k) for k, info in self._used_custom_guids.items()})
            report_json["publicCustomVisuals"] = cv_classes

        _write_json(os.path.join(def_dir, 'report.json'), report_json)

    # â”€â”€ Report page sub-methods (extracted from create_report_structure) â”€â”€

    def _create_dashboard_pages(self, pages_dir, dashboards, worksheets, converted_objects,
                                tooltip_page_map, *, standalone_worksheets=None,
                                page_name_map=None):
        """Create dashboard and standalone pages with navigation bindings."""
        page_names = []
        if page_name_map is None:
            page_name_map = {}
        # Reserve dashboard IDs before appending worksheet pages.
        for db_idx, db in enumerate(dashboards):
            page_name = f"ReportSection{uuid.uuid4().hex[:6]}" if db_idx > 0 else "ReportSection"
            page_names.append(page_name)
            for obj in db.get('objects', []):
                ws_name = obj.get('worksheetName')
                if (obj.get('type') == 'worksheetReference'
                        and isinstance(ws_name, str) and ws_name):
                    page_name_map.setdefault(ws_name, page_name)
            db_name = db.get('name')
            if isinstance(db_name, str) and db_name:
                page_name_map[db_name] = page_name

        if standalone_worksheets:
            standalone_pages = self._create_fallback_page(
                pages_dir, standalone_worksheets, converted_objects,
                existing_page_names=page_names)
            page_names.extend(standalone_pages)
            page_name_map.update(
                (ws['name'], page_name)
                for ws, page_name in zip(standalone_worksheets, standalone_pages)
                if isinstance(ws.get('name'), str) and ws['name'])

        for db_idx, db in enumerate(dashboards):
            page_name = page_names[db_idx]
            page_display_name = db.get('name') or ''
            if not isinstance(page_display_name, str) or not page_display_name.strip():
                page_display_name = f'Page {db_idx + 1}'

            # Create the page folder
            page_dir = os.path.join(pages_dir, page_name)
            os.makedirs(page_dir, exist_ok=True)

            # Get the size
            size = db.get('size', {})
            page_width = size.get('width', 1280)
            page_height = size.get('height', 720)

            # Store for _make_visual_position clamping
            self._current_page_width = page_width
            self._current_page_height = page_height

            page_json = {
                "$schema": SCHEMA_PAGE,
                "name": page_name,
                "displayName": page_display_name,
                "displayOption": "FitToPage",
                "height": page_height,
                "width": page_width
            }

            # Add page-level filters from dashboard filters
            db_filters = list(db.get('filters', []))
            if db_filters:
                page_filters = self._create_visual_filters(db_filters)
                if page_filters:
                    page_json["filterConfig"] = {"filters": page_filters}

            # Responsive breakpoints from Tableau device layouts
            device_layouts = db.get('device_layouts', [])
            phone_layout = next((dl for dl in device_layouts
                                 if dl.get('device_type') == 'phone'), None)
            if phone_layout:
                mobile_visuals = []
                for viz in phone_layout.get('zones', []):
                    vname = viz.get('name', '')
                    vpos = viz.get('position', {})
                    # Scale phone layout to PBI mobile dimensions (320Ã—568)
                    mob_w = 320
                    mob_h = 568
                    dl_max_w = max((z.get('position', {}).get('x', 0)
                                    + z.get('position', {}).get('w', 0)
                                    for z in phone_layout.get('zones', [])),
                                   default=mob_w) or mob_w
                    dl_max_h = max((z.get('position', {}).get('y', 0)
                                    + z.get('position', {}).get('h', 0)
                                    for z in phone_layout.get('zones', [])),
                                   default=mob_h) or mob_h
                    sx = mob_w / max(dl_max_w, 1)
                    sy = mob_h / max(dl_max_h, 1)
                    mobile_visuals.append({
                        "name": vname,
                        "position": {
                            "x": round(vpos.get('x', 0) * sx),
                            "y": round(vpos.get('y', 0) * sy),
                            "width": max(round(vpos.get('w', 200) * sx), 60),
                            "height": max(round(vpos.get('h', 100) * sy), 40),
                        }
                    })
                if mobile_visuals:
                    page_json["mobileState"] = {"visuals": mobile_visuals}

            _write_json(os.path.join(page_dir, 'page.json'), page_json)

            # Create visuals
            visuals_dir = os.path.join(page_dir, 'visuals')
            os.makedirs(visuals_dir, exist_ok=True)

            db_objects = db.get('objects', [])
            visual_count = 0
            worksheet_visuals = []

            # Build a calc_id â†’ caption lookup for slicers
            calcs = converted_objects.get('calculations', [])
            calc_id_to_caption = {}
            for c in calcs:
                cname = c.get('name', '').strip('[]')
                ccaption = c.get('caption', '')
                if cname and ccaption:
                    calc_id_to_caption[cname] = ccaption

            # Build grid layout map from zone hierarchy when available.
            zone_hierarchy = db.get('zone_hierarchy', {})
            layout_map = self._build_zone_layout_map(zone_hierarchy, page_width, page_height)

            # Compute scale factor from Tableau to Power BI pixels (fallback).
            # Tableau dashboard size IS the canvas — zone coords are absolute
            # pixels within that canvas. Since page_width/height already match
            # the dashboard size, the natural scale is 1.0 (pixel-perfect).
            # We only DOWNSCALE when an object overflows past the page bounds;
            # we never UPSCALE, because that would distort positions when
            # objects don't fully fill the canvas.
            max_x = max((o.get('position', {}).get('x', 0) + o.get('position', {}).get('w', 0) for o in db_objects), default=page_width)
            max_y = max((o.get('position', {}).get('y', 0) + o.get('position', {}).get('h', 0) for o in db_objects), default=page_height)
            scale_x = min(1.0, page_width / max(max_x, 1))
            scale_y = min(1.0, page_height / max(max_y, 1))

            for obj in db_objects:
                # Resolve position: grid layout map (preferred) or proportional (fallback)
                obj_type = obj.get('type', '')
                obj_name = obj.get('worksheetName', '') or obj.get('name', '') or obj.get('param_name', '')
                # Filter/parameter controls use their own position (their 'name'
                # refers to the filtered worksheet, not themselves).
                grid_pos = None
                if obj_type not in ('filter_control', 'parameter_control') and layout_map and obj_name:
                    grid_pos = layout_map.get(obj_name)
                if grid_pos:
                    eff_obj = dict(obj, position=grid_pos)
                    eff_sx, eff_sy = 1.0, 1.0
                else:
                    eff_obj = obj
                    eff_sx, eff_sy = scale_x, scale_y

                if obj.get('type') == 'worksheetReference':
                    ws_name = obj.get('worksheetName', '')
                    ws_data = self._find_worksheet(worksheets, ws_name)
                    visual_id = self._create_visual_worksheet(
                        visuals_dir, ws_data, eff_obj, eff_sx, eff_sy,
                        visual_count, worksheets, converted_objects,
                        tooltip_page_map=tooltip_page_map)
                    if visual_id:
                        worksheet_visuals.append((ws_name, visual_id))
                    visual_count += 1

                    # Sprint 121: annotation textbox overlays
                    if ws_data and ws_data.get('annotations'):
                        for ann in ws_data['annotations']:
                            self._create_annotation_overlay(
                                visuals_dir, ann, eff_obj, eff_sx, eff_sy,
                                visual_count)
                            visual_count += 1

                elif obj.get('type') == 'text':
                    self._create_visual_textbox(visuals_dir, eff_obj, eff_sx, eff_sy, visual_count)
                    visual_count += 1

                elif obj.get('type') == 'image':
                    self._create_visual_image(visuals_dir, eff_obj, eff_sx, eff_sy, visual_count)
                    visual_count += 1

                elif obj.get('type') == 'filter_control':
                    self._create_visual_filter_control(visuals_dir, eff_obj, eff_sx, eff_sy,
                                                        visual_count, calc_id_to_caption,
                                                        converted_objects)
                    visual_count += 1

                elif obj.get('type') == 'parameter_control':
                    self._create_visual_parameter_control(visuals_dir, eff_obj, eff_sx, eff_sy,
                                                           visual_count, converted_objects)
                    visual_count += 1

            self._expose_occluded_card_values(visuals_dir, worksheet_visuals)

            # Create action buttons for URL and sheet-navigate actions
            actions = converted_objects.get('actions', [])
            if actions:
                page_worksheets = {o.get('worksheetName') for o in db_objects
                                   if o.get('worksheetName')}
                page_worksheets.add(db.get('name', ''))
                db_actions = [
                    a for a in actions
                    if a.get('type') in ('url', 'sheet-navigate')
                    and _action_belongs_to_page(a, page_display_name, page_worksheets)
                ]
                if db_actions:
                    created = self._create_action_visuals(visuals_dir, db_actions,
                                                           scale_x, scale_y, visual_count,
                                                           page_display_name,
                                                           page_name_map=page_name_map)
                    visual_count += created

                # Sprint 122: Set-value actions → hidden slicer + bookmarks + button
                set_actions = [a for a in actions if a.get('type') == 'set-value'
                               and _action_belongs_to_page(a, page_display_name, page_worksheets)]
                if set_actions:
                    set_created, set_bms = self._generate_set_action_artifacts(
                        visuals_dir, set_actions, visual_count,
                        page_display_name, converted_objects)
                    visual_count += set_created
                    if set_bms:
                        if not hasattr(self, '_set_action_bookmarks'):
                            self._set_action_bookmarks = []
                        self._set_action_bookmarks.extend(set_bms)

                # Sprint 122: Parameter actions → slicer visuals
                param_actions = [a for a in actions if a.get('type') == 'param'
                                 and _action_belongs_to_page(a, page_display_name, page_worksheets)]
                if param_actions:
                    param_created = self._generate_parameter_action_slicers(
                        visuals_dir, param_actions, visual_count, converted_objects)
                    visual_count += param_created

            # Create slicer for Pages shelf (Tableau play-axis animation)
            pages_shelf = db.get('pages_shelf', {})
            if pages_shelf:
                self._create_pages_shelf_slicer(
                    visuals_dir, pages_shelf, scale_x, scale_y,
                    visual_count, converted_objects)
                visual_count += 1
                # Generate motion chart bookmarks for this dashboard
                self._motion_chart_bookmarks.extend(
                    self._create_motion_chart_bookmarks(
                        pages_shelf, page_display_name, db.get('name', '')))

            # Power BI shows page tabs natively -- no explicit navigator needed

            print(f"  ðŸ“Š Page '{page_display_name}': {visual_count} visuals created")

        return page_names

    @staticmethod
    def _get_standalone_worksheets(dashboards, worksheets):
        """Return visible worksheets absent from typed dashboard worksheet zones."""
        referenced_worksheets = set()
        for db in dashboards:
            for obj in db.get('objects', []):
                if obj.get('type') == 'worksheetReference':
                    ws_name = obj.get('worksheetName')
                    if isinstance(ws_name, str) and ws_name:
                        referenced_worksheets.add(ws_name)

            zones = [db.get('zone_hierarchy', {})]
            while zones:
                zone = zones.pop()
                if not isinstance(zone, dict):
                    continue
                if zone.get('zone_type') == 'worksheet':
                    ws_name = zone.get('name')
                    if isinstance(ws_name, str) and ws_name:
                        referenced_worksheets.add(ws_name)
                zones.extend(zone.get('children', []))

        return [
            ws for ws in worksheets
            if ws.get('name') not in referenced_worksheets
            and not any(
                hidden is True
                or (isinstance(hidden, str) and hidden.strip().lower() == 'true')
                for hidden in (ws.get('hidden'), ws.get('is_hidden'))
            )
        ]

    def _create_fallback_page(self, pages_dir, worksheets, converted_objects,
                              *, existing_page_names=None):
        """Create worksheet pages, optionally appending to reserved pages."""
        page_names = []
        used_page_names = set(existing_page_names or [])

        if not worksheets:
            if existing_page_names is not None:
                return page_names
            # No worksheets either â€” create a single empty page
            page_name = "ReportSection"
            page_names.append(page_name)
            page_dir = os.path.join(pages_dir, page_name)
            os.makedirs(page_dir, exist_ok=True)
            page_json = {
                "$schema": SCHEMA_PAGE,
                "name": page_name,
                "displayName": "Tableau Migration",
                "displayOption": "FitToPage",
                "height": 720,
                "width": 1280
            }
            _write_json(os.path.join(page_dir, 'page.json'), page_json)
            os.makedirs(os.path.join(page_dir, 'visuals'), exist_ok=True)
            print("  ðŸ“Š Default page: 0 visuals created")
            return page_names

        for idx, ws in enumerate(worksheets):
            page_name = ("ReportSection" if idx == 0 and existing_page_names is None
                         else f"ReportSection{uuid.uuid4().hex[:6]}")
            if page_name != "ReportSection":
                while (page_name in used_page_names
                       or os.path.exists(os.path.join(pages_dir, page_name))):
                    page_name = f"ReportSection{uuid.uuid4().hex[:6]}"
            used_page_names.add(page_name)
            page_names.append(page_name)

            ws_name = ws.get('name') or ''
            if not isinstance(ws_name, str) or not ws_name.strip():
                ws_name = f'Sheet {idx+1}'
            page_dir = os.path.join(pages_dir, page_name)
            os.makedirs(page_dir, exist_ok=True)

            page_json = {
                "$schema": SCHEMA_PAGE,
                "name": page_name,
                "displayName": ws_name,
                "displayOption": "FitToPage",
                "height": 720,
                "width": 1280
            }
            _write_json(os.path.join(page_dir, 'page.json'), page_json)

            visuals_dir = os.path.join(page_dir, 'visuals')
            os.makedirs(visuals_dir, exist_ok=True)

            visual_id = uuid.uuid4().hex[:8]
            visual_dir = os.path.join(visuals_dir, visual_id)

            visual_type = self._resolve_worksheet_visual_type(ws)

            # Check for custom visual GUID
            mark_type = ws.get('original_mark_class')
            if mark_type and hasattr(self, '_used_custom_guids'):
                from visual_generator import resolve_custom_visual_type
                custom_vtype, guid_info = resolve_custom_visual_type(mark_type)
                if guid_info:
                    visual_type = custom_vtype
                    key = mark_type.lower().replace(' ', '').replace('_', '')
                    self._used_custom_guids[key] = guid_info

                # The AppSource Gantt visual requires Task/Start/Duration buckets.
                # Tableau Gantt exports often contain only a category and measures;
                # emitting that custom visual produces DataViewMapping errors. Use a
                # native chart until a complete Task mapping is available.
                if visual_type == 'ganttChart':
                    visual_type = 'clusteredBarChart'

            if visual_type == 'ganttChart':
                visual_type = 'clusteredBarChart'

            # Validate scatter chart has measures for X/Y
            if visual_type == 'scatterChart':
                skip_names = {'Measure Names', 'Measure Values', 'Multiple Values',
                              ':Measure Names', ':Measure Values'}
                fields = ws.get('fields', [])
                has_measure = any(
                    self._is_measure_field(self._clean_field_name(f.get('name', '')))
                    for f in fields
                    if self._clean_field_name(f.get('name', '')) not in skip_names
                )
                if not has_measure:
                    visual_type = 'tableEx'
                else:
                    # Detect :Measure Names + Multiple Values pattern (strip/dot plot)
                    has_measure_names = any(
                        f.get('name', '') in (':Measure Names', 'Measure Names')
                        for f in fields
                    )
                    has_multiple_values = any(
                        f.get('name', '') == 'Multiple Values'
                        for f in fields
                    )
                    if has_measure_names and has_multiple_values:
                        visual_type = 'clusteredBarChart'

                    # Detect placeholder scatter: all row measures are min(1) dummies
                    if visual_type == 'scatterChart':
                        calc_formulas = {}
                        for c in converted_objects.get('calculations', []):
                            cname = self._clean_field_name(c.get('name', '')).strip('[]')
                            calc_formulas[cname] = (c.get('formula', '') or '').strip().lower()
                        row_meas = [
                            f for f in fields
                            if f.get('shelf') == 'rows'
                            and self._is_measure_field(self._clean_field_name(f.get('name', '')))
                        ]
                        if row_meas and all(
                            calc_formulas.get(self._clean_field_name(f.get('name', '')).strip('[]'), '') == 'min(1)'
                            for f in row_meas
                        ):
                            visual_type = 'multiRowCard'

                    # Downgrade scatter when ALL measures return non-numeric
                    # values (string/boolean/date). PBI would otherwise raise
                    # DataViewMappingError_ScatterXIncorrectAggregate because
                    # X/Y axes require numeric aggregation.
                    if visual_type == 'scatterChart':
                        _bim_meas_types = getattr(self, '_actual_bim_measure_types', {}) or {}
                        _non_numeric = {'string', 'boolean', 'text',
                                        'datetime', 'date', 'binary'}
                        if _bim_meas_types:
                            measure_fields = [
                                f for f in fields
                                if self._clean_field_name(f.get('name', '')) not in skip_names
                                and self._is_measure_field(
                                    self._clean_field_name(f.get('name', '')))
                            ]
                            if measure_fields:
                                def _measure_is_numeric(mf):
                                    raw = self._clean_field_name(mf.get('name', ''))
                                    entity, prop = None, raw
                                    if hasattr(self, '_field_map') and raw in self._field_map:
                                        entity, prop = self._field_map[raw]
                                    # Try direct lookup; if no entity, scan all entries
                                    if entity:
                                        t = _bim_meas_types.get((entity, prop))
                                    else:
                                        t = None
                                        for (te, tp), tt in _bim_meas_types.items():
                                            if tp == prop:
                                                t = tt
                                                break
                                    if t is None:
                                        return True  # unknown → assume numeric
                                    return t.lower() not in _non_numeric
                                if not any(_measure_is_numeric(mf) for mf in measure_fields):
                                    visual_type = 'multiRowCard' if len(measure_fields) > 1 else 'card'

            if ws.get('chart_type') != visual_type:
                ws['chart_type'] = visual_type

            visual_json = {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json",
                "name": visual_id,
                "position": {
                    "x": 10,
                    "y": 10,
                    "z": 0,
                    "height": 700,
                    "width": 1260,
                    "tabOrder": 0
                },
                "visual": {
                    "visualType": visual_type,
                    "drillFilterOtherVisuals": True
                }
            }

            if ws.get('fields'):
                query = self._build_visual_query(ws)
                self._append_visual_query_notes(visual_json, ws)
                if query:
                    visual_json["visual"]["query"] = query
                    self._apply_worksheet_sort(visual_json, ws, query)
                    override_type = ws.pop('_override_visual_type', None)
                    if override_type:
                        visual_json["visual"]["visualType"] = override_type
                        fallback_note = ws.pop('_visual_fallback_note', None)
                        if fallback_note:
                            visual_json.setdefault('annotations', []).append({
                                'name': 'MigrationNote', 'value': fallback_note,
                            })

            visual_json['visual']['visualContainerObjects'] = {
                'title': [{'properties': self._worksheet_title_properties(
                    ws_name, ws, converted_objects)}],
            }
            self._apply_visual_formatting(visual_json, ws_name, ws)

            if ws.get('filters') and "query" in visual_json["visual"]:
                filter_notes = []
                visual_filters = self._create_visual_filters(
                    ws['filters'], notes=filter_notes)
                if visual_filters:
                    visual_json["filterConfig"] = {"filters": visual_filters}
                for note in filter_notes:
                    visual_json.setdefault('annotations', []).append({
                        'name': 'MigrationNote', 'value': note,
                    })

            if visual_json.get('visual', {}).get('visualType') == 'ganttChart':
                visual_json['visual']['visualType'] = 'clusteredBarChart'
            _write_json(os.path.join(visual_dir, 'visual.json'), visual_json, ensure_ascii=False)

        print(f"  ðŸ“Š {len(worksheets)} worksheet pages created (1 visual each)")
        return page_names

    def _create_tooltip_pages(self, pages_dir, page_names, worksheets, converted_objects):
        """Creates tooltip pages from worksheets with viz_in_tooltip data."""
        tooltip_page_map = {}
        tooltip_worksheets = [ws for ws in worksheets if ws.get('tooltip', {}).get('viz_in_tooltip')]
        # Also check tooltips list for viz_in_tooltip entries
        if not tooltip_worksheets:
            tooltip_worksheets = []
            for ws in worksheets:
                tooltips = ws.get('tooltips', [])
                if isinstance(tooltips, list):
                    for tip in tooltips:
                        if isinstance(tip, dict) and tip.get('type') == 'viz_in_tooltip':
                            tooltip_worksheets.append(ws)
                            break

        for tip_ws in tooltip_worksheets:
            tip_name = f"Tooltip_{uuid.uuid4().hex[:8]}"
            tip_display = f"Tooltip - {tip_ws.get('name', 'Tooltip')}"
            page_names.append(tip_name)
            # Track tooltip page for visual binding
            tooltip_page_map[tip_ws.get('name', '')] = tip_name
            # Also map from viz_in_tooltip references
            tooltips = tip_ws.get('tooltips', [])
            if isinstance(tooltips, list):
                for tip in tooltips:
                    if isinstance(tip, dict) and tip.get('type') == 'viz_in_tooltip':
                        tooltip_page_map[tip.get('worksheet', '')] = tip_name

            tip_dir = os.path.join(pages_dir, tip_name)
            os.makedirs(tip_dir, exist_ok=True)

            tip_page = {
                "$schema": SCHEMA_PAGE,
                "name": tip_name,
                "displayName": tip_display,
                "displayOption": "FitToPage",
                "height": 320,
                "width": 480,
                "pageType": "Tooltip"
            }
            _write_json(os.path.join(tip_dir, 'page.json'), tip_page)

            # Create a visual for the tooltip
            tip_visuals_dir = os.path.join(tip_dir, 'visuals')
            os.makedirs(tip_visuals_dir, exist_ok=True)
            self._create_visual_worksheet(
                tip_visuals_dir, tip_ws,
                {'type': 'worksheetReference', 'worksheetName': tip_ws.get('name', ''),
                 'position': {'x': 0, 'y': 0, 'w': 480, 'h': 320}},
                1.0, 1.0, 0, worksheets, converted_objects
            )
            print(f"  ðŸ’¡ Tooltip page '{tip_display}' created")

    def _create_mobile_pages(self, pages_dir, page_names, dashboards, worksheets, converted_objects):
        """Creates mobile layout pages from phone device layouts."""
        for db_idx, db in enumerate(dashboards):
            device_layouts = db.get('device_layouts', [])
            for dl in device_layouts:
                device_type = dl.get('device_type', '')
                if device_type == 'phone' and not dl.get('auto_generated', False):
                    mobile_page_name = f"MobileLayout_{uuid.uuid4().hex[:8]}"
                    mobile_display = f"{db.get('name', 'Dashboard')} (Phone)"
                    page_names.append(mobile_page_name)

                    mobile_dir = os.path.join(pages_dir, mobile_page_name)
                    os.makedirs(mobile_dir, exist_ok=True)

                    mobile_page = {
                        "$schema": SCHEMA_PAGE,
                        "name": mobile_page_name,
                        "displayName": mobile_display,
                        "displayOption": "FitToPage",
                        "height": 568,
                        "width": 320,
                    }
                    _write_json(os.path.join(mobile_dir, 'page.json'), mobile_page)

                    # Create visuals for visible zones
                    mobile_visuals_dir = os.path.join(mobile_dir, 'visuals')
                    os.makedirs(mobile_visuals_dir, exist_ok=True)

                    vis_count = 0
                    for zone in dl.get('zones', []):
                        zone_name = zone.get('name', '')
                        ws_data = self._find_worksheet(worksheets, zone_name)
                        if ws_data:
                            self._create_visual_worksheet(
                                mobile_visuals_dir, ws_data,
                                {'type': 'worksheetReference', 'worksheetName': zone_name,
                                 'position': zone.get('position', {'x': 0, 'y': vis_count * 200, 'w': 320, 'h': 200})},
                                1.0, 1.0, vis_count, worksheets, converted_objects
                            )
                            vis_count += 1

                    print(f"  ðŸ“± Mobile layout page '{mobile_display}': {vis_count} visuals")

    def _build_field_mapping(self, converted_objects):
        """Builds the mapping from Tableau fields to the Power BI model.

        Solves 2 problems:
        1. Visuals reference the Tableau internal ID (e.g. federated.xxx) instead
           of the Power BI table name (e.g. CORN, cities.csv)
        2. Visuals reference Tableau calculation IDs (e.g. Calculation_114...)
           instead of the Power BI measure name (e.g. Filiere, Lat_upgrade)

        Tables are now all real physical tables.
        Measures are on the main table (the one with the most columns).

        Creates self._field_map: {raw_field_name: (table_name, property_name)}
        """
        self._field_map = {}

        datasources = converted_objects.get('datasources', [])

        # Phase 1: Collect deduplicated physical tables
        # Use table_rename_map from the TMDL generator so table names in the
        # report (Entity references) match the semantic model exactly.
        rename_map = getattr(self, '_table_rename_map', {})
        column_renames = getattr(self, '_column_rename_map', {})
        best_tables = {}
        # Build ds_table_map from rename_map entries — these are
        # datasources whose tables got renamed due to cross-datasource
        # collision.  Also includes the primary (un-renamed) datasource.
        self._ds_table_map = {ds_name: new_name
                              for (ds_name, _orig), new_name in rename_map.items()}
        # Build collision_tables: the set of table names involved in
        # cross-datasource name collisions (original + all renamed variants).
        # Used to scope the entity override — only fields resolving to a
        # collision table get overridden, leaving multi-table datasources
        # (like Salesforce) unaffected.
        _collision_originals = {orig for (_ds, orig) in rename_map}
        self._collision_tables = set(_collision_originals)
        for new_name in rename_map.values():
            self._collision_tables.add(new_name)
        for ds in datasources:
            ds_name = ds.get('name', '')
            tables = ds.get('tables', [])
            # Extract physical columns from datasource-level list (excluding calculations)
            ds_cols = [c for c in ds.get('columns', []) if not c.get('calculation')]
            for table in tables:
                tname = table.get('name', '?')
                if not tname or tname == 'Unknown':
                    continue
                # Inherit datasource-level columns into tables that have none
                # (common for Tableau Extracts: single table with columns at DS level)
                if not table.get('columns') and ds_cols and len(tables) == 1:
                    # Clean DS-level columns: strip bracket notation and skip special columns
                    cleaned_cols = []
                    for c in ds_cols:
                        raw = c.get('name', '')
                        if raw.startswith('[:') or not raw:
                            continue  # Skip special Tableau columns (e.g. [:Measure Names])
                        clean = dict(c)
                        clean['name'] = raw.strip('[]')
                        cleaned_cols.append(clean)
                    table['columns'] = cleaned_cols
                # Apply table rename from TMDL generator (multi-datasource collision)
                actual_name = rename_map.get((ds_name, tname), tname)
                if actual_name not in best_tables or len(table.get('columns', [])) > len(best_tables[actual_name].get('columns', [])):
                    best_tables[actual_name] = table

        # Map primary (un-renamed) datasources to their original table.
        # These DSes have tables matching collision names but no rename entry.
        if _collision_originals:
            for ds in datasources:
                ds_name = ds.get('name', '')
                if ds_name and ds_name not in self._ds_table_map:
                    for table in ds.get('tables', []):
                        tname = table.get('name', '')
                        if tname in _collision_originals:
                            self._ds_table_map[ds_name] = tname
                            break

        # Phase 2: Identify the main table (the one with the most columns)
        main_table = None
        max_cols = 0
        for tname, t in best_tables.items():
            ncols = len(t.get('columns', []))
            if ncols > max_cols:
                max_cols = ncols
                main_table = tname
        self._main_table = main_table or 'Table'
        self._datasources_ref = datasources

        # Phase 3: Map columns of each physical table
        #   Also track physical measure columns (role='measure' from Tableau)
        #   When a column name exists in multiple tables, prefer the main table
        #   so visual field references resolve to the primary data table.
        for tname, t in best_tables.items():
            for col in t.get('columns', []):
                cname = col.get('name', '?')
                # The semantic model names a captioned column after what its
                # partition produces, so the report has to ask for that name.
                prop = column_renames.get((tname, cname), cname)
                # Only overwrite if: (a) not yet mapped, or
                # (b) this IS the main table (main table wins ties)
                if cname not in self._field_map or tname == main_table:
                    self._field_map[cname] = (tname, prop)
                # Also index by caption for visual references using display name
                caption = col.get('caption', '')
                if caption:
                    if caption not in self._field_map or tname == main_table:
                        self._field_map[caption] = (tname, prop)
                    stripped_caption = caption.strip()
                    if stripped_caption and (
                            stripped_caption not in self._field_map or tname == main_table):
                        self._field_map[stripped_caption] = (tname, prop)

        # Phase 4: Map Tableau calculations (rawID -> caption/friendly name)
        # Measures are on the main table
        measures_table = main_table or 'Table'
        self._measure_names = set()  # Track which fields are measures (not dimensions) â€” for bucket assignment

        # _bim_measure_names: authoritative set of DAX measures in the BIM
        # model.  If the TMDL generator populated _actual_bim_measure_names
        # (the real model output), we use that; otherwise we fall back to
        # Tableau metadata (role='measure') which is less accurate because
        # Tableau can label a calculated column (e.g. DATEDIFF) as a measure.
        if hasattr(self, '_actual_bim_measure_names') and self._actual_bim_measure_names:
            self._bim_measure_names: set[str] = set(self._actual_bim_measure_names)
        else:
            self._bim_measure_names = set()

        # Phase 4a: Physical columns with role='measure' (from Tableau XML)
        #   These go into _measure_names for visual bucket classification (Y not Category)
        #   but NOT into _bim_measure_names (they're physical columns, not DAX measures)
        for tname, t in best_tables.items():
            for col in t.get('columns', []):
                if col.get('role', '') == 'measure':
                    cname = col.get('name', '?')
                    self._measure_names.add(cname)

        # Phase 4b: Calculated measures â€” index in _field_map and _measure_names
        for ds in datasources:
            for calc in ds.get('calculations', []):
                raw_name = calc.get('name', '').replace('[', '').replace(']', '')
                caption = (calc.get('caption', '') or raw_name).replace('[', '').replace(']', '')
                canonical_caption = caption.strip()
                if raw_name not in self._field_map:
                    self._field_map[raw_name] = (measures_table, canonical_caption)
                # Also index by caption for filters using the readable name
                if caption and caption not in self._field_map:
                    self._field_map[caption] = (measures_table, canonical_caption)
                if canonical_caption and canonical_caption not in self._field_map:
                    self._field_map[canonical_caption] = (measures_table, canonical_caption)
                # Track measure names for Category vs Y assignment
                if calc.get('role', '') == 'measure':
                    self._measure_names.add(raw_name)
                    if caption:
                        self._measure_names.add(caption)

        # Phase 4c: Reconcile measure placements against actual BIM model.
        # Phase 4b puts every calculation on `measures_table` (the main table),
        # but the TMDL generator places each measure on whatever table its
        # source columns live on.  When the two disagree, visual field lookups
        # fail and the visual renders empty.  Use _actual_bim_symbols to fix
        # the field map so resolved (entity, prop) actually exists in the BIM.
        _bim_sym = getattr(self, '_actual_bim_symbols', None) or set()
        if _bim_sym:
            # Build {prop_name: entity} from BIM symbols.  When the same prop
            # exists on multiple tables we prefer the entity that already
            # contains many measures (heuristic: first match wins, but skip
            # Calendar/parameter tables which shouldn't host migrated calcs).
            bim_by_prop = {}
            for entity, prop in _bim_sym:
                if entity in ('Calendar',):
                    continue
                bim_by_prop.setdefault(prop, entity)
                bim_by_prop.setdefault(prop.strip(), entity)
            # Fix _field_map entries whose resolved tuple is missing from BIM.
            for key, (entity, prop) in list(self._field_map.items()):
                if (entity, prop) in _bim_sym:
                    continue
                # Try caption-based lookup
                target = bim_by_prop.get(prop) or bim_by_prop.get(prop.strip())
                canonical_prop = next(
                    (candidate for candidate_entity, candidate in _bim_sym
                     if candidate_entity == target and candidate.strip() == prop.strip()),
                    prop.strip(),
                ) if target else prop.strip()
                if target and (target, canonical_prop) in _bim_sym:
                    self._field_map[key] = (target, canonical_prop)

        # Also gather measure names from top-level calculations
        for calc in converted_objects.get('calculations', []):
            if calc.get('role', '') == 'measure':
                raw_name = calc.get('name', '').replace('[', '').replace(']', '')
                caption = (calc.get('caption', '') or raw_name).replace('[', '').replace(']', '')
                canonical_caption = caption.strip()
                if raw_name and raw_name not in self._field_map:
                    self._field_map[raw_name] = (measures_table, canonical_caption)
                if caption and caption not in self._field_map:
                    self._field_map[caption] = (measures_table, canonical_caption)
                if canonical_caption and canonical_caption not in self._field_map:
                    self._field_map[canonical_caption] = (measures_table, canonical_caption)
                self._measure_names.add(raw_name)
                if caption:
                    self._measure_names.add(caption)
                    self._measure_names.add(canonical_caption)

        # Phase 5: Map extracted groups (BIM-generated calculated columns)
        groups = converted_objects.get('groups', [])
        self._group_field_map = {}
        self._ambiguous_group_fields = set()
        for g in groups:
            group_name = g.get('name', '').replace('[', '').replace(']', '')
            if group_name and g.get('group_type') == 'combined':
                datasource = g.get('datasource', '')
                if _bim_sym:
                    group_symbols = getattr(self, '_actual_bim_group_symbols', None)
                    candidates = {
                        pair for pair in (_bim_sym if group_symbols is None else group_symbols)
                        if pair[1] == group_name
                    }
                    if datasource:
                        source = next((ds for ds in datasources
                                       if ds.get('name') == datasource), None)
                        if source is not None:
                            source_tables = {
                                rename_map.get((datasource, table.get('name')),
                                               table.get('name'))
                                for table in source.get('tables', [])
                            }
                            candidates = {pair for pair in candidates
                                          if pair[0] in source_tables}
                    if len(candidates) != 1:
                        if not candidates:
                            from powerbi_import.tmdl_sets import (
                                _combined_group_reference_status,
                            )
                            if _combined_group_reference_status(
                                    g, converted_objects) is False:
                                logger.warning(
                                    "Skipping PBIR mapping for unresolved "
                                    "unreferenced combined group %r", group_name)
                                continue
                        raise ValueError(
                            f"Combined group {group_name!r}: expected one produced "
                            f"model column for datasource {datasource!r}, "
                            f"found {sorted(candidates)!r}")
                    binding = next(iter(candidates))
                else:
                    binding = (measures_table, group_name)
                raw_name = g.get('raw_name', '').replace('[', '').replace(']', '')
                for alias in {group_name, raw_name} - {''}:
                    if datasource:
                        key = (datasource, alias)
                        previous = self._group_field_map.get(key)
                        if previous and previous != binding:
                            raise ValueError(
                                f"Combined group field {alias!r} is ambiguous "
                                f"in datasource {datasource!r}")
                        self._group_field_map[key] = binding
                    if alias in self._ambiguous_group_fields:
                        continue
                    previous = self._field_map.get(alias)
                    if previous and previous != binding:
                        self._field_map.pop(alias)
                        self._ambiguous_group_fields.add(alias)
                    else:
                        self._field_map[alias] = binding
                continue
            if group_name and group_name not in self._field_map:
                if not _bim_sym or (measures_table, group_name) in _bim_sym:
                    self._field_map[group_name] = (measures_table, group_name)

        # Phase 5b: Map parameters with dedicated tables (list/range domain)
        # These parameters have their own table in the semantic model named
        # after their caption, with a SELECTEDVALUE measure of the same name.
        # Override the Phase 4b mapping (which put them on the main table).
        # Track parameter-table names so visual filters whose target is a
        # parameter measure can be skipped (PBI rejects such filters with
        # "primary projections must have set equality with the corresponding
        # subset in the filter target" because parameter tables have no
        # relationships to fact tables).
        self._parameter_table_names = set()
        self._unavailable_parameter_names = set()
        parameters = converted_objects.get('parameters', [])
        for param in parameters:
            domain = param.get('domain_type', '')
            if domain in ('list', 'range'):
                raw_name = param.get('name', '').replace('[', '').replace(']', '')
                caption = param.get('caption', '') or raw_name
                if caption:
                    # Parameter table name = caption, measure name = caption
                    if not _bim_sym or (caption, caption) in _bim_sym:
                        self._field_map[raw_name] = (caption, caption)
                        self._field_map[caption] = (caption, caption)
                        self._bim_measure_names.add(caption)
                        self._measure_names.add(raw_name)
                        self._measure_names.add(caption)
                        self._parameter_table_names.add(caption)
                    else:
                        self._unavailable_parameter_names.add(caption)

        # Phase 6: Detect synthetic "Number of Records" fields in worksheets.
        # These are generated by the extractor when Tableau uses COUNT(*) on
        # __tableau_internal_object_id__.  Register as a BIM measure.
        worksheets = converted_objects.get('worksheets', [])
        for ws in worksheets:
            for f in ws.get('fields', []):
                if f.get('name') == 'Number of Records':
                    self._field_map['Number of Records'] = (measures_table, 'Number of Records')
                    self._measure_names.add('Number of Records')
                    actual_bim = getattr(self, '_actual_bim_measure_names', set()) or set()
                    if (not actual_bim) or ('Number of Records' in actual_bim):
                        self._bim_measure_names.add('Number of Records')
                    break

        # Save the main table for fallback
        self._main_table = measures_table

    def _is_measure_field(self, field_name):
        """Check if a field is a measure (aggregate) vs a dimension.

        Checks both extraction-time classification (``_measure_names``) and
        TMDL-time classification (``_bim_measure_names``).  The latter is
        needed because the TMDL generator may reclassify a calculation as a
        measure when it transitively depends on aggregation (e.g. a string-
        split field that references SUM()).  Without this, such fields are
        placed in Category/Group dimension roles with a ``Measure`` wrapper,
        causing PBI error ``SecondaryGroupsWithoutPrimary``.
        """
        clean = field_name.replace('[', '').replace(']', '')
        if hasattr(self, '_measure_names') and clean in self._measure_names:
            return True
        if hasattr(self, '_bim_measure_names') and clean in self._bim_measure_names:
            return True
        # Resolve via field_map and check the resolved name
        if hasattr(self, '_field_map') and clean in self._field_map:
            _, prop = self._field_map[clean]
            if hasattr(self, '_measure_names') and prop in self._measure_names:
                return True
            if hasattr(self, '_bim_measure_names') and prop in self._bim_measure_names:
                return True
        return False

    def _clean_field_name(self, name):
        """Strip all known Tableau derivation prefixes from a field name"""
        clean = _RE_DERIVATION_PREFIX.sub('', name)
        clean = _RE_TABLE_CALC_PREFIX.sub('', clean)
        clean = _RE_TYPE_SUFFIX.sub('', clean)
        return clean.strip()

    _RE_PARAM_REF = re.compile(r'<\[Parameters\]\.\[([^\]]+)\]>')

    def _resolve_parameter_title(self, title, converted_objects):
        """Replace <[Parameters].[Name]> references in titles with parameter captions."""
        if not title or '[Parameters]' not in title:
            return title
        params = converted_objects.get('parameters', [])
        param_caption_map = {}
        for p in params:
            pname = p.get('name', '').strip('[]')
            caption = p.get('caption', pname)
            param_caption_map[pname] = caption
        def _repl(m):
            pname = m.group(1)
            return param_caption_map.get(pname, pname)
        return self._RE_PARAM_REF.sub(_repl, title)

    # â”€â”€ PBIR data-role names per visual type â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # (dimension_roles, measure_roles) â€” must match PBI Desktop expectations
    _VISUAL_DATA_ROLES = {
        "card":                              ([], ["Values"]),
        "multiRowCard":                      ([], ["Values"]),
        "kpi":                               ([], ["Indicator", "TrendAxis"]),
        "barChart":                          (["Category", "Series"], ["Y"]),
        "clusteredBarChart":                 (["Category", "Series"], ["Y"]),
        "stackedBarChart":                   (["Category", "Series"], ["Y"]),
        "hundredPercentStackedBarChart":      (["Category", "Series"], ["Y"]),
        "clusteredColumnChart":              (["Category", "Series"], ["Y"]),
        "columnChart":                       (["Category", "Series"], ["Y"]),
        "stackedColumnChart":                (["Category", "Series"], ["Y"]),
        "hundredPercentStackedColumnChart":   (["Category", "Series"], ["Y"]),
        "lineChart":                         (["Category", "Series"], ["Y"]),
        "areaChart":                         (["Category", "Series"], ["Y"]),
        "stackedAreaChart":                  (["Category", "Series"], ["Y"]),
        "hundredPercentStackedAreaChart":     (["Category", "Series"], ["Y"]),
        "pieChart":                          (["Category"], ["Y"]),
        "donutChart":                        (["Category"], ["Y"]),
        "waterfallChart":                    (["Category", "Breakdown"], ["Y"]),
        "funnel":                            (["Category"], ["Y"]),
        "gauge":                             ([], ["Y", "MinValue", "MaxValue", "TargetValue"]),
        "treemap":                           (["Group", "Details"], ["Values"]),
        "sunburst":                          (["Group"], ["Values"]),
        "scatterChart":                      (["Category", "Series"], ["X", "Y", "Size"]),
        "tableEx":                           (["Values"], ["Values"]),
        "table":                             (["Values"], ["Values"]),
        "matrix":                            (["Rows", "Columns"], ["Values"]),
        "pivotTable":                        (["Rows", "Columns"], ["Values"]),
        "slicer":                            (["Values"], []),
        "lineStackedColumnComboChart":       (["Category", "Series"], ["Y", "Y2"]),
        "lineClusteredColumnComboChart":     (["Category", "Series"], ["Y", "Y2"]),
        "map":                               (["Category", "Series"], ["Size"]),
        "azureMap":                          (["Category", "Series"], ["Y", "X", "Size"]),
        "filledMap":                         (["Category", "Series"], ["Size"]),
        "shapeMap":                          (["Location"], ["Color"]),
        "ribbonChart":                       (["Category", "Series"], ["Y"]),
        "boxAndWhisker":                     (["Category", "Sampling"], ["Value"]),
        "decompositionTree":                 (["TreeItems"], ["Values"]),
        "wordCloud":                         (["Category"], ["Values"]),
    }

    # Date-related words for treemap Group ordering (prefer non-date dims)
    _DATE_WORDS = frozenset({
        'date', 'time', 'datetime', 'timestamp',
        'year', 'month', 'quarter', 'week', 'day',
        'heure', 'annÃ©e', 'mois', 'trimestre', 'semaine', 'jour',
    })

    def _is_date_field(self, name):
        """Heuristic: does *name* look like a date / time column?"""
        lower = name.lower()
        return any(w in lower for w in self._DATE_WORDS)

    def _classify_shelf_fields(self, cleaned_fields):
        """Classify cleaned fields by shelf into role buckets.

        Returns a dict with keys: rows_dims, rows_meas, cols_dims, cols_meas,
        color_dims, color_meas, size_fields, tooltip_fields, text_fields,
        detail_dims, expanded_meas, other_dims, other_meas.
        """
        result = {
            'rows_dims': [], 'rows_meas': [],
            'cols_dims': [], 'cols_meas': [],
            'color_dims': [], 'color_meas': [],
            'size_fields': [], 'tooltip_fields': [], 'text_fields': [],
            'detail_dims': [],
            'expanded_meas': [], 'other_dims': [], 'other_meas': [],
        }
        def _as_dim(f):
            """Return a copy of `f` stripped of any shelf aggregation.

            Tableau encodes pills like ``sum:Ps Id`` even on the Color shelf,
            but PBI dimension wells (Series/Legend, Category, Group, Rows,
            Columns, Location, Breakdown) reject aggregations and would
            render the field as ``Sum of Ps Id``. When we route a field into
            a dimension bucket we drop the aggregation so the projection
            emits a plain ``Column`` wrapper.
            """
            field_name = self._clean_field_name(f['name'].strip('[]'))
            is_known_measure = self._is_measure_field(field_name)
            if 'aggregation' not in f and not is_known_measure:
                return f
            clean = dict(f)
            clean.pop('aggregation', None)
            if is_known_measure:
                clean['_force_column'] = True
            return clean

        for f in cleaned_fields:
            shelf = f.get('shelf', '')
            resolved_pair = f.get('_resolved_pair') or getattr(self, '_field_map', {}).get(f['name'])
            known_column_type = (getattr(self, '_actual_bim_column_types', {}) or {}).get(resolved_pair)
            discrete_date_column = (
                f.get('is_discrete') is True
                and str(known_column_type or '').lower() in ('date', 'datetime')
                and not f.get('aggregation')
                and resolved_pair
                and not self._is_bim_measure_symbol(
                    resolved_pair[0], resolved_pair[1], f['name'])
            )
            is_mea = (shelf == 'measure_value'
                      or (shelf == 'wedge-size'
                          and f.get('aggregation') in _TABLEAU_AGG_TO_PBI_FUNC)
                      or (f.get('aggregation') in ('ctd', 'countd')
                          and (known_column_type is not None
                               or resolved_pair in getattr(self, '_countd_measure_bindings', {})))
                      or (shelf in ('rows', 'columns')
                          and known_column_type is not None
                          and (known_column_type in ('int64', 'double', 'decimal')
                               or f.get('aggregation') in ('cnt', 'count', 'ctd', 'countd'))
                          and f.get('aggregation') in _TABLEAU_AGG_TO_PBI_FUNC
                          and not self._is_date_field(f['name']))
                      or self._is_measure_field(f['name']))
            if (f.get('_aggregation_disabled')
                    and not f.get('_preserve_unaggregated_axis')):
                is_mea = False
            if discrete_date_column:
                is_mea = False
            if (known_column_type is not None
                    and str(known_column_type).lower() not in _NUMERIC_MODEL_TYPES
                    and shelf in ('rows', 'columns', 'color', 'detail')
                    and f.get('aggregation') not in
                    ('cnt', 'count', 'ctd', 'countd', 'countnonnull', 'distinctcount')):
                is_mea = False
            # Tableau datasource roles are defaults; the worksheet's pill
            # role wins when an unaggregated field is explicitly discrete.
            # Missing discrete/continuous metadata is not evidence that a
            # known measure is a dimension, whether it is a model measure or
            # a physical column. Explicit "none:" pills lose their prefix
            # during field-name cleaning, so retain that evidence separately.
            explicitly_discrete = (
                f.get('is_discrete') is True
                or (f.get('is_discrete') is None
                    and f.get('is_continuous') is False)
                or f.get('_explicit_no_aggregation')
                or re.match(r'^none:', f['name'].strip('[]'), re.IGNORECASE)
                or f.get('role') == 'dimension'
            )
            if (shelf in ('rows', 'columns', 'color', 'detail')
                    and not f.get('aggregation')
                    and explicitly_discrete):
                is_mea = False

            if shelf == 'measure_value':
                result['expanded_meas'].append(f)
            elif shelf == 'tooltip':
                result['tooltip_fields'].append(f)
            elif shelf == 'size':
                result['size_fields'].append(f)
            elif shelf == 'text':
                result['text_fields'].append(f)
            elif shelf == 'detail':
                if not is_mea:
                    result['detail_dims'].append(_as_dim(f))
            elif shelf == 'color':
                if is_mea:
                    result['color_meas'].append(f)
                else:
                    result['color_dims'].append(_as_dim(f))
            elif shelf == 'rows':
                if is_mea:
                    result['rows_meas'].append(f)
                else:
                    result['rows_dims'].append(_as_dim(f))
            elif shelf == 'columns':
                if is_mea:
                    result['cols_meas'].append(f)
                else:
                    result['cols_dims'].append(_as_dim(f))
            else:
                if is_mea:
                    result['other_meas'].append(f)
                else:
                    result['other_dims'].append(_as_dim(f))
        return result

    def _resolve_worksheet_visual_type(self, ws_data, classified=None):
        """Resolve unambiguous Automatic marks using their shelf metadata."""
        from visual_generator import resolve_visual_type

        raw_type = ws_data.get('chart_type')
        visual_type = resolve_visual_type(raw_type) if raw_type else 'clusteredBarChart'
        if (self._has_explicit_dual_axis(ws_data)
                or (ws_data.get('original_mark_class') or '').lower() != 'automatic'):
            return visual_type
        if classified is None:
            fields = []
            for field in ws_data.get('fields', []):
                raw_name = field.get('name', '')
                clean = self._clean_field_name(raw_name)
                resolved = self._resolve_field_entity(
                    clean, datasource=field.get('datasource', ''),
                    source_table=field.get('source_table'),
                    source_column=field.get('source_column'))
                fields.append({
                    **field, 'name': clean, '_resolved_pair': resolved,
                    **({'_explicit_no_aggregation': True}
                       if re.match(r'^none:', raw_name.strip('[]'), re.IGNORECASE)
                       else {}),
                })
            classified = self._classify_shelf_fields(fields)

        rd, rm = classified['rows_dims'], classified['rows_meas']
        cd, cm = classified['cols_dims'], classified['cols_meas']
        shelf_dims = rd + cd
        if (len(shelf_dims) >= 2
                and classified['color_meas']
                and all(not field.get('aggregation')
                        and field.get('is_continuous') is not True
                        and field.get('is_discrete') is not False
                        for field in shelf_dims)):
            # Nested discrete headers plus a measure on Color are a crosstab.
            return 'matrix'

        if (len(rm) == len(cm) == 1 and not rd and not cd
                and not classified['expanded_meas']
                and visual_type in ('scatterChart', 'tableEx',
                                    'clusteredBarChart', 'clusteredColumnChart',
                                    'lineClusteredColumnComboChart',
                                    'lineStackedColumnComboChart')):
            return 'scatterChart'

        horizontal = len(rd) == len(cm) == 1 and not rm and not cd
        vertical = len(cd) == len(rm) == 1 and not cm and not rd
        if vertical and not classified['expanded_meas']:
            date_field = cd[0]
            datatype = (getattr(self, '_actual_bim_column_types', {}) or {}).get(
                date_field.get('_resolved_pair'))
            date_part = str(date_field.get('date_part') or '').lower()
            is_date = (str(datatype or '').lower() in ('date', 'datetime')
                       or date_part in ('yr', 'qr', 'mn', 'dy', 'wk', 'hr', 'mt',
                                        'sc', 'tyr', 'tqr', 'tmn', 'tdy', 'twk',
                                        'md', 'mdy', 'hms', 'trunc'))
            if (is_date and date_field.get('is_continuous') is True
                    and visual_type in ('clusteredBarChart', 'clusteredColumnChart',
                                        'barChart', 'columnChart')):
                return 'lineChart'

        stack_off = any(
            isinstance(fmt, dict) and str(fmt.get('stack-mode', '')).lower() == 'off'
            for fmt in (ws_data.get('formatting') or {}).values()
        )
        if (visual_type in ('clusteredBarChart', 'clusteredColumnChart')
                and classified['color_dims'] and not classified['expanded_meas']
                and not stack_off):
            if horizontal:
                return 'barChart'
            if vertical:
                return 'columnChart'
        return visual_type

    def _build_visual_query(self, ws_data):
        query = self._build_visual_query_impl(ws_data)
        if not query:
            return query
        for role in query.get('queryState', {}).values():
            unique = []
            for projection in role.get('projections', []):
                if projection and not any(
                        item['field'] == projection['field'] for item in unique):
                    unique.append(projection)
            _disambiguate_aggregation_query_refs(unique)
            used_refs = set()
            for projection in unique:
                original_ref = projection['queryRef']
                query_ref = original_ref
                suffix = 2
                while query_ref in used_refs:
                    query_ref = f'{original_ref}__{suffix}'
                    suffix += 1
                projection['queryRef'] = query_ref
                used_refs.add(query_ref)
            role['projections'] = unique
        return query

    def _build_visual_query_impl(self, ws_data):
        """Builds a query with queryState for a visual (PBIR v4.0 format).

        Uses shelf-aware field classification to assign Tableau fields to
        the correct PBIR data roles for each visual type so Power BI Desktop
        binds fields to the right data wells.

        Shelf mapping logic:
        - rows/columns dims  â†’ primary axis (Category/Group/Location/Rows)
        - rows/columns meas  â†’ value axis (Y/Values/Size)
        - color dims         â†’ Series / Legend (secondary grouping)
        - color meas         â†’ Tooltips (data for conditional formatting)
        - tooltip fields     â†’ Tooltips
        - size fields        â†’ Size
        - measure_value      â†’ expanded measures (from :Measure Names)
        """
        fields = ws_data.get('fields', [])
        if not fields:
            return None

        # â”€â”€ Clean & de-duplicate field names â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        skip_names = {'Measure Names', 'Measure Values', 'Multiple Values',
                      ':Measure Names', ':Measure Values',
                      'Longitude (generated)', 'Latitude (generated)'}
        cleaned_fields = []
        seen_fields = set()
        # Pre-compute set of valid model symbols for orphan field filtering
        _bim_sym = getattr(self, '_actual_bim_symbols', None) or set()
        _bim_props = {prop for (_, prop) in _bim_sym} if _bim_sym else set()
        for f in fields:
            raw_name = f.get('name', '')
            clean = self._clean_field_name(raw_name)
            ds = f.get('datasource', '')
            source_table = f.get('source_table')
            source = next((item for item in getattr(self, '_datasources_ref', [])
                           if item.get('name') == ds), None)
            if (source_table and source and source.get('tables')
                    and source_table.strip('[]') not in {
                        table.get('name', '').strip('[]')
                        for table in source['tables']}):
                unresolved = ws_data.setdefault('_unresolved_source_fields', [])
                if raw_name not in unresolved:
                    unresolved.append(raw_name)
                logger.warning(
                    "Field %r names a table outside datasource %r: %r",
                    raw_name, ds, source_table,
                )
                continue
            quick_type = f.get('table_calc', '')
            quick_agg = str(f.get('table_calc_agg', 'sum')).lower()
            unsupported_reason = getattr(self, '_unsupported_quick_table_calcs', {}).get(
                (f.get('datasource', ''), raw_name, quick_type, quick_agg))
            if unsupported_reason:
                unresolved = ws_data.setdefault('_quick_calc_unresolved', [])
                description = f'{quick_type} {raw_name} ({unsupported_reason})'
                if description not in unresolved:
                    unresolved.append(description)
                logger.warning("Quick table calculation omitted: %s", description)
                continue
            quick_countd = (
                quick_type and quick_agg in ('ctd', 'countd')
            )
            quick_binding = None
            if quick_countd:
                datasource = f.get('datasource', '')
                sources = getattr(self, '_datasources_ref', [])
                if not datasource or not sources or any(
                        source.get('name') == datasource for source in sources):
                    source_pair = self._resolve_field_entity(
                        clean, datasource=datasource, source_table=f.get('source_table'),
                        source_column=f.get('source_column'))
                    quick_binding = getattr(self, '_quick_table_calc_bindings', {}).get(
                        (*source_pair, f['table_calc'], 'countd'))
                if quick_binding is None:
                    unresolved = ws_data.setdefault('_quick_countd_unresolved', [])
                    if raw_name not in unresolved:
                        unresolved.append(raw_name)
                    logger.warning(
                        "Quick COUNTD %r in datasource %r has no verified model binding",
                        raw_name, datasource,
                    )
                    continue
                clean = quick_binding[1]
                note = ('Quick COUNTD excludes nulls but its table calculation '
                        'scope may differ from Tableau mark partitioning.')
                previous = ws_data.get('_visual_mapping_note')
                if not previous or note not in previous:
                    ws_data['_visual_mapping_note'] = f'{previous} {note}' if previous else note
            elif f.get('table_calc') == 'pcto':
                clean = f'% of Total {clean}'
            if clean in skip_names or raw_name in skip_names:
                continue
            # Tableau action targets are interaction metadata, not model
            # columns. Binding them creates orphan visual projections.
            if re.match(r'^Action\s*\(.+\)$', clean, re.IGNORECASE):
                continue
            if (clean in getattr(self, '_unavailable_parameter_names', set()) or
                    re.search(r'\(group\)$|\(bin\)$', clean, re.IGNORECASE) or
                    (clean.endswith('Set') and ':' in clean) or
                    clean in {'South Map', 'Rebounds'}):
                continue
            # Skip Tableau internal fields (e.g. __tableau_internal_object_id__)
            if clean.startswith('__tableau_internal') or raw_name.startswith('__tableau_internal'):
                continue
            field_identity = (
                clean, f.get('shelf'), f.get('aggregation'), f.get('datasource'))
            if field_identity in seen_fields:
                continue
            # Skip fields that don't exist in the semantic model — validate
            # by (entity, property) pair for precision. Use _resolve_field_entity
            # as the ultimate check since that's what determines the final
            # Entity/Property emitted in the visual JSON.
            resolved_pair = quick_binding
            if _bim_sym and quick_binding and quick_binding not in _bim_sym:
                unresolved = ws_data.setdefault('_quick_countd_unresolved', [])
                if raw_name not in unresolved:
                    unresolved.append(raw_name)
                logger.warning("Quick COUNTD binding %r is missing from the model", quick_binding)
                continue
            if _bim_sym and not quick_binding:
                ds = f.get('datasource', '')
                source_table = f.get('source_table')
                resolved_entity, resolved_prop = self._resolve_field_entity(
                    clean, datasource=ds, source_table=source_table,
                    source_column=f.get('source_column'))
                if ((resolved_entity, resolved_prop) not in _bim_sym
                        and (resolved_entity, resolved_prop.strip()) not in _bim_sym):
                    if ds and any(source.get('name') == ds
                                  for source in getattr(self, '_datasources_ref', [])):
                        unresolved = ws_data.setdefault('_unresolved_source_fields', [])
                        if raw_name not in unresolved:
                            unresolved.append(raw_name)
                        continue
                    owners = {(entity, prop) for entity, prop in _bim_sym
                              if prop == resolved_prop or prop == resolved_prop.strip()}
                    if len(owners) != 1:
                        continue
                    resolved_pair = next(iter(owners))
                else:
                    resolved_pair = (resolved_entity, resolved_prop)
            seen_fields.add(field_identity)
            categories = getattr(self, '_actual_bim_column_categories', None) or {}
            pair = resolved_pair
            if pair is None and categories:
                pair = self._resolve_field_entity(
                    clean, datasource=f.get('datasource', ''),
                    source_table=f.get('source_table'), source_column=f.get('source_column'))
            category = categories.get(pair) if pair else None
            field_entry = {
                **f, 'name': clean,
                **({'_resolved_pair': resolved_pair} if resolved_pair else {}),
                **({'_data_category': category} if category else {}),
                **({'_explicit_no_aggregation': True}
                   if re.match(r'^none:', raw_name.strip('[]'), re.IGNORECASE)
                   else {}),
            }
            if (f.get('aggregation') in ('ctd', 'countd') and resolved_pair
                    and not self._is_bim_measure_symbol(
                        resolved_pair[0], resolved_pair[1], clean, prefer_column=True)
                    and resolved_pair not in getattr(self, '_countd_measure_bindings', {})):
                unresolved = ws_data.setdefault('_countd_unresolved', [])
                if clean not in unresolved:
                    unresolved.append(clean)
            date_part = str(f.get('date_part') or '').lower()
            is_discrete_month = (
                date_part in ('month', 'mn')
                and f.get('is_continuous') is not True
                and (f.get('is_discrete') is True
                     or (f.get('is_discrete') is None
                         and f.get('is_continuous') is False))
            )
            if is_discrete_month and resolved_pair:
                binding = getattr(self, '_related_month_bindings', {}).get(
                    resolved_pair)
                if binding:
                    ws_data['_discrete_month_binding'] = binding
                    field_entry['_resolved_pair'] = (binding[0], binding[1])
            is_discrete_year = (
                date_part in ('year', 'yr')
                and f.get('is_continuous') is not True
                and (f.get('is_discrete') is True
                     or (f.get('is_discrete') is None
                         and f.get('is_continuous') is False))
            )
            if is_discrete_year and resolved_pair:
                binding = getattr(self, '_related_year_bindings', {}).get(
                    resolved_pair)
                if binding:
                    ws_data['_discrete_year_binding'] = binding
                    field_entry['_resolved_pair'] = binding
            is_continuous_month = (
                date_part == 'tmn'
                and f.get('is_discrete') is not True
                and (f.get('is_continuous') is True
                     or f.get('is_discrete') is False)
            )
            if is_continuous_month and resolved_pair:
                binding = getattr(
                    self, '_related_continuous_month_bindings', {}).get(
                        resolved_pair)
                if binding:
                    ws_data['_continuous_month_binding'] = binding
                    field_entry['_resolved_pair'] = (binding[0], binding[1])
                else:
                    ws_data['_visual_mapping_note'] = (
                        f"Continuous Tableau month for {clean}: raw date retained "
                        "because there is no unique validated active calendar relationship.")
            cleaned_fields.append(field_entry)

        if not cleaned_fields:
            return None

        aggregation = ws_data.get('aggregation') or {}
        aggregation_disabled = (
            isinstance(aggregation, dict)
            and aggregation.get('status') == 'explicit'
            and aggregation.get('enabled') is False
        )
        if aggregation_disabled:
            original_type = self._resolve_worksheet_visual_type(ws_data)
            for field in cleaned_fields:
                field['_aggregation_disabled'] = True
                field['_force_column'] = True
                field['_preserve_unaggregated_axis'] = (
                    original_type not in ('table', 'tableEx')
                    and field.get('shelf') in ('rows', 'columns'))
            note = (
                'Tableau aggregation is explicitly disabled; source fields are '
                'bound as per-row columns.'
            )
            if original_type == 'scatterChart':
                note += ' Scatter X/Y retain unaggregated columns; verify point-count limits.'
            elif original_type != 'tableEx':
                note += (
                    ' Numeric axis columns remain unaggregated in chart value '
                    'roles; verify mark grain and row-count behavior.'
                )
            previous = ws_data.get('_visual_mapping_note')
            ws_data['_visual_mapping_note'] = f'{previous} {note}' if previous else note

        adhoc_clustering = any(
            item.get('adhoc') for item in (ws_data.get('clustering') or [])
            if isinstance(item, dict)
        )
        if adhoc_clustering:
            original_type = self._resolve_worksheet_visual_type(ws_data)
            for field in cleaned_fields:
                if field.get('shelf') == 'detail':
                    field['_force_column'] = True
            note = (
                'Unsupported Tableau adhoc clustering: cluster labels and '
                'assignments were not calculated. Existing supported fields, '
                'including Detail-shelf fields, are shown in a table; this is '
                'an approximation, not a faithful clustering result.'
            )
            previous = ws_data.get('_visual_mapping_note')
            ws_data['_visual_mapping_note'] = f'{previous} {note}' if previous else note
            if original_type != 'tableEx':
                ws_data['_override_visual_type'] = 'tableEx'
                ws_data['_visual_fallback_note'] = note

        # A discrete date-part needs an actual date-part column (or a validated
        # model calendar binding). A raw timestamp is not an equivalent
        # projection. Only the relationship-validated month label is proved.
        unsupported_date_parts = []
        for field in list(cleaned_fields):
            part = str(field.get('date_part') or '').lower()
            part = {'year': 'yr', 'month': 'mn', 'day': 'dy',
                    'quarter': 'qr', 'week': 'wk'}.get(part, part)
            discrete = (field.get('is_discrete') is True
                        or (field.get('is_discrete') is None
                            and field.get('is_continuous') is False))
            if not (discrete and part in (
                    'yr', 'tyr', 'mn', 'tmn', 'dy', 'tdy', 'qr', 'tqr',
                    'wk', 'twk', 'hr', 'mt', 'sc', 'md', 'mdy', 'hms',
                    'trunc')):
                continue
            month_binding = ws_data.get('_discrete_month_binding')
            if (part == 'mn' and month_binding
                    and field.get('_resolved_pair') == month_binding[:2]):
                continue
            year_binding = ws_data.get('_discrete_year_binding')
            if (part == 'yr' and year_binding
                    and field.get('_resolved_pair') == year_binding):
                continue
            unsupported_date_parts.append(
                f"{field['name']} ({field.get('date_part') or part})")
            cleaned_fields.remove(field)
        if unsupported_date_parts:
            note = (
                'Discrete Tableau date-part projection was not emitted for '
                + ', '.join(unsupported_date_parts)
                + ': no verified model date-part column binding is available, '
                  'and the PBIR date-hierarchy projection schema has not been '
                  'validated. Raw timestamps are intentionally not substituted.'
            )
            previous = ws_data.get('_visual_mapping_note')
            ws_data['_visual_mapping_note'] = f'{previous} {note}' if previous else note
        if not cleaned_fields:
            return None

        # â”€â”€ Shelf-aware field classification â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        classified = self._classify_shelf_fields(cleaned_fields)
        rows_dims = classified['rows_dims']
        rows_meas = classified['rows_meas']
        cols_dims = classified['cols_dims']
        cols_meas = classified['cols_meas']
        color_dims = classified['color_dims']
        color_meas = classified['color_meas']
        size_fields = classified['size_fields']
        tooltip_fields = classified['tooltip_fields']
        text_fields = classified['text_fields']
        detail_dims = classified['detail_dims']
        expanded_meas = classified['expanded_meas']
        other_dims = classified['other_dims']
        other_meas = classified['other_meas']

        # Combined views (order matters for role assignment)
        # Default ordering: columns dims first (typically x-axis), then rows
        axis_dims = cols_dims + rows_dims + other_dims
        # For treemap: rows dims first (primary hierarchy grouping)
        hier_dims = rows_dims + cols_dims + other_dims
        # All axis measures (rows + columns + expanded from :Measure Names)
        axis_meas = rows_meas + cols_meas + expanded_meas + other_meas

        # Promote text-shelf measures to axis_meas when there are no axis
        # measures yet (e.g. pie/donut charts where the value sits on text
        # shelf in Tableau).
        text_meas = [f for f in text_fields if (
            f.get('aggregation') in ('ctd', 'countd')
            or self._is_measure_field(f['name']))]
        text_dims = [f for f in text_fields if f not in text_meas]
        text_measures_as_tooltips = []
        if not axis_meas and text_meas:
            axis_meas = text_meas
            text_fields = text_dims
        elif axis_meas and text_meas:
            text_fields = text_dims
            text_measures_as_tooltips = text_meas
            ws_data['_disable_data_labels'] = True
            text_note = (
                'Separate Tableau Text measure labels are emitted in Tooltips, '
                'with Power BI data labels disabled.')
            previous_note = ws_data.get('_visual_mapping_note')
            ws_data['_visual_mapping_note'] = (
                f'{previous_note} {text_note}' if previous_note else text_note)

        # Tooltip-grade fields: explicit tooltips + color measures + remaining text dims
        tip_fields = (tooltip_fields + color_meas + text_fields
                      + text_measures_as_tooltips)
        # Tableau Detail is metadata for mark grain, not a chart axis/series.
        # Keep it available for future interaction contracts without adding
        # every detail field to the visible Power BI data wells.
        _ = detail_dims
        # Legacy combined lists (for fallback logic)
        all_dims = axis_dims + color_dims
        all_meas = axis_meas + size_fields
        tooltip_dims = [f for f in tip_fields
                if not self._is_measure_field(f['name'])]
        tooltip_meas = [f for f in tip_fields
                if self._is_measure_field(f['name'])]
        fallback_dims = axis_dims + color_dims + detail_dims + tooltip_dims
        fallback_meas = axis_meas + size_fields + color_meas + tooltip_meas

        visual_type = (
            ws_data.get('_override_visual_type')
            or self._resolve_worksheet_visual_type(ws_data, classified)
        )
        query_state = {}
        if visual_type in ('pieChart', 'donutChart') and len(axis_meas) > 1:
            label_calcs = [f for f in axis_meas
                           if f.get('table_calc') and f.get('shelf') in ('text', 'label')]
            if label_calcs and any(f not in label_calcs for f in axis_meas):
                axis_meas = [f for f in axis_meas if f not in label_calcs]
                tip_fields = tip_fields + label_calcs

        # â”€â”€ Per-visual-type role assignment â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

        if visual_type == 'scatterChart':
            # Scatter: all dims → Details (point grouping)
            #          cols measures â†’ X, rows measures â†’ Y, 3rd â†’ Size
            scatter_dims = axis_dims + detail_dims
            if not scatter_dims and tooltip_dims:
                scatter_dims = tooltip_dims[:1]
                tip_fields = [field for field in tip_fields
                              if field is not scatter_dims[0]]
            if detail_dims:
                # Detail explicitly defines mark grain; keep all of it even
                # when a colour field is also present.
                query_state['Category'] = {
                    'projections': [self._make_projection_entry(d, force_column=True)
                                    for d in scatter_dims]
                }
                if color_dims:
                    query_state['Series'] = {
                        'projections': [self._make_projection_entry(d, force_column=True)
                                        for d in color_dims]
                    }
            else:
                # With no explicit Detail shelf, retain the established
                # Category/Series ordering and expose extra dimensions as
                # scalar tooltips rather than dropping them.
                if scatter_dims:
                    query_state['Category'] = self._make_projection(scatter_dims[0])
                series_dims = scatter_dims[1:] + color_dims
                if series_dims:
                    query_state['Series'] = self._make_projection(series_dims[0])
                if len(series_dims) > 1:
                    tip_fields.extend(series_dims[1:])
                    note = ('Additional scatter dimensions are shown as scalar '
                            'tooltips; mark grouping or colour may differ.')
                    previous = ws_data.get('_visual_mapping_note')
                    ws_data['_visual_mapping_note'] = f'{previous} {note}' if previous else note
            # In Tableau: columns = X-axis, rows = Y-axis.
            # Use cols_meas first for X, rows_meas for Y.
            axis_candidates = cols_meas + rows_meas + expanded_meas + other_meas
            scatter_meas = list(axis_candidates)
            scatter_tooltip_meas = [
                candidate for candidate in tooltip_meas
                if candidate not in color_meas
                and candidate not in text_measures_as_tooltips
            ]
            for candidate in scatter_tooltip_meas:
                identity = (candidate['name'], candidate.get('aggregation'),
                            candidate.get('datasource'))
                if not any((item['name'], item.get('aggregation'), item.get('datasource'))
                           == identity for item in scatter_meas):
                    scatter_meas.append(candidate)
            if (str(ws_data.get('original_mark_class') or '').lower() in ('circle', 'shape')
                    and not (cols_meas and rows_meas)
                    and not expanded_meas and not other_meas):
                projections = [self._make_projection_entry(f)
                               for f in axis_dims + detail_dims + color_dims
                               + axis_candidates + tip_fields + size_fields]
                query_state = {'Values': {'projections': []}}
                for projection in projections:
                    if projection and not any(
                            item['field'] == projection['field']
                            for item in query_state['Values']['projections']):
                        query_state['Values']['projections'].append(projection)
                ws_data['_override_visual_type'] = 'tableEx'
                ws_data['_visual_fallback_note'] = (
                    'Single-axis Tableau Circle/Shape plot has no second scatter axis; '
                    'source values and mark detail were preserved as a table.')
                return {'queryState': query_state}
            # Only infer bubble size for legacy, entirely unshelved axis
            # measures. Explicit Tableau shelves and tooltips do not imply
            # a Size encoding.
            size_f = size_fields[:1]
            if (not size_f and len(axis_candidates) >= 3
                    and all(not f.get('shelf') for f in axis_candidates)):
                size_f = axis_candidates[2:3]
                note = ('Scatter Size was inferred from an unshelved measure; '
                        'verify the original size encoding.')
                previous = ws_data.get('_visual_mapping_note')
                ws_data['_visual_mapping_note'] = f'{previous} {note}' if previous else note
            if len(scatter_meas) >= 2:
                if len(axis_candidates) < 2:
                    note = ('A scatter axis was inferred from a tooltip measure; '
                            'verify the original axis mapping.')
                    previous = ws_data.get('_visual_mapping_note')
                    ws_data['_visual_mapping_note'] = f'{previous} {note}' if previous else note
                query_state["X"] = self._make_scatter_axis_projection(scatter_meas[0])
                query_state["Y"] = self._make_scatter_axis_projection(scatter_meas[1])
                # Scatter has only X and Y axis wells. Keep remaining
                # measures on Tooltips unless one fills the Size role.
                tip_fields.extend(f for f in scatter_meas[2:] if f not in size_f)
            else:
                query_state.clear()
                fallback_projections = []
                for field in scatter_dims + detail_dims + scatter_meas + tip_fields:
                    projection = self._make_projection_entry(field)
                    if projection is not None and not any(
                            item['field'] == projection['field'] for item in fallback_projections):
                        fallback_projections.append(projection)
                query_state['Values'] = {
                    'projections': fallback_projections,
                }
                ws_data['_override_visual_type'] = 'tableEx'
                ws_data['_visual_fallback_note'] = (
                    'Scatter has no second resolved axis; source fields were preserved as a table.')
                return {'queryState': query_state}
            if size_f:
                query_state["Size"] = self._make_scatter_axis_projection(size_f[0])
            if tip_fields:
                query_state["Tooltips"] = {
                    "projections": [self._make_projection_entry(f)
                                    for f in tip_fields]
                }
            projections = [p for role in query_state.values()
                           for p in role.get('projections', []) if p is not None]
            _disambiguate_aggregation_query_refs(projections)

        elif visual_type in ('tableEx', 'table'):
            # Table: all fields (dims + measures) â†’ Values
            table_fields = (
                cleaned_fields if aggregation_disabled or adhoc_clustering
                else (all_dims + all_meas or cleaned_fields)
            )
            if table_fields:
                query_state["Values"] = {
                    "projections": [self._make_projection_entry(f)
                                    for f in table_fields]
                }

        elif visual_type == 'matrix':
            # Matrix: first dim â†’ Rows, second dim or color dim â†’ Columns,
            #         measures â†’ Values
            if not rows_dims and not cols_dims and not color_dims and len(other_dims) >= 2:
                # Older inputs can omit all shelf information. Preserve their
                # established first-row/remaining-columns interpretation.
                matrix_rows = other_dims[:1]
                matrix_columns = other_dims[1:]
            else:
                matrix_rows = rows_dims + other_dims
                matrix_columns = cols_dims + color_dims
            if matrix_rows:
                query_state["Rows"] = {
                    "projections": [self._make_projection_entry(d, force_column=True)
                                    for d in matrix_rows]
                }
            if matrix_columns:
                query_state["Columns"] = {
                    "projections": [self._make_projection_entry(d, force_column=True)
                                    for d in matrix_columns]
                }
            matrix_values = axis_meas + color_meas + size_fields
            if matrix_values:
                query_state["Values"] = {
                    "projections": [self._make_projection_entry(m)
                                    for m in matrix_values]
                }

        elif visual_type == 'card':
            targets = axis_meas or fallback_meas or fallback_dims
            if targets:
                source_aggregations = {(f['name'], f.get('shelf')): f.get('aggregation')
                                       for f in cleaned_fields}
                if len(targets) > 1:
                    ws_data['_override_visual_type'] = 'multiRowCard'
                column_types = getattr(self, '_actual_bim_column_types', {}) or {}
                if any(column_types.get(f.get('_resolved_pair')) in ('string', 'boolean')
                       and not self._is_measure_field(f['name']) for f in targets):
                    ws_data['_override_visual_type'] = 'multiRowCard'
                query_state["Values"] = {
                    "projections": [self._make_projection_entry(
                        {**f, 'aggregation': source_aggregations.get(
                            (f['name'], f.get('shelf'))) or 'max'}
                        if self._is_date_field(f['name']) else f)
                                    for f in targets]
                }

        elif visual_type == 'multiRowCard':
            # Prefer text fields + dims over placeholder measures (e.g. min(1))
            targets = (text_fields + all_dims if text_fields
                       else (axis_meas or fallback_meas or fallback_dims))
            targets = list(targets)
            target_names = {f['name'] for f in targets}
            for field in tooltip_fields:
                if field['name'] not in target_names:
                    targets.append(field)
                    target_names.add(field['name'])
            if targets:
                query_state["Values"] = {
                    "projections": [self._make_projection_entry(f) for f in targets]
                }

        elif visual_type in ('gauge', 'kpi'):
            if axis_meas:
                query_state["Y"] = self._make_projection(axis_meas[0])
            if len(axis_meas) >= 2:
                query_state["TargetValue"] = self._make_projection(axis_meas[1])
            if axis_dims:
                query_state["Category"] = self._make_projection(axis_dims[0])

        elif visual_type in ('treemap', 'sunburst'):
            # Treemap: dims â†’ Group (multiple levels, non-date first)
            tree_dims = hier_dims + color_dims or fallback_dims
            non_date = [d for d in tree_dims
                        if not self._is_date_field(d['name'])]
            date = [d for d in tree_dims
                    if self._is_date_field(d['name'])]
            ordered = non_date + date
            if ordered:
                query_state["Group"] = {
                    "projections": [self._make_projection_entry(d)
                                    for d in (ordered[:1] if visual_type == 'treemap' else ordered)]
                }
                if visual_type == 'treemap' and len(ordered) > 1:
                    query_state['Details'] = self._make_projection(ordered[1])
                    tip_fields = tip_fields + ordered[2:]
                    if len(ordered) > 2:
                        ws_data['_visual_mapping_note'] = (
                            'Treemap supports two grouping levels; additional source attributes remain in tooltips.')
            # Values: axis measures, or color measures (Tableau uses color
            # encoding for value on treemaps), or size fields
            tree_meas = axis_meas or color_meas or size_fields or fallback_meas
            if tree_meas:
                query_state["Values"] = self._make_projection(tree_meas[0])
                # Remove used measure from tip_fields to avoid duplicate
                used = tree_meas[0]
                tip_fields = [f for f in tip_fields if f is not used]
            if tip_fields:
                query_state["Tooltips"] = {
                    "projections": [self._make_projection_entry(f)
                                    for f in tip_fields]
                }

        elif visual_type in ('filledMap', 'shapeMap'):
            # Filled/shape map: dims â†’ Category (Location well),
            #                   color dim â†’ Series, measure â†’ Size/Color.
            # PBI Desktop (2025+) auto-converts filledMap â†’ map;
            # the map visual uses PBIR role "Category" for its Location well.
            # Tableau maps often use generated Lat/Lon (which we skip)
            # and put the real geo field on the tooltip shelf.
            loc_dims = axis_dims
            if not loc_dims:
                # Pull non-measure tooltip fields as Location fallback
                loc_dims = [f for f in tooltip_fields
                            if not self._is_measure_field(f['name'])]
                # Remove them from tip_fields so they're not duplicated
                tip_fields = [f for f in tip_fields if f not in loc_dims]
            loc_role = "Category"  # map visual PBIR role for Location well
            if loc_dims:
                query_state[loc_role] = {
                    "projections": [self._make_projection_entry(d)
                                    for d in loc_dims]
                }
            if color_dims:
                query_state["Series"] = self._make_projection(color_dims[0])
            value_role = "Color" if visual_type == 'shapeMap' else "Size"
            # For maps, Tableau's color measure â†’ PBI Size (data magnitude).
            val_src = axis_meas or color_meas
            if val_src:
                query_state[value_role] = self._make_projection(val_src[0])
                # Remove the used color_meas from tooltips to avoid duplication
                if val_src[0] in color_meas:
                    tip_fields = [f for f in tip_fields if f is not val_src[0]]
            if tip_fields:
                query_state["Tooltips"] = {
                    "projections": [self._make_projection_entry(f)
                                    for f in tip_fields]
                }

        elif visual_type == 'map':
            # Bubble map: dims â†’ Category (Location well),
            #             color dim â†’ Series, measure â†’ Size.
            # The map visual uses PBIR role "Category" for its Location well.
            # Same fallback as filledMap for generated Lat/Lon.
            loc_dims = axis_dims
            if not loc_dims:
                loc_dims = [f for f in tooltip_fields
                            if not self._is_measure_field(f['name'])]
                tip_fields = [f for f in tip_fields if f not in loc_dims]
            if loc_dims:
                query_state["Category"] = {
                    "projections": [self._make_projection_entry(d)
                                    for d in loc_dims]
                }
            if color_dims:
                query_state["Series"] = self._make_projection(color_dims[0])
            # For maps, Tableau's color measure â†’ PBI Size (data magnitude).
            sz = axis_meas or size_fields or color_meas
            if sz:
                query_state["Size"] = self._make_projection(sz[0])
                # Remove the used color_meas from tooltips to avoid duplication
                if sz[0] in color_meas:
                    tip_fields = [f for f in tip_fields if f is not sz[0]]
            if tip_fields:
                query_state["Tooltips"] = {
                    "projections": [self._make_projection_entry(f)
                                    for f in tip_fields]
                }

        elif visual_type == 'azureMap':
            lat = next((f for f in cleaned_fields if _is_latitude_field(f)), None)
            lon = next((f for f in cleaned_fields if _is_longitude_field(f)), None)
            has_coordinates = lat is not None and lon is not None
            if not has_coordinates:
                lat = lon = None
            used = [f for f in (lat, lon) if f is not None]
            location_candidates = axis_dims + detail_dims + text_dims + tooltip_dims
            location = next((d for d in location_candidates
                             if d not in used and
                             (has_coordinates or _is_geographic_dimension(d))), None)
            if location is not None:
                query_state["Category"] = {
                    "projections": [self._make_projection_entry(location, force_column=True)]
                }
                tip_fields = [f for f in tip_fields if f is not location]
            elif not has_coordinates:
                ws_data['_visual_mapping_note'] = (
                    'No resolved geographic location or coordinate pair; source geography is required for this map.')
            # Power BI refuses a Location bound alongside raw coordinates, and
            # asks for them averaged per location instead.
            coordinate_agg = {'aggregation': 'avg'} if location is not None else {}
            if lat:
                query_state["Y"] = {
                    "projections": [self._make_projection_entry(
                        {**lat, **coordinate_agg}, force_column=location is None)]
                }
            if lon:
                query_state["X"] = {
                    "projections": [self._make_projection_entry(
                        {**lon, **coordinate_agg}, force_column=location is None)]
                }
            if color_dims:
                query_state["Series"] = self._make_projection(color_dims[0])
            sz = [m for m in (size_fields + axis_meas + color_meas)
                  if m not in used]
            if sz:
                query_state["Size"] = self._make_projection(sz[0])
            tips = [f for f in tip_fields if f not in used and f not in sz[:1]]
            if tips:
                query_state["Tooltips"] = {
                    "projections": [self._make_projection_entry(f)
                                    for f in tips]
                }

        elif visual_type in ('lineClusteredColumnComboChart',
                             'lineStackedColumnComboChart'):
            # Pane extraction records the source axis role explicitly. Keep
            # that mapping authoritative even when pane/field order differs.
            if any(field.get('axis_role') in ('Y', 'Y2') for field in axis_meas):
                axis_rank = {'Y': 0, 'Y2': 1}
                axis_meas = sorted(
                    axis_meas,
                    key=lambda field: axis_rank.get(field.get('axis_role'), 2))
            if axis_dims:
                query_state["Category"] = self._make_projection(axis_dims[0])
            if color_dims:
                query_state["Series"] = self._make_projection(color_dims[0])
            elif len(axis_dims) >= 2:
                query_state["Series"] = self._make_projection(axis_dims[1])
            if axis_meas:
                query_state["Y"] = self._make_projection(axis_meas[0])
            if len(axis_meas) >= 2:
                query_state["Y2"] = self._make_projection(axis_meas[1])
            if tip_fields:
                query_state["Tooltips"] = {
                    "projections": [self._make_projection_entry(f) for f in tip_fields]
                }

        elif visual_type == 'waterfallChart':
            if axis_dims:
                query_state["Category"] = self._make_projection(axis_dims[0])
            if axis_meas:
                query_state["Y"] = self._make_projection(axis_meas[0])
            if len(axis_dims) >= 2:
                query_state["Breakdown"] = self._make_projection(axis_dims[1])

        elif visual_type == 'boxAndWhisker':
            if axis_dims:
                query_state["Category"] = self._make_projection(axis_dims[0])
            if axis_meas:
                query_state["Value"] = self._make_projection(axis_meas[0])

        elif visual_type == 'wordCloud':
            if axis_dims:
                query_state["Category"] = self._make_projection(axis_dims[0])
            if axis_meas:
                query_state["Values"] = self._make_projection(axis_meas[0])

        elif visual_type == 'decompositionTree':
            if axis_dims:
                query_state["TreeItems"] = self._make_projection(axis_dims[0])
            if axis_meas:
                query_state["Values"] = self._make_projection(axis_meas[0])

        else:
            # â”€â”€ Standard charts (bar, column, line, area, pie, donut,
            #    funnel, ribbon, stacked variants) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            if axis_dims:
                query_state["Category"] = self._make_projection(axis_dims[0])
            elif color_dims:
                # No axis dims â€” promote first color dim to Category
                query_state["Category"] = self._make_projection(color_dims[0])
                color_dims = color_dims[1:]  # consume it
            elif fallback_dims:
                query_state["Category"] = self._make_projection(fallback_dims[0])

            # Series: color dim (highest priority) or second axis dim
            # that isn't a date when the first dim already is.
            # Only promote axis dim to Series when there are measures for Y.
            if color_dims:
                query_state["Series"] = self._make_projection(color_dims[0])
            elif len(axis_dims) >= 2 and axis_meas:
                candidates = axis_dims[1:]
                non_date = [d for d in candidates
                            if not self._is_date_field(d['name'])]
                series_dim = non_date[0] if non_date else candidates[0]
                query_state["Series"] = self._make_projection(series_dim)

            if axis_meas:
                query_state["Y"] = {
                    "projections": [self._make_projection_entry(m)
                                    for m in axis_meas]
                }
            elif size_fields:
                # Tableau size-shelf measures (e.g. line chart thickness) â†’ Y
                query_state["Y"] = {
                    "projections": [self._make_projection_entry(m)
                                    for m in size_fields]
                }
            elif fallback_meas:
                query_state["Y"] = {
                    "projections": [self._make_projection_entry(m)
                                    for m in fallback_meas]
                }
            # If no measures, use last dim as pseudo-measure (only when
            # Series hasn't consumed it yet)
            elif len(axis_dims) > 1 and "Series" not in query_state:
                query_state.clear()
                query_state['Values'] = {
                    'projections': [self._make_projection_entry(f)
                                    for f in axis_dims + color_dims + tooltip_dims]
                }
                ws_data['_override_visual_type'] = 'tableEx'
                ws_data['_visual_fallback_note'] = (
                    'No resolved value field for this chart; source dimensions were preserved as a table.')

            if tip_fields:
                query_state["Tooltips"] = {
                    "projections": [self._make_projection_entry(f)
                                    for f in tip_fields]
                }

        # ── Fallback: only measures, no dimensions at all ────────────────
        # Chart types that require a Category/Group dimension but have none
        # must degrade — otherwise PBI raises SecondaryGroupsWithoutPrimary.
        # This occurs when fields classified as dimensions during Tableau
        # extraction are reclassified as measures by the TMDL generator
        # (e.g. string-split fields that transitively reference SUM()).
        # Prefer tableEx (preserves all data columns).
        _NEEDS_CATEGORY = {
            'barChart', 'clusteredBarChart', 'stackedBarChart',
            'hundredPercentStackedBarChart',
            'columnChart', 'clusteredColumnChart', 'stackedColumnChart',
            'hundredPercentStackedColumnChart',
            'lineChart', 'areaChart', 'stackedAreaChart',
            'hundredPercentStackedAreaChart',
            'pieChart', 'donutChart', 'waterfallChart', 'funnel',
            'ribbonChart', 'lineClusteredColumnComboChart',
            'lineStackedColumnComboChart',
            'boxAndWhisker', 'bulletChart', 'wordCloud',
        }
        if (axis_meas and not axis_dims
                and "Category" not in query_state
                and visual_type in _NEEDS_CATEGORY):
            query_state.clear()
            all_fields = axis_meas + color_meas
            query_state["Values"] = {
                "projections": [self._make_projection_entry(m)
                                for m in all_fields]
            }
            ws_data['_override_visual_type'] = 'tableEx'
            fallback_note = (
                f'{visual_type} requires a category dimension, but no source '
                'category was available; available measures were preserved as a table.')
            previous_note = ws_data.get('_visual_mapping_note')
            ws_data['_visual_mapping_note'] = (
                f'{previous_note}; {fallback_note}' if previous_note else fallback_note)

        # Post-process: remove None entries from projections (fields that
        # failed _bim_sym validation in _make_projection_entry) and drop
        # empty roles to prevent PBI Desktop "deleted columns" errors.
        roles_to_remove = []
        for role_name, role_val in query_state.items():
            if isinstance(role_val, dict) and 'projections' in role_val:
                role_val['projections'] = [
                    p for p in role_val['projections'] if p is not None
                ]
                if not role_val['projections']:
                    roles_to_remove.append(role_name)
        for r in roles_to_remove:
            del query_state[r]

        tooltip_role = query_state.get('Tooltips')
        if tooltip_role:
            bound_fields = [projection['field']
                            for role, value in query_state.items() if role != 'Tooltips'
                            for projection in value.get('projections', [])]
            unique_tooltips = []
            for projection in tooltip_role['projections']:
                if projection['field'] in bound_fields:
                    continue
                if 'Column' in projection['field']:
                    column = projection['field']['Column']
                    projection = {**projection, 'field': {
                        'Aggregation': {'Expression': {'Column': column}, 'Function': 3}},
                        'queryRef': 'Min(' + projection['queryRef'] + ')'}
                    if projection['field'] in bound_fields:
                        continue
                unique_tooltips.append(projection)
                bound_fields.append(projection['field'])
            if unique_tooltips:
                tooltip_role['projections'] = unique_tooltips
            else:
                del query_state['Tooltips']

        self._preserve_size_measures(query_state, visual_type, size_fields)

        query_state = self._enforce_role_contract(
            query_state, visual_type, ws_data, fallback_dims + fallback_meas)

        if not query_state:
            return None

        binding = (ws_data.get('_discrete_month_binding')
                   or ws_data.get('_discrete_year_binding'))
        if binding:
            category = next(iter(
                query_state.get('Category', {}).get('projections', [])
            ), None)
            if category and category.get('field', {}).get('Column'):
                query_state_result = {"queryState": query_state}
                query_state_result['sortDefinition'] = {
                    'sort': [{
                        'field': category['field'],
                        'direction': 'Ascending',
                    }],
                    'isDefaultSort': True,
                }
                return query_state_result
        return {"queryState": query_state}

    def _read_related_month_bindings(self, project_dir):
        """Find model date-part bindings proved by generated M and relationships."""
        from powerbi_import.tmdl_writers import _parse_source_column

        definition_dir = os.path.join(project_dir, 'definition')
        tables_dir = os.path.join(definition_dir, 'tables')
        relationships_path = os.path.join(definition_dir, 'relationships.tmdl')
        self._related_continuous_month_bindings = {}
        self._related_year_bindings = {}
        symbols = getattr(self, '_actual_bim_symbols', None)
        if not symbols or not os.path.isdir(tables_dir) or not os.path.isfile(
                relationships_path):
            return {}

        def name_value(value):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == "'":
                return value[1:-1].replace("''", "'")
            return value

        def column_blocks(text):
            matches = list(re.finditer(
                r'(?m)^[ \t]+column\s+(.+?)\s*$', text))
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
                block = text[match.end():end]
                attrs = {}
                for key in ('dataType', 'sourceColumn', 'sortByColumn'):
                    attr = re.search(
                        rf'(?m)^[ \t]+{key}:\s*(.+?)\s*$', block)
                    if attr:
                        attrs[key] = (
                            _parse_source_column(attr.group(1))
                            if key == 'sourceColumn' else name_value(attr.group(1)))
                yield name_value(match.group(1)), attrs

        def m_add_column_uses(expression, output_name, function, input_name):
            output_literal = re.escape(output_name.replace('"', '""'))
            input_name = re.escape(input_name.replace('"', '""'))
            input_reference = rf'(?:\[{input_name}\]|\[#"{input_name}"\])'
            return bool(re.search(
                rf'Table\.AddColumn\s*\(.*?,\s*"{output_literal}"\s*,'
                rf'\s*each\s*Date\.{function}\s*\(\s*{input_reference}'
                rf'(?:\s*,|\s*\))',
                expression, re.DOTALL,
            ))

        calendar_dates = {}
        calendar_month_starts = {}
        calendar_years = {}
        for filename in os.listdir(tables_dir):
            if not filename.lower().endswith('.tmdl'):
                continue
            path = os.path.join(tables_dir, filename)
            with open(path, encoding='utf-8') as stream:
                text = stream.read()
            table_match = re.search(
                r"(?m)^table\s+('(?:[^']|'')+'|[^\s]+)\s*$", text)
            if not table_match:
                continue
            table_name = name_value(table_match.group(1))
            columns = dict(column_blocks(text))
            partition = re.search(
                r'(?m)^[ \t]*source\s*=\s*(.*)\Z', text, re.DOTALL)
            if not partition:
                continue
            expression = partition.group(1)
            date_columns = [
                (name, attrs.get('sourceColumn', name))
                for name, attrs in columns.items()
                if attrs.get('dataType', '').lower() in ('datetime', 'date')
            ]
            for month_start_name, month_start_attrs in columns.items():
                if month_start_attrs.get('dataType', '').lower() not in (
                        'datetime', 'date'):
                    continue
                month_start_source = month_start_attrs.get(
                    'sourceColumn', month_start_name)
                for date_name, date_source in date_columns:
                    if (month_start_name != date_name
                            and m_add_column_uses(
                                expression, month_start_source,
                                'StartOfMonth', date_source)
                            and all((table_name, name) in symbols
                                    for name in (date_name, month_start_name))):
                        calendar_month_starts.setdefault(
                            (table_name, date_name), set()).add(
                                (table_name, month_start_name))
            for year_name, year_attrs in columns.items():
                if year_attrs.get('dataType', '').lower() != 'int64':
                    continue
                year_source = year_attrs.get('sourceColumn', year_name)
                for date_name, date_source in date_columns:
                    if (m_add_column_uses(
                            expression, year_source, 'Year', date_source)
                            and (table_name, date_name) in symbols
                            and (table_name, year_name) in symbols):
                        calendar_years.setdefault(
                            (table_name, date_name), set()).add(
                                (table_name, year_name))
            for label_name, label_attrs in columns.items():
                sort_name = label_attrs.get('sortByColumn')
                index_attrs = columns.get(sort_name, {})
                if (label_attrs.get('dataType', '').lower() != 'string'
                        or not sort_name
                        or index_attrs.get('dataType', '').lower() != 'int64'):
                    continue
                label_source = label_attrs.get('sourceColumn', label_name)
                index_source = index_attrs.get('sourceColumn', sort_name)
                for date_name, date_source in date_columns:
                    if (m_add_column_uses(
                            expression, label_source, 'MonthName', date_source)
                            and m_add_column_uses(
                                expression, index_source, 'Month', date_source)
                            and all((table_name, name) in symbols
                                    for name in (date_name, label_name, sort_name))):
                        calendar_dates.setdefault(
                            (table_name, date_name), set()).add(
                                (table_name, label_name, sort_name))

        def parse_reference(value):
            match = re.fullmatch(
                r"\s*('(?:[^']|'')+'|[^.]+)\.(('(?:[^']|'')+')|.+?)\s*",
                value)
            if not match:
                return None
            return name_value(match.group(1)), name_value(match.group(2))

        with open(relationships_path, encoding='utf-8') as stream:
            relationships = stream.read()
        relationship_headers = list(re.finditer(
            r'(?m)^relationship\s+.+?$', relationships))
        bindings = {}
        continuous_bindings = {}
        year_bindings = {}
        source_column_types = (
            getattr(self, '_actual_bim_column_types', None) or {})
        for index, header in enumerate(relationship_headers):
            end = (relationship_headers[index + 1].start()
                   if index + 1 < len(relationship_headers)
                   else len(relationships))
            block = relationships[header.end():end]
            from_match = re.search(r'(?m)^\s*fromColumn:\s*(.+?)\s*$', block)
            to_match = re.search(r'(?m)^\s*toColumn:\s*(.+?)\s*$', block)
            if not from_match or not to_match:
                continue
            active = re.search(r'(?m)^\s*isActive:\s*(\w+)\s*$', block)
            cardinality = re.search(
                r'(?m)^\s*toCardinality:\s*(\w+)\s*$', block)
            if (active and active.group(1).lower() != 'true'
                    or cardinality and cardinality.group(1).lower() != 'one'):
                continue
            source = parse_reference(from_match.group(1))
            target = parse_reference(to_match.group(1))
            if not source or not target or source not in symbols:
                continue
            month_bindings = calendar_dates.get(target)
            if month_bindings:
                bindings.setdefault(source, set()).update(month_bindings)
            continuous_candidates = calendar_month_starts.get(target)
            if continuous_candidates:
                continuous_bindings.setdefault(source, set()).update(
                    continuous_candidates)
            # Year is only interchangeable with a discrete Tableau year when
            # the exact physical DateTime source column has an active
            # many-to-one relationship to a generated Calendar date, and that
            # Calendar's integer Year is derived from that date in M. TMDL
            # omits isActive=true and toCardinality=one when they are defaults.
            strict_active = not active or active.group(1).lower() == 'true'
            source_type = str(source_column_types.get(source, '')).lower()
            year_candidates = calendar_years.get(target)
            if (strict_active
                    and (not cardinality or cardinality.group(1).lower() == 'one')
                    and source_type == 'datetime'
                    and year_candidates):
                year_bindings.setdefault(source, set()).update(year_candidates)

        self._related_continuous_month_bindings = {
            source: next(iter(candidates))
            for source, candidates in continuous_bindings.items()
            if len(candidates) == 1
        }
        self._related_year_bindings = {
            source: next(iter(candidates))
            for source, candidates in year_bindings.items()
            if len(candidates) == 1
        }
        return {
            source: next(iter(candidates))
            for source, candidates in bindings.items()
            if len(candidates) == 1
        }

    def _preserve_size_measures(self, query_state, visual_type, size_fields):
        """Keeps a Tableau size-shelf measure that the target visual cannot size by.

        Only scatter and map expose a Size role; elsewhere the measure would be
        dropped outright, so it moves to Tooltips rather than being lost.
        """
        if not size_fields or not query_state:
            return
        if 'Size' in (_MEASURE_ONLY_ROLES.get(visual_type) or ()):
            return
        if visual_type not in _TOOLTIP_CAPABLE_VISUALS:
            return

        bound = [p['field'] for role in query_state.values()
                 for p in (role.get('projections') or []) if p.get('field')]
        tooltips = query_state.setdefault('Tooltips', {'projections': []})
        for measure in size_fields:
            entry = self._make_projection_entry(measure)
            if entry is None:
                continue
            if entry.get('field') in bound:
                continue
            tooltips['projections'].append(entry)
            bound.append(entry['field'])
        if not tooltips['projections']:
            del query_state['Tooltips']

    def _enforce_role_contract(self, query_state, visual_type, ws_data, all_fields):
        """Drop bindings the visual cannot render, and degrade when a required
        role cannot be filled, so the report never ships an unrenderable visual."""
        # An earlier branch may already have downgraded to a type with a looser
        # contract; judging the original type would undo that better mapping.
        visual_type = ws_data.get('_override_visual_type') or visual_type

        def note(text):
            previous = ws_data.get('_visual_mapping_note')
            ws_data['_visual_mapping_note'] = f'{previous}; {text}' if previous else text

        unaggregated_numeric_columns = set()
        column_types = getattr(self, '_actual_bim_column_types', {}) or {}
        field_map = getattr(self, '_field_map', {}) or {}
        for field in all_fields:
            if (not field.get('_aggregation_disabled')
                    or field.get('shelf') not in ('rows', 'columns')):
                continue
            pair = field.get('_resolved_pair') or field_map.get(
                self._clean_field_name(field.get('name', '')))
            if not pair:
                continue
            column_type = str(column_types.get(pair, '')).lower()
            if (column_type in _NUMERIC_MODEL_TYPES
                    and not self._is_bim_measure_symbol(
                        pair[0], pair[1], field.get('name', ''),
                        prefer_column=True)):
                unaggregated_numeric_columns.add(pair)

        def is_unaggregated_numeric_column(projection):
            column = projection.get('field', {}).get('Column')
            if not column:
                return False
            source = column.get('Expression', {}).get('SourceRef', {})
            pair = (source.get('Entity'), column.get('Property'))
            return pair in unaggregated_numeric_columns

        for role in _MEASURE_ONLY_ROLES.get(visual_type, ()):
            body = query_state.get(role)
            if not body:
                continue
            kept = [
                projection for projection in body['projections']
                if ('Column' not in projection['field']
                    or is_unaggregated_numeric_column(projection))
            ]
            if len(kept) != len(body['projections']):
                dropped = [p['queryRef'].split('.', 1)[-1]
                           for p in body['projections']
                           if ('Column' in p['field']
                               and not is_unaggregated_numeric_column(p))]
                note(f'{role} accepts only aggregated values; '
                     f'non-numeric field(s) left unbound: {", ".join(dropped)}')
            if kept:
                body['projections'] = kept
            else:
                del query_state[role]

        required = _REQUIRED_VISUAL_ROLES.get(visual_type, ())
        if required and not all(query_state.get(r, {}).get('projections')
                                for r in required):
            missing = [r for r in required if not query_state.get(r, {}).get('projections')]
            entries = []
            for field in all_fields:
                entry = self._make_projection_entry(field)
                if not entry or any(entry['field'] == item['field'] for item in entries):
                    continue
                entries.append(entry)
            if not entries:
                return {}
            if len(entries) > 10:
                omitted = len(entries) - 10
                entries = entries[:10]
                note(f'{omitted} additional unique field(s) omitted from the '
                     'table fallback, which is limited to 10 projections')
            _disambiguate_aggregation_query_refs(entries)
            query_state = {"Values": {"projections": entries}}
            ws_data['_override_visual_type'] = 'tableEx'
            note(f'{visual_type} needs {", ".join(missing)}; '
                 'source fields could not fill that role, shown as a table instead')
        return query_state

    def _make_projection(self, field):
        """Creates a simple projection for a field"""
        return {
            "projections": [self._make_projection_entry(field)]
        }

    def _make_scatter_axis_projection(self, field):
        """Creates a projection for scatter chart X/Y/Size axes.
        BIM measures use Measure wrapper; physical columns use Aggregation
        wrapper (Sum) since scatter axes require explicit aggregation."""
        return {
            "projections": [self._make_scatter_axis_entry(field)]
        }

    def _make_scatter_axis_entry(self, field):
        """Creates projection entry for scatter chart axes.
        Named DAX measures â†’ Measure wrapper.
        Physical columns â†’ Aggregation wrapper with shelf-aware Function ID."""
        if field.get('_aggregation_disabled'):
            return self._make_projection_entry(field, force_column=True)
        raw_name = field.get('name', 'Field')
        clean_name = self._clean_field_name(raw_name)

        if field.get('_resolved_pair'):
            entity, prop = field['_resolved_pair']
        elif field.get('datasource'):
            entity, prop = self._resolve_field_entity(
                clean_name, datasource=field['datasource'],
                source_table=field.get('source_table'),
                source_column=field.get('source_column'))
        elif hasattr(self, '_field_map') and clean_name in self._field_map:
            entity, prop = self._field_map[clean_name]
        else:
            entity = getattr(self, '_main_table', 'Table')
            prop = clean_name
            for ds in getattr(self, '_datasources_ref', []):
                for calc in ds.get('calculations', []):
                    calc_id = calc.get('name', '').replace('[', '').replace(']', '')
                    if calc_id == clean_name:
                        prop = calc.get('caption', clean_name).strip()
                        self._field_map[clean_name] = (entity, prop)
                        break

        # Override entity for multi-datasource renamed tables
        ds_ref = field.get('datasource', '')
        collision = getattr(self, '_collision_tables', set())
        if (not field.get('_resolved_pair') and ds_ref and entity in collision
            and hasattr(self, '_ds_table_map') and ds_ref in self._ds_table_map):
            ds_entity = self._ds_table_map[ds_ref]
            if ds_entity != entity:
                entity = ds_entity

        shelf_agg = field.get('aggregation', '')
        is_bim_measure = self._is_bim_measure_symbol(
            entity, prop, clean_name, prefer_column=bool(shelf_agg))
        agg_func = _TABLEAU_AGG_TO_PBI_FUNC.get(shelf_agg, 0)
        source_prop = prop
        if not is_bim_measure and shelf_agg in ('ctd', 'countd'):
            binding = getattr(self, '_countd_measure_bindings', {}).get((entity, prop))
            if binding:
                prop = binding
                is_bim_measure = True
            else:
                logger.warning(
                    "COUNTD %s.%s has no nonblank measure; distinct count may include BLANK",
                    entity, prop,
                )

        if is_bim_measure:
            measure_type = (getattr(self, '_actual_bim_measure_types', {}) or {}).get(
                (entity, prop), '')
            if str(measure_type).lower() in (
                    'string', 'text', 'date', 'datetime', 'boolean', 'bool'):
                logger.warning(
                    "Cannot use nonnumeric measure %s.%s (%s) on a scatter axis",
                    entity, prop, measure_type,
                )
                return None
        elif not self._supports_numeric_aggregation(entity, prop, agg_func):
            return None

        if is_bim_measure:
            field_ref = {
                "Measure": {
                    "Expression": {"SourceRef": {"Entity": entity}},
                    "Property": prop
                }
            }
        else:
            # Physical column: wrap as Aggregation with shelf-aware Function ID
            field_ref = {
                "Aggregation": {
                    "Expression": {
                        "Column": {
                            "Expression": {"SourceRef": {"Entity": entity}},
                            "Property": prop
                        }
                    },
                    "Function": agg_func
                }
            }

        # Final validation against semantic model
        _bim_sym = getattr(self, '_actual_bim_symbols', None) or set()
        if _bim_sym and (entity, prop) not in _bim_sym:
            if (entity, prop.strip()) not in _bim_sym:
                return None
            prop = prop.strip()

        return {
            "field": field_ref,
            "queryRef": f"{entity}.{prop}",
            "nativeQueryRef": source_prop,
            "active": True
        }

    def _is_bim_measure_symbol(self, entity, prop, clean_name, prefer_column=False):
        """Resolve measure identity by table and name when model symbols exist."""
        measure_symbols = getattr(self, '_actual_bim_measure_symbols', None)
        if measure_symbols is not None:
            return (entity, prop) in measure_symbols
        column_types = getattr(self, '_actual_bim_column_types', {}) or {}
        if prefer_column and (entity, prop) in column_types:
            return False
        measure_names = getattr(self, '_bim_measure_names', set())
        return clean_name in measure_names or prop in measure_names

    def _supports_numeric_aggregation(self, entity, prop, function):
        column_type = (getattr(self, '_actual_bim_column_types', {}) or {}).get(
            (entity, prop), '')
        if function in (0, 1, 6) and str(column_type).lower() in (
                'string', 'text', 'date', 'datetime', 'boolean', 'bool'):
            logger.warning(
                "Cannot apply numeric aggregation %s to %s.%s (%s)",
                function, entity, prop, column_type,
            )
            return False
        return True

    def _make_projection_entry(self, field, force_column=False):
        """Creates a projection entry for a field, resolved to the Power BI model.

        Wrapper selection:
        - Named DAX measures (in _bim_measure_names) â†’ ``Measure`` wrapper
        - Physical columns with explicit Tableau aggregation (cnt, avg, etc.)
          â†’ ``Aggregation`` wrapper with the corresponding PBI Function ID
        - Physical numeric columns treated as measures by Tableau
          (in _measure_names but NOT in _bim_measure_names) â†’ ``Aggregation``
          wrapper with Function 0 (Sum) so PBI shows explicit aggregation
        - Everything else (dimension columns) â†’ ``Column`` wrapper

        *force_column* keeps the bare ``Column`` wrapper whatever the field
        looks like, for roles where Power BI rejects an aggregate.
        """
        raw_name = field.get('name', 'Field')

        # Clean all known Tableau prefixes
        clean_name = self._clean_field_name(raw_name)

        # Resolve via mapping
        if field.get('_resolved_pair'):
            entity, prop = field['_resolved_pair']
        elif field.get('datasource'):
            entity, prop = self._resolve_field_entity(
                clean_name, datasource=field['datasource'],
                source_table=field.get('source_table'),
                source_column=field.get('source_column'))
        elif hasattr(self, '_field_map') and clean_name in self._field_map:
            entity, prop = self._field_map[clean_name]
        else:
            # Fallback: use main table (not raw Tableau datasource name) as Entity
            entity = getattr(self, '_main_table', 'Table')
            prop = clean_name
            # Try to resolve Tableau calculation IDs to captions
            for ds in getattr(self, '_datasources_ref', []):
                for calc in ds.get('calculations', []):
                    calc_id = calc.get('name', '').replace('[', '').replace(']', '')
                    if calc_id == clean_name:
                        prop = calc.get('caption', clean_name).strip()
                        if hasattr(self, '_field_map'):
                            self._field_map[clean_name] = (entity, prop)
                        break

        # Override entity when the field comes from a datasource whose table
        # was renamed (multi-datasource collision).  The field's 'datasource'
        # attribute tells us which Tableau datasource it belongs to, and
        # _ds_table_map resolves that to the correct (possibly renamed) table.
        # Override when entity is in the collision group — ambiguous columns
        # shared across renamed tables need datasource-based disambiguation.
        ds_ref = field.get('datasource', '')
        collision = getattr(self, '_collision_tables', set())
        if (not field.get('_resolved_pair') and ds_ref and entity in collision
            and hasattr(self, '_ds_table_map') and ds_ref in self._ds_table_map):
            ds_entity = self._ds_table_map[ds_ref]
            symbols = getattr(self, '_actual_bim_symbols', None) or set()
            if ds_entity != entity and (not symbols or (ds_entity, prop) in symbols):
                entity = ds_entity

        # Tableau captions occasionally carry trailing whitespace while the
        # generated TMDL symbol is canonicalized.  Resolve that harmless
        # difference before building any role, including tooltips.
        actual_measure_names = getattr(self, '_actual_bim_measure_names', set()) or set()
        canonical_prop = next(
            (name for name in actual_measure_names if name.strip() == prop.strip()),
            None,
        )
        if canonical_prop:
            prop = canonical_prop
        prop = prop.strip()

        shelf_agg = field.get('aggregation', '')
        explicit_agg_func = _TABLEAU_AGG_TO_PBI_FUNC.get(shelf_agg)
        force_column = (force_column or bool(field.get('_force_column'))
                        or bool(field.get('_explicit_no_aggregation'))
                        or bool(re.match(r'^none:', raw_name.strip('[]'), re.IGNORECASE)))

        # Determine wrapper type
        is_bim_measure = self._is_bim_measure_symbol(
            entity, prop, clean_name, prefer_column=bool(shelf_agg))
        source_prop = prop
        if not force_column and not is_bim_measure and shelf_agg in ('ctd', 'countd'):
            binding = getattr(self, '_countd_measure_bindings', {}).get((entity, prop))
            if binding:
                prop = binding
                is_bim_measure = True
            else:
                logger.warning(
                    "COUNTD %s.%s has no nonblank measure; distinct count may include BLANK",
                    entity, prop,
                )
        is_physical_measure = (
            not is_bim_measure
            and hasattr(self, '_measure_names')
            and (clean_name in self._measure_names or prop in self._measure_names)
        )

        if is_bim_measure and not force_column:
            # Named DAX measure â†’ Measure wrapper
            field_ref = {
                "Measure": {
                    "Expression": {"SourceRef": {"Entity": entity}},
                    "Property": prop
                }
            }
        elif not force_column and (is_physical_measure
                                   or explicit_agg_func is not None):
            # Physical column with aggregation â€” use shelf aggregation if present
            agg_func = explicit_agg_func if explicit_agg_func is not None else 0
            if not self._supports_numeric_aggregation(entity, prop, agg_func):
                return None
            field_ref = {
                "Aggregation": {
                    "Expression": {
                        "Column": {
                            "Expression": {"SourceRef": {"Entity": entity}},
                            "Property": prop
                        }
                    },
                    "Function": agg_func
                }
            }
        else:
            # Dimension column â†’ Column wrapper
            field_ref = {
                "Column": {
                    "Expression": {"SourceRef": {"Entity": entity}},
                    "Property": prop
                }
            }

        # Final validation: skip field if (entity, prop) doesn't exist in
        # the semantic model — prevents PBI Desktop "deleted columns" errors.
        _bim_sym = getattr(self, '_actual_bim_symbols', None) or set()
        if _bim_sym and (entity, prop) not in _bim_sym:
            if (entity, prop.strip()) not in _bim_sym:
                return None
            prop = prop.strip()
            # Update prop in field_ref (nested dict mutation)
            for wrapper in ('Column', 'Measure'):
                if wrapper in field_ref:
                    field_ref[wrapper]['Property'] = prop
                    break
            if 'Aggregation' in field_ref:
                field_ref['Aggregation']['Expression']['Column']['Property'] = prop

        return {
            "field": field_ref,
            "queryRef": f"{entity}.{prop}",
            "nativeQueryRef": source_prop,
            "active": True
        }

    def _create_bookmarks(self, stories, page_names=None, *, page_name_map=None):
        """Converts Tableau stories to Power BI bookmarks (PBIR format)."""
        bookmarks = []
        pages = list(page_names or [])
        # A bookmark whose section names no real page makes Power BI refuse
        # the project. Tableau stores a worksheet name, which is not a page.
        fallback = pages[0] if pages else 'ReportSection'
        for story in stories:
            story_name = story.get('name', 'Story')
            for sp_idx, sp in enumerate(story.get('story_points', [])):
                caption = sp.get('caption', f'{story_name} - Point {sp_idx + 1}')
                captured = (sp.get('captured_sheet') or '').strip()
                captured = (page_name_map or {}).get(captured, captured)
                active_section = captured if captured in pages else fallback
                bookmark = {
                    "name": f"Bookmark_{uuid.uuid4().hex[:8]}",
                    "displayName": caption,
                    "explorationState": {
                        "version": "1.0",
                        "activeSection": active_section,
                        "sections": {
                            active_section: {"visualContainers": {}}
                        },
                    }
                }
                bookmarks.append(bookmark)
        return bookmarks

    def _create_motion_chart_bookmarks(self, pages_shelf, page_name, worksheet_name=''):
        """Create bookmark sequence from Pages shelf for motion chart approximation.

        Generates one bookmark per known value of the Pages shelf field,
        simulating Tableau's play-axis animation as a bookmark sequence.

        Args:
            pages_shelf: Pages shelf dict with 'field' and optional 'values'.
            page_name: PBI page name for the bookmark.
            worksheet_name: Source Tableau worksheet name for labeling.

        Returns:
            list[dict]: Motion chart bookmarks.
        """
        from powerbi_import.visual_generator import _build_motion_chart_bookmarks
        field = pages_shelf.get('field', '')
        if not field:
            return []
        values = pages_shelf.get('values', [])
        if not values:
            # Generate placeholder frames when no values are available
            values = [f"Frame {i+1}" for i in range(5)]
        return _build_motion_chart_bookmarks(
            field, values, page_name, worksheet_name)

    def _create_swap_bookmarks(self, dynamic_zones, page_name):
        """Create bookmarks from dynamic zone visibility (sheet-swap containers).

        Each dynamic zone maps to a bookmark that toggles visual visibility
        for its zone. This simulates Tableau's show/hide sheet feature.

        Args:
            dynamic_zones: List from extract_dynamic_zone_visibility
            page_name: The PBI page name these bookmarks belong to

        Returns:
            List of PBI bookmark dicts
        """
        bookmarks = []
        zone_names = [dz.get('zone_name', '') for dz in dynamic_zones if dz.get('zone_name')]

        for dz in dynamic_zones:
            zone_name = dz.get('zone_name', 'Zone')
            field = dz.get('field', '')
            value = dz.get('value', '')
            label = f"Show {zone_name}" if zone_name else f"Swap: {field}={value}"
            section = page_name or 'ReportSection'

            # Build visual container state: show target zone, hide others
            visual_containers = {}
            for zn in zone_names:
                if zn == zone_name:
                    continue
                visual_containers[zn] = {
                    "singleVisual": {},
                    "display": {"mode": "hidden"},
                }

            bookmark = {
                "name": f"Swap_{uuid.uuid4().hex[:8]}",
                "displayName": label,
                "explorationState": {
                    "version": "1.0",
                    "activeSection": section,
                    "sections": {
                        section: {"visualContainers": visual_containers}
                    },
                },
                "options": {
                    "applyOnlyToTargetVisuals": True,
                    "targetVisualNames": [zone_name],
                },
            }
            bookmarks.append(bookmark)
        return bookmarks

    def _write_bookmark_files(self, def_dir, bookmarks):
        """Write bookmarks as individual PBIR bookmark files.

        PBIR stores each bookmark as a top-level ``<name>.bookmark.json`` file
        under ``definition/bookmarks/``, indexed by ``bookmarks.json``.
        """
        bookmarks_dir = os.path.join(def_dir, 'bookmarks')
        os.makedirs(bookmarks_dir, exist_ok=True)
        written = []
        for bm in bookmarks:
            bm_name = bm["name"]
            bookmark_json = {
                "$schema": SCHEMA_BOOKMARK,
                "name": bm["name"],
                "displayName": bm["displayName"] or bm_name,
                "explorationState": bm["explorationState"],
            }
            if bm.get("options"):
                bookmark_json["options"] = bm["options"]
            _write_json(os.path.join(bookmarks_dir, f'{bm_name}.bookmark.json'), bookmark_json)
            written.append(bm_name)

        if written:
            _write_json(os.path.join(bookmarks_dir, 'bookmarks.json'), {
                "$schema": SCHEMA_BOOKMARKS_METADATA,
                "items": [{"name": name} for name in written],
            })

    def _copy_custom_shapes(self, def_dir, converted_objects):
        """Copy extracted custom shape files to RegisteredResources/.

        Shape images are extracted from .twbx during the extraction phase
        into ``<extraction_output_dir>/shapes/``.  This method copies them
        into the PBIR ``RegisteredResources/`` folder so that Power BI
        Desktop can reference them.
        """
        import shutil
        shapes = converted_objects.get('custom_shapes', [])
        if not shapes:
            return
        if os.environ.get('TTPBI_UPLOAD_ROOT'):
            # This legacy search has no per-workbook extraction context. Never
            # fall back to the application's shared tableau_export for uploads.
            # Refuse explicitly until a private shape source is supplied.
            from powerbi_import.security_validator import SecurityError
            message = "Custom shapes require private extraction; shared shape lookup is forbidden for uploads"
            # The importer can catch generation errors. Latch the subprocess
            # violation so that a partial project never becomes a successful job.
            sys.audit('ttpbi.upload_denied', message)
            raise SecurityError(message)

        # Determine source shapes directory â€” typically tableau_export/shapes/
        # Search common extraction output locations
        search_dirs = [
            os.path.join('tableau_export', 'shapes'),
            os.path.join(os.path.dirname(def_dir), '..', 'tableau_export', 'shapes'),
        ]
        shapes_src = None
        for sd in search_dirs:
            if os.path.isdir(sd):
                shapes_src = sd
                break

        if not shapes_src:
            return

        res_dir = os.path.join(def_dir, 'RegisteredResources')
        os.makedirs(res_dir, exist_ok=True)

        copied = 0
        for shape in shapes:
            filename = shape.get('filename', '')
            if not filename:
                continue
            src_path = os.path.join(shapes_src, filename)
            if os.path.isfile(src_path):
                dst_path = os.path.join(res_dir, filename)
                try:
                    shutil.copy2(src_path, dst_path)
                    copied += 1
                except (OSError, PermissionError) as exc:
                    logger.debug("Could not copy shape %s: %s", filename, exc)

        if copied:
            print(f"  ðŸ–¼ï¸  Copied {copied} custom shape(s) to RegisteredResources/")

    def _create_report_filters(self, converted_objects):
        """Creates report-level filters from parameters"""
        report_filters = []

        params = converted_objects.get('parameters', [])
        for param in params:
            # Support both extracted (caption/value) and converted (displayName/currentValue) format
            param_name = param.get('displayName', param.get('caption', param.get('name', '')))
            if not param_name:
                continue
            param_name = param_name.replace('[', '').replace(']', '')

            current_value = param.get('currentValue', param.get('value', ''))
            if not current_value:
                continue

            # Clean quotes
            if isinstance(current_value, str):
                current_value = current_value.strip('"')

            # Resolve Entity via _field_map (parameters = measures on main table)
            entity, prop = self._resolve_field_entity(param_name)

            # Look up column data type for type-aware literal formatting.
            # Parameters resolve to a measure on the parameter table, so also
            # fall back to measure-type lookup when no column match is found.
            _bim_col_types = getattr(self, '_actual_bim_column_types', {}) or {}
            _bim_meas_types = getattr(self, '_actual_bim_measure_types', {}) or {}
            _p_col_type = (_bim_col_types.get((entity, prop))
                           or _bim_col_types.get((entity, prop.strip()))
                           or _bim_meas_types.get((entity, prop))
                           or _bim_meas_types.get((entity, prop.strip())))

            filter_obj = {
                "name": f"Filter_{uuid.uuid4().hex[:8]}",
                "type": "Categorical",
                "field": {
                    "Column": {
                        "Expression": {"SourceRef": {"Entity": entity}},
                        "Property": prop
                    }
                },
                "filter": {
                    "Version": 2,
                    "From": [{"Name": "p", "Entity": entity, "Type": 0}],
                    "Where": [{
                        "Condition": {
                            "In": {
                                "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "p"}}, "Property": prop}}],
                                "Values": [[{"Literal": {"Value": _pbi_literal(current_value, _p_col_type)}}]]
                            }
                        }
                    }]
                }
            }
            report_filters.append(filter_obj)

        return report_filters

    def _resolve_countd_sort_binding(self, worksheet, reference):
        datasource, field = reference
        bindings = getattr(self, '_countd_measure_bindings', {})
        candidates = set()
        if datasource:
            if not any(source.get('name') == datasource
                       for source in getattr(self, '_datasources_ref', [])):
                return None
            candidates.add(self._resolve_field_entity(field, datasource=datasource))
        else:
            for item in worksheet.get('fields', []):
                if self._clean_field_name(item.get('name', '')) != field:
                    continue
                ds = item.get('datasource', '')
                if ds:
                    candidates.add(self._resolve_field_entity(field, datasource=ds))
            if not candidates:
                renames = getattr(self, '_column_rename_map', {})
                candidates = {
                    pair for pair in bindings
                    if pair[1] == field or renames.get((pair[0], field)) == pair[1]
                }
        candidates = {pair for pair in candidates if pair in bindings}
        if len(candidates) != 1:
            return None
        entity, column = next(iter(candidates))
        return entity, bindings[(entity, column)]

    def _resolve_field_entity(self, field_name, datasource='', source_table=None,
                              source_column=None):
        """Resolves a field name to (entity_table, property_name) via _field_map.

        Args:
            field_name: Raw or cleaned field name.
            datasource: Optional Tableau datasource reference for multi-
                datasource disambiguation.
        """
        clean = field_name.replace('[', '').replace(']', '')
        if not source_table:
            group_binding = getattr(self, '_group_field_map', {}).get(
                (datasource, clean))
            if group_binding:
                return group_binding
            if not datasource and clean in getattr(self, '_ambiguous_group_fields', set()):
                raise ValueError(
                    f"Combined group field {clean!r} requires an unambiguous datasource")
        main = getattr(self, '_main_table', clean)
        collision = getattr(self, '_collision_tables', set())
        source = next((item for item in getattr(self, '_datasources_ref', [])
                       if datasource and item.get('name') == datasource), None)
        if source_table:
            table = source_table.strip('[]')
            entity = getattr(self, '_table_rename_map', {}).get((datasource, table), table)
            column = (source_column or clean).strip('[]')
            selection = source.get('source_selection') if source is not None else None
            if (source is not None and isinstance(selection, dict)
                    and selection.get('policy') == 'prefer_native_database'
                    and selection.get('selected') == 'native_database'):
                local_names = {
                    item['name'].strip('[]')
                    for table_data in source.get('tables', [])
                    if table_data.get('name') == table
                    for item in table_data.get('columns', [])
                    if item.get('name')
                    and (item.get('sourceColumn') or item.get('name')) == column
                }
                if len(local_names) == 1:
                    column = next(iter(local_names))
            prop = getattr(self, '_column_rename_map', {}).get((entity, column), column)
            return entity, prop
        if source is not None:
            rename_map = getattr(self, '_table_rename_map', {})
            table_bindings = [
                (rename_map.get((datasource, table.get('name')), table.get('name')),
                 table)
                for table in source.get('tables', []) if table.get('name')
            ]
            source_tables = {name for name, _ in table_bindings}
            properties = {clean}
            mapped = getattr(self, '_field_map', {}).get(clean)
            if mapped:
                properties.add(mapped[1])
            for calculation in source.get('calculations', []):
                if calculation.get('name', '').strip('[]') == clean:
                    properties.add(calculation.get('caption') or clean)
            measure_renames = getattr(self, '_measure_rename_map', {})
            column_renames = getattr(self, '_column_rename_map', {})
            symbols = getattr(self, '_actual_bim_symbols', set()) or set()
            candidates = set()
            for entity in source_tables:
                for prop in properties:
                    renamed = column_renames.get(
                        (entity, prop), measure_renames.get((entity, prop), prop))
                    if (entity, renamed) in symbols:
                        candidates.add((entity, renamed))
            local_table = (source.get('col_local_name_map') or {}).get(clean)
            if local_table:
                table = local_table.strip('[]')
                entity = rename_map.get((datasource, table), table)
                if entity not in source_tables:
                    logger.warning(
                        "Field %r names a table outside datasource %r: %r",
                        clean, datasource, local_table,
                    )
                    return datasource, clean
                local_candidates = {pair for pair in candidates if pair[0] == entity}
                if len(local_candidates) == 1:
                    return next(iter(local_candidates))
                if not local_candidates:
                    return entity, column_renames.get((entity, clean), clean)
                logger.warning(
                    "Field %r has ambiguous physical lineage in datasource %r",
                    clean, datasource,
                )
                return datasource, clean
            if len(candidates) == 1:
                return next(iter(candidates))
            if len(candidates) > 1:
                connected_tables = {
                    name for name, table in table_bindings
                    if table.get('connection') or table.get('connection_details')
                }
                connected = {candidate for candidate in candidates
                             if candidate[0] in connected_tables}
                if len(connected) == 1:
                    return next(iter(connected))
                logger.warning(
                    "Field %r is ambiguous within datasource %r; "
                    "no foreign table will be selected",
                    clean, datasource,
                )
                return datasource, clean
            if not candidates:
                entity = next(iter(source_tables)) if len(source_tables) == 1 else datasource
                return entity, clean
        if hasattr(self, '_field_map'):
            # Direct match
            if clean in self._field_map:
                entity, prop = self._field_map[clean]
                # Override entity for multi-datasource renamed tables —
                # only when entity is in the collision group (ambiguous)
                if (datasource and entity in collision
                        and hasattr(self, '_ds_table_map')
                        and datasource in self._ds_table_map
                        and self._ds_table_map[datasource] != entity):
                    entity = self._ds_table_map[datasource]
                return (entity, prop)
            # Try without attr:/ prefix
            for prefix in ('attr:', ':'):
                if clean.startswith(prefix) and clean[len(prefix):] in self._field_map:
                    entity, prop = self._field_map[clean[len(prefix):]]
                    if (datasource and entity in collision
                            and hasattr(self, '_ds_table_map')
                            and datasource in self._ds_table_map
                            and self._ds_table_map[datasource] != entity):
                        entity = self._ds_table_map[datasource]
                    return (entity, prop)
            # Partial match (calc ID may contain Calculation_xxx)
            for key, val in self._field_map.items():
                if key == clean or val[1] == clean:
                    return val
        # Fallback: use ds_table_map if datasource known, else main table
        if datasource and hasattr(self, '_ds_table_map') and datasource in self._ds_table_map:
            return (self._ds_table_map[datasource], clean)
        return (main, clean)

    def _normalize_datasource_filters(self, filters):
        """Translate extraction's datasource-filter records to PBIR inputs."""
        normalized = []
        sources = getattr(self, '_datasources_ref', [])
        for item in filters:
            if item.get('field'):
                normalized.append(item)
                continue
            column = item.get('column', '')
            source_label = item.get('datasource', '')
            matches = [
                source for source in sources
                if source_label in (source.get('name'), source.get('caption'))
            ]
            if not column or len(matches) != 1 or not matches[0].get('name'):
                logger.warning(
                    "Datasource filter %r from %r has no unique source binding; "
                    "it is not emitted.",
                    column, source_label,
                )
                continue
            source_name = matches[0]['name']
            matching_prefixes = [
                source['name'] for source in sources
                if source.get('name') and column.startswith(source['name'] + '.')
            ]
            if matching_prefixes:
                prefix = max(matching_prefixes, key=len)
                if prefix != source_name:
                    logger.warning(
                        "Datasource filter %r declares source %r but references %r; "
                        "it is not emitted.",
                        column, source_name, prefix,
                    )
                    continue
                column = column[len(prefix) + 1:]
            column = self._clean_field_name(column.replace('[', '').replace(']', ''))
            minimum, maximum = item.get('range_min'), item.get('range_max')
            is_range = minimum is not None or maximum is not None
            normalized.append({
                'field': column,
                'datasource': source_name,
                'type': 'range' if is_range else 'categorical',
                'values': item.get('values', []),
                'min': minimum,
                'max': maximum,
                'exclude': item.get('filter_type', '').lower() in ('exclude', 'excluded'),
            })
        return normalized

    def _collect_report_scope_filters(self, converted_objects):
        """Return filters whose workbook-wide scope is explicit.

        ``filters`` is a flat extraction list that can include worksheet
        filters, including filters belonging to worksheets omitted from a
        dashboard. Never promote those records to report scope implicitly.
        Datasource filters remain report-wide by their dedicated input list.
        """
        workbook_filters = [
            item for item in converted_objects.get('filters', [])
            if str(item.get('scope', '')).strip().lower()
            in ('workbook', 'workbook-wide', 'report')
        ]
        datasource_filters = self._normalize_datasource_filters(
            converted_objects.get('datasource_filters', []))
        return workbook_filters + datasource_filters

    def _create_visual_filters(self, filters, notes=None):
        """Creates visual-level filters from worksheet filters"""
        visual_filters = []

        # Tableau virtual fields that have no PBI equivalent
        skip_fields = {'Measure Names', 'Measure Values', 'Multiple Values',
                       ':Measure Names', ':Measure Values'}

        for f in filters:
            field = f.get('field', '')
            if not field:
                continue

            # Parse [datasource].[derivation:field:suffix] pattern from global filters
            # (worksheet-level filters are already cleaned by the extractor)
            ds_ref = f.get('datasource', '')
            _col_m = re.findall(r'\[([^\]]+)\]\.\[([^\]]+)\]', field) if '.' in field and '[' in field else None
            if _col_m:
                _ds_part, _field_part = _col_m[0]
                if ds_ref and ds_ref != _ds_part:
                    logger.warning(
                        "Filter %r declares datasource %r but references %r; "
                        "it is not emitted.",
                        field, ds_ref, _ds_part,
                    )
                    continue
                if not ds_ref:
                    ds_ref = _ds_part
                # Detect date-part prefix before stripping
                _date_part_prefix = str(f.get('date_part') or '')
                _dp_match = re.match(
                    r'^(yr|mn|dy|qr|wk|md|mdy|hms|hr|mt|sc|trunc):', _field_part)
                if _dp_match:
                    _date_part_prefix = _dp_match.group(1)
                # Strip derivation prefix (none:, sum:, attr:, etc.) and suffix (:nk, :qk, etc.)
                clean_field = re.sub(
                    r'^(none|sum|avg|count|cnt|countd|min|max|usr|yr|mn|dy|qr|wk|attr|md|mdy|hms|hr|mt|sc|thr|trunc):',
                    '', _field_part)
                clean_field = re.sub(r':(nk|qk|ok|fn|tn)$', '', clean_field)
            else:
                # Clean field name (remove Tableau brackets)
                clean_field = field.replace('[', '').replace(']', '')
                _date_part_prefix = str(f.get('date_part') or '')

            # Skip Tableau virtual fields (no PBI column exists)
            if clean_field in skip_fields or field.replace('[', '').replace(']', '') in skip_fields:
                continue

            # Skip date-part filters (yr:, qr:, etc.) â€” PBI cannot filter
            # a DateTime column with categorical date-part string values
            # Skip "all selected" and action filters (no data filtering needed)
            if f.get('type') == 'all':
                continue
            if clean_field.startswith('Action '):
                continue

            # Resolve Entity (table) and Property (column) via mapping
            known_sources = getattr(self, '_datasources_ref', [])
            if ds_ref and known_sources and not any(
                    source.get('name') == ds_ref for source in known_sources):
                logger.warning(
                    "Filter %r references unknown datasource %r; it is not emitted.",
                    clean_field, ds_ref,
                )
                continue
            entity, prop = self._resolve_field_entity(clean_field, datasource=ds_ref)

            # Skip filters whose target is a measure on a What-If parameter
            # table.  Parameter tables are dimensionless (no relationship to
            # fact tables), so PBI cannot reconcile a parameter-measure
            # filter with the visual's projection set and rejects the query
            # with "primary projections must have set equality with the
            # corresponding subset in the filter target."  These filters
            # come from Tableau worksheets where a calculated field that
            # references a parameter was used as a filter — the resulting
            # filter is semantically a no-op in PBI's data model.
            _param_tables = getattr(self, '_parameter_table_names', set()) or set()
            if entity in _param_tables:
                continue

            # Skip filter if the resolved (entity, prop) pair doesn't exist
            # in the semantic model — prevents PBI Desktop "deleted columns" errors.
            _bim_sym = getattr(self, '_actual_bim_symbols', None) or set()
            if not ds_ref and sum(1 for _, name in _bim_sym if name == prop) > 1:
                logger.warning(
                    "Filter %r has no datasource and matches multiple model tables; "
                    "it is not emitted.",
                    clean_field,
                )
                continue
            if _bim_sym and (entity, prop) not in _bim_sym:
                # Also try with stripped trailing/leading whitespace
                if (entity, prop.strip()) in _bim_sym:
                    prop = prop.strip()
                else:
                    logger.warning(
                        "Filter %r in datasource %r has no verified model field; "
                        "it is not emitted.", clean_field, ds_ref,
                    )
                    continue

            # Determine if the field is a measure (requires Measure wrapper, not Column)
            _bim_measures = getattr(self, '_bim_measure_names', set())
            _measure_symbols = getattr(self, '_actual_bim_measure_symbols', None)
            _is_measure = ((entity, prop) in _measure_symbols
                           if _measure_symbols is not None else prop in _bim_measures)
            _field_kind = "Measure" if _is_measure else "Column"

            # Look up column data type so filter literals can be formatted
            # to match the column's declared type.  Without this, PBI's
            # SQExprValidationVisitor.visitIn crashes with
            # ``e.accept is not a function`` when a string column receives
            # unquoted numeric literals (e.g. Theme="1" emitted as integer 1).
            # The same type-mismatch risk applies to measure-targeted filters
            # (especially parameter measures that return strings like "true").
            _col_type = None
            if not _is_measure:
                _bim_col_types = getattr(self, '_actual_bim_column_types', {}) or {}
                _col_type = (_bim_col_types.get((entity, prop))
                             or _bim_col_types.get((entity, prop.strip())))
            else:
                _bim_meas_types = getattr(self, '_actual_bim_measure_types', {}) or {}
                _col_type = (_bim_meas_types.get((entity, prop))
                             or _bim_meas_types.get((entity, prop.strip())))

            filter_type = f.get('type', 'categorical')
            filter_mode = f.get('filter_mode', '')

            date_part = f.get('date_part') or _date_part_prefix
            if (date_part and filter_type != 'range'
                    and f.get('min') is None):
                values = f.get('values', [])
                intervals = [
                    _date_part_interval(value, date_part) for value in values
                ]
                if (not intervals or any(interval is None for interval in intervals)
                        or str(_col_type or '').lower() not in (
                            'date', 'datetime', 'datetime2', 'date/time')):
                    reason = (
                        f"Date-part In filter for {clean_field!r} was not emitted: "
                        "its values do not identify complete date intervals or the "
                        "bound model column is not verified as a date."
                    )
                    logger.warning(reason)
                    if notes is not None:
                        notes.append(reason)
                    continue

                merged_intervals = []
                for start, end in sorted(set(intervals)):
                    if merged_intervals and merged_intervals[-1][1] == start:
                        merged_intervals[-1] = (merged_intervals[-1][0], end)
                    else:
                        merged_intervals.append((start, end))

                left = {
                    _field_kind: {
                        "Expression": {"SourceRef": {"Source": "t"}},
                        "Property": prop,
                    }
                }

                def interval_condition(interval):
                    start, end = interval
                    return {"And": {
                        "Left": {"Comparison": {
                            "ComparisonKind": 2,  # >= interval start
                            "Left": left,
                            "Right": {"Literal": {"Value": start}},
                        }},
                        "Right": {"Comparison": {
                            "ComparisonKind": 3,  # < next interval boundary
                            "Left": left,
                            "Right": {"Literal": {"Value": end}},
                        }},
                    }}

                condition = interval_condition(merged_intervals[0])
                for interval in merged_intervals[1:]:
                    condition = {"Or": {
                        "Left": condition,
                        "Right": interval_condition(interval),
                    }}
                if f.get('exclude', False):
                    condition = {"Not": {"Expression": condition}}
                where = [{"Condition": condition}]
                if len(merged_intervals) == 1 and not f.get('exclude', False):
                    start, end = merged_intervals[0]
                    where = [
                        {"Condition": {"Comparison": {
                            "ComparisonKind": 2,
                            "Left": left,
                            "Right": {"Literal": {"Value": start}},
                        }}},
                        {"Condition": {"Comparison": {
                            "ComparisonKind": 3,
                            "Left": left,
                            "Right": {"Literal": {"Value": end}},
                        }}},
                    ]
                visual_filters.append({
                    "name": f"Filter_{uuid.uuid4().hex[:8]}",
                    "type": "Advanced",
                    "field": {
                        _field_kind: {
                            "Expression": {"SourceRef": {"Entity": entity}},
                            "Property": prop,
                        }
                    },
                    "filter": {
                        "Version": 2,
                        "From": [{"Name": "t", "Entity": entity, "Type": 0}],
                        "Where": where,
                    },
                })
                continue

            # Sprint 77: Relative date filter
            if filter_mode == 'relative-date':
                period = f.get('period', 'day')
                period_map = {'day': 0, 'week': 1, 'month': 2,
                              'quarter': 3, 'year': 4}
                period_type_map = {'last': 0, 'this': 1, 'next': 2}
                pbi_filter = {
                    "name": f"Filter_{uuid.uuid4().hex[:8]}",
                    "type": "RelativeDate",
                    "field": {
                        "Column": {
                            "Expression": {"SourceRef": {"Entity": entity}},
                            "Property": prop
                        }
                    },
                    "filter": {
                        "Version": 2,
                        "From": [{"Name": "t", "Entity": entity, "Type": 0}],
                        "Where": [{
                            "Condition": {
                                "RelativeDate": {
                                    "Expression": {
                                        "Column": {
                                            "Expression": {"SourceRef": {"Source": "t"}},
                                            "Property": prop
                                        }
                                    },
                                    "TimeUnit": period_map.get(period, 0),
                                    "TimeUnitCount": f.get('period_count', 1),
                                    "RelativeDateFilterType": period_type_map.get(
                                        f.get('period_type', 'last'), 0),
                                    "IncludeToday": True,
                                }
                            }
                        }]
                    }
                }
                visual_filters.append(pbi_filter)
                continue

            # Sprint 77: Context filter â†’ report-level (emit as normal filter
            # with annotation â€” PBI evaluates all filters simultaneously)
            if filter_mode == 'context' and f.get('values'):
                pass  # fall through to categorical handling below

            if filter_type == 'range' or f.get('min') is not None:
                # PBI does not support Advanced/range filters on measures
                # at report or page level — skip them entirely.
                if _is_measure:
                    continue

                # Range filter (dates, numbers)
                pbi_filter = {
                    "name": f"Filter_{uuid.uuid4().hex[:8]}",
                    "type": "Advanced",
                    "field": {
                        _field_kind: {
                            "Expression": {"SourceRef": {"Entity": entity}},
                            "Property": prop
                        }
                    },
                    "filter": {
                        "Version": 2,
                        "From": [{"Name": "t", "Entity": entity, "Type": 0}],
                        "Where": []
                    }
                }
                conditions = []
                if f.get('min') is not None and str(f['min']).strip():
                    conditions.append({
                        "Comparison": {
                            "ComparisonKind": 2,  # >=
                            "Left": {_field_kind: {"Expression": {"SourceRef": {"Source": "t"}}, "Property": prop}},
                            "Right": {"Literal": {"Value": _filter_literal(f['min'], _date_part_prefix, 'min')}}
                        }
                    })
                if f.get('max') is not None and str(f['max']).strip():
                    conditions.append({
                        "Comparison": {
                            "ComparisonKind": 3,  # <=
                            "Left": {_field_kind: {"Expression": {"SourceRef": {"Source": "t"}}, "Property": prop}},
                            "Right": {"Literal": {"Value": _filter_literal(f['max'], _date_part_prefix, 'max')}}
                        }
                    })
                if conditions:
                    pbi_filter["filter"]["Where"] = [{"Condition": c} for c in conditions]
                    visual_filters.append(pbi_filter)
            else:
                # Categorical filter
                values = f.get('values', [])
                is_exclude = f.get('exclude', False)

                # Filter out Tableau's null placeholder ('%null%').  Including
                # it as a string literal in an In expression triggers PBI's
                # visitIn type-mismatch crash for non-string columns and is
                # semantically incorrect (it is a placeholder, not a real
                # category value).  PBI handles nulls implicitly so dropping
                # the value is safe.
                def _is_null_placeholder(v):
                    s = str(v).strip()
                    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
                        s = s[1:-1]
                    return s == '%null%'
                values = [v for v in values if not _is_null_placeholder(v)]

                # Skip categorical filters with no values (empty Where breaks PBI)
                if not values:
                    continue

                # Measures require Advanced type with Comparison conditions —
                # PBI cannot apply Categorical/In filters on measures.
                if _is_measure:
                    # Boolean-valued measure filter — DROP.
                    #
                    # Tableau may filter on a measure that returns a boolean
                    # expression (e.g. ``CALCULATE(COUNT(...) > 0)`` or
                    # ``[col] = [param]``) and set the value to TRUE/FALSE.
                    # An earlier fix attempted to convert this to
                    # ``measure > 0`` / ``measure <= 0``, but that crashes
                    # PBI Desktop's ``SQExprValidationVisitor.visitCompare``
                    # with ``a.accept is not a function`` when the measure's
                    # actual return type is boolean (or string), because PBI
                    # rejects ``boolean > numeric`` and ``string > numeric``
                    # at filter-validation time.
                    #
                    # The measure return-type heuristic in tmdl_generator
                    # cannot reliably detect boolean returns inside
                    # CALCULATE wrappers, so the only safe action is to
                    # drop boolean-valued measure filters universally.
                    # The Tableau filter semantic (``where condition holds``)
                    # is typically already encoded in the measure's own DAX
                    # expression, so removing the filter is a no-op for the
                    # visual's data shape.
                    _measure_lits = [_pbi_literal(v, _col_type) for v in values]
                    _all_bool = bool(_measure_lits) and all(
                        lit in ('true', 'false') for lit in _measure_lits
                    )
                    if _all_bool:
                        logger.info(
                            "Dropping boolean-valued measure filter on "
                            "'%s'.'%s' (values=%s, exclude=%s) — PBI Desktop "
                            "cannot validate boolean/string measure "
                            "comparisons against numeric literals; the "
                            "filter semantic should be encoded in the "
                            "measure DAX expression instead.",
                            entity, prop, _measure_lits, is_exclude
                        )
                        continue

                    pbi_filter = {
                        "name": f"Filter_{uuid.uuid4().hex[:8]}",
                        "type": "Advanced",
                        "field": {
                            "Measure": {
                                "Expression": {"SourceRef": {"Entity": entity}},
                                "Property": prop
                            }
                        },
                        "filter": {
                            "Version": 2,
                            "From": [{"Name": "t", "Entity": entity, "Type": 0}],
                            "Where": []
                        }
                    }
                    # Convert categorical values to Comparison (Equal) conditions
                    comparisons = []
                    for v in values:
                        comparisons.append({
                            "Condition": {
                                "Comparison": {
                                    "ComparisonKind": 0,  # Equal
                                    "Left": {"Measure": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": prop}},
                                    "Right": {"Literal": {"Value": _pbi_literal(v, _col_type)}}
                                }
                            }
                        })
                    if is_exclude:
                        # Negate: wrap each comparison in Not
                        comparisons = [{"Condition": {"Not": {"Expression": c["Condition"]}}} for c in comparisons]
                    pbi_filter["filter"]["Where"] = comparisons
                    visual_filters.append(pbi_filter)
                    continue

                # Boolean column filters are unsupported by PBI Desktop's
                # query engine: both In(boolean) and Comparison==boolean
                # forms trigger SQExprValidationVisitor crashes
                # (visitIn / visitCompare → "a.accept is not a function"),
                # producing a "Broken_Filters" or report-render error.
                # The Tableau semantic ("show rows where boolean is TRUE")
                # is typically already encoded in the boolean column's M
                # expression itself, so the filter is redundant.
                # Drop boolean column filters with a migration note rather
                # than emit a JSON form that PBI cannot render.
                pbi_literals = [_pbi_literal(v, _col_type) for v in values]
                all_boolean = bool(pbi_literals) and all(
                    lit in ('true', 'false') for lit in pbi_literals
                )

                if all_boolean:
                    logger.info(
                        "Skipping boolean column filter on '%s.%s' "
                        "(values=%s, exclude=%s) — PBI Desktop does not "
                        "support boolean-literal column filters; the "
                        "filter semantic should be encoded in the M "
                        "column expression instead.",
                        entity, prop, pbi_literals, is_exclude
                    )
                    continue

                pbi_filter = {
                    "name": f"Filter_{uuid.uuid4().hex[:8]}",
                    "type": "Categorical",
                    "field": {
                        _field_kind: {
                            "Expression": {"SourceRef": {"Entity": entity}},
                            "Property": prop
                        }
                    },
                    "filter": {
                        "Version": 2,
                        "From": [{"Name": "t", "Entity": entity, "Type": 0}],
                        "Where": []
                    }
                }

                condition = {
                    "In": {
                        "Expressions": [{_field_kind: {"Expression": {"SourceRef": {"Source": "t"}}, "Property": prop}}],
                        "Values": [[{"Literal": {"Value": _pbi_literal(v, _col_type)}}] for v in values]
                    }
                }
                if is_exclude:
                    condition = {"Not": {"Expression": condition}}
                pbi_filter["filter"]["Where"].append({"Condition": condition})

                visual_filters.append(pbi_filter)

        # Deduplicate by (Entity, Property) — PBI only allows one filter per
        # field at each level.  Keep the first filter that has actual conditions
        # (non-empty Where) so per-worksheet duplicates are collapsed.
        seen_keys = {}
        deduped = []
        for flt in visual_filters:
            col_info = flt.get('field', {}).get('Column') or flt.get('field', {}).get('Measure', {})
            entity = col_info.get('Expression', {}).get('SourceRef', {}).get('Entity', '')
            prop = col_info.get('Property', '')
            key = (entity, prop)
            if key in seen_keys:
                # Prefer filters with actual conditions over empty ones
                prev_idx = seen_keys[key]
                prev_where = deduped[prev_idx].get('filter', {}).get('Where', [])
                cur_where = flt.get('filter', {}).get('Where', [])
                if not prev_where and cur_where:
                    deduped[prev_idx] = flt  # replace empty with substantive
                continue
            seen_keys[key] = len(deduped)
            deduped.append(flt)

        return deduped

    def _apply_visual_formatting(self, visual_json, ws_name, ws_data):
        """Apply query-bound formatting consistently to dashboard and standalone visuals."""
        visual = visual_json['visual']
        visual_type = visual['visualType']
        state = visual.get('query', {}).get('queryState', {})
        objects = self._build_visual_objects(
            ws_name, ws_data, visual_type, query_state=state)
        if (ws_data and any(
                item.get('adhoc')
                for item in (ws_data.get('clustering') or [])
                if isinstance(item, dict))
                and not any(
                    'Unsupported Tableau adhoc clustering'
                    in item.get('value', '')
                    for item in visual_json.get('annotations', [])
                    if item.get('name') == 'MigrationNote')):
            visual_json.setdefault('annotations', []).append({
                'name': 'MigrationNote',
                'value': (
                    'Unsupported Tableau adhoc clustering: cluster labels and '
                    'assignments were not calculated. Existing supported visual '
                    'fields remain bound; this report is an approximation, not '
                    'a faithful clustering result.'
                ),
            })
        if any('dataBars' in item.get('properties', {})
               for item in objects.get('columnFormatting', [])):
            compatible = {'matrix': 'pivotTable', 'table': 'tableEx'}.get(visual_type)
            if compatible:
                visual['visualType'] = compatible
                visual_json.setdefault('annotations', []).append({
                    'name': 'MigrationNote',
                    'value': (
                        f'Legacy {visual_type} mapped to {compatible} to preserve data bars; '
                        'existing query projections and formatting retained.'
                    ),
                })
        if ws_data:
            unbound = ws_data.pop('_unresolved_data_bar_fields', [])
            if unbound:
                visual_json.setdefault('annotations', []).append({
                    'name': 'MigrationNote',
                    'value': 'Data bars could not bind to projected Values fields; review required: ' +
                             ', '.join(unbound),
                })
            if state:
                self._build_field_format_objects(objects, ws_data, state)
            unresolved = ws_data.pop('_unresolved_reference_lines', [])
            if unresolved:
                reasons = sorted({
                    item.get('_unresolved_reason', 'numeric value was unavailable')
                    for item in unresolved
                })
                visual_json.setdefault('annotations', []).append({
                    'name': 'MigrationNote',
                    'value': (f'{len(unresolved)} reference line(s) could not be '
                              f'represented: {"; ".join(reasons)}.'),
                })
        for key in ('background', 'border', 'padding', 'dropShadow'):
            if key in objects:
                visual['visualContainerObjects'][key] = objects.pop(key)
        visual['objects'] = objects

    def _build_visual_objects(self, ws_name, ws_data, visual_type, query_state=None):
        """Builds visual objects (title, colors, labels, legend, axes).

        Orchestrator that delegates to focused sub-methods.
        """
        objects = {}

        if not ws_data:
            return objects

        formatting = ws_data.get('formatting', {})
        mark_encoding = ws_data.get('mark_encoding', {})

        self._build_label_objects(
            objects, formatting, mark_encoding,
            value_cell=visual_type in ('card', 'multiRowCard'))
        if ws_data.get('_disable_data_labels'):
            labels = objects.setdefault('labels', [{'properties': {}}])
            labels[0].setdefault('properties', {})['show'] = _L('false')
        self._build_legend_objects(objects, mark_encoding, formatting)
        self._build_axis_objects(objects, ws_data, visual_type)
        self._build_visual_styling_objects(
            objects, ws_data, visual_type, formatting, mark_encoding, query_state=query_state)
        self._build_color_encoding_objects(
            objects, ws_data, visual_type, mark_encoding, query_state=query_state)
        self._build_analytics_objects(objects, ws_data, visual_type, formatting)

        if visual_type in ('card', 'multiRowCard', 'tableEx', 'table', 'matrix', 'pivotTable',
                           'pieChart', 'donutChart', 'azureMap', 'map', 'filledMap',
                           'shapeMap', 'treemap', 'gauge', 'kpi'):
            objects.pop('categoryAxis', None)
            objects.pop('valueAxis', None)
        if visual_type == 'funnel':
            value_axis = objects.get('valueAxis')
            if value_axis and all(
                    isinstance(axis, dict)
                    and isinstance(axis.get('properties', {}), dict)
                    and set(axis.get('properties', {})).issubset({'show'})
                    for axis in value_axis):
                objects.pop('valueAxis', None)
        if visual_type == 'card':
            for labels in objects.get('labels', []):
                labels.get('properties', {}).pop('show', None)
        elif visual_type == 'multiRowCard' and 'labels' in objects:
            objects['dataLabels'] = objects.pop('labels')
            for labels in objects['dataLabels']:
                labels.get('properties', {}).pop('show', None)
        elif visual_type in ('tableEx', 'table'):
            objects.pop('legend', None)
            objects.pop('dataPoint', None)
            labels = objects.pop('labels', [])
            if labels:
                properties = labels[0].get('properties', {})
                properties.pop('show', None)
                if 'color' in properties:
                    properties['fontColor'] = properties.pop('color')
                if properties:
                    objects.setdefault('values', [{'properties': {}}])[0]['properties'].update(properties)

        return objects

    # â”€â”€ Visual-object sub-methods (extracted from _build_visual_objects) â”€â”€

    def _build_label_objects(self, objects, formatting, mark_encoding, value_cell=False):
        """Data labels, label color, and font formatting."""
        # Data labels â€” from formatting.mark.mark-labels-show OR mark_encoding.label
        show_labels = False
        mark_fmt = formatting.get('mark', {})
        if isinstance(mark_fmt, dict):
            show_labels = mark_fmt.get('mark-labels-show', '').lower() == 'true'
        if mark_encoding.get('label', {}).get('show'):
            show_labels = True

        if show_labels:
            label_props: dict[str, object] = {
                "show": _L("true")
            }
            # Apply label font size
            label_info = mark_encoding.get('label', {})
            if label_info.get('font_size'):
                label_props["fontSize"] = _L(f"{label_info['font_size']}D")
            if label_info.get('font_family'):
                label_props["fontFamily"] = _L(f"'{label_info['font_family']}'")
            if label_info.get('font_color'):
                label_props["color"] = {
                    "solid": {"color": _L(f"'{label_info['font_color']}'")}
                }
            # Map label position (Tableau â†’ PBI)
            pos_map = {'top': "'OutsideEnd'", 'center': "'InsideCenter'",
                       'bottom': "'InsideBase'", 'left': "'Left'", 'right': "'Right'"}
            if label_info.get('position') and label_info['position'] in pos_map:
                label_props["labelPosition"] = _L(pos_map[label_info['position']])
            objects["labels"] = [{"properties": label_props}]

        # Label color (from formatting.label.color)
        label_fmt = formatting.get('label', {})
        if isinstance(label_fmt, dict) and label_fmt.get('color'):
            if "labels" not in objects:
                objects["labels"] = [{"properties": {}}]
            objects["labels"][0]["properties"]["color"] = {
                "solid": {"color": _L(f"'{label_fmt['color']}'")}
            }

        cell_fmt = formatting.get('cell', {})
        if not isinstance(cell_fmt, dict):
            cell_fmt = {}
        cell_props = {}
        cell_color = cell_fmt.get('font-color') or cell_fmt.get('color')
        if cell_color:
            cell_props['color'] = {'solid': {'color': _L(f"'{cell_color}'")}}
        if cell_fmt.get('font-family'):
            cell_props['fontFamily'] = _L(f"'{cell_fmt['font-family']}'")
        if cell_fmt.get('font-size'):
            try:
                cell_props['fontSize'] = _L(f"{float(cell_fmt['font-size']):g}D")
            except (TypeError, ValueError):
                logger.debug("Could not parse cell font size: %s", cell_fmt['font-size'])
        if cell_props:
            label_props = objects.setdefault('labels', [{'properties': {}}])[0]['properties']
            for key, value in cell_props.items():
                if value_cell:
                    label_props[key] = value
                else:
                    label_props.setdefault(key, value)

        # Font formatting (family + size from extracted formatting)
        font_props = formatting.get('font', {})
        if isinstance(font_props, dict):
            font_family = font_props.get('family', '')
            font_size = font_props.get('size', '')
            if font_family or font_size:
                if "labels" not in objects:
                    objects["labels"] = [{"properties": {}}]
                if font_family and not (value_cell and cell_fmt.get('font-family')):
                    objects["labels"][0]["properties"]["fontFamily"] = _L(f"'{font_family}'")
                if font_size and not (value_cell and cell_fmt.get('font-size')):
                    try:
                        fs_val = int(float(str(font_size).replace('pt', '').replace('px', '').strip()))
                        objects["labels"][0]["properties"]["fontSize"] = _L(f"{fs_val}D")
                    except (ValueError, TypeError):
                        logger.debug("Could not parse label fontSize: %s", font_size)

        # Number format mapping on labels
        fmt_info = formatting.get('number_format', formatting.get('format_string', ''))
        if fmt_info:
            pbi_fmt = self._convert_number_format(fmt_info)
            if pbi_fmt and "labels" in objects:
                objects["labels"][0]["properties"]["labelDisplayUnits"] = _L(f"'{pbi_fmt}'")

    def _build_field_format_objects(self, objects, ws_data, query_state):
        """Apply source numeric formats only to their bound query references."""
        formatting = ws_data.get('formatting', {})
        formats = {}
        value_format = None
        declared = list(formatting.get('field_formats', {}).items())
        cell = formatting.get('cell', {})
        if isinstance(cell, dict) and cell.get('field') and cell.get('text-format'):
            declared.append((cell['field'], cell['text-format']))
        for raw_field, raw_format in declared:
            fmt = self._convert_number_format(raw_format)
            if not fmt or not re.search(r'[0#]', fmt) or not re.fullmatch(
                    r"""[0#.,%‰$€£+\-(); /\\'"]+""", fmt):
                continue
            parts = re.findall(r'\[([^\]]+)\]', raw_field)
            name = self._clean_field_name(parts[-1] if parts else raw_field)
            if name.lstrip(':') in ('Multiple Values', 'Measure Values'):
                value_format = fmt
                continue
            source_match = re.match(r'^\[([^\]]+)\]\.\[', raw_field)
            pair = self._resolve_field_entity(
                name, datasource=source_match.group(1) if source_match else '')
            formats[pair] = fmt

        general = objects.setdefault('general', [])
        numeric_types = {'int64', 'integer', 'double', 'decimal', 'number', 'real', 'float'}
        for role_name, role in query_state.items():
            for projection in role.get('projections', []):
                expression = projection.get('field', {})
                aggregated = 'Aggregation' in expression
                if aggregated:
                    expression = expression['Aggregation'].get('Expression', {})
                field = expression.get('Measure', expression.get('Column', {}))
                pair = (field.get('Expression', {}).get('SourceRef', {}).get('Entity'),
                        field.get('Property'))
                fmt = formats.get(pair)
                dtype = str((getattr(self, '_actual_bim_column_types', {}) or {}).get(
                    pair, '')).lower()
                if (not fmt and role_name in ('Y', 'Y2', 'Values', 'Indicator', 'Value', 'X', 'Size')
                        and ('Measure' in expression or aggregated or dtype in numeric_types)):
                    fmt = value_format
                ref = projection.get('queryRef')
                if fmt and ref and not any(
                        item.get('selector', {}).get('metadata') == ref for item in general):
                    general.append({
                        'properties': {'formatString': _L("'" + fmt.replace("'", "''") + "'")},
                        'selector': {'metadata': ref},
                    })
        default = self._convert_number_format(
            formatting.get('number_format', formatting.get('format_string', '')))
        if default:
            general.insert(0, {'properties': {
                'formatString': _L("'" + default.replace("'", "''") + "'")}})
        if not general:
            objects.pop('general', None)

    def _build_legend_objects(self, objects, mark_encoding, formatting):
        """Legend configuration from color encoding."""
        color_field = mark_encoding.get('color', {}).get('field', '')
        if color_field and color_field != 'Multiple Values':
            legend_props = {
                "show": _L("true"),
            }
            # Extract legend position from formatting
            legend_fmt = formatting.get('legend', formatting.get('color-legend', {}))
            if isinstance(legend_fmt, dict):
                legend_pos = legend_fmt.get('position', legend_fmt.get('legend-position', ''))
                legend_pos_map = {
                    'right': "'Right'", 'left': "'Left'",
                    'top': "'Top'", 'bottom': "'Bottom'",
                    'top-right': "'TopRight'", 'bottom-right': "'BottomRight'",
                    'top-left': "'TopLeft'", 'bottom-left': "'BottomLeft'",
                }
                if legend_pos.lower() in legend_pos_map:
                    legend_props["position"] = _L(legend_pos_map[legend_pos.lower()])
                else:
                    legend_props["position"] = _L("'Right'")
                # Legend title
                legend_title = legend_fmt.get('title', '')
                if legend_title:
                    legend_props["titleText"] = _L(f"'{legend_title}'")
                    legend_props["showTitle"] = _L("true")
                # Legend font size
                legend_font_size = legend_fmt.get('font-size', '')
                if legend_font_size:
                    legend_props["fontSize"] = _L(f"{legend_font_size}D")
            else:
                legend_props["position"] = _L("'Right'")
            objects["legend"] = [{"properties": legend_props}]

    def _build_axis_objects(self, objects, ws_data, visual_type):
        """Axis display, explicit axes, dual axis, enhanced config, continuous/discrete, sync."""
        formatting = ws_data.get('formatting', {})

        # Axis display (formatting.axis.display)
        axis_fmt = formatting.get('axis', {})
        if isinstance(axis_fmt, dict):
            axis_display = axis_fmt.get('display', 'true')
            show_axis = axis_display.lower() != 'none' if axis_display else True
            if show_axis:
                objects["categoryAxis"] = [{
                    "properties": {
                        "show": _L("true")
                    }
                }]
                objects["valueAxis"] = [{
                    "properties": {
                        "show": _L("true")
                    }
                }]

        # Explicit axes (if extracted)
        axes_data = ws_data.get('axes')
        if axes_data is None or (isinstance(axes_data, list) and not axes_data):
            axes_data = {}
        if not isinstance(axes_data, dict):
            raise ValueError("Worksheet axes must be a mapping or an empty list")
        if axes_data:
            x_axis = axes_data.get('x', {})
            if x_axis:
                cat_props = {
                    "show": _L("true")
                }
                if x_axis.get('title'):
                    cat_props["titleText"] = _L(f"'{x_axis['title']}'")
                    cat_props["showAxisTitle"] = _L("true")
                if x_axis.get('reversed'):
                    cat_props["reverseOrder"] = _L("true")
                objects["categoryAxis"] = [{"properties": cat_props}]

            y_axis = axes_data.get('y', {})
            val_props = {}
            if y_axis:
                val_props = {
                    "show": _L("true")
                }
                if y_axis.get('title'):
                    val_props["titleText"] = _L(f"'{y_axis['title']}'")
                    val_props["showAxisTitle"] = _L("true")
                # Apply axis range (min/max)
                if not y_axis.get('auto_range', True):
                    if y_axis.get('range_min') is not None:
                        val_props["start"] = _L(f"{y_axis['range_min']}D")
                    if y_axis.get('range_max') is not None:
                        val_props["end"] = _L(f"{y_axis['range_max']}D")
                # Apply log scale
                if y_axis.get('scale') == 'log':
                    val_props["axisScale"] = _L("'Log'")
                # Apply reversed axis
                if y_axis.get('reversed'):
                    val_props["reverseOrder"] = _L("true")
                objects["valueAxis"] = [{"properties": val_props}]

            # Dual-axis / combo-chart secondary axis
            if axes_data.get('dual_axis') and visual_type in ('lineClusteredColumnComboChart', 'lineStackedColumnComboChart'):
                y2_props = {
                    "show": _L("true")
                }
                # If synced, set same scale properties
                if axes_data.get('dual_axis_sync'):
                    if not y_axis.get('auto_range', True):
                        if y_axis.get('range_min') is not None:
                            y2_props["start"] = val_props.get("start", {})
                        if y_axis.get('range_max') is not None:
                            y2_props["end"] = val_props.get("end", {})
                    if y_axis.get('scale') == 'log':
                        y2_props["axisScale"] = _L("'Log'")
                objects["y1AxisReferenceLine"] = [{"properties": {}}]  # Marker for combo secondary axis

        # Enhanced axis config (label rotation, show toggles)
        axes_detail = axes_data
        if axes_detail:
            for axis_key, axis_obj_key in [('x', 'categoryAxis'), ('y', 'valueAxis')]:
                ax = axes_detail.get(axis_key, {})
                if not ax:
                    continue
                if axis_obj_key in objects:
                    props = objects[axis_obj_key][0].get("properties", {})
                else:
                    props = {"show": _L("true")}
                if ax.get('show_title') is False:
                    props["showAxisTitle"] = _L("false")
                if ax.get('show_label') is False:
                    props["show"] = _L("false")
                if ax.get('label_rotation'):
                    try:
                        rot = int(float(ax['label_rotation']))
                        if rot != 0:
                            props["labelAngle"] = _L(f"{rot}L")
                    except (ValueError, TypeError):
                        logger.debug("Could not parse axis label_rotation: %s", ax.get('label_rotation'))
                if ax.get('format'):
                    props["labelDisplayUnits"] = _L("'0L'")
                objects[axis_obj_key] = [{"properties": props}]

        # Continuous vs discrete axis scale
        for axis_key, axis_obj_key in [('x', 'categoryAxis'), ('y', 'valueAxis')]:
            ax = axes_detail.get(axis_key, {})
            if ax.get('is_continuous') is True:
                if axis_obj_key in objects:
                    objects[axis_obj_key][0]["properties"]["axisType"] = _L("'Continuous'")
            elif ax.get('is_continuous') is False and axis_obj_key in objects:
                objects[axis_obj_key][0]["properties"]["axisType"] = _L("'Categorical'")

        if ws_data.get('_discrete_month_binding'):
            category_axis = objects.setdefault(
                'categoryAxis', [{'properties': {'show': _L('true')}}])
            category_axis[0].setdefault('properties', {})[
                'axisType'] = _L("'Categorical'")
        elif ws_data.get('_continuous_month_binding'):
            category_axis = objects.setdefault(
                'categoryAxis', [{'properties': {'show': _L('true')}}])
            category_axis[0].setdefault('properties', {})[
                'axisType'] = _L("'Continuous'")

        # Dual-axis synchronization (secShow / secAxisLabel)
        dual_axis = ws_data.get('dual_axis', {})
        if isinstance(dual_axis, dict) and dual_axis.get('enabled'):
            if "valueAxis" not in objects:
                objects["valueAxis"] = [{"properties": {"show": _L("true")}}]
            if dual_axis.get('synchronized'):
                objects["valueAxis"][0]["properties"]["secShow"] = _L("true")
                objects["valueAxis"][0]["properties"]["secAxisLabel"] = _L("true")

    def _build_visual_styling_objects(self, objects, ws_data, visual_type, formatting, mark_encoding,
                                      query_state=None):
        """Background, table formatting, data bars, totals, padding."""
        color_enc = mark_encoding.get('color', {})

        # Background color
        bg_color = formatting.get('background_color', '')
        if not bg_color and isinstance(formatting.get('pane', {}), dict):
            bg_color = formatting.get('pane', {}).get('background-color', '')
        if bg_color:
            objects["background"] = [{
                "properties": {
                    "color": {
                        "solid": {"color": _L(f"'{bg_color}'")}
                    }
                }
            }]

        # Table/matrix-specific formatting (header font, row banding, grid)
        if visual_type in ('tableEx', 'table', 'matrix'):
            header_style = formatting.get('header_style', formatting.get('column-header_style', {}))
            if isinstance(header_style, dict):
                col_headers_props = {}
                if header_style.get('font-size'):
                    col_headers_props["fontSize"] = _L(f"{header_style['font-size']}D")
                if header_style.get('font-weight') == 'bold':
                    col_headers_props["bold"] = _L("true")
                if header_style.get('font-color'):
                    col_headers_props["fontColor"] = {
                        "solid": {"color": _L(f"'{header_style['font-color']}'")}
                    }
                if col_headers_props:
                    objects["columnHeaders"] = [{"properties": col_headers_props}]

            # Row banding (alternating row colors)
            row_style = formatting.get('worksheet_style', {})
            if isinstance(row_style, dict) and row_style.get('band-color'):
                objects["values"] = [{
                    "properties": {
                        "backColor": {
                            "solid": {"color": _L(f"'{row_style['band-color']}'")}
                        }
                    }
                }]

            # Grid/border
            if isinstance(header_style, dict) and header_style.get('border-style', 'none') != 'none':
                grid_props: dict[str, object] = {"show": _L("true")}
                if header_style.get('border-color'):
                    grid_props["color"] = {
                        "solid": {"color": _L(f"'{header_style['border-color']}'")}
                    }
                objects["gridlines"] = [{"properties": grid_props}]

        # Data bars for table/matrix columns
        if visual_type in ('tableEx', 'matrix', 'pivotTable', 'table'):
            value_fields = [f for f in ws_data.get('fields', [])
                            if f.get('role') == 'measure']
            if value_fields and color_enc.get('type') == 'quantitative':
                color_field = color_enc.get('field')
                if isinstance(color_field, str) and color_field.strip():
                    color_name = self._clean_field_name(color_field).casefold()
                    value_fields = [
                        field for field in value_fields
                        if isinstance(field.get('name'), str)
                        and self._clean_field_name(field['name']).casefold() == color_name
                    ]
                    if not value_fields:
                        ws_data.setdefault('_unresolved_data_bar_fields', []).append(color_field)
                from visual_generator import _build_data_bar_config
                data_bar_rules = []
                seen_refs = set()
                for value_field in value_fields:
                    field_name = value_field.get('name', '')
                    if not isinstance(field_name, str) or not field_name:
                        continue
                    resolved = self._make_projection_entry(value_field)
                    projections = [resolved] if resolved else []
                    if query_state is not None:
                        projections = [
                            projection for projection in query_state.get('Values', {}).get('projections', [])
                            if resolved and projection.get('queryRef')
                            and projection.get('field') == resolved.get('field')
                        ]
                    if not projections:
                        unbound = ws_data.setdefault('_unresolved_data_bar_fields', [])
                        if field_name not in unbound:
                            unbound.append(field_name)
                    for projection in projections:
                        query_ref = projection.get('queryRef')
                        if not query_ref or query_ref in seen_refs:
                            continue
                        seen_refs.add(query_ref)
                        data_bar_rules.append(_build_data_bar_config(query_ref))
                if data_bar_rules:
                    objects.setdefault('columnFormatting', []).extend(data_bar_rules)

            # Default row banding fallback
            if "values" not in objects:
                objects["values"] = [{
                    "properties": {
                        "backColor": {"solid": {"color": _L("'#F2F2F2'")}}
                    }
                }]

            # Totals and subtotals
            totals = ws_data.get('totals', {})
            if totals and (totals.get('grand_totals') or totals.get('subtotals')):
                objects.setdefault("total", [{"properties": {}}])
                objects["total"][0]["properties"]["totals"] = _L("true")
                if totals.get('subtotals'):
                    objects.setdefault("subTotals", [{"properties": {}}])
                    objects["subTotals"][0]["properties"]["rowSubtotals"] = _L("true")

        # Per-object padding
        padding = ws_data.get('padding', {})
        if isinstance(padding, dict) and padding:
            pad_props = {}
            for side in ('top', 'bottom', 'left', 'right'):
                val = padding.get(f'padding_{side}', padding.get(f'margin_{side}', 0))
                if val:
                    pad_props[side] = _L(f"{val}L")
            if pad_props:
                objects["padding"] = [{"properties": pad_props}]

    def _build_quantitative_fill(self, color_enc, palette_colors):
        """Tableau colours a continuous measure by gradient; PBI expresses that
        as a FillRule driven by the same measure."""
        entry = None
        field_name = color_enc.get('field', '')
        if field_name:
            entry = self._make_projection_entry({'name': field_name})
        if not entry:
            return None
        low = palette_colors[0] if palette_colors else '#F2F2F2'
        high = palette_colors[-1] if len(palette_colors) >= 2 else '#4472C4'
        gradient = {"linearGradient2": {
            "min": {"color": {"Literal": {"Value": f"'{low}'"}}},
            "max": {"color": {"Literal": {"Value": f"'{high}'"}}},
        }}
        rule = {"FillRule": {"Input": entry["field"], "FillRule": gradient}}
        return {"solid": {"color": {"expr": rule}}}

    def _build_color_encoding_objects(self, objects, ws_data, visual_type, mark_encoding,
                                      query_state=None):
        """Conditional formatting gradient, per-value colors, stepped thresholds."""
        color_enc = mark_encoding.get('color', {})
        color_mode = color_enc.get('type', '')  # 'quantitative' â†’ gradient, 'categorical' â†’ distinct
        if color_mode == 'quantitative':
            # Data-driven color scale
            palette_colors = color_enc.get('palette_colors', [])
            gradient_fill = self._build_quantitative_fill(color_enc, palette_colors)
            if gradient_fill is not None:
                objects["dataPoint"] = [{"properties": {"fill": gradient_fill}}]
            elif palette_colors:
                objects["dataPoint"] = [{
                    "properties": {
                        "fill": {
                            "solid": {"color": _L(f"'{palette_colors[0]}'")}
                        }
                    }
                }]

        # Per-value color assignments (categorical color map)
        if not objects.get("dataPoint"):
            from visual_generator import build_categorical_color_objects
            dp_rules = build_categorical_color_objects(
                color_enc, query_state or {}, resolve_field=self._resolve_field_entity,
                clean_field=self._clean_field_name)
            if dp_rules:
                objects["dataPoint"] = dp_rules

        # Stepped color thresholds (discrete conditional formatting)
        # Produces PBI rules-based conditional formatting with numeric thresholds
        if not objects.get("dataPoint"):
            color_thresholds = color_enc.get('thresholds', [])
            if color_thresholds and len(color_thresholds) >= 2:
                # Sort thresholds by value for proper rule ordering
                sorted_thresh = sorted(
                    [t for t in color_thresholds if t.get('value') is not None],
                    key=lambda t: float(t['value']),
                )
                # Add thresholds without values (catch-all) at end
                no_value = [t for t in color_thresholds if t.get('value') is None]
                all_thresh = sorted_thresh + no_value

                # Build rules-based conditional formatting
                cf_rules = []
                for idx, thresh in enumerate(all_thresh):
                    color = thresh.get('color', '#cccccc')
                    rule_props: dict[str, object] = {
                        "fill": {"solid": {"color": _L(f"'{color}'")}}
                    }
                    if thresh.get('value') is not None:
                        rule_props["inputValue"] = _L(f"{thresh['value']}D")
                        # Add comparison operator for stepped ranges
                        if idx < len(sorted_thresh) - 1:
                            rule_props["operator"] = _L("'LessThanOrEqual'")
                        else:
                            rule_props["operator"] = _L("'GreaterThan'")
                    cf_rules.append({"properties": rule_props})
                objects["dataPoint"] = cf_rules

                # Also set up the conditionalFormatting array for the visual
                color_field = color_enc.get('field', '')
                if color_field and cf_rules:
                    objects["conditionalFormatting"] = [{
                        "properties": {
                            "show": _L("true"),
                            "colorStyle": _L("'rulesGradient'"),
                        }
                    }]

    def _reference_line_shelf(self, ws_data, axis_column):
        """Resolve an axis-column without borrowing a different datasource's field."""
        def identity(value):
            text = str(value or '').strip()
            parts = re.findall(r'\[([^\]]+)\]', text)
            if parts and re.fullmatch(r'\[[^\]]+\](?:\.\[[^\]]+\])*', text):
                return (parts[0] if len(parts) > 1 else '',
                        self._clean_field_name(parts[-1]))
            return '', self._clean_field_name(text)

        axis_source, axis_field = identity(axis_column)
        matches = []
        for field in ws_data.get('fields', []):
            embedded_source, field_name = identity(field.get('name', ''))
            if field_name != axis_field:
                continue
            source = str(field.get('datasource') or '').strip()
            if source.startswith('[') and source.endswith(']'):
                source = source[1:-1]
            if source and embedded_source and source != embedded_source:
                return None, (f'axis-column {axis_field!r} has ambiguous '
                              'datasource identity in the worksheet field')
            matches.append((source or embedded_source, field.get('shelf')))

        # Marks-card copies do not define an axis when an axis-shelf instance
        # exists. They must not make an otherwise unique axis ambiguous.
        axis_matches = [match for match in matches if match[1] in ('rows', 'columns')]
        if axis_matches:
            matches = axis_matches
        if not matches:
            return None, (f'axis-column {axis_field!r} does not resolve to one '
                          'worksheet shelf')

        sources = {source for source, _ in matches if source}
        unknown = [match for match in matches if not match[0]]
        if axis_source:
            qualified = [match for match in matches if match[0] == axis_source]
            if qualified and not unknown:
                matches = qualified
            elif sources and not qualified and not unknown:
                return None, (f'axis-column {axis_field!r} datasource {axis_source!r} '
                              f'does not match worksheet datasource(s) '
                              f'{", ".join(sorted(sources))}')
            elif sources and unknown:
                return None, (f'axis-column {axis_field!r} has ambiguous '
                              f'datasource identity for {axis_source!r}')
        elif len(sources) > 1 or (sources and unknown):
            return None, (f'axis-column {axis_field!r} has ambiguous '
                          'datasource identity across worksheet fields')

        shelves = {shelf for _, shelf in matches}
        if len(shelves) != 1:
            return None, (f'axis-column {axis_field!r} does not resolve to one '
                          'worksheet shelf')
        # A single unqualified legacy field can still identify its shelf;
        # multiple unidentified instances on that shelf cannot prove identity.
        if not sources and len(matches) > 1:
            return None, (f'axis-column {axis_field!r} has ambiguous '
                          'datasource identity across worksheet fields')
        shelf = next(iter(shelves))
        if shelf not in ('rows', 'columns'):
            return None, f'axis-column {axis_field!r} is not on rows or columns'
        return shelf, None

    def _build_analytics_objects(self, objects, ws_data, visual_type, formatting):
        """Reference lines, trend lines, annotations, forecast, map, analytics stats, small multiples."""
        main_table = getattr(self, '_main_table', 'Table')

        # Reference lines (bands are omitted unless their representation is proved).
        ref_lines = ws_data.get('reference_lines', [])
        if ref_lines:
            y_ref_lines = []
            x_ref_lines = []
            dynamic_ref_lines = []
            for ref in ref_lines:
                ref_value = ref.get('value')
                ref_label = ref.get('label', '')
                ref_color = ref.get('color', '#666666')
                ref_style = ref.get('style', 'dashed')
                kind = str(ref.get('type') or '').strip().lower()
                values = ref.get('values')
                if (ref.get('is_band') is True
                        or kind in ('band', 'reference-band', 'reference_band',
                                    'distribution-band', 'distribution_band')
                        or ('value_from' in ref and 'value_to' in ref)
                        or (isinstance(values, (list, tuple)) and len(values) >= 2)):
                    ws_data.setdefault('_unresolved_reference_lines', []).append({
                        **ref,
                        '_unresolved_reason': (
                            'reference band is not supported; its bounds and '
                            'shading were omitted rather than emitted as a single line'),
                    })
                    continue
                ref_type = str(ref.get('computation') or kind or 'constant').strip().lower()
                if ref_type == 'mean':
                    ref_type = 'average'

                # Resolve Tableau's axis-column against worksheet shelves;
                # never guess the owning axis from the visual type.
                axis_column = ref.get('axis_column') or ref.get('axis-column')
                source_attributes = ref.get('source_attributes')
                if isinstance(source_attributes, dict):
                    axis = str(source_attributes.get('axis', ref.get('axis', '')) or '').strip().lower()
                    # Extraction defaults an absent axis to y. Only a source
                    # axis attribute is an explicit declaration of that y.
                    axis_explicit = 'axis' in source_attributes or axis not in ('', 'y')
                else:
                    axis = str(ref.get('axis', '') or '').strip().lower()
                    axis_explicit = bool(axis)
                if axis_column:
                    shelf, reason = self._reference_line_shelf(ws_data, axis_column)
                    if reason:
                        ws_data.setdefault('_unresolved_reference_lines', []).append({
                            **ref, '_unresolved_reason': reason,
                        })
                        continue
                    resolved_axis = 'x' if shelf == 'columns' else 'y'
                    if not axis_explicit:
                        declared_axis = resolved_axis
                    elif axis in ('x', 'column', 'columns', 'horizontal'):
                        declared_axis = 'x'
                    elif axis in ('y', 'row', 'rows', 'vertical'):
                        declared_axis = 'y'
                    else:
                        ws_data.setdefault('_unresolved_reference_lines', []).append({
                            **ref,
                            '_unresolved_reason': f'unsupported axis orientation {axis!r}',
                        })
                        continue
                    if declared_axis != resolved_axis:
                        ws_data.setdefault('_unresolved_reference_lines', []).append({
                            **ref,
                            '_unresolved_reason': (
                                f'axis-column {str(axis_column)!r} resolves to the '
                                f'{resolved_axis.upper()} shelf but declares {axis!r}'),
                        })
                        continue
                    axis = resolved_axis
                if axis in ('x', 'column', 'columns', 'horizontal'):
                    if not axis_column:
                        ws_data.setdefault('_unresolved_reference_lines', []).append({
                            **ref,
                            '_unresolved_reason': (
                                'X-axis reference line has no resolved axis-column; '
                                'line was omitted'),
                        })
                        continue
                    target_axis = 'x'
                elif axis in ('', 'y', 'row', 'rows', 'vertical'):
                    target_axis = 'y'
                else:
                    ws_data.setdefault('_unresolved_reference_lines', []).append({
                        **ref,
                        '_unresolved_reason': f'unsupported axis orientation {axis!r}',
                    })
                    continue

                if ref_type in ('average', 'median', 'percentile', 'min', 'max'):
                    if target_axis == 'x':
                        ws_data.setdefault('_unresolved_reference_lines', []).append({
                            **ref,
                            '_unresolved_reason': (
                                'dynamic X-axis reference lines are not supported; '
                                'line was omitted'),
                        })
                        continue
                    # Dynamic reference line (analytics pane)
                    from visual_generator import _build_dynamic_reference_line
                    dyn_line = _build_dynamic_reference_line(
                        ref_type=ref_type,
                        field_name=ref.get('field'),
                        table_name=main_table,
                        label=ref_label,
                        color=ref_color,
                        style=ref_style,
                    )
                    if dyn_line:
                        dynamic_ref_lines.append(dyn_line)
                else:
                    # Constant reference line
                    try:
                        numeric_value = float(ref_value)
                        if not math.isfinite(numeric_value):
                            raise ValueError('reference-line value is not finite')
                    except (ValueError, TypeError):
                        ws_data.setdefault('_unresolved_reference_lines', []).append({
                            **ref,
                            '_unresolved_reason': (
                                'reference-line value is missing or ambiguous; '
                                'line was omitted'),
                        })
                        continue
                    line_def = {
                        "value": _L(f"{numeric_value}D"),
                        "show": _L("true"),
                        "displayName": _L("'" + str(ref_label).replace("'", "''") + "'"),
                        "lineColor": {"solid": {"color": _L(f"'{ref_color}'")}},
                        "style": _L(f"'{ref_style}'") if ref_style in ('solid', 'dashed', 'dotted') else _L("'dashed'"),
                    }
                    (x_ref_lines if target_axis == 'x' else y_ref_lines).append(line_def)

            if y_ref_lines:
                objects.setdefault('y1AxisReferenceLine', []).extend(
                    {'properties': line, 'selector': {'id': str(index)}}
                    for index, line in enumerate(y_ref_lines))
            if x_ref_lines:
                objects.setdefault('xAxisReferenceLine', []).extend(
                    {'properties': line, 'selector': {'id': str(index)}}
                    for index, line in enumerate(x_ref_lines))
            if dynamic_ref_lines:
                if "valueAxis" not in objects:
                    objects["valueAxis"] = [{"properties": {"show": _L("true")}}]
                objects["valueAxis"][0]["properties"]["referenceLine"] = dynamic_ref_lines

        # Trend lines (analytics pane) â€” Sprint 123: full regression type config
        trend_lines = ws_data.get('trend_lines', [])
        if trend_lines:
            trend_objs = []
            for tl in trend_lines:
                trend_type = tl.get('type', 'linear').capitalize()
                if trend_type not in ('Linear', 'Exponential', 'Logarithmic',
                                      'Polynomial', 'Power', 'MovingAverage'):
                    trend_type = 'Linear'
                trend_obj = {
                    "show": _L("true"),
                    "lineColor": {"solid": {"color": _L(f"'{tl.get('color', '#666666')}'")}}
                }
                trend_obj["regressionType"] = _L(f"'{trend_type}'")
                if trend_type == 'Polynomial':
                    order = tl.get('order', tl.get('degree', 2))
                    trend_obj["polynomialOrder"] = _L(f"{order}L")
                if tl.get('show_equation'):
                    trend_obj["displayEquation"] = _L("true")
                if tl.get('show_r_squared'):
                    trend_obj["displayRSquared"] = _L("true")
                if tl.get('show_confidence'):
                    trend_obj["confidenceBand"] = _L("true")
                trend_objs.append({"properties": trend_obj})
            objects["trend"] = trend_objs

        # Clustering â†’ MigrationNote (no native PBI clustering in PBIR)
        clustering = ws_data.get('clustering', [])
        if clustering:
            cl = clustering[0]
            num_clusters = cl.get('num_clusters', 'auto')
            variables = cl.get('variables', [])
            hint = f"Tableau clustering ({num_clusters} clusters"
            if variables:
                hint += f", fields: {', '.join(variables[:5])}"
            hint += "). Use R/Python visual with k-means clustering in Power BI."
            objects.setdefault("subTitle", [{"properties": {}}])
            objects["subTitle"][0]["properties"]["show"] = _L("true")
            existing = objects["subTitle"][0]["properties"].get("text")
            if existing:
                hint = existing.get("expr", {}).get("Literal", {}).get("Value", "''").strip("'") + " | " + hint
            objects["subTitle"][0]["properties"]["text"] = _L(json.dumps(hint))

        # Annotations â†’ subtitle text
        annotations = ws_data.get('annotations', [])
        if annotations and not clustering:
            anno_texts = [a.get('text', '') for a in annotations if a.get('text')]
            if anno_texts:
                subtitle_text = "; ".join(anno_texts[:3])
                objects.setdefault("subTitle", [{"properties": {}}])
                objects["subTitle"][0]["properties"]["show"] = _L("true")
                objects["subTitle"][0]["properties"]["text"] = _L(json.dumps(subtitle_text))

        # Forecast config (analytics pane) â€” Sprint 123: seasonality + model
        forecasts = ws_data.get('forecasting', [])
        if forecasts:
            fc = forecasts[0]
            forecast_obj = {
                "show": _L("true"),
                "forecastLength": _L(f"{fc.get('periods', 5)}L"),
                "confidenceBandStyle": _L("'fill'"),
            }
            ci = fc.get('prediction_interval', '95')
            forecast_obj["confidenceLevel"] = _L(f"'{ci}'")
            if fc.get('ignore_last', '0') != '0':
                forecast_obj["ignoreLast"] = _L(f"{fc['ignore_last']}L")
            if fc.get('periods_back', 0):
                forecast_obj["forecastBackLength"] = _L(f"{fc['periods_back']}L")
            model = fc.get('model', 'automatic')
            model_map = {'automatic': "'Auto'", 'additive': "'Additive'",
                         'multiplicative': "'Multiplicative'",
                         'ets_aaa': "'Auto'", 'ets_mmm': "'Multiplicative'"}
            forecast_obj["seasonality"] = _L(model_map.get(model.lower(), "'Auto'"))
            if not fc.get('show_prediction_bands', True):
                forecast_obj["confidenceBandStyle"] = _L("'none'")
            objects["forecast"] = [{"properties": forecast_obj}]

        # Map options (washout/transparency + style + zoom/center)
        map_opts = ws_data.get('map_options', {})
        if map_opts and visual_type in ('map', 'filledMap', 'azureMap'):
            map_props = {}
            washout = map_opts.get('washout', '0.0')
            try:
                wo_val = float(washout)
                if wo_val > 0 and visual_type != 'azureMap':
                    map_props["transparency"] = _L(f"{int(wo_val * 100)}L")
            except (ValueError, TypeError):
                logger.debug("Could not parse map washout: %s", washout)
            style = map_opts.get('style', 'road')
            style_map = {'normal': "'road'", 'light': "'grayscale'",
                         'dark': "'darkGrayscale'", 'satellite': "'aerial'",
                         'streets': "'road'"}
            pbi_style = style_map.get(style.lower(), "'road'")
            if visual_type == 'azureMap':
                azure_styles = {"'road'": "'road'", "'grayscale'": "'grayscale_light'",
                                "'darkGrayscale'": "'grayscale_dark'", "'aerial'": "'satellite'"}
                map_props['defaultStyle'] = _L(azure_styles.get(pbi_style, "'road'"))
            else:
                map_props["mapStyle"] = _L(pbi_style)
            # Zoom level
            zoom_level = map_opts.get('zoom_level')
            if zoom_level is not None:
                map_props["autoZoom"] = _L("false")
                map_props['zoom' if visual_type == 'azureMap' else 'zoomLevel'] = _L(f"{zoom_level}L")
            # Center coordinates
            center_lat = map_opts.get('center_lat')
            center_lon = map_opts.get('center_lon')
            if center_lat is not None and center_lon is not None:
                map_props['centerLatitude' if visual_type == 'azureMap' else 'latitude'] = _L(f"{center_lat}D")
                map_props['centerLongitude' if visual_type == 'azureMap' else 'longitude'] = _L(f"{center_lon}D")
            if map_props:
                objects['mapControls' if visual_type == 'azureMap' else 'mapControl'] = [{"properties": map_props}]

        # Reference bands, statistical lines, confidence intervals (analytics_stats) â€” Sprint 123
        analytics_stats = ws_data.get('analytics_stats', [])
        for stat in analytics_stats:
            if stat.get('type') == 'stat_reference' and ref_lines:
                # The extractor also records each <reference-line> here, but
                # this copy lacks axis, datasource and band identity. The
                # authoritative reference_lines path above has already either
                # emitted it or recorded why it was omitted.
                continue
            if stat.get('type') == 'distribution_band':
                band_from = stat.get('value_from', '')
                band_to = stat.get('value_to', '')
                computation = stat.get('computation', '').lower()
                if "valueAxis" not in objects:
                    objects["valueAxis"] = [{"properties": {"show": _L("true")}}]
                objects["valueAxis"][0]["properties"].setdefault("referenceLine", [])
                if computation in ('standard deviation', 'std_dev', 'stddev'):
                    # Standard deviation band â†’ percentile line pair
                    objects["valueAxis"][0]["properties"]["referenceLine"].append({
                        "type": "Band",
                        "lowerBound": str(band_from) if band_from else "-1",
                        "upperBound": str(band_to) if band_to else "1",
                        "transparency": _L("60L"),
                        "show": _L("true"),
                        "displayName": _L("'Std Dev Band'"),
                        "style": _L("'dashed'"),
                    })
                elif computation in ('percentile', 'quantile', 'iqr'):
                    # Percentile band (e.g. IQR: 25thâ€“75th)
                    objects["valueAxis"][0]["properties"]["referenceLine"].append({
                        "type": "Band",
                        "lowerBound": str(band_from) if band_from else "25",
                        "upperBound": str(band_to) if band_to else "75",
                        "transparency": _L("50L"),
                        "show": _L("true"),
                        "displayName": _L(f"'Percentile {band_from}-{band_to}'"),
                        "style": _L("'dashed'"),
                    })
                elif band_from and band_to:
                    objects["valueAxis"][0]["properties"]["referenceLine"].append({
                        "type": "Band",
                        "lowerBound": str(band_from),
                        "upperBound": str(band_to),
                        "transparency": _L("50L"),
                        "show": _L("true"),
                    })
            elif stat.get('type') == 'confidence_interval':
                ci_level = stat.get('level', '95')
                if "valueAxis" not in objects:
                    objects["valueAxis"] = [{"properties": {"show": _L("true")}}]
                objects["valueAxis"][0]["properties"].setdefault("referenceLine", [])
                objects["valueAxis"][0]["properties"]["referenceLine"].append({
                    "type": "Band",
                    "transparency": _L("70L"),
                    "show": _L("true"),
                    "displayName": _L(f"'{ci_level}% CI'"),
                    "style": _L("'dotted'"),
                })
            elif stat.get('type') in ('stat_line', 'stat_reference'):
                comp = stat.get('computation', stat.get('stat', ''))
                stat_map = {'mean': 'Average', 'median': 'Median',
                            'constant': 'Constant', 'percentile': 'Percentile',
                            'mode': 'Average', 'min': 'Min', 'max': 'Max'}
                stat_type = stat_map.get(comp.lower(), 'Average')
                if "valueAxis" not in objects:
                    objects["valueAxis"] = [{"properties": {"show": _L("true")}}]
                objects["valueAxis"][0]["properties"].setdefault("referenceLine", [])
                ref_entry = {
                    "type": stat_type,
                    "show": _L("true"),
                    "style": _L("'dashed'"),
                }
                if stat_type == 'Percentile' and stat.get('value'):
                    ref_entry["percentile"] = _L(f"{stat['value']}D")
                if stat_type == 'Constant' and stat.get('value'):
                    ref_entry["value"] = str(stat['value'])
                objects["valueAxis"][0]["properties"]["referenceLine"].append(ref_entry)

        # Small multiples formatting
        sm_field = ws_data.get('small_multiples', '')
        if not sm_field:
            pages_shelf = ws_data.get('pages_shelf', {})
            if isinstance(pages_shelf, dict):
                sm_field = pages_shelf.get('field', '')
        if sm_field:
            objects["smallMultiple"] = [{"properties": {
                "layoutMode": _L("'Flow'"),
                "showChartTitle": _L("true"),
            }}]

    def _create_slicer_visual(self, visual_id, x, y, w, h, field_name, table_name, z_order,
                               slicer_mode='Dropdown', title=None):
        """Creates a slicer visual for a filter/parameter control with field binding.

        Args:
            slicer_mode: PBI slicer mode string â€” ``'Dropdown'``, ``'List'``,
                ``'Between'`` (range/slider), ``'Basic'`` (relative date),
                ``'Date'`` (date picker), or ``'Search'`` (wildcard text).
            title: Display title for the slicer. Defaults to field_name.
        """
        clean_field = field_name.replace('[', '').replace(']', '')
        clean_table = table_name if table_name else getattr(self, '_main_table', 'Table')

        # Map extended modes to PBI mode strings
        pbi_mode = slicer_mode
        if slicer_mode == 'Search':
            pbi_mode = 'Dropdown'  # Dropdown with search enabled
        elif slicer_mode == 'Date':
            pbi_mode = 'Basic'  # Basic = date picker in PBI

        # Build objects with the correct mode
        slicer_objects = {
            "data": [{
                "properties": {
                    "mode": _L(f"'{pbi_mode}'")
                }
            }],
            # Hide the slicer's built-in header — the visual title already
            # shows the field name, so having both creates a duplicate label.
            "header": [{
                "properties": {
                    "show": _L("false")
                }
            }]
        }

        # Keep slicer objects minimal for PBIR compatibility across Desktop builds.
        # Extra blocks such as numericInputStyle/search/selection/relativeDate
        # can trigger client-side rendering errors in some versions.

        slicer = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json",
            "name": visual_id,
            "position": {
                "x": x, "y": y, "z": z_order * 1000,
                "height": max(h, 48) if pbi_mode == 'Dropdown' else h, "width": w,
                "tabOrder": z_order * 1000
            },
            "visual": {
                "visualType": "slicer",
                "objects": slicer_objects,
                "visualContainerObjects": {
                    "title": [{
                        "properties": {
                            "show": _L("true"),
                            "text": _L(f"'{title or clean_field or clean_table}'")
                        }
                    }]
                },
                "drillFilterOtherVisuals": True
            }
        }

        # Add query binding (PBIR queryState format with RoleProjection)
        # Only emit if the (entity, prop) pair exists in the semantic model.
        _bim_sym = getattr(self, '_actual_bim_symbols', None) or set()
        if clean_field and clean_table:
            # Validate field exists in model
            emit_query = True
            if _bim_sym and (clean_table, clean_field) not in _bim_sym:
                if (clean_table, clean_field.strip()) in _bim_sym:
                    clean_field = clean_field.strip()
                else:
                    emit_query = False
            if emit_query:
                slicer["visual"]["query"] = {
                    "queryState": {
                        "Values": {
                            "projections": [{
                                "field": {
                                    "Column": {
                                        "Expression": {"SourceRef": {"Entity": clean_table}},
                                        "Property": clean_field
                                    }
                                },
                                "queryRef": f"{clean_table}.{clean_field}",
                                "nativeQueryRef": clean_field,
                            }]
                        }
                    }
                }

        return slicer

    def _detect_slicer_mode(self, obj, column_name, converted_objects, *, resolved_pair=None):
        """Detect the best PBI slicer mode for a Tableau filter control.

        Returns one of: ``'Dropdown'``, ``'List'``, ``'Between'``, ``'Basic'``,
            ``'Date'``, ``'Search'``.
        """
        param_ref = obj.get('param', '')

        # Sprint 77: Check filter_mode from classified filter data
        filter_mode = obj.get('filter_mode', '')
        if filter_mode == 'relative-date':
            return 'Basic'
        if filter_mode == 'wildcard':
            return 'Search'
        if filter_mode == 'top-n':
            return 'List'
        if filter_mode == 'range':
            return 'Between'

        pair = resolved_pair or self._resolve_field_entity(column_name)
        dtype = str((getattr(self, '_actual_bim_column_types', {}) or {}).get(pair, '')).lower()
        if dtype in ('date', 'datetime'):
            return 'Between' if ':qk]' in param_ref else 'Date'

        # Check if this is a range parameter â†’ slider (Between)
        for param in converted_objects.get('parameters', []):
            p_name = param.get('name', '').replace('[', '').replace(']', '')
            if p_name and (p_name in param_ref or p_name == column_name):
                if param.get('domain_type') == 'range':
                    return 'Between'
                if param.get('domain_type') == 'list':
                    return 'List'

        # Check column data type across datasources
        col_lower = column_name.lower()
        value_count = 0
        for ds in converted_objects.get('datasources', []):
            for table in ds.get('tables', []):
                for col in table.get('columns', []):
                    name = (col.get('caption', '') or col.get('name', '')).lower()
                    if name == col_lower:
                        dtype = col.get('datatype', '').lower()
                        if dtype in ('date', 'datetime'):
                            return 'Between' if ':qk]' in param_ref else 'Date'
                        if dtype in ('integer', 'real', 'float', 'number'):
                            return 'Between'
                        # Track cardinality hint
                        card = col.get('cardinality', 0)
                        if card:
                            value_count = card

        # High cardinality (>20) â†’ Dropdown, low â†’ List
        if value_count and value_count <= 20:
            return 'List'

        # Default to Dropdown for categorical text fields
        return 'Dropdown'

    def _create_page_navigator(self, visuals_dir, page_width, page_height, z_index):
        """Create a pageNavigator visual (Tableau dashboard tab strip equivalent).

        Places a horizontal page navigator bar at the bottom of the page.
        """
        visual_id = uuid.uuid4().hex[:8]
        visual_dir = os.path.join(visuals_dir, visual_id)
        os.makedirs(visual_dir, exist_ok=True)

        nav_height = 40
        visual_json = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json",
            "name": visual_id,
            "position": {
                "x": 0,
                "y": page_height - nav_height,
                "z": z_index * 1000,
                "height": nav_height,
                "width": page_width,
                "tabOrder": z_index * 1000,
            },
            "visual": {
                "visualType": "pageNavigator",
                "objects": {
                    "content": [{
                        "properties": {
                            "navigationStyle": {"expr": {"Literal": {"Value": "'Tabs'"}}},
                            "showTooltips": {"expr": {"Literal": {"Value": "true"}}},
                        }
                    }]
                }
            },
        }
        _write_json(os.path.join(visual_dir, 'visual.json'), visual_json)

    def _create_pages_shelf_slicer(self, visuals_dir, pages_shelf, scale_x, scale_y,
                                    visual_count, converted_objects):
        """Create an animation-hint slicer from Tableau Pages shelf.

        In Tableau the Pages shelf allows playback through dimension values;
        Power BI has no direct equivalent.  We create a standard slicer bound
        to the same field and annotate it with a comment so that the user
        knows it originated from a Pages shelf.
        """
        field = pages_shelf.get('field', '')
        if not field:
            return
        table_name = self._find_column_table(field, converted_objects)
        visual_id = uuid.uuid4().hex[:8]
        visual_dir = os.path.join(visuals_dir, visual_id)
        os.makedirs(visual_dir, exist_ok=True)
        slicer = self._create_slicer_visual(
            visual_id, 10, 10, 400, 50, field, table_name, visual_count)
        slicer.setdefault('visual', {}).setdefault('objects', {})
        slicer['visual']['objects']['general'] = [{
            'properties': {
                'comments': _L("'Pages Shelf / Play Axis: animate through values'")
            }
        }]
        _write_json(os.path.join(visual_dir, 'visual.json'), slicer)

    @staticmethod
    def _convert_number_format(tableau_format):
        """Convert Tableau number format string to PBI display units / format.

        Common Tableau patterns::

            ###,###    â†’ #,0
            $#,#00.00  â†’ $#,0.00
            0.0%       â†’ 0.0%
            0.00       â†’ 0.00
        """
        if not tableau_format or not isinstance(tableau_format, str):
            return ''
        fmt = tableau_format.strip()
        if re.match(r'^[pn][0#]', fmt):
            fmt = fmt[1:]
        # Already a PBI-compatible format
        if fmt in ('0', '0.0', '0.00', '#,0', '#,0.0', '#,0.00',
                   '0%', '0.0%', '0.00%'):
            return fmt
        # Currency
        if '$' in fmt:
            return fmt.replace('#,#', '#,0').replace('##', '#0')
        # Percentage
        if '%' in fmt:
            return fmt
        # Thousands separator
        if ',' in fmt:
            return fmt.replace('#,#', '#,0')
        return fmt

    def _create_drillthrough_pages(self, pages_dir, page_names, worksheets,
                                    converted_objects):
        """Create drill-through pages from Tableau filter/set actions.

        Inspects actions for ``filter`` or ``set-value`` types that target
        specific worksheets.  Each unique target becomes a PBI drill-through
        page with ``pageType: "Drillthrough"`` and a drillthrough filter on
        the source field.
        """
        actions = converted_objects.get('actions', [])
        if not actions:
            return

        # Collect unique target worksheets from filter/set actions
        drillthrough_targets = {}  # target_ws_name â†’ source_field
        for action in actions:
            a_type = action.get('type', '')
            if a_type not in ('filter', 'set-value'):
                continue
            target_sheets = action.get('target_worksheets', [])
            if not target_sheets:
                target = action.get('target_worksheet', '')
                if target:
                    target_sheets = [target]
            source_field = action.get('field', action.get('source_field', ''))

            for ts in target_sheets:
                # Skip if the target is already a dashboard page (not drill-through)
                if ts not in drillthrough_targets:
                    drillthrough_targets[ts] = source_field

        if not drillthrough_targets:
            return

        for target_ws, source_field in drillthrough_targets.items():
            ws_data = self._find_worksheet(worksheets, target_ws)
            if not ws_data:
                continue

            dt_page_name = f"Drillthrough_{uuid.uuid4().hex[:8]}"
            dt_display = f"Drillthrough - {target_ws}"
            page_names.append(dt_page_name)

            dt_dir = os.path.join(pages_dir, dt_page_name)
            os.makedirs(dt_dir, exist_ok=True)

            dt_page = {
                "$schema": SCHEMA_PAGE,
                "name": dt_page_name,
                "displayName": dt_display,
                "displayOption": "FitToPage",
                "height": 720,
                "width": 1280,
                "pageType": "Drillthrough"
            }

            # Add drill-through filter if source field is known
            if source_field:
                clean_field = source_field.replace('[', '').replace(']', '')
                table_name = self._find_column_table(clean_field, converted_objects)
                if table_name:
                    dt_page["drillthrough"] = {
                        "filters": [{
                            "name": f"Filter_{clean_field}",
                            "field": {
                                "Column": {
                                    "Expression": {"SourceRef": {"Entity": table_name}},
                                    "Property": clean_field
                                }
                            },
                            "type": "Categorical"
                        }]
                    }

            _write_json(os.path.join(dt_dir, 'page.json'), dt_page)

            # Create visuals on the drill-through page
            dt_visuals_dir = os.path.join(dt_dir, 'visuals')
            os.makedirs(dt_visuals_dir, exist_ok=True)
            self._create_visual_worksheet(
                dt_visuals_dir, ws_data,
                {'type': 'worksheetReference', 'worksheetName': target_ws,
                 'position': {'x': 0, 'y': 0, 'w': 1280, 'h': 720}},
                1.0, 1.0, 0, worksheets, converted_objects
            )
            print(f"  [Drillthrough] page '{dt_display}' created")

    def _find_column_table(self, column_name, converted_objects):
        """Finds the table containing a given column"""
        # Prefer _field_map (already applies table renames)
        if hasattr(self, '_field_map') and column_name in self._field_map:
            return self._field_map[column_name][0]
        rename_map = getattr(self, '_table_rename_map', {})
        datasources = converted_objects.get('datasources', [])
        for ds in datasources:
            ds_name = ds.get('name', '')
            for table in ds.get('tables', []):
                tname = table.get('name', '')
                for col in table.get('columns', []):
                    col_caption = col.get('caption', col.get('name', ''))
                    if col_caption == column_name or col.get('name', '') == column_name:
                        return rename_map.get((ds_name, tname), tname)
                # Also search in calculations
                for calc in ds.get('calculations', []):
                    calc_caption = calc.get('caption', '')
                    if calc_caption == column_name:
                        # The calculation is in the main table of this datasource
                        tables = ds.get('tables', [])
                        if tables:
                            t0 = tables[0].get('name', '')
                            return rename_map.get((ds_name, t0), t0)
        return ''

    def _detect_script_visual(self, ws_data, converted_objects):
        """Check if a worksheet uses SCRIPT_* analytics extensions.

        Scans the worksheet's fields against the project's calculation
        formulas.  Returns the first ``script_info`` dict (from
        ``detect_script_functions``) if a SCRIPT_* call is found, else
        ``None``.
        """
        if not ws_data:
            return None

        # Gather all raw formulas for this worksheet's fields
        field_names = set()
        for f in ws_data.get('fields', []):
            fname = self._clean_field_name(f.get('name', ''))
            if fname:
                field_names.add(fname)

        # Search calculations for SCRIPT_* usage
        try:
            from dax_converter import detect_script_functions
        except ImportError:
            return None

        if field_names:
            calcs = converted_objects.get('calculations', [])
            for calc in calcs:
                calc_name = calc.get('caption', calc.get('name', '')).strip('[]')
                raw_formula = calc.get('formula', '')
                if not raw_formula:
                    continue
                # Check if this calculation is used in the worksheet
                if calc_name in field_names or calc.get('name', '').strip('[]') in field_names:
                    scripts = detect_script_functions(raw_formula)
                    if scripts:
                        return scripts[0]

        # Also check fields directly for embedded SCRIPT_* in mark_encoding
        mark_enc = ws_data.get('mark_encoding', {})
        for enc_key, enc_val in mark_enc.items():
            if isinstance(enc_val, dict):
                formula = enc_val.get('formula', '')
                if formula:
                    scripts = detect_script_functions(formula)
                    if scripts:
                        return scripts[0]

        return None

    def _find_worksheet(self, worksheets, name):
        """Finds a worksheet by name"""
        for ws in worksheets:
            if ws.get('name') == name:
                return ws
        return None

    def _generate_automation_artifacts(self, project_dir, report_name, converted_objects):
        """Generate post-migration automation artifacts (RLS script, credential template)."""
        try:
            from powerbi_import.permission_mapper import (
                generate_credential_template,
                generate_rls_powershell,
            )
        except ImportError:
            return

        # RLS PowerShell script â€” only if user_filters / RLS roles exist
        user_filters = converted_objects.get('user_filters', [])
        if user_filters:
            rls_path = os.path.join(project_dir, 'assign_rls_roles.ps1')
            roles = []
            for uf in user_filters:
                roles.append({
                    'name': uf.get('role_name', uf.get('name', 'DefaultRole')),
                    'members': uf.get('users', uf.get('members', [])),
                })
            result = generate_rls_powershell(roles, rls_path, dataset_name=report_name)
            if result:
                print(f"  âœ“ RLS PowerShell script: {rls_path}")

        # Credential template â€” always generate if datasources have connections
        datasources = converted_objects.get('datasources', [])
        if datasources:
            cred_path = os.path.join(project_dir, 'credentials_template.json')
            result = generate_credential_template(datasources, cred_path)
            if result:
                print(f"  âœ“ Credential template: {cred_path}")

    def create_metadata(self, project_dir, report_name, converted_objects):
        """Creates migration metadata file for documentation."""
        # Count visuals and pages from the generated report
        pages_count = 0
        visuals_count = 0
        report_def = os.path.join(project_dir, f"{report_name}.Report", "definition", "pages")
        if os.path.isdir(report_def):
            for entry in os.listdir(report_def):
                entry_path = os.path.join(report_def, entry)
                # Only count pages that have page.json (skip stale leftovers)
                if (os.path.isdir(entry_path) and entry.startswith('ReportSection')
                        and os.path.isfile(os.path.join(entry_path, 'page.json'))):
                    pages_count += 1
                    vis_dir = os.path.join(entry_path, 'visuals')
                    if os.path.isdir(vis_dir):
                        # Only count visual dirs that have visual.json
                        visuals_count += len([d for d in os.listdir(vis_dir)
                                              if os.path.isdir(os.path.join(vis_dir, d))
                                              and os.path.isfile(os.path.join(vis_dir, d, 'visual.json'))])

        # Check for theme
        theme_applied = os.path.exists(os.path.join(
            project_dir, f"{report_name}.Report", "definition",
            "RegisteredResources", "TableauMigrationTheme.json"
        ))

        # Read TMDL stats
        tmdl_stats = {}
        tables_dir = os.path.join(project_dir, f"{report_name}.SemanticModel",
                                  "definition", "tables")
        if os.path.isdir(tables_dir):
            tmdl_stats['tables'] = len([f for f in os.listdir(tables_dir) if f.endswith('.tmdl')])

        # Count measures and relationships from TMDL files
        measures_count = 0
        columns_count = 0
        if os.path.isdir(tables_dir):
            for tmdl_file in os.listdir(tables_dir):
                if tmdl_file.endswith('.tmdl'):
                    try:
                        with open(os.path.join(tables_dir, tmdl_file), 'r', encoding='utf-8') as f:
                            content = f.read()
                        measures_count += content.count('\n\tmeasure ')
                        columns_count += content.count('\n\tcolumn ')
                    except (IOError, OSError) as exc:
                        logger.warning("Could not read TMDL file %s for stats: %s", tmdl_file, exc)
        tmdl_stats['measures'] = measures_count
        tmdl_stats['columns'] = columns_count

        fallback_partitions = []
        self_healed_tables = []
        if os.path.isdir(tables_dir):
            for tmdl_file in os.listdir(tables_dir):
                if not tmdl_file.endswith('.tmdl'):
                    continue
                try:
                    with open(os.path.join(tables_dir, tmdl_file), 'r', encoding='utf-8') as f:
                        text = f.read()
                except OSError:
                    continue
                if 'otherwise' in text and '#table(' in text:
                    fallback_partitions.append(tmdl_file[:-5])
                if 'Self-heal: placeholder column created' in text:
                    self_healed_tables.append(tmdl_file[:-5])

        relationships_count = 0
        rels_path = os.path.join(project_dir, f"{report_name}.SemanticModel",
                                 "definition", "relationships.tmdl")
        if os.path.isfile(rels_path):
            try:
                with open(rels_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                relationships_count = content.count('\nrelationship ')
            except (IOError, OSError) as exc:
                logger.warning("Could not read relationships.tmdl for stats: %s", exc)
        tmdl_stats['relationships'] = relationships_count

        # Collect visual type mappings used (Tableau mark â†’ PBI visual)
        visual_types_used = {}
        visual_details = []  # per-worksheet detail for HTML report
        for ws in converted_objects.get('worksheets', []):
            ws_name = ws.get('name', 'Unknown')
            mark = ws.get('mark_type', 'Automatic')
            pbi_type = ws.get('chart_type', 'clusteredBarChart')
            visual_types_used[ws_name] = mark

            # Collect field names and classify as dim/measure
            ws_fields = ws.get('fields', [])
            dim_names = []
            mea_names = []
            dax_mea_names = []
            for f in ws_fields:
                fname = f.get('name', '')
                # Strip Tableau derivation prefixes
                fname = _RE_DERIVATION_PREFIX.sub('', fname)
                if not fname or fname in (':Measure Names', 'Measure Names',
                                          ':Measure Values', 'Measure Values',
                                          'Multiple Values',
                                          'Longitude (generated)', 'Latitude (generated)'):
                    continue
                if f.get('shelf') == 'measure_value' or (
                    hasattr(self, '_measure_names') and fname in self._measure_names
                ):
                    mea_names.append(fname)
                    if hasattr(self, '_bim_measure_names') and fname in self._bim_measure_names:
                        dax_mea_names.append(fname)
                else:
                    dim_names.append(fname)
            visual_details.append({
                'worksheet': ws_name,
                'tableau_mark': mark,
                'pbi_visual': pbi_type,
                'dimensions': dim_names,
                'measures': mea_names,
                'dax_measures': dax_mea_names,
                'field_count': len(dim_names) + len(mea_names),
            })

        # Collect approximation warnings from visual generation
        from powerbi_import.visual_generator import get_approximation_note
        approximations = []
        for ws_name, mark in visual_types_used.items():
            note = get_approximation_note(mark)
            if note:
                approximations.append({"worksheet": ws_name, "source_type": mark, "note": note})

        # Theme detail
        theme_detail = {}
        if theme_applied:
            theme_detail['status'] = 'applied'
        else:
            has_colors = any(
                d.get('formatting', {}).get('background_color')
                for d in converted_objects.get('dashboards', [])
            )
            theme_detail['status'] = 'skipped'
            theme_detail['reason'] = 'no dashboard colors detected' if not has_colors else 'generation error'

        metadata = {
            "generated_at": datetime.now().isoformat(),
            "source": "Tableau Migration",
            "report_name": report_name,
            "objects_converted": {
                "worksheets": len(converted_objects.get('worksheets', [])),
                "dashboards": len(converted_objects.get('dashboards', [])),
                "datasources": len(converted_objects.get('datasources', [])),
                "calculations": len(converted_objects.get('calculations', [])),
                "parameters": len(converted_objects.get('parameters', [])),
                "filters": len(converted_objects.get('filters', [])),
                "stories": len(converted_objects.get('stories', [])),
                "sets": len(converted_objects.get('sets', [])),
                "groups": len(converted_objects.get('groups', [])),
                "bins": len(converted_objects.get('bins', [])),
                "hierarchies": len(converted_objects.get('hierarchies', [])),
                "user_filters": len(converted_objects.get('user_filters', [])),
                "actions": len(converted_objects.get('actions', [])),
                "custom_sql": len(converted_objects.get('custom_sql', []))
            },
            "generated_output": {
                "pages": pages_count,
                "visuals": visuals_count,
                "theme_applied": theme_applied,
                "theme_detail": theme_detail
            },
            "tmdl_stats": tmdl_stats,
            "degradations": {
                "fallback_partitions": sorted(fallback_partitions),
                "self_healed_tables": sorted(self_healed_tables),
                "hyper_unread": sorted(
                    item.get("filename", "")
                    for item in converted_objects.get("hyper_files", [])
                    if not item.get("hyper_reader_tables")
                ),
            },
            "hyper_extracts": [
                {
                    "filename": item.get("filename", ""),
                    "format": item.get("hyper_reader_format", "unknown"),
                    "row_count": item.get("actual_row_count", 0),
                    "status": "read" if item.get("hyper_reader_tables") else "not_read",
                }
                for item in converted_objects.get("hyper_files", [])
            ],
            "dax_measure_names": sorted(getattr(self, '_bim_measure_names', set())),
            "visual_type_mappings": visual_types_used,
            "visual_details": visual_details,
            "approximations": approximations,
        }
        metadata_file = os.path.join(project_dir, 'migration_metadata.json')
        _write_json(metadata_file, metadata)

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # Paginated Report Generation
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

    def _create_paginated_report(self, project_dir, report_name, converted_objects):
        """Generate a paginated report layout (RDL-style) for print-oriented output.

        Creates a ``PaginatedReport/`` directory inside the project with:
        - ``report.json``: Paginated report definition with fixed page dimensions
        - ``pages/``: One ``pageN.json`` per worksheet with tabular layout
        - ``header.json`` / ``footer.json``: Placeholder header/footer definitions

        Paginated reports use fixed positioning (inches/cm) rather than
        responsive layout.  This is a starting point that users can refine
        in Power BI Report Builder.
        """
        pag_dir = os.path.join(project_dir, 'PaginatedReport')
        pages_dir = os.path.join(pag_dir, 'pages')
        os.makedirs(pages_dir, exist_ok=True)

        # --- Report-level definition ---
        report_def = {
            "$schema": "paginated-report/1.0.0",
            "name": f"{report_name}_Paginated",
            "description": f"Paginated report generated from Tableau workbook '{report_name}'",
            "pageWidth": "8.5in",
            "pageHeight": "11in",
            "marginTop": "0.5in",
            "marginBottom": "0.5in",
            "marginLeft": "0.75in",
            "marginRight": "0.75in",
            "orientation": "Portrait",
            "pageCount": 0,
            "dataSource": report_name,
        }

        # --- Header / Footer ---
        header_def = {
            "height": "0.75in",
            "items": [
                {
                    "type": "textbox",
                    "value": report_name,
                    "style": {"fontSize": "14pt", "fontWeight": "bold"},
                    "position": {"left": "0in", "top": "0.1in", "width": "5in", "height": "0.4in"},
                },
                {
                    "type": "textbox",
                    "value": "=Globals!ExecutionTime",
                    "style": {"fontSize": "8pt", "textAlign": "right"},
                    "position": {"left": "5in", "top": "0.1in", "width": "2in", "height": "0.3in"},
                },
            ],
        }
        footer_def = {
            "height": "0.5in",
            "items": [
                {
                    "type": "textbox",
                    "value": "=Globals!PageNumber & \" of \" & Globals!TotalPages",
                    "style": {"fontSize": "8pt", "textAlign": "center"},
                    "position": {"left": "2.5in", "top": "0.05in", "width": "2in", "height": "0.3in"},
                },
            ],
        }

        _write_json(os.path.join(pag_dir, 'header.json'), header_def)
        _write_json(os.path.join(pag_dir, 'footer.json'), footer_def)

        # --- One page per worksheet showing a table of the worksheet's fields ---
        worksheets = converted_objects.get('worksheets', [])
        page_num = 0
        for ws in worksheets:
            ws_name = ws.get('name', f'Sheet{page_num + 1}')
            fields = ws.get('fields', [])
            if not fields:
                continue
            page_num += 1

            # Build column definitions from fields
            col_width = min(2.0, 7.0 / max(len(fields), 1))
            columns = []
            for idx, field in enumerate(fields):
                fname = field if isinstance(field, str) else field.get('name', f'Column{idx}')
                columns.append({
                    "name": fname,
                    "width": f"{col_width:.2f}in",
                    "header": fname,
                    "style": {"fontSize": "9pt"},
                })

            page_def = {
                "pageNumber": page_num,
                "name": ws_name,
                "body": {
                    "height": "9in",
                    "items": [
                        {
                            "type": "tablix",
                            "name": f"Table_{ws_name}",
                            "position": {"left": "0in", "top": "0in",
                                         "width": f"{min(col_width * len(fields), 7.0):.2f}in",
                                         "height": "2in"},
                            "columns": columns,
                            "dataSetName": ws_name,
                            "headerRow": True,
                            "repeatHeaderOnNewPage": True,
                        }
                    ],
                },
            }
            _write_json(os.path.join(pages_dir, f'page{page_num}.json'), page_def)

        report_def['pageCount'] = page_num
        _write_json(os.path.join(pag_dir, 'report.json'), report_def)

        return pag_dir
