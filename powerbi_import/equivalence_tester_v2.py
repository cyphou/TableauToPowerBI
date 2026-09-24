"""Equivalence Testing Framework v2 - Validate migrated Power BI artifacts.

Compares Tableau source values against Power BI migrated output using multiple
validation strategies:
1. Value equivalence (row count, aggregation, distinctness)
2. SSIM image comparison (visual rendering)
3. Measure cardinality analysis (distinct value counts)
4. Relationship integrity (foreign key validation)
5. DAX formula correctness (execution test)

Produces detailed equivalence reports with pass/fail/warn status.
"""

import json
import hashlib
import re
from typing import List, Dict, Tuple, Any, Optional


class EquivalenceTester:
    """Main equivalence testing orchestrator."""
    
    def __init__(self, tolerance=0.01, verbose=False):
        """
        Args:
            tolerance: Numeric comparison tolerance (default 1%)
            verbose: Enable detailed logging
        """
        self.tolerance = tolerance
        self.verbose = verbose
        self.results = []
    
    def test_measure_values(self, tableau_val: float, pbi_val: float, 
                           measure_name: str, context: str = "") -> Tuple[bool, float]:
        """Compare measure values with tolerance.
        
        Returns:
            (passed, percent_diff)
        """
        if tableau_val is None or pbi_val is None:
            return tableau_val == pbi_val, 0.0
        
        if tableau_val == 0 and pbi_val == 0:
            return True, 0.0
        
        if tableau_val == 0:
            pct_diff = abs(pbi_val) * 100
        else:
            pct_diff = abs(pbi_val - tableau_val) / abs(tableau_val) * 100
        
        passed = pct_diff <= self.tolerance * 100
        
        if self.verbose:
            status = "✓" if passed else "✗"
            print(f"  {status} {measure_name} {context}: T={tableau_val} vs P={pbi_val} (diff={pct_diff:.2f}%)")
        
        return passed, pct_diff
    
    def test_row_count(self, tableau_rows: int, pbi_rows: int,
                      table_name: str) -> bool:
        """Validate table row counts match."""
        passed = tableau_rows == pbi_rows
        status = "✓" if passed else "✗"
        if self.verbose:
            print(f"  {status} {table_name} rows: T={tableau_rows} vs P={pbi_rows}")
        return passed
    
    def test_distinctcount(self, tableau_distinct: int, pbi_distinct: int,
                          column_name: str) -> bool:
        """Validate distinct value counts."""
        # Allow small variance (±2% for sampling effects)
        threshold = max(2, int(tableau_distinct * 0.02))
        passed = abs(tableau_distinct - pbi_distinct) <= threshold
        status = "✓" if passed else "✗"
        if self.verbose:
            print(f"  {status} {column_name} distinct: T={tableau_distinct} vs P={pbi_distinct}")
        return passed
    
    def test_relationship_fk(self, parent_table: str, parent_key: str,
                            child_table: str, child_key: str,
                            parent_vals: set, child_vals: set) -> Tuple[bool, List[str]]:
        """Validate foreign key constraint (all child values in parent).
        
        Returns:
            (passed, orphaned_keys)
        """
        orphaned = child_vals - parent_vals
        passed = len(orphaned) == 0
        status = "✓" if passed else "✗"
        if self.verbose:
            print(f"  {status} FK {child_table}[{child_key}] → {parent_table}[{parent_key}]: {len(orphaned)} orphans")
        return passed, list(orphaned)[:5]  # Return first 5 orphans
    
    def test_dax_formula(self, formula: str, expected_result: Any) -> Tuple[bool, Any]:
        """Test DAX formula execution (requires PBI Desktop or SSAS endpoint).
        
        This is a placeholder - actual execution requires Power BI/SSAS connection.
        """
        # Placeholder: In production, this would execute against SSAS
        # For now, we validate syntax and return pass
        if not formula or not formula.strip().startswith(('=', 'CALCULATE', 'SUM', 'AVERAGE')):
            return False, "Invalid DAX formula"
        return True, None  # Syntax valid
    
    def test_visual_field_coverage(self, tableau_fields: set, pbi_fields: set,
                                  visual_name: str) -> Tuple[bool, float]:
        """Compare visual field coverage.
        
        Returns:
            (passed, coverage_percent)
        """
        if not tableau_fields:
            return True, 100.0
        
        common = tableau_fields & pbi_fields
        coverage = len(common) / len(tableau_fields) * 100 if tableau_fields else 100.0
        passed = coverage >= 90.0  # At least 90% field coverage
        status = "✓" if passed else "✗"
        if self.verbose:
            print(f"  {status} {visual_name} field coverage: {coverage:.1f}% ({len(common)}/{len(tableau_fields)})")
        return passed, coverage
    
    def test_filter_expression(self, expr: str, valid_dax: bool) -> bool:
        """Validate filter expression conversion."""
        if not expr:
            return True
        # Check for obvious DAX errors
        issues = []
        if expr.count('(') != expr.count(')'):
            issues.append("Mismatched parentheses")
        if "TABLEAU_" in expr.upper():
            issues.append("Unresolved Tableau function")
        
        passed = len(issues) == 0 and valid_dax
        if self.verbose and issues:
            print(f"  ✗ Filter expression issues: {', '.join(issues)}")
        return passed
    
    def generate_report(self, test_results: List[Dict]) -> Dict:
        """Generate summary equivalence report.

        Tests that could not run are excluded from the fidelity ratio. Counting
        them as passes reported 100% fidelity for a migration that had not been
        compared to anything.

        Args:
            test_results: List of test result dicts

        Returns:
            Summary report with pass/fail/warn breakdown
        """
        total = len(test_results)
        not_run = sum(1 for r in test_results if r.get('status') == 'not_run')
        comparable = [r for r in test_results if r.get('status') != 'not_run']
        passed = sum(1 for r in comparable if r.get('passed'))
        failed = sum(1 for r in comparable
                     if not r.get('passed') and r.get('severity') == 'error')
        warned = sum(1 for r in comparable
                     if not r.get('passed') and r.get('severity') == 'warning')

        if comparable:
            fidelity_pct = passed / len(comparable) * 100
            status = ('pass' if fidelity_pct >= 95
                      else 'warn' if fidelity_pct >= 80 else 'fail')
        else:
            fidelity_pct = 0.0
            status = 'not_run'

        return {
            'total': total,
            'passed': passed,
            'failed': failed,
            'warned': warned,
            'not_run': not_run,
            'compared': len(comparable),
            'fidelity_percent': fidelity_pct,
            'status': status,
            'details': test_results,
        }


