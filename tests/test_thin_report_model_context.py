"""Thin reports bind to the semantic model they are generated for.

A thin report is written by a fresh ``PowerBIProjectGenerator`` that never saw
the semantic model. Without the model's name index it kept the raw Tableau
names (``Employee_Count``, ``federated.1``) and bound measures as aggregated
columns, so the visuals referenced members the model does not have and the
openability gate rejected the project. The three flows that write a thin
report now hand the generator the index of the model they just wrote:

* ``import_shared_model`` (PBIP and Fabric output),
* the single-workbook Fabric project, and
* ``--add-to-model``.

The end-to-end classes read the generated TMDL and visual JSON back from disk
and check every projected field against the model, so they fail on a report
that points at something the model does not contain.
"""

import contextlib
import glob
import io
import json
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tableau_export'))
sys.path.insert(0, os.path.join(ROOT, 'powerbi_import'))

import migrate  # noqa: E402
from powerbi_import.fabric_project_generator import FabricProjectGenerator  # noqa: E402
from powerbi_import.import_to_powerbi import PowerBIImporter  # noqa: E402
from powerbi_import.openability import check_openability  # noqa: E402
from powerbi_import.pbip_generator import PowerBIProjectGenerator  # noqa: E402
from powerbi_import.thin_report_generator import ThinReportGenerator  # noqa: E402
from tableau_export.extract_tableau_data import TableauExtractor  # noqa: E402

_SAMPLES = ('Superstore_Sales', 'HR_Analytics')
_WORK_DIR = None
_EXTRACTED = {}


def _sample(name):
    return os.path.join(ROOT, 'examples', 'tableau_samples', name + '.twb')


def _extract(name):
    """Extract a public sample workbook once per test run."""
    global _WORK_DIR
    if name not in _EXTRACTED:
        path = _sample(name)
        if not os.path.isfile(path):
            raise unittest.SkipTest(f'sample workbook not found: {path}')
        if _WORK_DIR is None:
            _WORK_DIR = tempfile.mkdtemp(prefix='thin_model_context_')
        target = os.path.join(_WORK_DIR, 'extract_' + name)
        os.makedirs(target)
        with contextlib.redirect_stdout(io.StringIO()):
            extracted = TableauExtractor(path, output_dir=target).extract_all()
        if not extracted:
            raise AssertionError(f'extraction failed: {path}')
        _EXTRACTED[name] = target
    return _EXTRACTED[name]


def _importer(name):
    return PowerBIImporter(source_dir=_extract(name))


def _new_output_dir(label):
    _extract(_SAMPLES[0])
    target = os.path.join(_WORK_DIR, label)
    os.makedirs(target)
    return target


def tearDownModule():
    global _WORK_DIR
    if _WORK_DIR:
        shutil.rmtree(_WORK_DIR, ignore_errors=True)
    _WORK_DIR = None
    _EXTRACTED.clear()


_NAME = r"('(?:[^']|'')*'|[^\s=]+)"
_DECLARATION = re.compile(r'^\t(column|measure)\s+' + _NAME, re.MULTILINE)
_TABLE = re.compile(r'^table\s+' + _NAME, re.MULTILINE)


def _unquote(token):
    if token.startswith("'") and token.endswith("'"):
        return token[1:-1].replace("''", "'")
    return token


def _model_index(semantic_model_dir):
    """Read ``{table: (columns, measures)}`` from the TMDL files on disk."""
    index = {}
    for path in glob.glob(os.path.join(semantic_model_dir, 'definition', 'tables', '*.tmdl')):
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
        table = _TABLE.search(text)
        assert table, f'{path} declares no table'
        columns, measures = set(), set()
        for kind, name in _DECLARATION.findall(text):
            (measures if kind == 'measure' else columns).add(_unquote(name))
        index[_unquote(table.group(1))] = (columns, measures)
    return index


def _visual_refs(report_dir):
    """Every field a visual projects: (visual, kind, entity, property, aggregated)."""
    pattern = os.path.join(report_dir, 'definition', 'pages', '*', 'visuals', '*', 'visual.json')
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding='utf-8') as handle:
            visual = json.load(handle)
        state = visual.get('visual', {}).get('query', {}).get('queryState', {})
        for role in state.values():
            for projection in role.get('projections', []):
                kind, body = next(iter(projection['field'].items()))
                aggregated = kind == 'Aggregation'
                if aggregated:
                    kind, body = next(iter(body['Expression'].items()))
                yield (
                    os.path.basename(os.path.dirname(path)), kind,
                    body['Expression']['SourceRef']['Entity'], body['Property'],
                    aggregated,
                )


def _only(pattern):
    found = glob.glob(pattern)
    assert len(found) == 1, f'expected one match for {pattern}, got {found}'
    return found[0]


