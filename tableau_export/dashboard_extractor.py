"""Dashboard-level extraction: objects, zones, layout and device layouts.

Everything that reads a Tableau ``<dashboard>`` element and returns plain data.
None of it touches extractor state, so it lives here as plain functions;
``TableauExtractor`` keeps thin delegating methods.

Owned by **@extractor**.
"""

import re

from extract_primitives import (
    _clean_field_ref,
    _clean_tableau_run_text,
    _safe_int,
    _strip_brackets,
)


def _map_text_alignment(value):
    """Map a Tableau horizontal text alignment token to a PBI value.

    Tableau encodes alignment either numerically (``1``=left, ``2``=center,
    ``3``=right, ``4``=justify) or by name. Returns one of
    ``left|center|right|justify`` or ``''`` when unknown.
    """
    if value is None:
        return ''
    token = str(value).strip().lower()
    if not token:
        return ''
    numeric = {'0': 'left', '1': 'left', '2': 'center', '3': 'right', '4': 'justify'}
    if token in numeric:
        return numeric[token]
    if token in ('left', 'center', 'centre', 'right', 'justify'):
        return 'center' if token == 'centre' else token
    return ''


def _map_vertical_alignment(value):
    """Map a Tableau vertical anchor token to a PBI value.

    Returns one of ``top|middle|bottom`` or ``''`` when unknown.
    """
    if value is None:
        return ''
    token = str(value).strip().lower()
    if not token:
        return ''
    mapping = {
        '0': 'top', '1': 'top', '2': 'middle', '3': 'bottom',
        'top': 'top', 'center': 'middle', 'centre': 'middle',
        'middle': 'middle', 'bottom': 'bottom',
    }
    return mapping.get(token, '')


