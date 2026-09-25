"""Chart-type inference from a Tableau worksheet.

``determine_chart_type`` and the mark/shelf heuristics it delegates to. Pure
reads of the worksheet element -- no extractor state -- so they live here as
functions; ``TableauExtractor`` keeps thin delegating methods.

Owned by **@extractor**.
"""

import re

from extract_primitives import _clean_field_ref, _MARK_ENCODINGS


#: Marks that draw their value as text, so an empty Rows/Columns pair means a
#: KPI card rather than an unrecognised chart.
_CARD_MARK_CLASSES = {'automatic', 'text'}


def _extract_mark_class(worksheet):
    """Returns the raw Tableau mark class string (e.g. 'Bar', 'Gantt Bar').

    Used downstream by the PBIR generator to look up custom visual
    GUIDs for mark types that have AppSource equivalents.
    """
    for pane in worksheet.findall('.//pane'):
        mark = pane.find('.//mark')
        if mark is not None and mark.get('class'):
            return mark.get('class')
    for mark in worksheet.findall('.//style/mark'):
        if mark.get('class'):
            return mark.get('class')
    return None

def determine_chart_type(worksheet):
    """Determines the chart type from the Tableau mark type.

    When the mark class is 'Automatic', infers the visual type from
    field shelf assignments (columns/rows/color) instead of defaulting
    to 'table'.

    Handles three XML formats for the mark type:
      1. ``<mark class="Bar" />`` inside ``<pane>`` (standard)
      2. ``<mark class="Bar" />`` inside ``<style>`` (standard)
      3. ``<mark-type>bar</mark-type>`` element text (minimal/test fixtures)

    The fallback returns a *valid* Power BI visualType (never the raw
    Tableau name) so downstream visual.json files always parse in
    PBI Desktop. (Bug fix: previously returned ``'bar'`` which is not
    a valid PBI visual type and renders as a blank rectangle.)
    """
    mark_class = None
    # Search for the mark class in panes
    for pane in worksheet.findall('.//pane'):
        mark = pane.find('.//mark')
        if mark is not None and mark.get('class'):
            mark_class = mark.get('class')
            break

    # Search in style/mark
    if mark_class is None:
        for mark in worksheet.findall('.//style/mark'):
            if mark.get('class'):
                mark_class = mark.get('class')
                break

    # Search for <mark-type>X</mark-type> element-text format
    # (used by minimal Tableau XML and some test fixtures)
    if mark_class is None:
        mt_elem = worksheet.find('.//mark-type')
        if mt_elem is not None and mt_elem.text:
            mark_class = mt_elem.text.strip()

    # Fallback: map encoding → use a *valid* PBI visual type
    if mark_class is None:
        if worksheet.find('.//encoding/map') is not None:
            return 'map'
        return 'clusteredBarChart'

    card_type = _shelfless_marks_card_type(worksheet, mark_class)
    if card_type:
        return card_type

    # For explicit mark types, use the mapping directly
    if mark_class.lower() != 'automatic':
        pbi_type = _map_tableau_mark_to_type(mark_class)
        # Bar orientation: dimension on cols + measure on rows = vertical column chart
        if pbi_type == 'clusteredBarChart':
            pbi_type = _detect_bar_orientation(worksheet, pbi_type)
        return pbi_type

    # Automatic: infer from field shelf assignments
    return _infer_automatic_chart_type(worksheet)

def _shelfless_marks_card_type(worksheet, mark_class):
    """Power BI visual for a sheet that draws only on the Marks card.

    With Rows and Columns empty, Tableau reads the Marks card alone: a
    measure on Size with a dimension on Colour is a packed-bubble plot,
    and Text on its own is a KPI figure. Both were becoming data grids.
    Returns None when the sheet is a genuine table.
    """
    if (mark_class or 'automatic').lower() not in _CARD_MARK_CLASSES:
        return None
    for shelf_tag in ('cols', 'rows'):
        shelf = worksheet.find(f'./table/{shelf_tag}')
        if shelf is not None and shelf.text and shelf.text.strip():
            return None
    for shelf_elem in ('shelf-columns', 'shelf-rows'):
        if worksheet.find(f'.//{shelf_elem}') is not None:
            return None

    counts = {}
    for encoding in worksheet.findall('.//encodings'):
        for enc_type in _MARK_ENCODINGS:
            for elem in encoding.findall(f'./{enc_type}'):
                if elem.get('column'):
                    counts[enc_type] = counts.get(enc_type, 0) + 1

    # Size measure + Colour dimension = one shape per category, area by
    # measure. Power BI's treemap says the same thing; a scatter would
    # need X and Y this sheet does not have.
    if counts.get('size') and counts.get('color'):
        return 'treemap'
    if counts.get('size'):
        return None

    text_fields = counts.get('text', 0)
    if not text_fields:
        return None
    return 'card' if text_fields == 1 else 'multiRowCard'

