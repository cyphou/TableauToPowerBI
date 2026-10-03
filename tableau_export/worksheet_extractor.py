"""Worksheet-level extraction: fields, filters, formatting and tooltips.

Reads a Tableau ``<worksheet>`` element and returns plain data. No extractor
state, so these are functions; ``TableauExtractor`` keeps thin delegating
methods.

Owned by **@extractor**.
"""

import re

from extract_primitives import (
    _clean_field_ref,
    _clean_tableau_run_text,
    _MARK_ENCODINGS,
    _read_filter_condition,
    _strip_brackets,
)


def extract_worksheet_fields(worksheet):
    """Extracts fields used in the worksheet"""
    fields = []

    # Regex for Tableau derivation prefixes (none, sum, avg, count, usr, yr, etc.)
    derivation_re = r'^(none|sum|avg|count|cnt|ctd|countd|min|max|usr|yr|mn|dy|qr|wk|attr|md|mdy|hms|hr|mt|sc|thr|trunc|tyr|tqr|tmn|tdy|twk):'
    suffix_re = r':(nk|qk|ok|fn|tn)(?::\d+)?$'
    # Quick table calc prefixes (pcto = % of total, pctd = % difference, running_*)
    table_calc_re = r'^(pcto|pctd|diff|running_sum|running_avg|running_count|running_min|running_max|rank|rank_unique|rank_dense):(sum|avg|count|min|max|countd)?:?'

    # ── Non-standard shelf format ─────────────────────────────
    # Some Tableau exports (and minimal test fixtures) use
    # ``<shelf-columns><field>[ds].[col]</field></shelf-columns>``
    # instead of the standard ``<table><cols>[ds].[col]</cols>``.
    # Normalise by collecting their text into a synthetic shelf string
    # so the loop below sees the same format.
    non_standard_shelves = {}
    for shelf_name, elem_name in [('columns', 'shelf-columns'),
                                   ('rows', 'shelf-rows')]:
        shelf_elem = worksheet.find(f'.//{elem_name}')
        if shelf_elem is None:
            continue
        # Collect text from child <field> elements (and any direct text)
        parts = []
        if shelf_elem.text and shelf_elem.text.strip():
            parts.append(shelf_elem.text.strip())
        for child in shelf_elem.findall('./field'):
            if child.text and child.text.strip():
                parts.append(child.text.strip())
        if parts:
            non_standard_shelves[shelf_name] = ' '.join(parts)

    # Extract from <table><rows> and <table><cols> (text content with field refs)
    for shelf_name, shelf_tag in [('columns', 'cols'), ('rows', 'rows')]:
        shelf = worksheet.find(f'./table/{shelf_tag}')
        shelf_text = shelf.text if shelf is not None and shelf.text else None
        # Fall back to non-standard <shelf-columns>/<shelf-rows> if standard absent
        if not shelf_text:
            shelf_text = non_standard_shelves.get(shelf_name)
        if shelf_text:
            # Text contains refs like [datasource].[field:type]
            # or three-part [datasource].[column].[aggregation:instance:suffix]
            # Use a regex that captures 2 or 3 bracket groups.
            three_part_re = r'\[([^\]]+)\]\.\[([^\]]+)\](?:\.\[([^\]]+)\])?'
            refs = re.findall(three_part_re, shelf_text)
            for match in refs:
                ds_ref, field_ref, instance_ref = match[0], match[1], match[2]

                # Three-part ref: [ds].[__tableau_internal_object_id__].[cnt:...:qk]
                # means COUNT(*) on the table rows.
                if '__tableau_internal' in field_ref and instance_ref:
                    inst_agg = re.match(r'^(cnt|sum|avg|min|max|countd|median):', instance_ref)
                    if inst_agg:
                        fields.append({
                            'name': 'Number of Records',
                            'shelf': shelf_name,
                            'datasource': ds_ref,
                            'aggregation': inst_agg.group(1),
                        })
                        continue
                    # No aggregation on internal field → skip it entirely
                    continue

                # Detect quick table calc prefix before cleaning
                table_calc_match = re.match(table_calc_re, field_ref)
                table_calc_type = None
                table_calc_agg = None
                if table_calc_match:
                    table_calc_type = table_calc_match.group(1)
                    table_calc_agg = table_calc_match.group(2) or 'sum'

                # Detect aggregation prefix (cnt:, sum:, avg:, etc.)
                agg_prefix_match = re.match(r'^(cnt|sum|avg|min|max|countd|median|attr|stdev|stdevp|var|varp):', field_ref)
                shelf_agg = agg_prefix_match.group(1) if agg_prefix_match else None

                # Clean the field name (remove derivation prefix and type suffix)
                clean_name = re.sub(table_calc_re, '', field_ref)
                clean_name = re.sub(derivation_re, '', clean_name)
                clean_name = re.sub(suffix_re, '', clean_name)

                # COUNT on __tableau_internal_object_id__ = COUNT(*)
                # Convert to synthetic "Number of Records" measure.
                if '__tableau_internal' in clean_name and shelf_agg:
                    field_data = {
                        'name': 'Number of Records',
                        'shelf': shelf_name,
                        'datasource': ds_ref,
                        'aggregation': shelf_agg,
                    }
                    fields.append(field_data)
                    continue

                field_data = {
                    'name': clean_name,
                    'shelf': shelf_name,
                    'datasource': ds_ref
                }
                if table_calc_type:
                    field_data['table_calc'] = table_calc_type
                    field_data['table_calc_agg'] = table_calc_agg
                if shelf_agg:
                    field_data['aggregation'] = shelf_agg
                fields.append(field_data)

    # Extract from encodings (color, size, shape, detail, tooltip, label, text)
    for encoding in worksheet.findall('.//encodings'):
        for enc_type in _MARK_ENCODINGS:
            for enc_elem in encoding.findall(f'./{enc_type}'):
                column = enc_elem.get('column', '')
                if column:
                    # Extract [datasource].[field]
                    col_refs = re.findall(r'\[([^\]]+)\]\.\[([^\]]+)\]', column)
                    if col_refs:
                        raw = col_refs[0][1]
                        table_calc_match = re.match(table_calc_re, raw)
                        clean = re.sub(table_calc_re, '', raw)
                        clean = re.sub(derivation_re, '', clean)
                        clean = re.sub(suffix_re, '', clean)
                        entry = {
                            'name': clean,
                            'shelf': enc_type,
                            'datasource': col_refs[0][0]
                        }
                        if table_calc_match:
                            entry['table_calc'] = table_calc_match.group(1)
                            entry['table_calc_agg'] = table_calc_match.group(2) or 'sum'
                        fields.append(entry)

    # â”€â”€ Slice fields (Detail shelf of Marks card) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # ``<slices><column>[ds].[derivation:Field:suffix]</column></slices>``
    # enumerates dimensions placed on the Detail (or Marks) shelf that
    # are not encoded via color/size/shape/text.  Without these, tableEx
    # and Text-mark visuals render empty because the only field they
    # know about is the one in the `<text>`/`<label>` encoding.
    existing_for_slices = {(f.get('name', ''), f.get('datasource', ''))
                           for f in fields}
    for slice_elem in worksheet.findall('.//slices/column'):
        ref = (slice_elem.text or '').strip()
        if not ref:
            continue
        col_refs = re.findall(r'\[([^\]]+)\]\.\[([^\]]+)\]', ref)
        if not col_refs:
            continue
        ds_ref, field_ref = col_refs[0]
        # Detect aggregation prefix (cnt:, sum:, etc.)
        agg_prefix_match = re.match(r'^(cnt|sum|avg|min|max|countd|median|attr|stdev|stdevp|var|varp):', field_ref)
        shelf_agg = agg_prefix_match.group(1) if agg_prefix_match else None
        clean = re.sub(derivation_re, '', field_ref)
        clean = re.sub(suffix_re, '', clean)
        if not clean or clean.startswith('__tableau_internal'):
            continue
        if (clean, ds_ref) in existing_for_slices:
            continue
        existing_for_slices.add((clean, ds_ref))
        entry = {'name': clean, 'shelf': 'detail', 'datasource': ds_ref}
        if shelf_agg:
            entry['aggregation'] = shelf_agg
        fields.append(entry)


    # ── LOD (Level of Detail) fields ──────────────────────────
    # <lod column="[ds].[none:FieldName:nk]"/> elements set the mark
    # granularity — critical for scatter charts where each dot should
    # represent one entity (e.g. customer), not the grand total.
    existing_names = {f.get('name', '') for f in fields}
    for lod_elem in worksheet.findall('.//lod'):
        col_ref = lod_elem.get('column', '')
        if not col_ref:
            continue
        col_refs = re.findall(r'\[([^\]]+)\]\.\[([^\]]+)\]', col_ref)
        if col_refs:
            clean = re.sub(derivation_re, '', col_refs[0][1])
            clean = re.sub(suffix_re, '', clean)
            # Strip multi-part instance qualifiers (e.g. :qk:1, :nk:2)
            clean = re.sub(r':(nk|qk|ok|fn|tn):\d+$', '', clean)
            if clean and clean not in existing_names:
                existing_names.add(clean)
                fields.append({
                    'name': clean,
                    'shelf': 'detail',
                    'datasource': col_refs[0][0],
                })

    # ── Expand :Measure Names / Multiple Values ───────────────
    # When a worksheet uses these virtual fields, the actual measures
    # are listed in <datasource-dependencies> <column-instance> entries
    # with aggregation derivations (Sum, Avg, Count, CountD, User, ...).
    # We cross-reference with <column role='measure'> to only include
    # columns that are truly measures (not CountD on dimension columns).
    has_measure_names = any(
        f.get('name', '') in (':Measure Names', 'Measure Names')
        for f in fields
    )
    if has_measure_names:
        agg_derivations = {
            'Sum', 'Avg', 'Count', 'CountD', 'Min', 'Max',
            'Median', 'Stdev', 'Var', 'User', 'Attribute',
        }
        # Collect existing field names to avoid duplicates
        existing_names = {f.get('name', '') for f in fields}
        expanded_any = False
        for dep in worksheet.findall('.//datasource-dependencies'):
            ds_ref = dep.get('datasource', '')
            # Build a set of column names with role='measure'
            measure_cols = set()
            for col_elem in dep.findall('column'):
                if col_elem.get('role', '') == 'measure':
                    measure_cols.add(col_elem.get('name', '').strip('[]'))
            # Also include calculation columns (User derivation) —
            # they may have role='measure' in column definition
            for ci in dep.findall('column-instance'):
                deriv = ci.get('derivation', '')
                if deriv not in agg_derivations:
                    continue
                col_name = ci.get('column', '').strip('[]')
                # Skip internal Tableau columns
                if col_name.startswith('__tableau_internal'):
                    continue
                # Only include columns that are measures (or User-derived calcs)
                if col_name not in measure_cols and deriv != 'User':
                    continue
                if col_name in existing_names:
                    continue
                existing_names.add(col_name)
                expanded_any = True
                # Map derivation to aggregation key for PBI
                agg_key = deriv.lower() if deriv != 'User' else ''
                field_entry = {
                    'name': col_name,
                    'shelf': 'measure_value',
                    'datasource': ds_ref,
                }
                if agg_key:
                    field_entry['aggregation'] = agg_key
                fields.append(field_entry)

        # Fallback: if no <column-instance> entries provided explicit
        # aggregations (common for minimal/hand-authored TWBs), expand
        # every <column role='measure'> directly with a default Sum.
        if not expanded_any:
            for dep in worksheet.findall('.//datasource-dependencies'):
                ds_ref = dep.get('datasource', '')
                for col_elem in dep.findall('column'):
                    if col_elem.get('role', '') != 'measure':
                        continue
                    col_name = col_elem.get('name', '').strip('[]')
                    if not col_name or col_name.startswith('__tableau_internal'):
                        continue
                    if col_name in existing_names:
                        continue
                    existing_names.add(col_name)
                    fields.append({
                        'name': col_name,
                        'shelf': 'measure_value',
                        'datasource': ds_ref,
                        'aggregation': 'sum',
                    })

    return fields