def extract_dashboard_objects(dashboard):
    """Extracts all dashboard objects: worksheets, text, images, web, filters, blank.

    Also detects floating vs tiled mode.
    """
    objects = []
    seen_names = set()

    for zone in dashboard.findall('.//zone'):
        zone_name = zone.get('name', '')
        zone_type = zone.get('type', '')
        zone_id = zone.get('id', '')
        # Tableau FCP-prefixed attributes: _.fcp.XXX...type / _.fcp.XXX...type-v2
        if not zone_type:
            for attr_name, attr_val in zone.attrib.items():
                if attr_name.endswith('...type') and not attr_name.endswith('...type-v2'):
                    zone_type = attr_val
                    break
        zone_type_v2 = zone.get('type-v2', '')
        if not zone_type_v2:
            for attr_name, attr_val in zone.attrib.items():
                if attr_name.endswith('...type-v2'):
                    zone_type_v2 = attr_val
                    break
        is_fixed = zone.get('is-fixed') == 'true' or zone_type_v2 == 'fix'
        is_floating = zone.get('is-floating') == 'true'

        pos = {
            'x': _safe_int(zone.get('x', 0)),
            'y': _safe_int(zone.get('y', 0)),
            'w': _safe_int(zone.get('w', 300)),
            'h': _safe_int(zone.get('h', 200)),
        }

        layout_mode = 'floating' if is_floating else ('fixed' if is_fixed else 'tiled')

        # Texte
        if zone_type == 'text' or zone_type_v2 == 'text':
            text_content = ''
            text_runs = []
            formatted = zone.find('.//formatted-text')
            if formatted is not None:
                parts = []
                for run in formatted.findall('.//run'):
                    run_text = _clean_tableau_run_text(run)
                    if run_text:
                        parts.append(run_text)
                        run_data = {'text': run_text}
                        if run.get('bold', run.get('fontweight', '')).lower() in ('true', 'bold'):
                            run_data['bold'] = True
                        if run.get('italic', run.get('fontstyle', '')).lower() in ('true', 'italic'):
                            run_data['italic'] = True
                        color = run.get('fontcolor', run.get('color', ''))
                        if color:
                            run_data['color'] = color
                        font_size = run.get('fontsize', '')
                        if font_size:
                            run_data['font_size'] = font_size
                        align = _map_text_alignment(
                            run.get('fontalignment', run.get('alignment', '')))
                        if align:
                            run_data['alignment'] = align
                        url = run.get('href', run.get('url', ''))
                        if url:
                            run_data['url'] = url
                        text_runs.append(run_data)
                text_content = ''.join(parts)
            # Zone-level horizontal / vertical text anchoring
            text_align = ''
            vertical_align = ''
            for fmt in zone.findall('.//zone-style/format'):
                fattr = fmt.get('attr', '')
                if fattr == 'text-align' and not text_align:
                    text_align = _map_text_alignment(fmt.get('value', ''))
                elif fattr == 'vertical-align' and not vertical_align:
                    vertical_align = _map_vertical_alignment(fmt.get('value', ''))
            # Deduplicate text zones (desktop+device layouts)
            dedup_txt = f"txt_{zone_id}_{text_content}"
            if dedup_txt in seen_names:
                continue
            seen_names.add(dedup_txt)
            objects.append({
                'type': 'text',
                'name': zone_name or f'text_{zone_id}',
                'content': text_content,
                'text_runs': text_runs,
                'text_align': text_align,
                'vertical_align': vertical_align,
                'position': pos,
                'layout': layout_mode
            })
            continue

        # Image
        if zone_type == 'bitmap' or zone_type_v2 == 'bitmap':
            img_src = ''
            img_elem = zone.find('.//zone-style/format[@attr="image"]')
            if img_elem is not None:
                img_src = img_elem.get('value', '')
            # Fallback: use 'param' attribute (embedded TWBX images)
            if not img_src:
                img_src = zone.get('param', '')
            # Deduplicate image zones (desktop+device layouts)
            dedup_img = f"img_{zone_id}_{img_src}"
            if dedup_img in seen_names:
                continue
            seen_names.add(dedup_img)
            objects.append({
                'type': 'image',
                'name': zone_name or f'image_{zone_id}',
                'source': img_src,
                'position': pos,
                'layout': layout_mode
            })
            continue

        # Page web
        if zone_type == 'web' or zone_type_v2 == 'web':
            url = zone.get('url', '') or zone.findtext('.//url', '')
            objects.append({
                'type': 'web',
                'name': zone_name or f'web_{zone_id}',
                'url': url,
                'position': pos,
                'layout': layout_mode
            })
            continue

        # Blank / spacer
        if zone_type == 'empty' or zone_type_v2 == 'empty':
            objects.append({
                'type': 'blank',
                'name': f'blank_{zone_id}',
                'position': pos,
                'layout': layout_mode
            })
            continue

        # Navigation button
        if zone_type == 'nav' or zone_type_v2 == 'nav' or zone_type_v2 == 'button':
            target = zone.get('target-sheet', zone.get('param', ''))
            objects.append({
                'type': 'navigation_button',
                'name': zone_name or f'nav_{zone_id}',
                'target_sheet': target,
                'position': pos,
                'layout': layout_mode
            })
            continue

        # Download button (export)
        if zone_type == 'export' or zone_type_v2 == 'export':
            objects.append({
                'type': 'download_button',
                'name': zone_name or f'download_{zone_id}',
                'position': pos,
                'layout': layout_mode
            })
            continue

        # Extension object
        if zone_type == 'extension' or zone_type_v2 == 'extension':
            ext_id = zone.get('extension-id', '')
            objects.append({
                'type': 'extension',
                'name': zone_name or f'ext_{zone_id}',
                'extension_id': ext_id,
                'position': pos,
                'layout': layout_mode
            })
            continue

        # Per-object padding/margins from zone-style format elements
        obj_padding = {}
        zone_style = zone.find('zone-style')
        if zone_style is not None:
            for fmt in zone_style.findall('format'):
                attr_name = fmt.get('attr', '')
                attr_val = fmt.get('value', '')
                if attr_name.startswith(('padding-', 'margin-')):
                    try:
                        obj_padding[attr_name] = int(attr_val)
                    except (ValueError, TypeError):
                        pass
                elif attr_name.startswith('border-'):
                    key = attr_name.replace('-', '_')
                    obj_padding[key] = attr_val
        # Also check direct zone attributes
        for pad_attr in ('padding-top', 'padding-bottom', 'padding-left', 'padding-right',
                         'margin-top', 'margin-bottom', 'margin-left', 'margin-right'):
            val = zone.get(pad_attr, '')
            if val and pad_attr not in obj_padding:
                try:
                    obj_padding[pad_attr] = int(val)
                except (ValueError, TypeError):
                    pass

        # Filtre (quick filter / parameter control)
        if zone_type == 'filter' or zone_type_v2 == 'filter':
            param_ref = zone.get('param', '')
            # Deduplicate by param (nested zones create duplicates)
            dedup_key = f"fc_{param_ref}" if param_ref else f"fc_{zone_name}_{zone_id}"
            if dedup_key not in seen_names:
                seen_names.add(dedup_key)
                # Extract the column/calculation name from the param
                calc_column_name = ''
                if 'none:' in param_ref:
                    calc_id = param_ref.split('none:')[1].split(':')[0]
                    calc_column_name = calc_id
                objects.append({
                    'type': 'filter_control',
                    'name': zone_name or f'filter_{zone_id}',
                    'field': zone_name,
                    'param': param_ref,
                    'calc_column_id': calc_column_name,
                    'position': pos,
                    'layout': layout_mode
                })
            continue

        # Parameter control (dropdown/slider for a Tableau parameter)
        if zone_type == 'paramctrl' or zone_type_v2 == 'paramctrl':
            param_ref = zone.get('param', '')
            dedup_key = f"pc_{param_ref}" if param_ref else f"pc_{zone_id}"
            if dedup_key not in seen_names:
                seen_names.add(dedup_key)
                # Extract param name: [Parameters].[Parameter 1] → Parameter 1
                param_name = param_ref
                pm = re.search(r'\[Parameters\]\.\[([^\]]+)\]', param_ref)
                if pm:
                    param_name = pm.group(1)
                objects.append({
                    'type': 'parameter_control',
                    'name': f'param_{param_name}',
                    'param': param_ref,
                    'param_name': param_name,
                    'position': pos,
                    'layout': layout_mode
                })
            continue

        # Worksheet reference (the default case)
        if zone_name and zone_name not in seen_names:
            seen_names.add(zone_name)
            ws_obj = {
                'type': 'worksheetReference',
                'name': zone_name,
                'worksheetName': zone_name,
                'position': pos,
                'layout': layout_mode
            }
            if obj_padding:
                ws_obj['padding'] = obj_padding
            objects.append(ws_obj)

    return objects

