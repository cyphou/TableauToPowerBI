"""
Migration Planner — Sprint 167

Enterprise migration planning engine: wave assignment, effort estimation,
dependency-aware scheduling, workspace mapping, and HTML dashboard.
"""

import json
import logging
import math
import os

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Effort Estimation Model
# ═══════════════════════════════════════════════════════════════════

# Weights calibrated against observed migration times (hours)
_EFFORT_WEIGHTS = {
    'visuals': 0.3,
    'measures': 0.4,
    'connectors': 0.2,
    'rls_roles': 0.1,
}

# Base time per unit (minutes)
_BASE_TIME_PER_UNIT = {
    'visuals': 2.5,       # ~2.5 min per visual for review/adjustment
    'measures': 4.0,      # ~4 min per measure for DAX validation
    'connectors': 15.0,   # ~15 min per connector for gateway setup
    'rls_roles': 20.0,    # ~20 min per RLS role for Azure AD mapping
}


def estimate_effort(workbook_stats):
    """Estimate migration effort for a single workbook.

    Args:
        workbook_stats: Dict with keys: visuals, measures, connectors, rls_roles,
                        pages, tables, relationships, parameters.

    Returns:
        dict: {score (0-100), hours, category (Simple/Medium/Complex/Very Complex)}
    """
    visuals = workbook_stats.get('visuals', 0)
    measures = workbook_stats.get('measures', 0)
    connectors = workbook_stats.get('connectors', 1)
    rls_roles = workbook_stats.get('rls_roles', 0)

    # Weighted complexity score (0-100)
    raw_score = (
        min(visuals / 50, 1.0) * _EFFORT_WEIGHTS['visuals'] * 100 +
        min(measures / 80, 1.0) * _EFFORT_WEIGHTS['measures'] * 100 +
        min(connectors / 5, 1.0) * _EFFORT_WEIGHTS['connectors'] * 100 +
        min(rls_roles / 3, 1.0) * _EFFORT_WEIGHTS['rls_roles'] * 100
    )
    score = min(int(raw_score), 100)

    # Time estimation (hours)
    total_minutes = (
        visuals * _BASE_TIME_PER_UNIT['visuals'] +
        measures * _BASE_TIME_PER_UNIT['measures'] +
        connectors * _BASE_TIME_PER_UNIT['connectors'] +
        rls_roles * _BASE_TIME_PER_UNIT['rls_roles']
    )
    # Add overhead for testing/validation (30%)
    total_hours = (total_minutes * 1.3) / 60

    # Categorize
    if score <= 25:
        category = 'Simple'
    elif score <= 50:
        category = 'Medium'
    elif score <= 75:
        category = 'Complex'
    else:
        category = 'Very Complex'

    return {
        'score': score,
        'hours': round(total_hours, 1),
        'category': category,
    }


# ═══════════════════════════════════════════════════════════════════
# Wave Planning
# ═══════════════════════════════════════════════════════════════════