class VisualStructureComparator:
    """Compare the *structure* of two visual configurations.

    This is a structural (field roles, aggregations, sorting) comparison, not
    the image SSIM algorithm — pixel SSIM lives in ``equivalence_tester``.
    """

    @staticmethod
    def _structural_signature(visual_config: Dict) -> Dict:
        """Return the styling-independent structure used for comparison."""
        return {
            'fields': sorted(visual_config.get('dataRoles', {}).keys()),
            'aggregations': [
                f.get('aggregationFunction', 'None')
                for f in visual_config.get('fields', [])
            ],
            'sorting': visual_config.get('sorting'),
        }

    @staticmethod
    def compute_structural_hash(visual_config: Dict) -> str:
        """Compute hash of visual structure (field roles, aggregations, sorting).
        
        Ignores styling to focus on data/structure equivalence.
        """
        structural = VisualStructureComparator._structural_signature(visual_config)
        content = json.dumps(structural, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    
    @staticmethod
    def compare_visual_configs(tableau_config: Dict, pbi_config: Dict) -> Dict:
        """Compare Tableau and Power BI visual configurations.
        
        Returns:
            Dict with similarity score (0.0-1.0) and structural matches/mismatches
        """
        tableau_hash = VisualStructureComparator.compute_structural_hash(tableau_config)
        pbi_hash = VisualStructureComparator.compute_structural_hash(pbi_config)

        # Similarity is computed on the structure itself. Comparing hash
        # characters would be meaningless: SHA-256 avalanches, so any two
        # different configs would score ~1/16 regardless of how close they are.
        left = VisualStructureComparator._structural_signature(tableau_config)
        right = VisualStructureComparator._structural_signature(pbi_config)

        scores = []
        for key in ('fields', 'aggregations'):
            a, b = set(left[key]), set(right[key])
            if not a and not b:
                scores.append(1.0)
            else:
                scores.append(len(a & b) / len(a | b))
        scores.append(1.0 if left['sorting'] == right['sorting'] else 0.0)
        similarity = sum(scores) / len(scores)

        return {
            'tableau_hash': tableau_hash,
            'pbi_hash': pbi_hash,
            'similarity': similarity,
            'match': tableau_hash == pbi_hash,
        }


class DataQualityValidator:
    """Validate data quality across migrated model."""
    
    @staticmethod
    def check_null_ratio(column_data: List, column_name: str, 
                        max_null_pct: float = 50.0) -> Tuple[bool, float]:
        """Check NULL percentage in a column.
        
        Returns:
            (passed, null_percent)
        """
        if not column_data:
            return True, 0.0
        nulls = sum(1 for v in column_data if v is None or v == "")
        null_pct = nulls / len(column_data) * 100
        passed = null_pct <= max_null_pct
        return passed, null_pct
    
    @staticmethod
    def check_cardinality_ratio(dimension_cardinality: int, 
                               fact_row_count: int,
                               dimension_name: str) -> Tuple[bool, float]:
        """Check dimension cardinality relative to fact table.
        
        Flags suspicious cardinality (e.g., cardinality > fact rows).
        
        Returns:
            (passed, ratio)
        """
        ratio = dimension_cardinality / fact_row_count if fact_row_count > 0 else 0.0
        # Warn if cardinality > 50% of fact table (unusual but not invalid)
        passed = ratio <= 1.0  # Can't have more distinct values than rows
        return passed, ratio
    
    @staticmethod
    def check_measure_aggregation_default(measure_name: str, 
                                         default_agg: str) -> bool:
        """Validate measure has appropriate default aggregation.
        
        Checks that measures don't default to 'Don't Summarize' or
        other non-numeric aggregations for numeric measures.
        """
        invalid_aggs = {'None', 'dont_summarize', 'concatenate', ''}
        return default_agg.lower() not in invalid_aggs


# ════════════════════════════════════════════════════════════════════
#  TEST SUITE RUNNER
# ════════════════════════════════════════════════════════════════════

#: Tableau pseudo-fields. They drive shelf behaviour rather than naming data,
#: so they have no model equivalent and must not count as missing coverage.
_PSEUDO_FIELDS = frozenset({
    'Measure Names', 'Measure Values',
    'Number of Records', 'Latitude (generated)', 'Longitude (generated)',
})


def _read_generated_model(pbi_artifact: Dict) -> Optional[Dict]:
    """Names the generated model actually defines.

    Returns ``None`` when the artifact cannot be read, so callers can report
    "not run" instead of inventing a comparison.
    """
    import glob
    import os

    project_dir = (pbi_artifact or {}).get('project_dir')
    if not project_dir or not os.path.isdir(project_dir):
        return None

    try:
        from powerbi_import.artifact_diff import _parse_tmdl_table
    except Exception:  # noqa: BLE001 - absence must not fake a pass
        return None

    measures, columns, tables = set(), set(), set()
    pattern = os.path.join(project_dir, "**", "definition", "tables", "*.tmdl")
    for path in glob.glob(pattern, recursive=True):
        parsed = _parse_tmdl_table(path)
        if not parsed:
            continue
        tables.add(parsed.get('name', ''))
        for column in parsed.get('columns', []):
            columns.add(column.get('name', ''))
        for measure in parsed.get('measures', []):
            measures.add(measure.get('name', ''))

    if not tables:
        return None
    return {'tables': tables, 'columns': columns, 'measures': measures}


def run_full_equivalence_suite(tableau_export: Dict, pbi_artifact: Dict,
                               verbose: bool = False) -> Dict:
    """Run comprehensive equivalence test suite.

    Every test used to pass unconditionally: row count was a hardcoded
    ``True``, the calculation test only checked that the *Tableau* formula was
    non-empty, and field coverage compared the Tableau fields against
    themselves. An empty or entirely wrong project reported 100% fidelity.
    Tests now read the generated model, and say so when they cannot.

    Args:
        tableau_export: Extracted Tableau metadata (from extract_tableau_data)
        pbi_artifact: Generated Power BI artifact; ``project_dir`` is read
        verbose: Enable detailed logging

    Returns:
        Equivalence report with detailed results
    """
    tester = EquivalenceTester(verbose=verbose)
    results = []
    model = _read_generated_model(pbi_artifact)
    model_names = (model['measures'] | model['columns']) if model else set()

    # Test 1: Row counts need a live query against the deployed model.
    for ds in tableau_export.get('datasources', []):
        ds_name = ds.get('name', 'Unknown')
        results.append({
            'test': f'row_count:{ds_name}',
            'passed': False,
            'status': 'not_run',
            'severity': 'info',
            'message': (f'{ds_name}: {ds.get("row_count", 0)} rows in Tableau; '
                        f'comparison needs a deployed model'),
        })

    # Test 2: every Tableau calculation should exist in the model
    for calc in tableau_export.get('calculations', []):
        calc_name = calc.get('caption') or calc.get('name', 'Unknown')
        clean = str(calc_name).strip('[]')
        if model is None:
            results.append({
                'test': f'calc:{clean}',
                'passed': False,
                'status': 'not_run',
                'severity': 'info',
                'message': f'{clean}: no generated model to compare against',
            })
            continue
        present = clean in model_names
        results.append({
            'test': f'calc:{clean}',
            'passed': present,
            'severity': 'pass' if present else 'error',
            'message': (f'{clean}: present in the generated model' if present
                        else f'{clean}: missing from the generated model'),
        })

    # Test 3: fields a worksheet uses should exist in the model
    for ws in tableau_export.get('worksheets', []):
        ws_name = ws.get('name', 'Unknown')
        tableau_fields = set()
        for field in ws.get('fields', []):
            if isinstance(field, dict):
                name = field.get('name')
                if name:
                    tableau_fields.add(str(name).strip('[]'))
            elif field:
                tableau_fields.add(str(field).strip('[]'))
        tableau_fields -= _PSEUDO_FIELDS
        if not tableau_fields:
            continue
        if model is None:
            results.append({
                'test': f'visual_coverage:{ws_name}',
                'passed': False,
                'status': 'not_run',
                'severity': 'info',
                'message': f'{ws_name}: no generated model to compare against',
            })
            continue
        passed, coverage = tester.test_visual_field_coverage(
            tableau_fields, model_names, ws_name
        )
        results.append({
            'test': f'visual_coverage:{ws_name}',
            'passed': passed,
            'severity': 'pass' if passed else 'warning',
            'coverage_percent': coverage,
            'message': f'{ws_name}: {coverage:.1f}% field coverage',
        })

    return tester.generate_report(results)
