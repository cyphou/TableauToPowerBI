"""
Migration report generator.

Produces a structured JSON report listing every converted item with
its conversion status (exact, approximate, placeholder, unsupported, or
unverified when the report has no evidence to classify a visual).

Usage:
    report = MigrationReport("my_workbook")
    report.add_item("calculation", "Profit Ratio", "exact", dax="DIVIDE([Profit],[Sales])")
    report.add_item("calculation", "MAKEPOINT field", "unsupported", note="No DAX spatial equivalent")
    report.save("artifacts/powerbi_projects/reports/")
"""

import json
import os
import re
from datetime import datetime

# ── Patterns that indicate approximate / placeholder / unsupported conversions ──

# DAX comments left by dax_converter for unsupported functions
_UNSUPPORTED_PATTERNS = [
    (re.compile(r'/\*\s*MAKEPOINT', re.IGNORECASE), 'MAKEPOINT (no DAX spatial equivalent)'),
    (re.compile(r'/\*\s*SCRIPT_(BOOL|INT|REAL|STR)', re.IGNORECASE), 'Analytics extension (SCRIPT_*)'),
    (re.compile(r'/\*\s*CORR.*no direct DAX', re.IGNORECASE), 'CORR (no direct DAX equivalent)'),
    (re.compile(r'/\*\s*COVAR.*no direct DAX', re.IGNORECASE), 'COVAR (no direct DAX equivalent)'),
]

_APPROXIMATE_PATTERNS = [
    (re.compile(r'/\*.*approximate\s*\*/', re.IGNORECASE), 'Approximate DAX conversion'),
    (re.compile(r'/\*.*manual conversion needed', re.IGNORECASE), 'Manual conversion needed'),
    (re.compile(r'/\*.*placeholder', re.IGNORECASE), 'Placeholder formula'),
]

# Tableau functions that should not appear in DAX output
_TABLEAU_LEAK_PATTERNS = [
    re.compile(r'\bCOUNTD\s*\(', re.IGNORECASE),
    re.compile(r'\bZN\s*\(', re.IGNORECASE),
    re.compile(r'\bIFNULL\s*\(', re.IGNORECASE),
    re.compile(r'\bATTR\s*\(', re.IGNORECASE),
    re.compile(r'\bDATETRUNC\s*\(', re.IGNORECASE),
    re.compile(r'\bDATEPART\s*\(', re.IGNORECASE),
]

_DAX_DIAGNOSTIC_TOKENS = re.compile(
    r'''"(?:[^"]|"")*"|'(?:[^']|'')*'|\[(?:[^\]]|\]\])*\]|(?P<comment>/\*[\s\S]*?\*/)'''
)
_COUNTD_UNSUPPORTED_NOTE = re.compile(
    r'\bTODO:\s*(?:COUNTD row context spans tables|'
    r'RUNNING_(?:SUM|AVG|COUNT|MAX|MIN) of COUNTD\b)',
    re.IGNORECASE,
)


def has_unsupported_countd_marker(dax):
    """Recognize our COUNTD diagnostic comments, excluding quoted text."""
    return any(
        match.group('comment') and _COUNTD_UNSUPPORTED_NOTE.search(match.group('comment'))
        for match in _DAX_DIAGNOSTIC_TOKENS.finditer(dax)
    )