def assign_waves(workbooks, dependency_graph=None, max_per_wave=10):
    """Assign workbooks to migration waves based on complexity and dependencies.

    Rules:
    1. Workbooks sharing a published datasource must be in the same wave.
    2. Simpler workbooks first (lower waves).
    3. Max workbooks per wave = max_per_wave.

    Args:
        workbooks: List of dicts with keys: id, name, stats, datasource_ids.
        dependency_graph: Optional {datasource_id: [workbook_ids]} mapping.
        max_per_wave: Maximum workbooks per migration wave.

    Returns:
        list[dict]: Wave assignments [{wave: 1, workbooks: [...], effort_hours: N}]
    """
    dep_graph = dependency_graph or {}

    # Build dependency clusters (workbooks that must migrate together)
    clusters = []
    assigned = set()

    # First: group by shared datasources
    for ds_id, wb_ids in dep_graph.items():
        cluster_wbs = [w for w in workbooks if w.get('id') in wb_ids]
        if len(cluster_wbs) > 1:
            cluster_ids = {w['id'] for w in cluster_wbs}
            # Merge with existing cluster if overlap
            merged = False
            for existing in clusters:
                if existing['ids'] & cluster_ids:
                    existing['ids'] |= cluster_ids
                    existing['workbooks'] = [
                        w for w in workbooks if w['id'] in existing['ids']
                    ]
                    merged = True
                    break
            if not merged:
                clusters.append({
                    'ids': cluster_ids,
                    'workbooks': cluster_wbs,
                })
            assigned |= cluster_ids

    # Remaining workbooks as individual clusters
    for wb in workbooks:
        if wb.get('id') not in assigned:
            clusters.append({
                'ids': {wb['id']},
                'workbooks': [wb],
            })

    # Sort clusters by average complexity (simple first)
    for cluster in clusters:
        scores = []
        for wb in cluster['workbooks']:
            stats = wb.get('stats', {})
            effort = estimate_effort(stats)
            scores.append(effort['score'])
        cluster['avg_score'] = sum(scores) / len(scores) if scores else 0

    clusters.sort(key=lambda c: c['avg_score'])

    # Assign to waves
    waves = []
    current_wave = {'wave': 1, 'workbooks': [], 'effort_hours': 0}

    for cluster in clusters:
        cluster_hours = sum(
            estimate_effort(wb.get('stats', {}))['hours']
            for wb in cluster['workbooks']
        )
        cluster_size = len(cluster['workbooks'])

        # Start new wave if current is full
        if (len(current_wave['workbooks']) + cluster_size > max_per_wave
                and current_wave['workbooks']):
            waves.append(current_wave)
            current_wave = {
                'wave': len(waves) + 1,
                'workbooks': [],
                'effort_hours': 0,
            }

        for wb in cluster['workbooks']:
            effort = estimate_effort(wb.get('stats', {}))
            current_wave['workbooks'].append({
                'id': wb.get('id'),
                'name': wb.get('name'),
                'effort': effort,
            })
        current_wave['effort_hours'] += cluster_hours

    if current_wave['workbooks']:
        waves.append(current_wave)

    # Round effort hours
    for wave in waves:
        wave['effort_hours'] = round(wave['effort_hours'], 1)

    return waves


# ═══════════════════════════════════════════════════════════════════
# Workspace Mapping
# ═══════════════════════════════════════════════════════════════════

def generate_workspace_mapping(workbooks, strategy='by_project'):
    """Map Tableau projects to PBI workspace structure.

    Args:
        workbooks: List of workbook dicts with 'project' field.
        strategy: 'by_project' (1:1 Tableau project → PBI workspace),
                  'consolidated' (group by domain), or 'flat' (single workspace).

    Returns:
        dict: {workspace_name: [workbook_names]}
    """
    if strategy == 'flat':
        return {'Migrated Content': [w.get('name', '') for w in workbooks]}

    if strategy == 'by_project':
        mapping = {}
        for wb in workbooks:
            project = wb.get('project', 'Default')
            ws_name = f"PBI - {project}"
            mapping.setdefault(ws_name, []).append(wb.get('name', ''))
        return mapping

    # consolidated: group small projects together
    project_counts = {}
    for wb in workbooks:
        project = wb.get('project', 'Default')
        project_counts.setdefault(project, []).append(wb.get('name', ''))

    mapping = {}
    small_projects = []
    for project, wbs in project_counts.items():
        if len(wbs) >= 3:
            mapping[f"PBI - {project}"] = wbs
        else:
            small_projects.extend(wbs)

    if small_projects:
        mapping['PBI - Consolidated'] = small_projects

    return mapping


# ═══════════════════════════════════════════════════════════════════
# Permission Mapping
# ═══════════════════════════════════════════════════════════════════

_TABLEAU_TO_PBI_ROLE = {
    'Creator': 'Admin',
    'Explorer': 'Member',
    'ExplorerCanPublish': 'Member',
    'Viewer': 'Viewer',
    'ServerAdministrator': 'Admin',
    'SiteAdministratorCreator': 'Admin',
    'SiteAdministratorExplorer': 'Admin',
}


def generate_permission_mapping(users, groups, workspace_mapping):
    """Map Tableau users/groups to PBI workspace roles.

    Args:
        users: List of Tableau user dicts {name, siteRole, email}.
        groups: List of Tableau group dicts {name, users: [...]}.
        workspace_mapping: {workspace_name: [workbook_names]}.

    Returns:
        list[dict]: Permission assignments.
    """
    assignments = []

    for user in (users or []):
        site_role = user.get('siteRole', 'Viewer')
        pbi_role = _TABLEAU_TO_PBI_ROLE.get(site_role, 'Viewer')
        assignments.append({
            'type': 'user',
            'identity': user.get('email', user.get('name', '')),
            'pbi_role': pbi_role,
            'source_role': site_role,
            'workspaces': list(workspace_mapping.keys()),
        })

    for group in (groups or []):
        # Map group to Azure AD security group
        assignments.append({
            'type': 'group',
            'identity': group.get('name', ''),
            'pbi_role': 'Member',
            'source': 'Tableau Group',
            'member_count': len(group.get('users', [])),
            'workspaces': list(workspace_mapping.keys()),
            'note': f'Create Azure AD security group "{group.get("name")}" and add members',
        })

    return assignments