def extract_worksheet_filters(worksheet):
    """Extracts worksheet filters from <filter> elements"""
    filters = []
    for filt in worksheet.findall('.//filter'):
        column_ref = filt.get('column', '')
        # Extract field name from [datasource].[field]
        col_match = re.findall(r'\[([^\]]+)\]\.\[([^\]]+)\]', column_ref)
        if col_match:
            ds_ref, field_ref = col_match[0]
            clean_name = _clean_field_ref(field_ref)
        else:
            ds_ref = ''
            field_ref = column_ref
            clean_name = _strip_brackets(column_ref)

        # Detect date-part filters (yr:, qr:, mn:, dy:, wk:, etc.)
        _date_part_match = re.match(
            r'^(yr|qr|mn|dy|wk|tyr|tqr|tmn|tdy|twk|hr|mt|sc|trunc):',
            field_ref)
        _date_part = _date_part_match.group(1) if _date_part_match else None

        filter_type = ''
        filter_values = []
        filter_min = None
        filter_max = None
        include_null = False
        exclude_mode = False

        filter_type, filter_values, filter_min, filter_max, exclude_mode = \
            _read_filter_condition(filt)

        filters.append({
            'field': clean_name,
            'datasource': ds_ref,
            'type': filter_type,
            'values': filter_values,
            'min': filter_min,
            'max': filter_max,
            'exclude': exclude_mode,
            'include_null': include_null,
            'is_context': filt.get('context', '') == 'true',
            'date_part': _date_part
        })
    return filters