def extract_dashboard_filters(dashboard):
    """Extracts dashboard filters from <filter> elements"""
    filters = []
    for filt in dashboard.findall('.//filter'):
        column_ref = filt.get('column', '')
        col_match = re.findall(r'\[([^\]]+)\]\.\[([^\]]+)\]', column_ref)
        if col_match:
            ds_ref, field_ref = col_match[0]
            clean_name = _clean_field_ref(field_ref)
        else:
            ds_ref = ''
            clean_name = _strip_brackets(column_ref)

        filter_values = [v.text for v in filt.findall('.//value') if v.text]
        filters.append({
            'field': clean_name,
            'datasource': ds_ref,
            'values': filter_values
        })
    return filters

def extract_dashboard_parameters(dashboard):
    """Extracts parameter controls from the dashboard"""
    params = []
    for zone in dashboard.findall('.//zone'):
        param_ref = zone.get('param', '')
        if param_ref:
            params.append({
                'name': param_ref,
                'zone_name': zone.get('name', ''),
                'position': {
                    'x': _safe_int(zone.get('x', 0)),
                    'y': _safe_int(zone.get('y', 0)),
                    'w': _safe_int(zone.get('w', 200)),
                    'h': _safe_int(zone.get('h', 30)),
                }
            })
    return params

def extract_layout_containers(dashboard):
    """Extracts layout container hierarchy (horizontal/vertical nesting).

    Tableau uses <layout-container> elements to organize zones
    into horizontal and vertical groups with spacing.
    """
    containers = []
    for lc in dashboard.findall('.//layout-container'):
        container = {
            'orientation': lc.get('orientation', 'vertical'),  # horizontal or vertical
            'position': {
                'x': _safe_int(lc.get('x', 0)),
                'y': _safe_int(lc.get('y', 0)),
                'w': _safe_int(lc.get('w', 0)),
                'h': _safe_int(lc.get('h', 0)),
            },
            'children': [],
        }
        # Extract child zone references
        for child in lc.findall('.//zone'):
            child_name = child.get('name', '')
            if child_name:
                container['children'].append(child_name)
        containers.append(container)
    return containers

def extract_device_layouts(dashboard):
    """Extracts device-specific layouts (phone, tablet, desktop).

    Tableau dashboards can have different layouts per device type,
    with different zone visibility and positioning.
    """
    layouts = []
    for dl in dashboard.findall('.//device-layout'):
        device_type = dl.get('device-type', 'default')

        # Get zones visible in this device layout
        visible_zones = []
        for zone in dl.findall('.//zone'):
            zone_name = zone.get('name', '')
            if zone_name:
                visible_zones.append({
                    'name': zone_name,
                    'position': {
                        'x': _safe_int(zone.get('x', 0)),
                        'y': _safe_int(zone.get('y', 0)),
                        'w': _safe_int(zone.get('w', 0)),
                        'h': _safe_int(zone.get('h', 0)),
                    }
                })

        layouts.append({
            'device_type': device_type,  # phone, tablet, desktop
            'zones': visible_zones,
            'auto_generated': dl.get('auto-generated', 'false') == 'true',
        })
    return layouts