# ═══════════════════════════════════════════════════════════════════
# Migration Plan Generation
# ═══════════════════════════════════════════════════════════════════

def generate_migration_plan(server_inventory, dependency_graph=None,
                            workspace_strategy='by_project',
                            max_per_wave=10):
    """Generate a complete migration plan from server inventory.

    Args:
        server_inventory: Dict with keys:
            workbooks: [{id, name, project, stats, datasource_ids}]
            users: [{name, siteRole, email}]
            groups: [{name, users}]
            datasources: [{id, name, type}]
        dependency_graph: Optional {datasource_id: [workbook_ids]}.
        workspace_strategy: Workspace mapping strategy.
        max_per_wave: Max workbooks per migration wave.

    Returns:
        dict: Complete migration plan.
    """
    workbooks = server_inventory.get('workbooks', [])
    users = server_inventory.get('users', [])
    groups = server_inventory.get('groups', [])

    # Wave planning
    waves = assign_waves(workbooks, dependency_graph, max_per_wave)

    # Workspace mapping
    workspace_mapping = generate_workspace_mapping(workbooks, workspace_strategy)

    # Permission mapping
    permissions = generate_permission_mapping(users, groups, workspace_mapping)

    # Summary statistics
    total_effort = sum(w['effort_hours'] for w in waves)
    total_workbooks = sum(len(w['workbooks']) for w in waves)

    plan = {
        'summary': {
            'total_workbooks': total_workbooks,
            'total_waves': len(waves),
            'total_effort_hours': round(total_effort, 1),
            'workspace_count': len(workspace_mapping),
            'user_count': len(users),
            'group_count': len(groups),
        },
        'waves': waves,
        'workspace_mapping': workspace_mapping,
        'permissions': permissions,
    }

    return plan


# ═══════════════════════════════════════════════════════════════════
# HTML Dashboard
# ═══════════════════════════════════════════════════════════════════