def extract_formatting(element):
    """Extracts formatting information (colors, fonts, backgrounds, borders)"""
    formatting = {}

    # Extract styles from <style-rule>  
    for style_rule in element.findall('.//style-rule'):
        rule_element = style_rule.get('element', '')
        format_elem = style_rule.find('.//format')
        if format_elem is not None:
            attrs = dict(format_elem.attrib)
            if attrs:
                formatting[rule_element] = attrs
        # Also collect all format children (some style-rules have multiple formats)
        for fmt in style_rule.findall('.//format'):
            attr_name = fmt.get('attr', '')
            attr_val = fmt.get('value', '')
            if attr_name and attr_val and rule_element:
                formatting.setdefault(rule_element, {})[attr_name] = attr_val

    # Extract format encodings from <format>
    for fmt in element.findall('.//format'):
        field = fmt.get('field', '')
        fmt_str = fmt.get('value', '')
        if field and fmt_str:
            formatting.setdefault('field_formats', {})[field] = fmt_str

    # Background color
    for pane_fmt in element.findall('.//pane/format'):
        if pane_fmt.get('attr') == 'fill-color':
            formatting['background_color'] = pane_fmt.get('value', '')

    # Table/header formatting depth (font sizes, weights, colors, borders, banding)
    for fmt_attr in ('font-size', 'font-family', 'font-weight', 'font-color',
                     'text-align', 'border-style', 'border-color', 'border-width',
                     'band-color', 'band-size'):
        for fmt in element.findall(f'.//format[@attr="{fmt_attr}"]'):
            scope = fmt.get('scope', 'worksheet')
            val = fmt.get('value', '')
            if val:
                formatting.setdefault(f'{scope}_style', {})[fmt_attr] = val

    # Legend position and formatting
    legend_elem = element.find('.//legend')
    if legend_elem is not None:
        legend_info = {}
        legend_pos = legend_elem.get('position', '')
        if legend_pos:
            legend_info['position'] = legend_pos
        legend_title = legend_elem.get('title', '')
        if legend_title:
            legend_info['title'] = legend_title
        # Check for legend style attributes
        for attr in ('font-size', 'font-family', 'font-weight', 'font-color'):
            val = legend_elem.get(attr, '')
            if val:
                legend_info[attr] = val
        if legend_info:
            formatting['legend'] = legend_info

    # Also check legend style rule 
    if 'legend-title' in formatting:
        formatting.setdefault('legend', {})['title_style'] = formatting['legend-title']
    if 'color-legend' in formatting:
        formatting.setdefault('legend', {}).update({
            k: v for k, v in formatting['color-legend'].items()
            if k not in formatting.get('legend', {})
        })

    return formatting