class _BindingAssertions:
    """Shared checks for a report generated next to a semantic model."""

    def assert_report_binds_to_model(self, report_dir, semantic_model_dir):
        index = _model_index(semantic_model_dir)
        refs = list(_visual_refs(report_dir))
        self.assertTrue(index, 'the model has no tables, nothing can bind')
        self.assertTrue(refs, 'the report projects no field, the check would be vacuous')
        for visual, kind, entity, prop, aggregated in refs:
            label = f'{os.path.basename(report_dir)}/{visual}: {kind} {entity}.{prop}'
            with self.subTest(field=label):
                self.assertIn(entity, index, f'{label}: table is not in the model')
                columns, measures = index[entity]
                if kind == 'Measure':
                    self.assertFalse(aggregated, f'{label}: a measure must not be aggregated')
                    self.assertIn(prop, measures, f'{label}: not a measure of the model')
                else:
                    self.assertIn(prop, columns, f'{label}: not a column of the model')
        return refs


class TestModelContextAccessors(unittest.TestCase):
    """``apply_model_context`` and ``export_model_context`` on the generator."""

    def _context(self):
        return {
            'actual_bim_measures': {'Total Sales'},
            'actual_bim_symbols': {('Orders', 'Sales'), ('Orders', 'Total Sales')},
            'actual_bim_column_types': {('Orders', 'Sales'): 'double'},
            'actual_bim_column_categories': {('Orders', 'City'): 'city'},
            'actual_bim_measure_types': {('Orders', 'Total Sales'): 'double'},
            'measure_rename_map': {'Calculation_1': 'Total Sales'},
            'table_rename_map': {('federated.1', 'Orders'): 'Orders'},
            'column_rename_map': {'Orders': {'Calculation_2': 'Total Profit'}},
            'related_month_bindings': {
                ('Orders', 'Order Date'): ('Calendar', 'MonthName', 'Month'),
            },
        }

    def test_what_is_applied_is_what_is_exported(self):
        generator = PowerBIProjectGenerator()
        generator.apply_model_context(self._context())
        self.assertEqual(self._context(), generator.export_model_context())

    def test_a_context_moves_between_generators(self):
        first, second = PowerBIProjectGenerator(), PowerBIProjectGenerator()
        first.apply_model_context(self._context())
        second.apply_model_context(first.export_model_context())
        self.assertEqual(first.export_model_context(), second.export_model_context())
        self.assertEqual({('Orders', 'Sales'), ('Orders', 'Total Sales')},
                         second._actual_bim_symbols)

    def test_applying_copies_the_context(self):
        context = self._context()
        generator = PowerBIProjectGenerator()
        generator.apply_model_context(context)
        context['actual_bim_symbols'].add(('Orders', 'Injected'))
        context['measure_rename_map']['x'] = 'y'
        self.assertEqual(self._context(), generator.export_model_context())

    def test_exporting_copies_the_state(self):
        generator = PowerBIProjectGenerator()
        generator.apply_model_context(self._context())
        exported = generator.export_model_context()
        exported['actual_bim_symbols'].clear()
        exported['column_rename_map'].clear()
        self.assertEqual(self._context(), generator.export_model_context())

    def test_missing_and_empty_entries_become_empty_containers(self):
        generator = PowerBIProjectGenerator()
        generator.apply_model_context(self._context())
        generator.apply_model_context({'actual_bim_symbols': None, 'measure_rename_map': {}})
        exported = generator.export_model_context()
        self.assertEqual(set(), exported['actual_bim_symbols'])
        self.assertEqual({}, exported['measure_rename_map'])
        self.assertEqual(set(), exported['actual_bim_measures'])
        self.assertEqual({}, exported['related_month_bindings'])

    def test_a_fresh_generator_exports_an_empty_index(self):
        exported = PowerBIProjectGenerator().export_model_context()
        for key, value in exported.items():
            with self.subTest(key=key):
                self.assertFalse(value)


class TestThinReportHandsTheContextOver(unittest.TestCase):
    """The thin generator gives its context to the generator that writes visuals."""

    def _generate(self, context):
        with tempfile.TemporaryDirectory() as out:
            thin = ThinReportGenerator('Model', out, model_context=context)
            with mock.patch.object(PowerBIProjectGenerator, 'apply_model_context',
                                   autospec=True) as apply, \
                    mock.patch.object(PowerBIProjectGenerator,
                                      '_generate_report_definition_content',
                                      autospec=True):
                thin._generate_report_content(os.path.join(out, 'R.Report'), 'R', {})
        return apply

    def test_the_context_is_applied_before_visuals_are_written(self):
        context = {'actual_bim_symbols': {('Orders', 'Sales')}}
        apply = self._generate(context)
        apply.assert_called_once_with(mock.ANY, context)

    def test_without_a_context_the_generator_is_left_alone(self):
        self._generate(None).assert_not_called()

    def test_the_context_is_optional_and_defaults_to_none(self):
        self.assertIsNone(ThinReportGenerator('Model', tempfile.gettempdir()).model_context)