def _detect_bar_orientation(worksheet, default):
    """Detects bar chart orientation from shelf assignments.

    In Tableau, a Bar mark with dimension on columns and measure on
    rows renders as vertical columns.  When measure is on columns and
    dimension (or nothing) on rows it renders as horizontal bars.

    Sprint 78: Extends to stacked and 100% stacked variants.
    """
    agg_prefixes = {'sum:', 'avg:', 'count:', 'cnt:', 'ctd:', 'countd:',
                    'min:', 'max:', 'attr:', 'median:', 'usr:'}
    cols_shelf = worksheet.find('./table/cols')
    rows_shelf = worksheet.find('./table/rows')
    cols_text = cols_shelf.text if cols_shelf is not None and cols_shelf.text else ''
    rows_text = rows_shelf.text if rows_shelf is not None and rows_shelf.text else ''

    def _has_measure(text):
        refs = re.findall(r'\[([^\]]+)\]\.\[([^\]]+)\]', text)
        for _, field_ref in refs:
            lower = field_ref.lower()
            if any(lower.startswith(p) for p in agg_prefixes):
                return True
        return False

    cols_has_measure = _has_measure(cols_text)
    rows_has_measure = _has_measure(rows_text)
    cols_has_fields = bool(re.search(r'\[.*\]\.\[.*\]', cols_text))
    rows_has_fields = bool(re.search(r'\[.*\]\.\[.*\]', rows_text))

    # Sprint 78: Map stacked variants based on orientation
    stacked_map_column = {
        'stackedBarChart': 'stackedColumnChart',
        'hundredPercentStackedBarChart': 'hundredPercentStackedColumnChart',
        'clusteredBarChart': 'clusteredColumnChart',
    }

    # Dimension on cols + measure on rows → vertical (column)
    if cols_has_fields and not cols_has_measure and rows_has_measure:
        return stacked_map_column.get(default, 'clusteredColumnChart')
    return default

def _infer_automatic_chart_type(worksheet):
    """Infers the chart type when Tableau uses 'Automatic' mark.

    Uses field shelf assignments (columns/rows) and field names to
    determine the most appropriate Power BI visual type.
    """
    date_words = {'date', 'time', 'year', 'month', 'day', 'week', 'quarter',
                  'datetime', 'timestamp', 'period', 'yr', 'mois',
                  # French
                  'année', 'annee', 'jour', 'semaine', 'trimestre',
                  'commande', 'expédition', 'expedition', 'livraison'}
    measure_words = {'sales', 'profit', 'revenue', 'amount', 'quantity', 'qty',
                     'count', 'sum', 'total', 'price', 'cost', 'margin',
                     'budget', 'forecast', 'actual', 'target', 'value',
                     'weight', 'height', 'distance', 'rate', 'ratio',
                     'score', 'index', 'number', 'num', 'avg', 'average',
                     # French
                     'ventes', 'vente', 'bénéfice', 'bénéfices', 'benefice',
                     'coût', 'cout', 'quantité', 'quantite', 'montant',
                     'prix', 'marge', 'remise', 'objectif', 'prévision',
                     'prevision', 'chiffre', 'recette', 'dépense', 'depense'}
    geo_words = {'latitude', 'longitude', 'lat', 'lon', 'lng',
                 'zip', 'postal', 'geo', 'geolocation'}
    # Geographic pairs that strongly indicate a map
    geo_pairs = {('latitude', 'longitude'), ('lat', 'lon'), ('lat', 'lng')}

    col_fields = []
    row_fields = []

    # Parse rows/cols shelf text for field references
    for shelf_tag, target in [('cols', col_fields), ('rows', row_fields)]:
        shelf = worksheet.find(f'./table/{shelf_tag}')
        if shelf is not None and shelf.text:
            refs = re.findall(r'\[([^\]]+)\]\.\[([^\]]+)\]', shelf.text)
            for _, field_ref in refs:
                # Strip derivation/aggregation prefixes
                clean = _clean_field_ref(field_ref)
                target.append(clean)

    def _is_date(name):
        return any(w in name.lower().split() for w in date_words)

    def _is_measure(name):
        return any(w in name.lower().split() for w in measure_words)

    # Check for map encoding
    if worksheet.find('.//encoding/map') is not None:
        return 'map'
    # Check for geographic field pairs (lat+lon)
    all_field_words = set()
    for f in col_fields + row_fields:
        all_field_words.update(f.lower().split())
    for w1, w2 in geo_pairs:
        if w1 in all_field_words and w2 in all_field_words:
            return 'map'

    all_row_measures = all(_is_measure(f) for f in row_fields) if row_fields else False
    all_col_measures = all(_is_measure(f) for f in col_fields) if col_fields else False
    has_date_col = any(_is_date(f) for f in col_fields)
    has_date_row = any(_is_date(f) for f in row_fields)

    # Two measures on rows + columns → scatter
    if col_fields and row_fields and all_col_measures and all_row_measures:
        return 'scatterChart'
    # Date on columns/rows with a measure → line
    if has_date_col and row_fields:
        return 'lineChart'
    if has_date_row and col_fields:
        return 'lineChart'
    # Dimension + measure → bar chart
    if col_fields and row_fields:
        return 'clusteredBarChart'
    # Only has fields on one axis → table
    if not col_fields and not row_fields:
        return 'table'
    return 'clusteredBarChart'