def generate_plan_html(plan):
    """Generate an interactive HTML migration plan dashboard.

    Args:
        plan: Dict from generate_migration_plan().

    Returns:
        str: HTML content.
    """
    summary = plan.get('summary', {})
    waves = plan.get('waves', [])
    workspace_mapping = plan.get('workspace_mapping', {})
    permissions = plan.get('permissions', [])

    html = [
        '<!DOCTYPE html><html><head>',
        '<meta charset="utf-8">',
        '<title>Migration Plan Dashboard</title>',
        '<style>',
        ':root{--primary:#2b579a;--success:#4caf50;--warning:#ff9800;--danger:#f44336}',
        'body{font-family:Segoe UI,sans-serif;margin:0;padding:2rem;background:#f0f2f5}',
        'h1{color:var(--primary);margin-bottom:0.5rem}',
        '.subtitle{color:#666;margin-bottom:2rem}',
        '.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));'
        'gap:1rem;margin-bottom:2rem}',
        '.stat-card{background:white;border-radius:8px;padding:1.5rem;'
        'box-shadow:0 2px 8px rgba(0,0,0,.08);text-align:center}',
        '.stat-card .value{font-size:2.2rem;font-weight:bold;color:var(--primary)}',
        '.stat-card .label{color:#666;font-size:0.9rem;margin-top:0.3rem}',
        'h2{color:var(--primary);margin-top:2rem;border-bottom:2px solid #e0e0e0;'
        'padding-bottom:0.5rem}',
        '.wave-card{background:white;border-radius:8px;padding:1.5rem;margin:1rem 0;'
        'box-shadow:0 2px 8px rgba(0,0,0,.08)}',
        '.wave-header{display:flex;justify-content:space-between;align-items:center;'
        'margin-bottom:1rem}',
        '.wave-title{font-size:1.2rem;font-weight:bold;color:var(--primary)}',
        '.effort-badge{background:var(--primary);color:white;padding:4px 12px;'
        'border-radius:16px;font-size:0.85rem}',
        'table{border-collapse:collapse;width:100%;margin:0.5rem 0}',
        'th,td{border:1px solid #e0e0e0;padding:8px 12px;text-align:left}',
        'th{background:var(--primary);color:white;font-weight:500}',
        'tr:nth-child(even){background:#f9f9f9}',
        '.cat-simple{color:var(--success);font-weight:bold}',
        '.cat-medium{color:#2196f3;font-weight:bold}',
        '.cat-complex{color:var(--warning);font-weight:bold}',
        '.cat-verycomplex{color:var(--danger);font-weight:bold}',
        '.workspace-list{columns:2;column-gap:2rem}',
        '.ws-item{break-inside:avoid;background:white;border-radius:8px;padding:1rem;'
        'margin-bottom:1rem;box-shadow:0 2px 4px rgba(0,0,0,.06)}',
        '.ws-name{font-weight:bold;color:var(--primary);margin-bottom:0.5rem}',
        '.ws-wbs{color:#555;font-size:0.9rem}',
        '</style></head><body>',
        '<h1>Migration Plan Dashboard</h1>',
        '<p class="subtitle">Auto-generated migration plan with wave assignments, '
        'effort estimates, and workspace mapping</p>',
    ]

    # Stats grid
    html.append('<div class="stats">')
    html.append(f'<div class="stat-card"><div class="value">'
                f'{summary.get("total_workbooks", 0)}</div>'
                f'<div class="label">Workbooks</div></div>')
    html.append(f'<div class="stat-card"><div class="value">'
                f'{summary.get("total_waves", 0)}</div>'
                f'<div class="label">Waves</div></div>')
    html.append(f'<div class="stat-card"><div class="value">'
                f'{summary.get("total_effort_hours", 0)}h</div>'
                f'<div class="label">Total Effort</div></div>')
    html.append(f'<div class="stat-card"><div class="value">'
                f'{summary.get("workspace_count", 0)}</div>'
                f'<div class="label">Workspaces</div></div>')
    html.append(f'<div class="stat-card"><div class="value">'
                f'{summary.get("user_count", 0)}</div>'
                f'<div class="label">Users</div></div>')
    html.append('</div>')

    # Waves
    html.append('<h2>Migration Waves</h2>')
    for wave in waves:
        wave_num = wave.get('wave', 0)
        effort = wave.get('effort_hours', 0)
        wbs = wave.get('workbooks', [])
        html.append(f'<div class="wave-card">')
        html.append(f'<div class="wave-header">')
        html.append(f'<span class="wave-title">Wave {wave_num}</span>')
        html.append(f'<span class="effort-badge">{effort}h effort | {len(wbs)} workbooks</span>')
        html.append(f'</div>')
        html.append('<table><tr><th>Workbook</th><th>Complexity</th>'
                    '<th>Score</th><th>Hours</th></tr>')
        for wb in wbs:
            effort_info = wb.get('effort', {})
            cat = effort_info.get('category', 'Simple')
            cat_class = f'cat-{cat.lower().replace(" ", "")}'
            html.append(
                f'<tr><td>{wb.get("name", "N/A")}</td>'
                f'<td class="{cat_class}">{cat}</td>'
                f'<td>{effort_info.get("score", 0)}</td>'
                f'<td>{effort_info.get("hours", 0)}</td></tr>'
            )
        html.append('</table></div>')

    # Workspace mapping
    html.append('<h2>Workspace Mapping</h2>')
    html.append('<div class="workspace-list">')
    for ws_name, wb_names in workspace_mapping.items():
        html.append(f'<div class="ws-item">')
        html.append(f'<div class="ws-name">{ws_name}</div>')
        html.append(f'<div class="ws-wbs">{", ".join(wb_names[:10])}'
                    f'{"..." if len(wb_names) > 10 else ""}</div>')
        html.append(f'</div>')
    html.append('</div>')

    # Permission summary
    if permissions:
        html.append('<h2>Permission Mapping</h2>')
        html.append('<table><tr><th>Type</th><th>Identity</th>'
                    '<th>PBI Role</th><th>Source Role</th></tr>')
        for perm in permissions[:50]:  # Limit display
            html.append(
                f'<tr><td>{perm.get("type", "")}</td>'
                f'<td>{perm.get("identity", "")}</td>'
                f'<td>{perm.get("pbi_role", "")}</td>'
                f'<td>{perm.get("source_role", perm.get("source", ""))}</td></tr>'
            )
        html.append('</table>')
        if len(permissions) > 50:
            html.append(f'<p><em>...and {len(permissions) - 50} more</em></p>')

    html.append('</body></html>')
    return '\n'.join(html)