class TestSharedModelThinReports(_BindingAssertions, unittest.TestCase):
    """``import_shared_model``: one model, one thin report per workbook."""

    @classmethod
    def setUpClass(cls):
        cls.projects = {}
        for output_format in ('pbip', 'fabric'):
            converted = [_importer(name)._load_converted_objects() for name in _SAMPLES]
            with contextlib.redirect_stdout(io.StringIO()):
                result = _importer(_SAMPLES[0]).import_shared_model(
                    model_name='SharedModel',
                    all_converted_objects=converted,
                    workbook_names=list(_SAMPLES),
                    output_dir=_new_output_dir('shared_' + output_format),
                    output_format=output_format,
                    force_merge=True,
                )
            cls.projects[output_format] = result

    def _model_and_reports(self, output_format):
        result = self.projects[output_format]
        reports = {os.path.basename(path)[:-len('.Report')]: path
                   for path in result['report_paths']}
        self.assertEqual(sorted(_SAMPLES), sorted(reports))
        model_path = result['model_path']
        return model_path, reports, os.path.dirname(model_path)

    def test_every_pbip_field_exists_in_the_model(self):
        model, reports, _ = self._model_and_reports('pbip')
        for report in reports.values():
            self.assert_report_binds_to_model(report, model)

    def test_every_fabric_field_exists_in_the_model(self):
        model, reports, _ = self._model_and_reports('fabric')
        for report in reports.values():
            self.assert_report_binds_to_model(report, model)

    def test_measures_are_bound_as_measures_and_keep_their_captions(self):
        _, reports, _ = self._model_and_reports('pbip')
        hr = {(kind, entity, prop) for _, kind, entity, prop, _ in
              _visual_refs(reports['HR_Analytics'])}
        self.assertIn(('Measure', 'Employees', 'Employee Count'), hr)
        self.assertIn(('Measure', 'Employees', 'Average Salary'), hr)
        self.assertFalse([ref for ref in hr if '_' in ref[2]],
                         f'raw Tableau names reached the visuals: {sorted(hr)}')
        sales = {(kind, entity, prop) for _, kind, entity, prop, _ in
                 _visual_refs(reports['Superstore_Sales'])}
        self.assertIn(('Measure', 'Orders', 'Total Sales'), sales)
        self.assertFalse([ref for ref in sales if ref[1] == 'federated.1'])

    def test_the_shared_project_is_openable(self):
        _, _, root = self._model_and_reports('pbip')
        report = check_openability(root)
        self.assertTrue(report.openable, f'blocking issues: {report.blocking_issues}')


class TestFabricSingleWorkbookReport(_BindingAssertions, unittest.TestCase):
    """The Fabric project writes its own report next to the Direct Lake model."""

    @classmethod
    def setUpClass(cls):
        cls.projects = {}
        for name in _SAMPLES:
            converted = _importer(name)._load_converted_objects()
            with contextlib.redirect_stdout(io.StringIO()):
                result = FabricProjectGenerator(_new_output_dir('fabric_' + name)) \
                    .generate_project(name, converted)
            cls.projects[name] = result['project_path']

    def test_report_fields_exist_in_the_direct_lake_model(self):
        for name, project in self.projects.items():
            with self.subTest(workbook=name):
                self.assert_report_binds_to_model(
                    _only(os.path.join(project, '*.Report')),
                    _only(os.path.join(project, '*.SemanticModel')),
                )

    def test_the_fabric_project_is_openable(self):
        for name, project in self.projects.items():
            with self.subTest(workbook=name):
                report = check_openability(project)
                self.assertTrue(report.openable,
                                f'blocking issues: {report.blocking_issues}')


class TestAddToModelReport(_BindingAssertions, unittest.TestCase):
    """``--add-to-model`` writes the new workbook's report against the regenerated model."""

    @classmethod
    def setUpClass(cls):
        converted = [_importer(_SAMPLES[0])._load_converted_objects()]
        with contextlib.redirect_stdout(io.StringIO()):
            result = _importer(_SAMPLES[0]).import_shared_model(
                model_name='AddModel',
                all_converted_objects=converted,
                workbook_names=[_SAMPLES[0]],
                output_dir=_new_output_dir('add_to_model'),
                output_format='pbip',
                force_merge=True,
            )
        cls.model_dir = os.path.dirname(result['model_path'])
        args = types.SimpleNamespace(
            add_to_model=(cls.model_dir, _sample(_SAMPLES[1])), force_merge=True)
        with contextlib.redirect_stdout(io.StringIO()):
            cls.exit_code = migrate._run_add_to_model(args)

    def test_the_workbook_is_added(self):
        self.assertEqual(migrate.ExitCode.SUCCESS, self.exit_code)

    def test_the_added_report_binds_to_the_regenerated_model(self):
        # Only the report written by this call is asserted: regenerating the
        # model does not carry over the first workbook's measures, which is a
        # separate matter from how a new report resolves its own fields.
        refs = self.assert_report_binds_to_model(
            _only(os.path.join(self.model_dir, 'HR_Analytics.Report')),
            _only(os.path.join(self.model_dir, '*.SemanticModel')),
        )
        self.assertIn(('Measure', 'Employees', 'Employee Count'),
                      {(kind, entity, prop) for _, kind, entity, prop, _ in refs})


if __name__ == '__main__':
    unittest.main()
