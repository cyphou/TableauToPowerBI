"""Focused regressions for composite-key metadata and combined-group row grain."""

import json
import os
import tempfile
import xml.etree.ElementTree as ET
from copy import deepcopy
from glob import glob

import pytest

from powerbi_import.m_validator import validate_m_query
from powerbi_import.migration_quality import build_quality_report
from powerbi_import.parity_registry import scan_project
from powerbi_import.pbip_generator import PowerBIProjectGenerator
from powerbi_import.tmdl_generator import (
    _build_semantic_model,
    _register_combined_group_calculation_bindings,
    generate_tmdl,
)
from powerbi_import.tmdl_relationships import (
    _composite_key_m_expression,
    _materialize_composite_logical_relationships,
)
from powerbi_import.tmdl_sets import (
    _combined_group_reference_status,
    _process_sets_groups_bins,
)
from powerbi_import.validator import ArtifactValidator
from tableau_export.extract_tableau_data import TableauExtractor


def test_partial_repair_project_is_data_free_and_remains_quality_fail(tmp_path, monkeypatch):
    from powerbi_import.errors import PartialGenerationError
    from powerbi_import.openability import check_openability

    objects = {'datasources': [], 'dashboards': [{
        'name': 'Overview', 'size': {'width': 1280, 'height': 720},
        'objects': [{'type': 'worksheetReference', 'worksheetName': 'Orders',
                     'position': {'x': 100, 'y': 150, 'w': 500, 'h': 300}}],
    }]}
    original = deepcopy(objects)
    generator = PowerBIProjectGenerator(output_dir=str(tmp_path))

    def reject_semantics(*args, **kwargs):
        raise PartialGenerationError('Composite key requires source evidence')

    monkeypatch.setattr(generator, 'create_tmdl_model', reject_semantics)
    project = generator.generate_project('RepairProject', objects)
    assert os.path.isfile(os.path.join(project, 'RepairProject.pbip'))
    with open(os.path.join(project, 'partial_generation.json'), encoding='utf-8') as handle:
        diagnostic = json.load(handle)
    assert diagnostic['status'] == 'partial'
    assert diagnostic['data_retained'] is False
    assert objects == original
    report = check_openability(project)
    assert report.openable, report.blocking_issues
    visual_paths = glob(os.path.join(project, '*.Report', 'definition', 'pages',
                                     '*', 'visuals', '*', 'visual.json'))
    assert visual_paths
    for path in visual_paths:
        with open(path, encoding='utf-8') as handle:
            visual = json.load(handle)['visual']
        assert visual['visualType'] == 'textbox'
        assert 'query' not in visual
    quality = build_quality_report(original, project, 'RepairProject')
    assert quality.status == 'FAIL'
    assert any('Composite key requires source evidence' in issue for issue in quality.blockers)
    assert any('partial_generation.json' in priority['evidence']
               for priority in quality.priorities)
    from powerbi_import.evidence_package import write_evidence_package
    import zipfile
    archive_path = os.path.join(str(tmp_path), 'evidence.zip')
    write_evidence_package(quality, archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        assert 'quality/partial_generation.json' in archive.namelist()
        payload = json.loads(archive.read('evidence_package.json'))
    assert payload['partial_generation']['status'] == 'partial'
    assert payload['partial_generation']['errors'][0]['message'] == diagnostic['errors'][0]['message']


@pytest.mark.parametrize('error_type,strict', [
    ('recoverable', True), ('unexpected', False), ('source', False),
])
def test_partial_mode_does_not_swallow_strict_or_unexpected_errors(
        tmp_path, monkeypatch, error_type, strict):
    from powerbi_import.errors import GenerationError, PartialGenerationError

    expected = {'recoverable': PartialGenerationError,
                'unexpected': RuntimeError, 'source': GenerationError}[error_type]
    generator = PowerBIProjectGenerator(output_dir=str(tmp_path))

    def reject_semantics(*args, **kwargs):
        raise expected('Generation refused')

    monkeypatch.setattr(generator, 'create_tmdl_model', reject_semantics)
    with pytest.raises(expected):
        generator.generate_project('StrictProject', {}, strict_generation=strict)
    assert not list(tmp_path.rglob('*.pbip'))
    assert not list(tmp_path.rglob('partial_generation.json'))


def test_partial_regeneration_isolates_and_preserves_previous_artifacts(tmp_path, monkeypatch):
    from powerbi_import.errors import PartialGenerationError

    project = tmp_path / 'RepairProject'
    retained = ['Data/source.csv', 'DAX/Old.dax', 'PowerQuery/Old.pq',
                'lineage_map.json', 'credentials_template.json', 'user_notes.txt']
    for relative in retained:
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic previous content', encoding='utf-8')
    generator = PowerBIProjectGenerator(output_dir=str(tmp_path))
    monkeypatch.setattr(generator, 'create_tmdl_model', lambda *args: (
        _ for _ in ()).throw(PartialGenerationError('Unsupported synthetic predicate')))
    generator.generate_project('RepairProject', {'dashboards': [{'name': 'Overview'}]})
    with open(project / 'partial_generation.json', encoding='utf-8') as handle:
        diagnostic = json.load(handle)
    previous = project / diagnostic['previous_output']['path']
    for relative in retained:
        assert not (project / relative).exists()
        assert (previous / relative).read_text(encoding='utf-8') == 'synthetic previous content'
    assert (project / 'RepairProject.pbip').exists()


def test_failed_partial_construction_does_not_replace_previous_output(tmp_path, monkeypatch):
    from powerbi_import.errors import PartialGenerationError

    project = tmp_path / 'RepairProject'
    project.mkdir()
    (project / 'user_notes.txt').write_text('retain me', encoding='utf-8')
    generator = PowerBIProjectGenerator(output_dir=str(tmp_path))

    def reject_model(*args):
        raise PartialGenerationError('Unsupported synthetic predicate')

    def reject_publication(*args):
        raise OSError('Synthetic disk write failure')

    monkeypatch.setattr(generator, 'create_tmdl_model', reject_model)
    monkeypatch.setattr(generator, 'create_pbip_file', reject_publication)
    with pytest.raises(OSError, match='Synthetic disk write failure'):
        generator.generate_project('RepairProject', {'dashboards': [{'name': 'Overview'}]})
    assert (project / 'user_notes.txt').read_text(encoding='utf-8') == 'retain me'
    assert not (project / 'partial_generation.json').exists()
    assert not list(tmp_path.glob('.p*'))


@pytest.mark.parametrize('consumer', ['none', 'calculation', 'filter', 'security', 'parameter', 'source'])
def test_selective_group_repair_retains_only_proven_independent_objects(tmp_path, consumer):
    unsafe_consumer = consumer != 'none'
    datasource = _composite_datasource()
    datasource['relationships'] = []
    datasource['relation_metadata'] = {}
    healthy = {'name': 'Independent', 'chart_type': 'clusteredBarChart', 'fields': [
        {'name': 'Code', 'shelf': 'rows'},
        {'name': 'Amount', 'shelf': 'columns', 'aggregation': 'sum'}]}
    affected = {'name': 'Dependent', 'chart_type': 'tableEx',
                'fields': [{'name': 'GroupRef', 'shelf': 'rows'}]}
    objects = {'datasources': [datasource], 'worksheets': [healthy, affected],
               'groups': [{'name': 'Unresolved group', 'raw_name': 'GroupRef',
                           'group_type': 'combined', 'source_fields': ['Missing', 'Code']}],
               'dashboards': [{'name': 'Overview', 'objects': [
                   {'type': 'worksheetReference', 'worksheetName': name,
                    'position': {'x': 40, 'y': offset, 'w': 500, 'h': 200}}
                   for name, offset in [('Independent', 40), ('Dependent', 300)]]}]}
    if consumer == 'calculation':
        objects['calculations'] = [{'name': 'Consumer', 'formula': 'COUNTD([GroupRef])'}]
    elif consumer == 'filter':
        objects['filters'] = [{'field': '[GroupRef]', 'values': ['synthetic']}]
    elif consumer == 'security':
        objects['user_filters'] = [{'name': 'Synthetic security', 'column': 'GroupRef'}]
    elif consumer == 'parameter':
        objects['parameters'] = [{'name': 'Synthetic selector', 'value': 'GroupRef'}]
    elif consumer == 'source':
        datasource['filters'] = [{'field': '[GroupRef]'}]
    project = PowerBIProjectGenerator(output_dir=str(tmp_path)).generate_project(
        'SelectiveRepair', objects, model_mode='import')
    with open(os.path.join(project, 'partial_generation.json'), encoding='utf-8') as handle:
        diagnostic = json.load(handle)
    assert diagnostic['data_retained'] is not unsafe_consumer
    paths = glob(os.path.join(project, '*.SemanticModel', 'definition', 'tables', '*.tmdl'))
    assert bool(any(os.path.basename(path) == 'MigrationRepair.tmdl' for path in paths)) is unsafe_consumer
    if not unsafe_consumer:
        assert diagnostic['recovery_scope'] == 'unaffected_model_and_visuals'
        assert any(item['name'] == 'Dependent' for item in diagnostic['affected_objects'])
        visuals = []
        for path in glob(os.path.join(project, '*.Report', 'definition', 'pages', '*',
                                      'visuals', '*', 'visual.json')):
            with open(path, encoding='utf-8') as handle:
                visuals.append(json.load(handle)['visual'])
        assert any(visual['visualType'] != 'textbox' for visual in visuals)


def test_complete_partial_complete_transition_retracts_stale_partial_state(tmp_path, monkeypatch):
    from powerbi_import.errors import PartialGenerationError

    datasource = _composite_datasource()
    datasource['relationships'] = []
    datasource['relation_metadata'] = {}
    objects = {'datasources': [datasource], 'dashboards': [{'name': 'Overview', 'objects': []}]}
    generator = PowerBIProjectGenerator(output_dir=str(tmp_path))
    project = generator.generate_project('Transition', objects)
    marker = os.path.join(project, 'Transition.pbip')
    with open(marker, 'rb') as handle:
        original_marker = handle.read()
    original_model = generator.create_tmdl_model

    def reject_model(*args):
        raise PartialGenerationError('Unsupported synthetic predicate')

    monkeypatch.setattr(generator, 'create_tmdl_model', reject_model)
    generator.generate_project('Transition', objects)
    with open(os.path.join(project, 'partial_generation.json'), encoding='utf-8') as handle:
        diagnostic = json.load(handle)
    preserved_marker = os.path.join(project, diagnostic['previous_output']['path'], 'Transition.pbip')
    with open(preserved_marker, 'rb') as handle:
        assert handle.read() == original_marker
    monkeypatch.setattr(generator, 'create_tmdl_model', original_model)
    generator.generate_project('Transition', objects, strict_generation=True)
    assert not os.path.exists(os.path.join(project, 'partial_generation.json'))
    with open(os.path.join(project, 'migration_metadata.json'), encoding='utf-8') as handle:
        assert json.load(handle)['generation_status'] == 'complete'
    assert not glob(os.path.join(project, '*.SemanticModel', 'definition', 'tables',
                                 'MigrationRepair.tmdl'))


def test_partial_publication_failure_restores_previous_output(tmp_path, monkeypatch):
    from powerbi_import.errors import PartialGenerationError

    project = tmp_path / 'RollbackProject'
    project.mkdir()
    (project / 'user_notes.txt').write_text('retain me', encoding='utf-8')
    generator = PowerBIProjectGenerator(output_dir=str(tmp_path))

    def reject_model(*args):
        raise PartialGenerationError('Unsupported synthetic predicate')

    replace = os.replace
    failed_once = False

    def fail_publish(source, destination):
        nonlocal failed_once
        if (not failed_once and os.path.isdir(source)
                and os.path.basename(source).startswith('.p')
                and os.path.normpath(destination) == os.path.normpath(str(project))):
            failed_once = True
            raise OSError('Synthetic publication failure')
        return replace(source, destination)

    monkeypatch.setattr(generator, 'create_tmdl_model', reject_model)
    monkeypatch.setattr(os, 'replace', fail_publish)
    with pytest.raises(OSError, match='Synthetic publication failure'):
        generator.generate_project('RollbackProject', {'dashboards': [{'name': 'Overview'}]})
    assert failed_once
    assert (project / 'user_notes.txt').read_text(encoding='utf-8') == 'retain me'
    assert not (project / 'partial_generation.json').exists()
    assert not list(tmp_path.glob('.p*'))


def _table(name, columns):
    schema = ', '.join(f'"{column}" = text' for column in columns)
    return {
        'name': name,
        'columns': [{'name': column, 'sourceColumn': column} for column in columns],
        'partitions': [{
            'source': {
                'type': 'm',
                'expression': (
                    f'let\n    Source = #table(type table [{schema}], {{}})\n'
                    'in\n    Source'
                ),
            },
        }],
    }


def test_combined_group_reference_requires_an_exact_identifier():
    consumers = {'worksheets': [{'fields': [{'name': '[Group 1]'}]}]}

    assert _combined_group_reference_status({'name': 'Group'}, consumers) is False
    assert _combined_group_reference_status({'name': 'Group 1'}, consumers) is True
    assert _combined_group_reference_status({'name': 'Group'}, {}) is None


@pytest.mark.parametrize(('filter_data', 'should_raise'), [
    ({'field': 'Combined Sensor', 'type': 'all', 'values': [],
      'min': None, 'max': None}, False),
    ({'field': 'Combined Sensor', 'type': 'categorical', 'values': ['sensor-1'],
      'min': None, 'max': None}, True),
])
def test_combined_group_ignores_only_inert_filter_consumers(filter_data, should_raise):
    datasource = _composite_datasource()
    datasource['relationships'] = []
    datasource['relation_metadata'] = {}
    for table in datasource['tables']:
        table['columns'].append({'name': 'Capteur', 'datatype': 'string'})
    datasource['columns'].extend([
        {'name': 'Capteur', 'datatype': 'string'},
        {'name': 'Capteur', 'datatype': 'string'},
    ])
    group = {
        'name': 'Combined Sensor', 'raw_name': 'Combined Sensor',
        'datasource': 'Synthetic', 'group_type': 'combined',
        'source_fields': ['Capteur'],
    }
    worksheet = {
        'name': 'Sensor filter',
        'fields': [{'name': 'Combined Sensor', 'shelf': 'filter', 'source': 'slices'}],
        'filters': [filter_data],
    }
    extra_objects = {
        'groups': [group], 'worksheets': [worksheet],
        '_worksheets': [worksheet], '_datasources': [datasource],
    }

    if should_raise:
        with pytest.raises(ValueError, match='ambiguous ownership'):
            _build_semantic_model(
                [datasource], extra_objects=extra_objects, model_mode='import')
    else:
        model = _build_semantic_model(
            [datasource], extra_objects=extra_objects, model_mode='import')
        assert not any(
            column.get('name') == 'Combined Sensor'
            for table in model['model']['tables']
            for column in table.get('columns', [])
        )


def test_combined_group_with_one_remote_host_is_materialized_on_that_host():
    fact = _table('Facts', ['FactID'])
    lookup = _table('Lookup', ['Code', 'Version'])
    model = {'model': {'tables': [fact, lookup], 'relationships': []}}
    group = {
        'name': 'Code and version',
        'group_type': 'combined',
        'source_fields': ['Code', 'Version'],
    }

    _process_sets_groups_bins(
        model, {'groups': [group]}, 'Facts',
        {'FactID': 'Facts', 'Code': 'Lookup', 'Version': 'Lookup'},
    )

    assert not any(column.get('name') == group['name'] for column in fact['columns'])
    generated = [
        column for column in lookup['columns']
        if column.get('name') == group['name']
    ]
    assert len(generated) == 1
    assert generated[0]['sourceColumn'] == group['name']
    assert {'name': 'Migration_CombinedGroup', 'value': 'approximated'} in generated[0]['annotations']
    expression = lookup['partitions'][0]['source']['expression']
    assert 'Text.From([#"Code"])' in expression
    assert 'Text.From([#"Version"])' in expression
    assert 'RELATED(' not in expression


def test_combined_group_routes_remote_caption_fields_without_raw_name_mapping():
    fact = _table('Facts', ['FactID'])
    lookup = _table('Lookup', ['Code display', 'Version display'])
    model = {
        'model': {'tables': [fact, lookup], 'relationships': []},
        '_column_rename_map': {
            ('Lookup', 'RawCode'): 'Code display',
            ('Lookup', 'RawVersion'): 'Version display',
        },
    }
    group = {
        'name': 'Code and version',
        'group_type': 'combined',
        'source_fields': ['Code display', 'Version display'],
    }

    _process_sets_groups_bins(
        model, {'groups': [group]}, 'Facts',
        {'FactID': 'Facts', 'RawCode': 'Lookup', 'RawVersion': 'Lookup'},
    )

    assert not any(column.get('name') == group['name'] for column in fact['columns'])
    assert any(column.get('name') == group['name'] for column in lookup['columns'])
    expression = lookup['partitions'][0]['source']['expression']
    assert 'Text.From([#"Code display"])' in expression
    assert 'Text.From([#"Version display"])' in expression


def test_combined_group_rejects_ambiguous_source_field_ownership():
    fact = _table('Facts', ['Shared'])
    lookup = _table('Lookup', ['Shared', 'Code'])
    model = {'model': {'tables': [fact, lookup], 'relationships': []}}

    with pytest.raises(ValueError, match=r'Combined group.*Shared.*ambiguous'):
        _process_sets_groups_bins(
            model, {'groups': [{
                'name': 'Ambiguous',
                'group_type': 'combined',
                'source_fields': ['Shared', 'Code'],
            }]}, 'Facts', {'Shared': 'Lookup', 'Code': 'Lookup'},
        )

    assert not any(column.get('name') == 'Ambiguous'
                   for table in (fact, lookup) for column in table['columns'])


def test_combined_group_rejects_unresolved_field_instead_of_skipping():
    fact = _table('Facts', ['Known'])
    model = {'model': {'tables': [fact], 'relationships': []}}

    with pytest.raises(ValueError, match=r'Combined group.*Missing.*unresolved'):
        _process_sets_groups_bins(
            model, {'groups': [{
                'name': 'Incomplete',
                'group_type': 'combined',
                'source_fields': ['Known', 'Missing'],
            }]}, 'Facts', {'Known': 'Facts'},
        )

    assert not any(column.get('name') == 'Incomplete' for column in fact['columns'])


def test_unreferenced_combined_group_with_unresolved_field_is_skipped(caplog):
    fact = _table('Facts', ['Known'])
    model = {'model': {'tables': [fact], 'relationships': []}}
    group = {
        'name': 'Unused combined group',
        'group_type': 'combined',
        'source_fields': ['Known', 'Missing'],
    }

    with caplog.at_level('WARNING', logger='powerbi_import.tmdl_sets'):
        _process_sets_groups_bins(
            model,
            {'groups': [group], 'worksheets': [], 'calculations': [], 'filters': []},
            'Facts', {'Known': 'Facts'},
        )

    assert not any(column.get('name') == group['name'] for column in fact['columns'])
    assert 'Skipping unresolved unreferenced combined group' in caplog.text


def test_referenced_combined_group_with_unresolved_field_still_fails():
    fact = _table('Facts', ['Known'])
    model = {'model': {'tables': [fact], 'relationships': []}}
    group = {
        'name': 'Used combined group',
        'group_type': 'combined',
        'source_fields': ['Known', 'Missing'],
    }

    with pytest.raises(ValueError, match=r'Combined group.*Missing.*unresolved'):
        _process_sets_groups_bins(
            model,
            {
                'groups': [group],
                'worksheets': [{'fields': [{'name': group['name']}]}],
                'calculations': [],
                'filters': [],
            },
            'Facts', {'Known': 'Facts'},
        )


def test_combined_group_rejects_field_without_a_valid_owner():
    fact = _table('Facts', ['Known'])
    unnamed = _table(None, ['Remote'])
    model = {'model': {'tables': [fact, unnamed], 'relationships': []}}

    with pytest.raises(ValueError, match=r'Combined group.*Remote.*no resolved owner'):
        _process_sets_groups_bins(
            model, {'groups': [{
                'name': 'Incomplete',
                'group_type': 'combined',
                'source_fields': ['Known', 'Remote'],
            }]}, 'Facts', {'Known': 'Facts'},
        )

    assert not any(column.get('name') == 'Incomplete' for column in fact['columns'])


def test_combined_group_requires_its_declared_datasource_to_exist():
    fact = _table('Facts', ['Known', 'Version'])
    model = {
        'model': {'tables': [fact], 'relationships': []},
        '_datasources': [{'name': 'Actual', 'tables': [{'name': 'Facts'}]}],
    }

    with pytest.raises(ValueError, match=r'datasource.*Missing.*(missing|unresolved)'):
        _process_sets_groups_bins(
            model, {'groups': [{
                'name': 'Combined',
                'datasource': 'Missing',
                'group_type': 'combined',
                'source_fields': ['Known', 'Version'],
            }]}, 'Facts', {'Known': 'Facts', 'Version': 'Facts'},
        )

    assert not any(column.get('name') == 'Combined' for column in fact['columns'])


def test_combined_group_rejects_an_ambiguous_declared_datasource():
    fact = _table('Facts', ['Known', 'Version'])
    model = {
        'model': {'tables': [fact], 'relationships': []},
        '_datasources': [
            {'name': 'Duplicate', 'tables': [{'name': 'Facts'}]},
            {'name': 'Duplicate', 'tables': [{'name': 'Facts'}]},
        ],
    }

    with pytest.raises(ValueError, match=r'datasource.*Duplicate.*ambiguous'):
        _process_sets_groups_bins(
            model, {'groups': [{
                'name': 'Combined',
                'datasource': 'Duplicate',
                'group_type': 'combined',
                'source_fields': ['Known', 'Version'],
            }]}, 'Facts', {'Known': 'Facts', 'Version': 'Facts'},
        )


def test_combined_group_without_source_fields_is_not_replaced_by_an_empty_string():
    fact = _table('Facts', ['Known'])
    model = {'model': {'tables': [fact], 'relationships': []}}

    with pytest.raises(ValueError, match=r'Combined group.*source fields'):
        _process_sets_groups_bins(
            model, {'groups': [{
                'name': 'Incomplete',
                'group_type': 'combined',
                'source_fields': [],
            }]}, 'Facts', {'Known': 'Facts'},
        )

    assert not any(column.get('name') == 'Incomplete' for column in fact['columns'])


@pytest.mark.parametrize('collision_type', ['column', 'measure', 'source_binding'])
def test_combined_group_rejects_a_case_insensitive_collision_on_its_host(collision_type):
    fact = _table('Facts', ['Known', 'Version'])
    if collision_type == 'column':
        fact['columns'].append({'name': 'combined', 'sourceColumn': 'combined'})
    elif collision_type == 'measure':
        fact['measures'] = [{'name': 'combined', 'expression': '1'}]
    else:
        fact['columns'].append({'name': 'Existing caption', 'sourceColumn': 'combined'})
    model = {'model': {'tables': [fact], 'relationships': []}}

    with pytest.raises(ValueError, match=r'Combined group.*already exists'):
        _process_sets_groups_bins(
            model, {'groups': [{
                'name': 'Combined',
                'group_type': 'combined',
                'source_fields': ['Known', 'Version'],
            }]}, 'Facts', {'Known': 'Facts', 'Version': 'Facts'},
        )


def test_remote_combined_group_rejects_a_same_named_main_table_column():
    fact = _table('Facts', ['Combined'])
    lookup = _table('Lookup', ['Code', 'Version'])
    model = {'model': {'tables': [fact, lookup], 'relationships': []}}

    with pytest.raises(ValueError, match=r'Combined group.*identifier.*Combined.*already exists'):
        _process_sets_groups_bins(
            model, {'groups': [{
                'name': 'Combined',
                'group_type': 'combined',
                'source_fields': ['Code', 'Version'],
            }]}, 'Facts', {'Code': 'Lookup', 'Version': 'Lookup'},
        )

    assert not any(column.get('displayFolder') == 'Groups' for column in lookup['columns'])


def test_combined_group_rejects_a_distinct_group_with_the_same_host_and_name():
    fact = _table('Facts', ['Known', 'Version'])
    model = {'model': {'tables': [fact], 'relationships': []}}
    group = {
        'name': 'Combined',
        'raw_name': 'FirstCombined',
        'group_type': 'combined',
        'source_fields': ['Known', 'Version'],
    }
    _process_sets_groups_bins(
        model, {'groups': [group]}, 'Facts', {'Known': 'Facts', 'Version': 'Facts'})

    duplicate = dict(group, raw_name='OtherCombined')
    with pytest.raises(ValueError, match=r'Combined group.*already exists'):
        _process_sets_groups_bins(
            model, {'groups': [duplicate]}, 'Facts', {'Known': 'Facts', 'Version': 'Facts'})


def test_combined_group_rejects_raw_identifier_colliding_with_a_source_column():
    fact = _table('Facts', ['Known', 'Version'])
    model = {'model': {'tables': [fact], 'relationships': []}}

    with pytest.raises(ValueError, match=r'Combined group.*identifier.*Known.*already exists'):
        _process_sets_groups_bins(
            model, {'groups': [{
                'name': 'Combined',
                'raw_name': 'Known',
                'group_type': 'combined',
                'source_fields': ['Known', 'Version'],
            }]}, 'Facts', {'Known': 'Facts', 'Version': 'Facts'},
        )


def test_group_raw_identifier_checks_only_its_declared_datasource():
    fact = _table('Facts', ['Known', 'Version'])
    other = _table('Other', ['RawCombined'])
    model = {
        'model': {'tables': [fact, other], 'relationships': []},
        '_datasources': [
            {'name': 'Source', 'tables': [{'name': 'Facts'}]},
            {'name': 'Different', 'tables': [{'name': 'Other'}]},
        ],
    }

    _process_sets_groups_bins(
        model, {'groups': [{
            'name': 'Combined',
            'raw_name': 'RawCombined',
            'datasource': 'Source',
            'group_type': 'combined',
            'source_fields': ['Known', 'Version'],
        }]}, 'Facts', {'Known': 'Facts', 'Version': 'Facts'},
    )

    assert any(column.get('name') == 'Combined' for column in fact['columns'])


def test_group_raw_identifier_collision_blocks_dependent_calculation():
    datasource = _composite_datasource()
    datasource['relationships'] = []
    datasource['relation_metadata'] = {}
    datasource['calculations'] = [{
        'name': '[Count]',
        'caption': 'Count',
        'formula': 'COUNTD([Code])',
        'datatype': 'integer',
        'role': 'measure',
    }]
    group = {
        'name': 'Code and version',
        'raw_name': 'Code',
        'datasource': 'Synthetic',
        'group_type': 'combined',
        'source_fields': ['Code', 'Version'],
    }

    with pytest.raises(ValueError, match=r'Combined group.*identifier.*Code.*already exists'):
        _build_semantic_model(
            [datasource], extra_objects={'groups': [group]}, model_mode='import')


def test_combined_group_rejects_unsupported_derivation_even_when_caption_names_a_function():
    fact = _table('Facts', ['Day', 'Version'])
    model = {'model': {'tables': [fact], 'relationships': []}}

    with pytest.raises(ValueError, match='unsupported date-part.*trunc'):
        _process_sets_groups_bins(
            model, {'groups': [{
                'name': 'Combined(YEAR(Day),Version)',
                'group_type': 'combined',
                'source_fields': ['trunc:Day', 'Version'],
            }]}, 'Facts', {'Day': 'Facts', 'Version': 'Facts'},
        )


def _extract_derived_combined_group(reference, name='Combined'):
    root = ET.fromstring(f'''
        <workbook>
          <datasource name="Source">
            <group name="[CombinedField]" caption="{name}">
              <groupfilter function="crossjoin">
                <groupfilter function="level-members" level="[{reference}]"/>
                <groupfilter function="level-members" level="[none:Version:nk]"/>
              </groupfilter>
            </group>
          </datasource>
        </workbook>
    ''')
    with tempfile.TemporaryDirectory() as output:
        extractor = TableauExtractor('synthetic.twb', output_dir=output)
        extractor.extract_groups(root)
        return extractor.workbook_data['groups'][0]


def _derived_group_model(dax_only=False):
    table = _table('Facts', ['Day', 'Version'])
    table['partitions'][0]['source']['expression'] = (
        'let\n    Source = #table(type table [Day = date, Version = text], {})\n'
        'in\n    Source'
    )
    if dax_only:
        table['columns'][0] = {
            'name': 'Day', 'expression': 'TODAY()', 'isCalculated': True,
        }
    return table, {
        'model': {'tables': [table], 'relationships': []},
        '_datasources': [{'name': 'Source', 'tables': [{'name': 'Facts'}]}],
    }


@pytest.mark.parametrize(('reference', 'function'), [
    ('yr:Day:qk', 'Date.Year'),
    ('tyr:Day:qk', 'Date.StartOfYear'),
    ('tmn:Day:qk', 'Date.StartOfMonth'),
    ('tqr:Day:qk', 'Date.StartOfQuarter'),
])
def test_extracted_combined_group_preserves_date_grain_in_m(reference, function):
    group = _extract_derived_combined_group(reference)
    table, model = _derived_group_model()

    _process_sets_groups_bins(
        model, {'groups': [group]}, 'Facts', {'Day': 'Facts', 'Version': 'Facts'})

    expression = table['partitions'][0]['source']['expression']
    assert f'{function}([#"Day"])' in expression
    assert validate_m_query(expression) == []


@pytest.mark.parametrize(('reference', 'expected'), [
    ('tyr:Day:qk', "DATE(YEAR('Facts'[Day]), 1, 1)"),
    ('tqr:Day:qk', "DATE(YEAR('Facts'[Day]), 3 * INT((MONTH('Facts'[Day]) - 1) / 3) + 1, 1)"),
])
def test_extracted_combined_group_preserves_truncated_date_grain_in_dax(reference, expected):
    group = _extract_derived_combined_group(reference)
    table, model = _derived_group_model(dax_only=True)

    _process_sets_groups_bins(
        model, {'groups': [group]}, 'Facts', {'Day': 'Facts', 'Version': 'Facts'})

    expression = next(col for col in table['columns'] if col['name'] == 'Combined')['expression']
    assert expected in expression
    assert "ISBLANK('Facts'[Day])" in expression


def test_extracted_plain_field_does_not_infer_derivation_from_caption():
    group = _extract_derived_combined_group(
        'none:Day:nk', name='Combined(YEAR(Day),Version)')
    table, model = _derived_group_model()

    _process_sets_groups_bins(
        model, {'groups': [group]}, 'Facts', {'Day': 'Facts', 'Version': 'Facts'})

    expression = table['partitions'][0]['source']['expression']
    assert 'Text.From([#"Day"])' in expression
    assert 'Date.Year([#"Day"])' not in expression


@pytest.mark.parametrize(('reference', 'field'), [
    ('none:sum:Day:nk', 'sum:Day'),
    ('none:Name:nk:nk', 'Name:nk'),
])
def test_extracted_combined_group_preserves_literal_prefix_and_suffix(reference, field):
    group = _extract_derived_combined_group(reference)
    table = _table('Facts', [field, 'Version'])
    model = {
        'model': {'tables': [table], 'relationships': []},
        '_datasources': [{'name': 'Source', 'tables': [{'name': 'Facts'}]}],
    }

    _process_sets_groups_bins(
        model, {'groups': [group]}, 'Facts', {field: 'Facts', 'Version': 'Facts'})

    expression = table['partitions'][0]['source']['expression']
    assert f'Text.From([#"{field}"])' in expression


@pytest.mark.parametrize('reference', [
    'trunc:Day:qk', 'twk:Day:qk', 'sum:Day:qk', 'pcto:sum:Day:qk',
])
def test_extracted_combined_group_rejects_unproven_component_derivations(reference):
    group = _extract_derived_combined_group(
        reference, name='Combined(YEAR(Day),Version)')
    _, model = _derived_group_model()

    with pytest.raises(ValueError, match=r'Combined group.*unsupported.*derivation'):
        _process_sets_groups_bins(
            model, {'groups': [group]}, 'Facts', {'Day': 'Facts', 'Version': 'Facts'})


@pytest.mark.parametrize('references', [
    ['tyr:Other:qk', 'none:Version:nk'],
    ['tyr:Day:qk'],
    ['tyr:Day:qk', None],
    'tyr:Day:qk',
])
def test_combined_group_rejects_inconsistent_raw_field_references(references):
    group = _extract_derived_combined_group('tyr:Day:qk')
    group['source_field_refs'] = references
    _, model = _derived_group_model()

    with pytest.raises(ValueError, match=r'Combined group.*source.*inconsistent'):
        _process_sets_groups_bins(
            model, {'groups': [group]}, 'Facts', {'Day': 'Facts', 'Version': 'Facts'})


def test_combined_group_identity_includes_component_derivations():
    group = _extract_derived_combined_group('yr:Day:qk')
    _, model = _derived_group_model()
    _process_sets_groups_bins(
        model, {'groups': [group]}, 'Facts', {'Day': 'Facts', 'Version': 'Facts'})
    before = deepcopy(model)
    _process_sets_groups_bins(
        model, {'groups': [group]}, 'Facts', {'Day': 'Facts', 'Version': 'Facts'})
    assert model == before
    other = deepcopy(group)
    other['source_field_refs'][0] = 'tyr:Day:qk'

    with pytest.raises(ValueError, match=r'Combined group.*already exists'):
        _process_sets_groups_bins(
            model, {'groups': [other]}, 'Facts', {'Day': 'Facts', 'Version': 'Facts'})


def test_extracted_year_group_reaches_pbip_model_and_quality_report():
    group = _extract_derived_combined_group('tyr:Day:qk', name='Calendar group')
    datasource = {
        'name': 'Source',
        'connection': {'class': 'sqlserver', 'server': 'example.invalid', 'dbname': 'db'},
        'tables': [{'name': 'Facts', 'columns': [
            {'name': 'Day', 'datatype': 'date'},
            {'name': 'Version', 'datatype': 'string'},
        ]}],
        'calculations': [],
        'relationships': [],
    }
    converted = {
        'datasources': [datasource],
        'groups': [group],
        'worksheets': [{
            'name': 'Group page',
            'type': 'worksheet',
            'datasource': 'Source',
            'fields': [{
                'name': 'CombinedField', 'datasource': 'Source',
                'shelf': 'rows', 'role': 'dimension',
            }],
        }],
    }

    with tempfile.TemporaryDirectory() as output:
        generator = PowerBIProjectGenerator(output_dir=output)
        project = generator.generate_project(
            'DerivedGroup', converted, model_mode='import', parameterize=False)
        model_tables = glob(os.path.join(
            project, 'DerivedGroup.SemanticModel', 'definition', 'tables', '*.tmdl'))
        assert model_tables
        contents = []
        for path in model_tables:
            with open(path, encoding='utf-8') as stream:
                contents.append(stream.read())
        assert any('Date.StartOfYear([#"Day"])' in content for content in contents)
        assert generator._resolve_field_entity('CombinedField', 'Source') == (
            'Facts', 'Calendar group')
        assert ArtifactValidator.validate_visual_references(project) == []
        report = build_quality_report(converted, project, 'DerivedGroup')
        assert any('Combined group conversion' in warning for warning in report.warnings)


def test_combined_group_quotes_source_headers_and_output_names_in_m():
    fact = _table('Facts', ['Code#(lf)', 'Version'])
    model = {'model': {'tables': [fact], 'relationships': []}}

    _process_sets_groups_bins(
        model, {'groups': [{
            'name': 'Group#(lf)',
            'group_type': 'combined',
            'source_fields': ['Code#(lf)', 'Version'],
        }]}, 'Facts', {'Code#(lf)': 'Facts', 'Version': 'Facts'},
    )

    expression = fact['partitions'][0]['source']['expression']
    assert '[#"Code#(#)(lf)"]' in expression
    assert '"Group#(#)(lf)"' in expression
    assert '#"Added Group#(#)(lf)"' in expression
    assert validate_m_query(expression) == []


@pytest.mark.parametrize('formula', [
    '"[CombinedField]"',
    "'[CombinedField]'",
    '1 /* [CombinedField] */',
    '1 // [CombinedField]\n',
])
def test_group_registration_ignores_names_in_formula_strings_and_comments(formula):
    group = {
        'name': 'Combined',
        'raw_name': 'CombinedField',
        'group_type': 'combined',
        'source_fields': ['Missing'],
    }
    column_table_map = {}

    bindings = _register_combined_group_calculation_bindings(
        [], {'groups': [group]}, {}, {}, 'Facts',
        [{'formula': formula}], {}, column_table_map, {},
    )

    assert bindings == set()
    assert column_table_map == {}


def test_combined_group_preserves_raw_owner_when_captions_exist_on_both_tables():
    fact = _table('Facts', ['FactID', 'Description'])
    lookup = _table('Lookup', ['Description'])
    model = {
        'model': {'tables': [fact, lookup], 'relationships': []},
        '_column_rename_map': {('Lookup', 'RawLookupDescription'): 'Description'},
    }

    _process_sets_groups_bins(
        model, {'groups': [{
            'name': 'Details',
            'group_type': 'combined',
            'source_fields': ['FactID', 'RawLookupDescription'],
        }]}, 'Facts', {'FactID': 'Facts', 'RawLookupDescription': 'Lookup'},
    )

    group = next(column for column in fact['columns'] if column['name'] == 'Details')
    assert "RELATED('Lookup'[Description])" in group['expression']
    assert "'Facts'[Description]" not in group['expression']


def _composite_datasource():
    left = {
        'name': 'Fact',
        'connection': 'synthetic_connection',
        'columns': [
            {'name': 'Code', 'datatype': 'string'},
            {'name': 'Version', 'datatype': 'integer'},
            {'name': 'Amount', 'datatype': 'integer'},
        ],
    }
    right = {
        'name': 'Lookup',
        'connection': 'synthetic_connection',
        'columns': [
            {'name': 'LookupCode', 'datatype': 'string'},
            {'name': 'LookupVersion', 'datatype': 'integer'},
            {'name': 'Description', 'datatype': 'string'},
        ],
    }
    predicate = {
        'operator': 'AND',
        'arguments': [
            {'operator': '=', 'left': {'table': 'Fact', 'column': 'Code'},
             'right': {'table': 'Lookup', 'column': 'LookupCode'}},
            {'operator': '=', 'left': {'table': 'Fact', 'column': 'Version'},
             'right': {'table': 'Lookup', 'column': 'LookupVersion'}},
        ],
    }
    return {
        'name': 'Synthetic',
        'connection': {
            'type': 'SQL Server', 'server': 'synthetic_server',
            'database': 'synthetic_database',
        },
        'tables': [left, right],
        'columns': left['columns'] + right['columns'],
        'relationships': [
            {'type': 'left',
             'left': {'table': 'Fact', 'column': 'Code'},
             'right': {'table': 'Lookup', 'column': 'LookupCode'}},
            {'type': 'left',
             'left': {'table': 'Fact', 'column': 'Version'},
             'right': {'table': 'Lookup', 'column': 'LookupVersion'}},
        ],
        'relation_metadata': {
            'logical_relationships': [{
                'left': {'tables': ['Fact']},
                'right': {'tables': ['Lookup']},
                'predicate': predicate,
                'requires_materialization': True,
            }],
        },
        'calculations': [],
    }


def _composite_policy(**overrides):
    policy = {
        'one_side': 'right',
        'comparison_case_sensitive': True,
        'comparison_exact': True,
        'nulls_match': False,
        'filter_direction': 'oneDirection',
    }
    policy.update(overrides)
    return {'Synthetic': {'0': policy}}


def _many_to_many_policy(**overrides):
    policy = {
        'cardinality': 'manyToMany',
        'comparison_case_sensitive': True,
        'comparison_exact': True,
        'nulls_match': False,
        'filter_direction': 'oneDirection',
        'filter_from': 'left',
    }
    policy.update(overrides)
    if policy['filter_direction'] == 'bothDirections':
        policy.pop('filter_from', None)
    return {'Synthetic': {'0': policy}}


def _two_composite_datasource():
    datasource = _composite_datasource()
    rates = {
        'name': 'Rates',
        'connection': 'synthetic_connection',
        'columns': [
            {'name': 'RateCode', 'datatype': 'string'},
            {'name': 'RateVersion', 'datatype': 'integer'},
        ],
    }
    datasource['tables'].append(rates)
    datasource['columns'].extend(rates['columns'])
    pairs = [('Code', 'RateCode'), ('Version', 'RateVersion')]
    datasource['relationships'].extend({
        'type': 'left',
        'left': {'table': 'Fact', 'column': left},
        'right': {'table': 'Rates', 'column': right},
    } for left, right in pairs)
    datasource['relation_metadata']['logical_relationships'].append({
        'left': {'tables': ['Fact']},
        'right': {'tables': ['Rates']},
        'predicate': {
            'operator': 'AND',
            'arguments': [{
                'operator': '=',
                'left': {'table': 'Fact', 'column': left},
                'right': {'table': 'Rates', 'column': right},
            } for left, right in pairs],
        },
        'requires_materialization': True,
    })
    return datasource


def _two_composite_policies():
    policies = _composite_policy()
    policies['Synthetic']['1'] = deepcopy(policies['Synthetic']['0'])
    return policies


def test_composite_relationship_uses_encoded_key_and_refresh_uniqueness_check():
    model = _build_semantic_model(
        [_composite_datasource()],
        extra_objects={'composite_relationship_policies': _composite_policy()},
        model_mode='import',
    )

    assert len(model['model']['relationships']) == 1
    relationship = model['model']['relationships'][0]
    assert relationship['fromTable'] == 'Fact'
    assert relationship['toTable'] == 'Lookup'
    assert relationship['fromCardinality'] == 'many'
    assert relationship['toCardinality'] == 'one'
    assert relationship['crossFilteringBehavior'] == 'oneDirection'
    assert relationship['fromColumn'] == relationship['toColumn']
    for table in model['model']['tables']:
        key = next(column for column in table['columns']
                   if column['name'] == relationship['fromColumn'])
        assert key['isHidden'] is True
        expression = table['partitions'][0]['source']['expression']
        assert validate_m_query(expression) == []
        assert 'Binary.ToText(Text.ToBinary' in expression
        assert 'Text.Length(__ttpbi_value_0)' in expression
        assert 'Table.RemoveColumns' in expression
        if table['name'] == relationship['toTable']:
            assert 'Migration.CompositeKeyNotUnique' in expression
        assert 'N:L:' in expression or 'N:R:' in expression
        assert 'COMBINEVALUES' not in expression


def test_combined_group_can_use_refresh_validated_composite_relationship():
    group = {
        'name': 'Amount and description',
        'group_type': 'combined',
        'source_fields': ['Amount', 'Description'],
    }

    model = _build_semantic_model(
        [_composite_datasource()],
        extra_objects={
            'groups': [group],
            'composite_relationship_policies': _composite_policy(),
        },
        model_mode='import',
    )

    fact = next(table for table in model['model']['tables'] if table['name'] == 'Fact')
    column = next(column for column in fact['columns'] if column['name'] == group['name'])
    assert "RELATED('Lookup'[Description])" in column['expression']
    assert 'Migration.CompositeKeyNotUnique' in next(
        table for table in model['model']['tables']
        if table['name'] == 'Lookup'
    )['partitions'][0]['source']['expression']


def test_remote_combined_group_visual_uses_produced_table_and_raw_identifier():
    datasource = _composite_datasource()
    datasource['relationships'] = []
    datasource['relation_metadata'] = {}
    group = {
        'name': 'Code and description',
        'raw_name': 'CombinedField',
        'datasource': 'Synthetic',
        'group_type': 'combined',
        'source_fields': ['LookupCode', 'Description'],
    }
    converted = {
        'datasources': [datasource],
        'groups': [group],
        'worksheets': [{
            'name': 'Group page',
            'type': 'worksheet',
            'datasource': 'Synthetic',
            'fields': [{
                'name': 'CombinedField',
                'datasource': 'Synthetic',
                'shelf': 'rows',
                'role': 'dimension',
            }],
        }],
    }
    with tempfile.TemporaryDirectory() as output:
        generator = PowerBIProjectGenerator(output_dir=output)
        project = generator.generate_project(
            'GroupBinding', converted, model_mode='import', parameterize=False)
        assert generator._resolve_field_entity('CombinedField') == (
            'Lookup', 'Code and description')
        assert generator._resolve_field_entity('CombinedField', 'Synthetic') == (
            'Lookup', 'Code and description')
        paths = glob(os.path.join(
            project, 'GroupBinding.Report', 'definition', 'pages', '*',
            'visuals', '*', 'visual.json'))
        assert paths
        documents = []
        for path in paths:
            with open(path, encoding='utf-8') as stream:
                documents.append(json.load(stream))
        assert any('"Property": "Code and description"' in json.dumps(document)
                   and '"Entity": "Lookup"' in json.dumps(document)
                   for document in documents), json.dumps(documents, indent=2)
        assert ArtifactValidator.validate_visual_references(project) == []
        parity = scan_project(converted, project, 'GroupBinding')
        usage = next(item for item in parity.usages if item.key == 'combined_groups')
        assert usage.status == 'approximated'
        assert usage.evidence_status == 'evidenced'
        report = build_quality_report(converted, project, 'GroupBinding')
        assert any('Combined group conversion' in item for item in report.warnings)
        strict = build_quality_report(
            converted, project, 'GroupBinding', quality_policy='production')
        assert any('Combined group conversion' in item for item in strict.blockers)


def test_measure_reference_to_remote_combined_group_uses_its_actual_column():
    datasource = _composite_datasource()
    datasource['relationships'] = []
    datasource['relation_metadata'] = {}
    datasource['calculations'] = [{
        'name': '[GroupCount]',
        'caption': 'Group count',
        'formula': 'COUNTD([CombinedField])',
        'datatype': 'integer',
        'role': 'measure',
    }]
    group = {
        'name': 'Code and description',
        'raw_name': 'CombinedField',
        'datasource': 'Synthetic',
        'group_type': 'combined',
        'source_fields': ['LookupCode', 'Description'],
    }

    model = _build_semantic_model(
        [datasource], extra_objects={'groups': [group]}, model_mode='import')

    measure = next(
        measure for table in model['model']['tables']
        for measure in table.get('measures', [])
        if measure['name'] == 'Group count'
    )
    assert "'Lookup'[Code and description]" in measure['expression']
    assert '[CombinedField]' not in measure['expression']
    assert "ISEMPTY('Lookup')" in measure['expression']


@pytest.mark.parametrize('with_relationship', [False, True])
def test_row_calculation_depending_on_remote_group_requires_proven_relationship(
        with_relationship):
    datasource = _composite_datasource()
    if not with_relationship:
        datasource['relationships'] = []
        datasource['relation_metadata'] = {}
    datasource['calculations'] = [{
        'name': '[GroupLabel]',
        'caption': 'Group label',
        'formula': 'IF [CombinedField] = "test" THEN "yes" ELSE "no" END',
        'datatype': 'string',
        'role': 'dimension',
    }]
    extra = {'groups': [{
        'name': 'Code and description',
        'raw_name': 'CombinedField',
        'datasource': 'Synthetic',
        'group_type': 'combined',
        'source_fields': ['LookupCode', 'Description'],
    }]}
    if with_relationship:
        extra['composite_relationship_policies'] = _composite_policy()
        model = _build_semantic_model(
            [datasource], extra_objects=extra, model_mode='import')
        fact = next(table for table in model['model']['tables'] if table['name'] == 'Fact')
        column = next(column for column in fact['columns'] if column['name'] == 'Group label')
        assert "RELATED('Lookup'[Code and description])" in column['expression']
    else:
        with pytest.raises(ValueError, match='unambiguous active to-one source'):
            _build_semantic_model(
                [datasource], extra_objects=extra, model_mode='import')


def test_local_group_dependent_calculation_is_not_injected_before_its_m_column():
    datasource = _composite_datasource()
    datasource['relationships'] = []
    datasource['relation_metadata'] = {}
    datasource['calculations'] = [{
        'name': '[GroupLabel]',
        'caption': 'Group label',
        'formula': 'IF [CombinedField] = "test" THEN "yes" ELSE "no" END',
        'datatype': 'string',
        'role': 'dimension',
    }]
    group = {
        'name': 'Code and version',
        'raw_name': 'CombinedField',
        'datasource': 'Synthetic',
        'group_type': 'combined',
        'source_fields': ['Code', 'Version'],
    }

    model = _build_semantic_model(
        [datasource], extra_objects={'groups': [group]}, model_mode='import')

    fact = next(table for table in model['model']['tables'] if table['name'] == 'Fact')
    column = next(column for column in fact['columns'] if column['name'] == 'Group label')
    assert "'Fact'[Code and version]" in column['expression']
    partition = fact['partitions'][0]['source']['expression']
    assert 'Table.AddColumn' in partition
    assert '"Code and version"' in partition
    assert '"Group label"' not in partition


def test_combined_group_rejects_composite_path_deactivated_by_another_cycle():
    datasource = _composite_datasource()
    bridge = {
        'name': 'Bridge',
        'connection': 'synthetic_connection',
        'columns': [{'name': 'BridgeKey', 'datatype': 'integer'}],
    }
    datasource['tables'].append(bridge)
    datasource['columns'].extend(bridge['columns'])
    datasource['relationships'].extend([
        {
            'type': 'left',
            'left': {'table': 'Fact', 'column': 'Amount'},
            'right': {'table': 'Bridge', 'column': 'BridgeKey'},
        },
        {
            'type': 'left',
            'left': {'table': 'Lookup', 'column': 'LookupVersion'},
            'right': {'table': 'Bridge', 'column': 'BridgeKey'},
        },
    ])
    extra = {
        'groups': [{
            'name': 'Amount and description',
            'group_type': 'combined',
            'source_fields': ['Amount', 'Description'],
        }],
        'composite_relationship_policies': _composite_policy(),
    }

    with pytest.raises(ValueError, match='unambiguous active to-one source'):
        _build_semantic_model([datasource], extra_objects=extra, model_mode='import')


def test_composite_relationship_requires_policy_and_never_keeps_partial_edges():
    from pytest import raises

    with raises(ValueError, match='complete predicate has multiple equality terms'):
        _build_semantic_model([_composite_datasource()], model_mode='import')


@pytest.mark.parametrize('strict', [False, True])
def test_batch_preserves_composite_failure_with_optional_repair_project(
        tmp_path, monkeypatch, strict):
    import migrate

    sources = tmp_path / 'sources'
    sources.mkdir()
    (sources / 'synthetic.twb').write_text('<workbook/>', encoding='utf-8')
    objects = {'datasources': [_composite_datasource()], 'dashboards': [{
        'name': 'Overview', 'objects': [],
        'size': {'width': 1280, 'height': 720},
    }]}

    def extract(source, output_dir, **kwargs):
        os.makedirs(output_dir, exist_ok=True)
        for key, value in objects.items():
            with open(os.path.join(output_dir, key + '.json'), 'w',
                      encoding='utf-8') as handle:
                json.dump(value, handle)
        return True

    def reject_postprocessing(*args, **kwargs):
        pytest.fail('A data-free repair project must not collect source data')

    monkeypatch.setattr(migrate, 'run_extraction', extract)
    monkeypatch.setattr(migrate, '_stage_declared_local_sources',
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(migrate, '_collect_local_data_files', reject_postprocessing)
    output = tmp_path / 'output'
    result = migrate.run_batch_migration(
        str(sources), output_dir=str(output), calendar_mode='fixed',
        strict_generation=strict, quiet=True)

    assert result == migrate.ExitCode.BATCH_PARTIAL_FAIL
    assert bool(list(output.rglob('*.pbip'))) is not strict
    assert bool(list(output.rglob('partial_generation.json'))) is not strict
    quality_paths = list(output.rglob('migration_quality_*.json'))
    assert len(quality_paths) == 1
    with open(quality_paths[0], encoding='utf-8') as handle:
        quality = json.load(handle)
    assert quality['status'] == 'FAIL'
    assert any('complete predicate has multiple equality terms' in issue
               for issue in quality['blockers'])
    with open(output / 'batch_quality_summary.json', encoding='utf-8') as handle:
        summary = json.load(handle)
    assert summary['generation_counts'] == {
        'complete': 0, 'partial': 0 if strict else 1, 'failed': 1 if strict else 0,
    }
    assert next(iter(summary['workbooks'].values()))['generation_status'] == (
        'failed' if strict else 'partial')
    if not strict:
        dashboard = (output / 'MIGRATION_DASHBOARD.html').read_text(encoding='utf-8')
        assert 'PARTIAL / NOT VALIDATED' in dashboard


def test_composite_relationship_rejects_case_insensitive_key_assumptions():
    from pytest import raises

    with raises(ValueError, match='case-sensitive source equality'):
        _build_semantic_model(
            [_composite_datasource()],
            extra_objects={
                'composite_relationship_policies':
                    _composite_policy(comparison_case_sensitive=False),
            },
            model_mode='import',
        )


def test_composite_relationship_can_declare_left_as_the_unique_side():
    model = _build_semantic_model(
        [_composite_datasource()],
        extra_objects={
            'composite_relationship_policies': _composite_policy(one_side='left'),
        },
        model_mode='import',
    )

    relationship = model['model']['relationships'][0]
    assert relationship['fromTable'] == 'Lookup'
    assert relationship['toTable'] == 'Fact'
    one_side = next(table for table in model['model']['tables']
                    if table['name'] == 'Fact')
    expression = one_side['partitions'][0]['source']['expression']
    assert 'Migration.CompositeKeyNotUnique' in expression


def test_composite_relationship_matching_nulls_preserves_other_components():
    model = _build_semantic_model(
        [_composite_datasource()],
        extra_objects={
            'composite_relationship_policies':
                _composite_policy(nulls_match=True),
        },
        model_mode='import',
    )

    for table in model['model']['tables']:
        expression = table['partitions'][0]['source']['expression']
        assert 'then "N0:" else "S"' in expression
        assert 'then "N0:" else "I"' in expression
        assert ') & (if ' in expression
        assert '"N:"' not in expression


def test_composite_relationships_on_one_table_keep_every_generated_key():
    model = _build_semantic_model(
        [_two_composite_datasource()],
        extra_objects={'composite_relationship_policies': _two_composite_policies()},
        model_mode='import',
    )

    relationships = model['model']['relationships']
    assert len(relationships) == 2
    fact = next(table for table in model['model']['tables'] if table['name'] == 'Fact')
    expression = fact['partitions'][0]['source']['expression']
    assert validate_m_query(expression) == []
    for relation in relationships:
        key = relation['fromColumn']
        assert any(column['name'] == key for column in fact['columns'])
        assert f'"{key}"' in expression


def test_composite_rejects_multiple_partitions_without_mutating_the_model():
    datasource = _composite_datasource()
    tables = [
        _table(table['name'], [column['name'] for column in table['columns']])
        for table in datasource['tables']
    ]
    for table in tables:
        table['partitions'][0]['mode'] = 'import'
    tables[1]['partitions'].append(deepcopy(tables[1]['partitions'][0]))
    model = {'model': {'tables': tables}, '_datasources': [datasource]}
    original = deepcopy(model)

    with pytest.raises(ValueError, match='single Import M partition'):
        _materialize_composite_logical_relationships(
            model, {'composite_relationship_policies': _composite_policy()})

    assert model == original


def test_composite_whole_number_type_uses_integer_encoding():
    expression = _composite_key_m_expression(
        [('Quantity', 'whole number'), ('Active', 'boolean')],
        side='L', nulls_match=False, row_name='Row',
    )
    assert 'Number.ToText([#"Quantity"], "0", "en-US")' in expression


def test_composite_key_quotes_literal_m_escape_sequences_in_source_headers():
    expression = _composite_key_m_expression(
        [('Code#(lf)', 'string'), ('Column\tVersion', 'integer')],
        side='L', nulls_match=False, row_name='Row',
    )
    assert '[#"Code#(#)(lf)"]' in expression
    assert '[#"Column#(tab)Version"]' in expression


def test_composite_key_encodes_date_and_datetime_with_invariant_formats():
    expression = _composite_key_m_expression(
        [('Day', 'date'), ('Timestamp', 'datetime')],
        side='L', nulls_match=False, row_name='Row',
    )

    assert 'Date.ToText([#"Day"], [Format = "yyyy-MM-dd", Culture = "en-US"])' in expression
    assert ('DateTime.ToText([#"Timestamp"], '
            '[Format = "yyyy-MM-ddTHH:mm:ss.fffffff", Culture = "en-US"])') in expression
    assert validate_m_query('let Key = ' + expression + ' in Key') == []


def test_composite_rejects_a_dax_only_key_before_mutating_the_model():
    datasource = _composite_datasource()
    tables = [
        _table(table['name'], [column['name'] for column in table['columns']])
        for table in datasource['tables']
    ]
    for table in tables:
        table['partitions'][0]['mode'] = 'import'
    column = tables[1]['columns'][0]
    del column['sourceColumn']
    column['expression'] = '"synthetic"'
    column['isCalculated'] = True
    model = {'model': {'tables': tables}, '_datasources': [datasource]}
    original = deepcopy(model)

    with pytest.raises(ValueError, match='source-bound.*M'):
        _materialize_composite_logical_relationships(
            model, {'composite_relationship_policies': _composite_policy()})

    assert model == original


def test_environment_composite_policies_can_cover_other_batch_datasources(monkeypatch):
    monkeypatch.setenv(
        'TTPBI_COMPOSITE_RELATIONSHIP_POLICIES', json.dumps(_composite_policy()))
    datasource = _composite_datasource()
    datasource['name'] = 'Other'
    datasource['relationships'] = []
    datasource['relation_metadata']['logical_relationships'] = []

    model = _build_semantic_model([datasource], model_mode='import')

    assert not model['model']['relationships']


def test_composite_relationship_rejects_non_exact_string_comparison():
    from pytest import raises

    with raises(ValueError, match='exact source string equality'):
        _build_semantic_model(
            [_composite_datasource()],
            extra_objects={
                'composite_relationship_policies':
                    _composite_policy(comparison_exact=False),
            },
            model_mode='import',
        )


def test_composite_relationship_survives_tmdl_serialization():
    with tempfile.TemporaryDirectory() as output:
        stats = generate_tmdl(
            [_composite_datasource()],
            'Synthetic',
            {'composite_relationship_policies': _composite_policy()},
            output,
            model_mode='import',
            parameterize=False,
        )
        path = os.path.join(output, 'definition', 'relationships.tmdl')
        with open(path, encoding='utf-8') as stream:
            written = stream.read()

    assert stats['relationships'] == 1
    assert '__TTPBI_CompositeKey_' in written
    assert 'fromCardinality: many' not in written
    assert 'toCardinality: many' not in written


@pytest.mark.parametrize('filter_direction', ['oneDirection', 'bothDirections'])
def test_composite_many_to_many_manifest_and_quality_warning(
    tmp_path, monkeypatch, filter_direction):
    datasource = _composite_datasource()
    objects = {'datasources': [datasource], 'worksheets': [], 'dashboards': []}
    monkeypatch.setenv(
        'TTPBI_COMPOSITE_RELATIONSHIP_POLICIES',
        json.dumps(_many_to_many_policy(filter_direction=filter_direction)),
    )

    project = PowerBIProjectGenerator(output_dir=str(tmp_path)).generate_project(
        'ManyToManyProject', objects, model_mode='import')
    semantic_model = os.path.join(project, 'ManyToManyProject.SemanticModel')
    manifest_path = os.path.join(semantic_model, 'composite_relationships.json')
    with open(manifest_path, encoding='utf-8') as handle:
        manifest = json.load(handle)

    relationship_path = os.path.join(
        semantic_model, 'definition', 'relationships.tmdl')
    with open(relationship_path, encoding='utf-8') as handle:
        relationships = handle.read()
    assert 'fromCardinality: many' in relationships
    assert 'toCardinality: many' in relationships
    assert f'crossFilteringBehavior: {filter_direction}' in relationships
    for table_name in ('Fact', 'Lookup'):
        table_path = os.path.join(
            semantic_model, 'definition', 'tables', table_name + '.tmdl')
        with open(table_path, encoding='utf-8') as handle:
            table_definition = handle.read()
        assert '__TTPBI_CompositeKey_' in table_definition
        assert 'Migration.CompositeKeyNotUnique' not in table_definition
    assert len(manifest) == 1
    assert manifest[0]['status'] == 'approximated'
    assert manifest[0]['key_components'] == [
        {'left_column': 'Code', 'right_column': 'LookupCode'},
        {'left_column': 'Version', 'right_column': 'LookupVersion'},
    ]

    quality = build_quality_report(objects, project, 'ManyToManyProject')
    assert len(quality.composite_relationships) == 1
    assert any('many-to-many' in warning.lower() for warning in quality.warnings)
    assert any(priority['action_kind'] == 'verify'
               and 'many-to-many' in priority['action'].lower()
               for priority in quality.priorities)
    html = quality.render_body()
    assert 'Composite many-to-many relationships' in html
    assert 'Validate filter propagation' in html