def save_migration_plan(plan, output_dir):
    """Save migration plan as JSON + HTML.

    Args:
        plan: Dict from generate_migration_plan().
        output_dir: Directory to write files.

    Returns:
        tuple: (json_path, html_path)
    """
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, 'migration_plan.json')
    html_path = os.path.join(output_dir, 'migration_plan.html')

    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(plan, f, indent=2)

    html_content = generate_plan_html(plan)
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

    logger.info(f'Migration plan saved: {json_path}, {html_path}')
    return json_path, html_path


# ═══════════════════════════════════════════════════════════════════
# Sprint 167 — Timeline & Topology Integration
# ═══════════════════════════════════════════════════════════════════

from datetime import datetime, timedelta


def generate_timeline(waves, team_size=1, start_date=None,
                      hours_per_day=6, buffer_days=2):
    """Generate a dated timeline for migration waves.

    Args:
        waves: Wave list from assign_waves().
        team_size: Number of concurrent migration engineers.
        start_date: Start date string (YYYY-MM-DD) or None for today.
        hours_per_day: Productive hours per engineer per day.
        buffer_days: Buffer days between waves for validation.

    Returns:
        list[dict]: Waves enriched with start_date, end_date, duration_days.
    """
    if start_date:
        current = datetime.strptime(start_date, '%Y-%m-%d')
    else:
        current = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    daily_capacity = team_size * hours_per_day
    timeline = []

    for wave in waves:
        effort = wave.get('effort_hours', 0)
        days_needed = max(1, math.ceil(effort / daily_capacity))

        wave_start = current
        wave_end = current + timedelta(days=days_needed - 1)

        timeline.append({
            **wave,
            'start_date': wave_start.strftime('%Y-%m-%d'),
            'end_date': wave_end.strftime('%Y-%m-%d'),
            'duration_days': days_needed,
            'team_size': team_size,
        })

        current = wave_end + timedelta(days=1 + buffer_days)

    return timeline


def generate_migration_plan_from_topology(topology, dependency_graph=None,
                                           workspace_strategy='by_project',
                                           max_per_wave=10, team_size=1,
                                           start_date=None):
    """Generate a migration plan from site topology (from dependency_graph.py).

    This bridges the topology discovery output to the planning engine.

    Args:
        topology: Dict from dependency_graph.build_site_topology().
        dependency_graph: Optional dep graph from dependency_graph.build_dependency_graph().
        workspace_strategy: Workspace mapping strategy.
        max_per_wave: Max workbooks per wave.
        team_size: Number of migration engineers.
        start_date: Start date (YYYY-MM-DD).

    Returns:
        dict: Complete migration plan with timeline.
    """
    # Extract workbook stats from topology
    workbooks = []
    for wb in topology.get('workbooks', []):
        stats = wb.get('stats', {})
        if not stats:
            # Build basic stats from available info
            stats = {
                'visuals': wb.get('visual_count', wb.get('sheetCount', 0)),
                'measures': 0,
                'connectors': len(wb.get('datasource_ids', [])),
                'rls_roles': 0,
            }

        workbooks.append({
            'id': wb.get('id', ''),
            'name': wb.get('name', ''),
            'project': wb.get('project', {}).get('name', '')
                       if isinstance(wb.get('project'), dict)
                       else wb.get('project', 'Default'),
            'stats': stats,
            'datasource_ids': wb.get('datasource_ids', []),
        })

    # Build dep graph from topology if not provided
    if not dependency_graph:
        dep_graph = {}
        for wb in workbooks:
            for ds_id in wb.get('datasource_ids', []):
                dep_graph.setdefault(ds_id, []).append(wb['id'])
        dependency_graph = dep_graph

    # Generate base plan
    server_inventory = {
        'workbooks': workbooks,
        'users': topology.get('users', []),
        'groups': topology.get('groups', []),
        'datasources': topology.get('datasources', []),
    }

    plan = generate_migration_plan(
        server_inventory,
        dependency_graph=dependency_graph,
        workspace_strategy=workspace_strategy,
        max_per_wave=max_per_wave,
    )

    # Add timeline
    timeline = generate_timeline(
        plan['waves'],
        team_size=team_size,
        start_date=start_date,
    )
    plan['timeline'] = timeline

    # Compute projected end date
    if timeline:
        plan['summary']['start_date'] = timeline[0]['start_date']
        plan['summary']['end_date'] = timeline[-1]['end_date']
        plan['summary']['team_size'] = team_size
        plan['summary']['total_days'] = sum(
            w['duration_days'] for w in timeline
        )

    return plan