def _map_tableau_mark_to_type(mark_class):
    """Maps Tableau mark types to Power BI visual types.

    Covers all Tableau mark classes and maps them to the closest
    Power BI visual type string expected by PBIR v4.0.

    Lookup is case-insensitive so both ``'Bar'`` (standard XML attribute)
    and ``'bar'`` (``<mark-type>`` element text) resolve correctly.
    """
    mark_map = {
        # ── Standard mark classes ──────────────────────────────
        'Automatic': 'clusteredBarChart',  # fallback; usually handled by _infer_automatic_chart_type
        'Bar': 'clusteredBarChart',
        'Stacked Bar': 'stackedBarChart',
        'Line': 'lineChart',
        'Area': 'areaChart',
        'Square': 'treemap',
        'Circle': 'scatterChart',
        'Shape': 'scatterChart',
        'Text': 'tableEx',
        'Map': 'map',
        'Pie': 'pieChart',
        'Gantt Bar': 'clusteredBarChart',
        'Polygon': 'map',
        'Multipolygon': 'map',
        'Density': 'map',
        # ── Extended mark/chart types (Tableau 2020+) ───────────
        'SemiCircle': 'donutChart',
        'Hex': 'treemap',
        'Histogram': 'clusteredColumnChart',
        'Box Plot': 'boxAndWhisker',
        'Box-and-Whisker': 'boxAndWhisker',
        'Bullet': 'gauge',
        'Waterfall': 'waterfallChart',
        'Funnel': 'funnel',
        'Treemap': 'treemap',
        'Heat Map': 'matrix',
        'Highlight Table': 'matrix',
        'Packed Bubble': 'scatterChart',
        'Packed Bubbles': 'scatterChart',
        'Word Cloud': 'wordCloud',
        'Radial': 'gauge',
        'Dual Axis': 'lineClusteredColumnComboChart',
        'Combo': 'lineClusteredColumnComboChart',
        'Combined Axis': 'lineClusteredColumnComboChart',
        'Line and Bar': 'lineClusteredColumnComboChart',
        'Reference Line': 'lineChart',
        'Reference Band': 'lineChart',
        'Trend Line': 'lineChart',
        'Dot Plot': 'scatterChart',
        'Strip Plot': 'scatterChart',
        'Lollipop': 'clusteredBarChart',
        'Bump Chart': 'lineChart',
        'Slope Chart': 'lineChart',
        'Butterfly Chart': 'hundredPercentStackedBarChart',
        'Pareto Chart': 'lineClusteredColumnComboChart',
        'Sankey': 'decompositionTree',
        'Chord': 'decompositionTree',
        'Network': 'decompositionTree',
        'Calendar': 'matrix',
        'Timeline': 'lineChart',
        'KPI': 'card',
        'Sparkline': 'lineChart',
        'Donut': 'donutChart',
        'Ring': 'donutChart',
        'Rose Chart': 'donutChart',
        'Waffle': 'hundredPercentStackedBarChart',
        'Gauge': 'gauge',
        'Speedometer': 'gauge',
        'Image': 'image',
    }
    if not mark_class:
        return 'clusteredBarChart'
    # Case-insensitive lookup: try exact key first, then lowercase
    if mark_class in mark_map:
        return mark_map[mark_class]
    lower = mark_class.lower()
    for key, val in mark_map.items():
        if key.lower() == lower:
            return val
    return 'clusteredBarChart'