def extract_tooltips(worksheet):
    """Extracts tooltips (fields, viz-in-tooltip, and custom formatting per run)"""
    tooltips = []

    # Text tooltip from <formatted-text>
    for tooltip_elem in worksheet.findall('.//tooltip'):
        formatted = tooltip_elem.find('.//formatted-text')
        if formatted is not None:
            # Reconstruct the text with per-run formatting
            parts = []
            runs = []
            for run in formatted.findall('.//run'):
                run_text = _clean_tableau_run_text(run)
                if run_text:
                    parts.append(run_text)
                    run_data = {'text': run_text}
                    bold = run.get('bold', run.get('fontweight', ''))
                    if bold and bold.lower() in ('true', 'bold'):
                        run_data['bold'] = True
                    color = run.get('fontcolor', run.get('color', ''))
                    if color:
                        run_data['color'] = color
                    font_size = run.get('fontsize', '')
                    if font_size:
                        run_data['font_size'] = font_size
                    # Detect field references <run>[field]</run>
                    field_match = re.match(r'^\s*\[([^\]]+)\]\s*$', run_text)
                    if field_match:
                        run_data['field_ref'] = field_match.group(1)
                    runs.append(run_data)
            if parts:
                tt = {'type': 'text', 'content': ''.join(parts)}
                if runs:
                    tt['runs'] = runs
                tooltips.append(tt)

        # Viz in tooltip (reference to another worksheet)
        viz_ref = tooltip_elem.get('viz', '')
        if viz_ref:
            tooltips.append({'type': 'viz_in_tooltip', 'worksheet': viz_ref})

    return tooltips
