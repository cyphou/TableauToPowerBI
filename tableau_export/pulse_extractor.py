"""Tableau Pulse metric extractor.

Parses Tableau Pulse metric definitions from workbook XML (2024+).
Pulse metrics define KPIs with time grains, targets, and filters
that map to Power BI Goals/Scorecards.

Usage:
    metrics = extract_pulse_metrics(root)
    # Returns list of metric dicts ready for goals_generator
"""

import xml.etree.ElementTree as ET
import logging
try:
    from .safe_xml import safe_find, safe_findall, safe_findtext, safe_get_attr
except ImportError:
    from safe_xml import safe_find, safe_findall, safe_findtext, safe_get_attr

logger = logging.getLogger(__name__)

# Tableau Pulse time grain → PBI cadence mapping
_TIME_GRAIN_MAP = {
    'day': 'Daily',
    'week': 'Weekly',
    'month': 'Monthly',
    'quarter': 'Quarterly',
    'year': 'Yearly',
}


def extract_pulse_metrics(root):
    """Extract Tableau Pulse metric definitions from workbook XML.

    Pulse metrics appear in Tableau 2024+ workbooks as
    ``<metric>`` or ``<pulse-metric>`` elements.

    Args:
        root: ElementTree root of the .twb XML

    Returns:
        list of dicts with keys:
            name, description, measure_field, time_dimension,
            time_grain, aggregation, target_value, target_label,
            filters (list of {field, operator, values}),
            definition_formula, number_format
    """
    if root is None:
        return []

    metrics = []

    # Search for <metric> and <pulse-metric> elements
    metric_elements = (
        safe_findall(root, './/metric') +
        safe_findall(root, './/pulse-metric') +
        safe_findall(root, './/metrics/metric')
    )

    seen_names = set()
    for elem in metric_elements:
        metric = _parse_metric_element(elem)
        if metric and metric['name'] not in seen_names:
            seen_names.add(metric['name'])
            metrics.append(metric)

    if metrics:
        logger.info("Extracted %d Pulse metrics", len(metrics))

    return metrics


def _parse_metric_element(elem):
    """Parse a single ``<metric>`` or ``<pulse-metric>`` XML element.

    Args:
        elem: ElementTree element

    Returns:
        dict or None if the element doesn't represent a valid metric
    """
    name = (
        safe_get_attr(elem, 'name', '') or
        safe_get_attr(elem, 'caption', '') or
        safe_findtext(elem, './/name', '') or
        safe_findtext(elem, './/caption', '')
    ).strip()

    if not name:
        return None

    description = (
        safe_get_attr(elem, 'description', '') or
        safe_findtext(elem, './/description', '')
    ).strip()

    # Measure/KPI field
    measure_field = (
        safe_get_attr(elem, 'measure', '') or
        safe_get_attr(elem, 'column', '') or
        safe_findtext(elem, './/measure', '') or
        safe_findtext(elem, './/measure-field', '')
    ).strip().strip('[]')

    # Time dimension
    time_dim = (
        safe_get_attr(elem, 'time-dimension', '') or
        safe_findtext(elem, './/time-dimension', '') or
        safe_findtext(elem, './/date-column', '')
    ).strip().strip('[]')

    # Time grain
    time_grain_raw = (
        safe_get_attr(elem, 'time-grain', '') or
        safe_get_attr(elem, 'granularity', '') or
        safe_findtext(elem, './/time-grain', '') or
        safe_findtext(elem, './/granularity', '')
    ).strip().lower()
    time_grain = _TIME_GRAIN_MAP.get(time_grain_raw, 'Monthly')

    # Aggregation
    aggregation = (
        safe_get_attr(elem, 'aggregation', '') or
        safe_findtext(elem, './/aggregation', '')
    ).strip().upper() or 'SUM'

    # Target
    target_value = None
    target_label = ''
    target_elem = safe_find(elem, './/target')
    if target_elem is not None:
        target_label = safe_get_attr(target_elem, 'label', '') or safe_findtext(target_elem, './/label', '')
        raw_val = safe_get_attr(target_elem, 'value', '') or target_elem.text or ''
        try:
            target_value = float(raw_val) if raw_val else None
        except (ValueError, TypeError):
            target_value = None

    # Definition formula (Tableau calculation)
    definition_formula = (
        safe_get_attr(elem, 'formula', '') or
        safe_findtext(elem, './/formula', '') or
        safe_findtext(elem, './/definition', '')
    ).strip()

    # Number format
    number_format = (
        safe_get_attr(elem, 'number-format', '') or
        safe_findtext(elem, './/number-format', '')
    ).strip()

    # Filters
    filters = []
    for filt_elem in safe_findall(elem, './/filter'):
        field = (safe_get_attr(filt_elem, 'column', '') or safe_get_attr(filt_elem, 'field', '')).strip('[]')
        operator = safe_get_attr(filt_elem, 'type', 'categorical')
        values = [v.text for v in safe_findall(filt_elem, './/value') if v.text]
        if field:
            filters.append({
                'field': field,
                'operator': operator,
                'values': values,
            })

    return {
        'name': name,
        'description': description,
        'measure_field': measure_field,
        'time_dimension': time_dim,
        'time_grain': time_grain,
        'aggregation': aggregation,
        'target_value': target_value,
        'target_label': target_label,
        'filters': filters,
        'definition_formula': definition_formula,
        'number_format': number_format,
    }


def has_pulse_metrics(root):
    """Quick check: does this workbook contain any Pulse metric definitions?"""
    if root is None:
        return False
    return bool(
        safe_findall(root, './/metric') or
        safe_findall(root, './/pulse-metric') or
        safe_findall(root, './/metrics/metric')
    )