class MigrationReport:
    """Tracks per-item migration status and generates a structured report."""

    # Valid statuses
    EXACT = 'exact'
    APPROXIMATE = 'approximate'
    PLACEHOLDER = 'placeholder'
    UNSUPPORTED = 'unsupported'
    SKIPPED = 'skipped'
    UNVERIFIED = 'unverified'

    _VALID_STATUSES = {
        EXACT, APPROXIMATE, PLACEHOLDER, UNSUPPORTED, SKIPPED, UNVERIFIED,
    }

    def __init__(self, report_name):
        self.report_name = report_name
        self.created_at = datetime.now().isoformat()
        self.items = []
        self.table_mapping = []  # source→target table mapping
        self._summary = None

    def add_item(self, category, name, status, *, dax=None, note=None,
                 source_formula=None):
        """Add a converted item to the report.

        Args:
            category: Object type (e.g. 'calculation', 'visual', 'relationship',
                      'parameter', 'set', 'group', 'bin', 'hierarchy',
                      'datasource', 'filter', 'rls_role', 'bookmark')
            name: Item name or identifier
            status: One of 'exact', 'approximate', 'placeholder', 'unsupported',
                    'skipped', or 'unverified'
            dax: Optional generated DAX formula
            note: Optional human-readable note
            source_formula: Optional original Tableau formula
        """
        if status not in self._VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'; must be one of {self._VALID_STATUSES}")

        # This report has no PBIR projection/render evidence at this call site.
        # A caller-supplied "exact" visual label is therefore not supportable.
        if category == 'visual' and status == self.EXACT:
            status = self.UNVERIFIED
            note = '; '.join(filter(None, (
                note, 'PBIR visual projection and rendered parity not verified'
            )))

        entry = {
            'category': category,
            'name': name,
            'status': status,
        }
        if source_formula:
            entry['source_formula'] = source_formula
        if dax:
            entry['dax'] = dax
        if note:
            entry['note'] = note

        self.items.append(entry)
        self._summary = None  # Invalidate cached summary

    # ── Bulk convenience methods ─────────────────────────────────

    def add_calculations(self, calculations, calc_map):
        """Classify and add all calculations from the extraction.

        Args:
            calculations: List of calculation dicts from calculations.json
            calc_map: Dict mapping calculation ID/name → generated DAX formula
        """
        # Build a normalized (whitespace-trimmed, case-folded) lookup so that
        # cosmetic name differences between extraction and the generated TMDL
        # (trailing spaces, casing such as 'index()' vs 'Index()') do not
        # produce false "No DAX output generated" skips.
        def _norm(key):
            return key.strip().casefold() if isinstance(key, str) else key

        calc_map_norm = {}
        for k, v in calc_map.items():
            calc_map_norm.setdefault(_norm(k), v)
            calc_map_norm.setdefault(_norm(k.replace('[', '').replace(']', '')), v)

        for calc in calculations:
            name = calc.get('caption') or calc.get('name', 'Unknown')
            source = calc.get('formula', '')
            calc_id = calc.get('name', name)
            # Clean version without brackets
            calc_id_clean = calc_id.replace('[', '').replace(']', '')

            # Try multiple lookup strategies for the DAX expression
            dax = (calc_map.get(calc_id) or
                   calc_map.get(name) or
                   calc_map.get(calc_id_clean) or
                   calc_map.get(name.replace('[', '').replace(']', '')) or
                   calc_map_norm.get(_norm(calc_id)) or
                   calc_map_norm.get(_norm(name)) or
                   calc_map_norm.get(_norm(calc_id_clean)) or
                   calc_map_norm.get(_norm(name.replace('[', '').replace(']', ''))) or
                   '')

            if not dax:
                self.add_item('calculation', name, self.SKIPPED,
                              source_formula=source,
                              note='No DAX output generated')
                continue

            status = self._classify_dax(dax)
            self.add_item('calculation', name, status,
                          source_formula=source, dax=dax)

    def add_visuals(self, worksheets, visual_type_map=None):
        """Record source worksheets without asserting target visual fidelity.

        Args:
            worksheets: List of worksheet dicts from worksheets.json
            visual_type_map: Optional dict of tableau_mark → candidate PBI type
        """
        for ws in worksheets:
            name = ws.get('name', 'Unknown')
            mark = ws.get('mark_type', ws.get('mark_encoding', {}).get('type', ''))
            if isinstance(mark, dict):
                mark = mark.get('type', '')

            mapped = (visual_type_map or {}).get(str(mark).lower())
            candidate = mapped or ws.get('chart_type')
            note = 'PBIR visual binding and rendered parity not verified'
            if candidate:
                note += f'; extracted or mapped candidate type: {candidate}'
            elif mark:
                note += f'; source mark: {mark}'
            self.add_item('visual', name, self.UNVERIFIED, note=note)

    def add_parameters(self, parameters):
        """Add parameter migration entries."""
        for p in parameters:
            name = p.get('name') or p.get('caption', 'Unknown')
            self.add_item('parameter', name, self.EXACT,
                          note=f"domain={p.get('domain_type', '?')}")

    def add_relationships(self, relationships):
        """Add relationship migration entries."""
        for rel in relationships:
            from_t = rel.get('fromTable', '?')
            to_t = rel.get('toTable', '?')
            name = f"{from_t} → {to_t}"
            card = rel.get('cardinality', '?')
            self.add_item('relationship', name, self.EXACT,
                          note=f"cardinality={card}")

    def add_hierarchies(self, hierarchies):
        """Add hierarchy migration entries."""
        for h in hierarchies:
            name = h.get('name', 'Unknown')
            levels = len(h.get('levels', []))
            self.add_item('hierarchy', name, self.EXACT,
                          note=f"{levels} levels")

    def add_sets(self, sets):
        """Add set migration entries."""
        for s in sets:
            name = s.get('name', 'Unknown')
            self.add_item('set', name, self.EXACT,
                          note='Boolean calculated column via IN')

    def add_groups(self, groups):
        """Add group migration entries."""
        for g in groups:
            name = g.get('name', 'Unknown')
            if g.get('group_type') == 'combined':
                self.add_item(
                    'group', name, self.APPROXIMATE,
                    note='Text concatenation; delimiter collisions, nulls, culture '
                         'and Tableau labels require source-to-target validation',
                )
            else:
                self.add_item('group', name, self.EXACT,
                              note='SWITCH calculated column')

    def add_bins(self, bins):
        """Add bin migration entries."""
        for b in bins:
            name = b.get('name', 'Unknown')
            self.add_item('bin', name, self.EXACT,
                          note='FLOOR calculated column')

    def add_stories(self, stories):
        """Add story → bookmark migration entries."""
        for s in stories:
            name = s.get('name') or s.get('caption', 'Unknown')
            points = len(s.get('story_points', []))
            self.add_item('bookmark', name, self.EXACT,
                          note=f"Converted to {points} bookmark(s)")

    def add_user_filters(self, user_filters):
        """Add RLS role migration entries.

        Classification logic:
        - user_filter with explicit user_mappings → EXACT (full DAX generated)
        - user_filter without mappings → EXACT (column = USERPRINCIPALNAME())
        - calculated_security with ISMEMBEROF → APPROXIMATE (needs Azure AD groups)
        - calculated_security with USERNAME/FULLNAME → EXACT (direct DAX mapping)
        """
        for uf in user_filters:
            name = uf.get('name') or uf.get('field', 'Unknown')
            uf_type = uf.get('type', 'user_filter')
            ismemberof_groups = uf.get('ismemberof_groups', [])

            if uf_type == 'calculated_security' and ismemberof_groups:
                groups = ', '.join(ismemberof_groups)
                self.add_item('rls_role', name, self.EXACT,
                              note=f'ISMEMBEROF → RLS role (assign Azure AD group members: {groups})')
            elif uf_type == 'calculated_security':
                funcs = ', '.join(uf.get('functions_used', []))
                self.add_item('rls_role', name, self.EXACT,
                              note=f'Calculated security ({funcs}) → USERPRINCIPALNAME()')
            elif uf.get('user_mappings'):
                self.add_item('rls_role', name, self.EXACT,
                              note='User filter with explicit mappings → RLS role')
            else:
                self.add_item('rls_role', name, self.EXACT,
                              note='User filter → RLS role with USERPRINCIPALNAME()')

    def add_datasources(self, datasources):
        """Add datasource migration entries and build table mapping."""
        for ds in datasources:
            name = ds.get('name') or ds.get('caption', 'Unknown')
            caption = ds.get('caption') or name
            conn = ds.get('connection', {})
            conn_type = conn.get('class', conn.get('type', '?'))
            tables = ds.get('tables', [])
            table_count = len(tables)
            self.add_item('datasource', name, self.EXACT,
                          note=f"{conn_type}, {table_count} table(s)")

            # Add per-table mapping entries
            for table in tables:
                if not isinstance(table, dict):
                    continue
                tbl_name = table.get('name', '')
                if not tbl_name or tbl_name == 'Unknown':
                    continue
                col_count = len(table.get('columns', []))
                self.table_mapping.append({
                    'source_datasource': caption,
                    'source_table': tbl_name,
                    'target_table': tbl_name,
                    'connection_type': conn_type,
                    'columns': col_count,
                })

    def add_table_mapping_from_tmdl(self, tmdl_tables):
        """Update table mapping with actual TMDL target table names.

        Call this after TMDL generation to record which target tables
        were actually created (some source tables may be deduplicated
        or renamed).

        Args:
            tmdl_tables: Set or list of table names present in the
                generated TMDL semantic model.
        """
        tmdl_set = set(tmdl_tables) if not isinstance(tmdl_tables, set) else tmdl_tables
        for entry in self.table_mapping:
            if entry['target_table'] not in tmdl_set:
                entry['target_table'] = '(deduplicated / merged)'

    # ── Classification helpers ───────────────────────────────────

    @classmethod
    def _classify_dax(cls, dax):
        """Classify a DAX formula's conversion fidelity.

        Returns one of: exact, approximate, placeholder, unsupported.
        """
        if not dax:
            return cls.SKIPPED

        if has_unsupported_countd_marker(dax):
            return cls.UNSUPPORTED

        # Check unsupported patterns first (highest severity)
        for pattern, _ in _UNSUPPORTED_PATTERNS:
            if pattern.search(dax):
                return cls.UNSUPPORTED

        # Check approximate patterns
        for pattern, _ in _APPROXIMATE_PATTERNS:
            if pattern.search(dax):
                return cls.APPROXIMATE

        # Check for leaked Tableau functions
        for pattern in _TABLEAU_LEAK_PATTERNS:
            if pattern.search(dax):
                return cls.APPROXIMATE

        return cls.EXACT

    # ── Summary computation ──────────────────────────────────────

    def get_summary(self):
        """Compute summary statistics from all tracked items."""
        if self._summary is not None:
            return self._summary

        by_status = {}
        by_category = {}

        for item in self.items:
            st = item['status']
            cat = item['category']

            by_status[st] = by_status.get(st, 0) + 1

            if cat not in by_category:
                by_category[cat] = {'total': 0}
            by_category[cat]['total'] += 1
            by_category[cat][st] = by_category[cat].get(st, 0) + 1

        total = len(self.items)
        exact = by_status.get(self.EXACT, 0)
        approx = by_status.get(self.APPROXIMATE, 0)
        placeholder = by_status.get(self.PLACEHOLDER, 0)
        unsupported = by_status.get(self.UNSUPPORTED, 0)
        skipped = by_status.get(self.SKIPPED, 0)
        unverified = by_status.get(self.UNVERIFIED, 0)

        # Do not publish a workbook-wide score that excludes unverified visuals:
        # that would let a workbook with no target visuals inherit a 100% score
        # from unrelated, verified item categories.
        scored = total - skipped - unverified
        visual_evidence_pending = any(
            item['category'] == 'visual' and
            item['status'] in (self.SKIPPED, self.UNVERIFIED)
            for item in self.items
        )
        if visual_evidence_pending:
            fidelity = None
        elif scored > 0:
            fidelity = round((exact * 100 + approx * 50) / scored, 1)
        else:
            fidelity = None if total else 100.0

        self._summary = {
            'total_items': total,
            'exact': exact,
            'approximate': approx,
            'placeholder': placeholder,
            'unsupported': unsupported,
            'skipped': skipped,
            'unverified': unverified,
            'fidelity_score': fidelity,
            'fidelity_score_scope': 'classification_heuristic_excludes_unverified_and_skipped',
            'visual_coverage': {
                'status': 'not_run',
                'reason': 'Worksheet-to-PBIR binding and rendered parity were not verified.',
            },
            'visual_artifact_reconciliation': {
                'source_worksheet_count': by_category.get('visual', {}).get('total', 0),
                'generated_visual_artifact_count': None,
                'status': 'not_run',
                'reason': 'This report was not given generated PBIR visual artifacts to inspect.',
            },
            'by_category': by_category,
        }
        return self._summary

    # ── Per-category completeness scoring ────────────────────────

    # Category weights for overall completeness
    _CATEGORY_WEIGHTS = {
        'calculation': 0.30,
        'visual': 0.25,
        'datasource': 0.15,
        'relationship': 0.10,
        'parameter': 0.05,
        'filter': 0.05,
        'hierarchy': 0.03,
        'set': 0.02,
        'group': 0.02,
        'bin': 0.01,
        'bookmark': 0.01,
        'rls_role': 0.01,
    }

    def get_completeness_score(self):
        """Compute per-category fidelity breakdown and weighted overall score.

        Returns:
            dict with keys:
            - ``categories``: dict of category → {total, exact, approximate,
              unsupported, skipped, fidelity_pct}
            - ``overall_score``: weighted score 0–100, or None when no tracked
              items were scored
            - ``grade``: letter grade (A/B/C/D/F), or None when not scored
        """
        summary = self.get_summary()
        by_cat = summary.get('by_category', {})

        categories = {}
        for cat, counts in by_cat.items():
            total = counts.get('total', 0)
            exact = counts.get(self.EXACT, 0)
            approx = counts.get(self.APPROXIMATE, 0)
            cat_skipped = counts.get(self.SKIPPED, 0)
            cat_unverified = counts.get(self.UNVERIFIED, 0)
            scored = total - cat_skipped - cat_unverified
            if scored > 0:
                fidelity = round((exact * 100 + approx * 50) / scored, 1)
            else:
                fidelity = None if total else 100.0
            categories[cat] = {
                'total': total,
                'exact': exact,
                'approximate': approx,
                'placeholder': counts.get(self.PLACEHOLDER, 0),
                'unsupported': counts.get(self.UNSUPPORTED, 0),
                'skipped': cat_skipped,
                'unverified': cat_unverified,
                'fidelity_pct': fidelity,
            }

        # A visual category with no PBIR evidence makes an overall score
        # unavailable. Per-category scores for independently verified items
        # remain useful, but must not stand in for visual fidelity.
        weighted_sum = 0.0
        weight_sum = 0.0
        for cat, info in categories.items():
            if info['fidelity_pct'] is None:
                continue
            w = self._CATEGORY_WEIGHTS.get(cat, 0.01)
            weighted_sum += info['fidelity_pct'] * w
            weight_sum += w

        visual_info = categories.get('visual', {})
        visual_evidence_pending = bool(
            visual_info.get('skipped') or visual_info.get('unverified')
        )
        overall = (
            None if visual_evidence_pending else
            round(weighted_sum / weight_sum, 1) if weight_sum > 0 else
            None if summary['total_items'] else 100.0
        )

        # Grade
        if overall is None:
            grade = None
        elif overall >= 90:
            grade = 'A'
        elif overall >= 75:
            grade = 'B'
        elif overall >= 60:
            grade = 'C'
        elif overall >= 40:
            grade = 'D'
        else:
            grade = 'F'

        return {
            'categories': categories,
            'overall_score': overall,
            'grade': grade,
            'status': ('partial' if overall is None and any(
                           info['fidelity_pct'] is not None
                           for info in categories.values()
                       ) else 'not_run' if overall is None else
                       'partial' if summary['unverified'] or summary['skipped']
                       else 'available'),
            'score_scope': 'classification_heuristic_excludes_unverified_and_skipped',
        }

    # ── Serialisation ────────────────────────────────────────────

    def to_dict(self):
        """Return the full report as a dictionary."""
        return {
            'report_name': self.report_name,
            'created_at': self.created_at,
            'summary': self.get_summary(),
            'completeness': self.get_completeness_score(),
            'table_mapping': self.table_mapping,
            'items': self.items,
        }

    def save(self, output_dir='artifacts/migration_reports'):
        """Save the report as a JSON file.

        Args:
            output_dir: Directory to write the report file.

        Returns:
            str: Path to the saved report file.
        """
        os.makedirs(output_dir, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'migration_report_{self.report_name}_{ts}.json'
        filepath = os.path.join(output_dir, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

        return filepath

    # ── Console output ───────────────────────────────────────────

    def print_summary(self):
        """Print a human-readable summary to the console."""
        s = self.get_summary()
        print()
        print('=' * 72)
        print(f'  MIGRATION REPORT: {self.report_name}')
        print('=' * 72)
        print(f'  Total items tracked:   {s["total_items"]}')
        score = f'{s["fidelity_score"]}%' if s['fidelity_score'] is not None else 'not run'
        print(f'  Extraction score:      {score} (classification heuristic)')
        print()
        print(f'    Exact:        {s["exact"]:>4}')
        print(f'    Approximate:  {s["approximate"]:>4}')
        print(f'    Placeholder:  {s["placeholder"]:>4}')
        print(f'    Unsupported:  {s["unsupported"]:>4}')
        print(f'    Skipped:      {s["skipped"]:>4}')
        print(f'    Unverified:   {s["unverified"]:>4}')

        if s['by_category']:
            print()
            print('  By category:')
            for cat, counts in sorted(s['by_category'].items()):
                total = counts['total']
                exact = counts.get('exact', 0)
                pct = round(exact / total * 100) if total else 0
                print(f'    {cat:<20} {total:>4} items  ({pct}% exact)')

        # Completeness score
        cs = self.get_completeness_score()
        print()
        grade = cs['grade'] if cs['grade'] is not None else 'not run'
        overall = f'{cs["overall_score"]}%' if cs['overall_score'] is not None else 'not run'
        print(f'  Completeness grade:    {grade} ({overall}; {cs["status"]})')
        if cs['categories']:
            print('  Per-category extraction:')
            for cat, info in sorted(cs['categories'].items()):
                value = (f'{info["fidelity_pct"]:.1f}%'
                         if info['fidelity_pct'] is not None else 'not run')
                print(f'    {cat:<20} {value:>6}  ({info["total"]} items)')

        # Table mapping
        if self.table_mapping:
            print()
            print('  Table mapping (source → target):')
            print(f'    {"Source Datasource":<30} {"Source Table":<30} {"Target Table":<30} {"Columns":>7}')
            print(f'    {"─" * 30} {"─" * 30} {"─" * 30} {"─" * 7}')
            for entry in self.table_mapping:
                src_ds = entry['source_datasource'][:30]
                src_tbl = entry['source_table'][:30]
                tgt_tbl = entry['target_table'][:30]
                cols = entry.get('columns', 0)
                print(f'    {src_ds:<30} {src_tbl:<30} {tgt_tbl:<30} {cols:>7}')

        # List unsupported items
        unsupported = [i for i in self.items if i['status'] == self.UNSUPPORTED]
        if unsupported:
            print()
            print(f'  Unsupported items ({len(unsupported)}):')
            for item in unsupported[:20]:
                note = f' — {item["note"]}' if item.get('note') else ''
                print(f'    [{item["category"]}] {item["name"]}{note}')
            if len(unsupported) > 20:
                print(f'    ... and {len(unsupported) - 20} more')

        # List approximate items
        approx = [i for i in self.items if i['status'] == self.APPROXIMATE]
        if approx:
            print()
            print(f'  Approximate conversions ({len(approx)}):')
            for item in approx[:20]:
                note = f' — {item["note"]}' if item.get('note') else ''
                print(f'    [{item["category"]}] {item["name"]}{note}')
            if len(approx) > 20:
                print(f'    ... and {len(approx) - 20} more')

        print()
        print('=' * 72)
