"""CSV partitions are wired to the right file and match its real header."""

import os
import csv
import json
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import migrate

NAME = "Report"


def _write(path, data, encoding="utf-8"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode, **({} if mode == "wb" else {"encoding": encoding, "newline": ""})) as f:
        f.write(data)
    return path


def _project(root, tables):
    """tables: {table_name: partition M body}. Returns the project dir."""
    project = os.path.join(root, NAME)
    definition = os.path.join(project, f"{NAME}.SemanticModel", "definition")
    data = os.path.join(project, "Data").replace("\\", "\\\\")
    _write(os.path.join(definition, "expressions.tmdl"),
           f'expression DataFolder = "{data}" meta [IsParameterQuery=true]\n')
    for table, body in tables.items():
        _write(os.path.join(definition, "tables", f"{table}.tmdl"),
               f"table {table}\n\tpartition {table} = m\n\t\tsource =\n{body}\n")
    return project


def _csv_partition(filename, columns, delimiter=",", count=None):
    types = ",\n".join('\t\t\t\t\t\t{"%s", type text}' % c.replace('"', '""') for c in columns)
    return (
        "\t\t\t\tlet\n"
        f'\t\t\t\t\tSource = Csv.Document(File.Contents(DataFolder & "\\\\{filename}"), '
        f'[Delimiter="{delimiter}", Columns={count or len(columns)}, Encoding=65001, '
        "QuoteStyle=QuoteStyle.None]),\n"
        '\t\t\t\t\t#"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n'
        '\t\t\t\t\t#"Changed Types" = Table.TransformColumnTypes(#"Promoted Headers", {\n'
        f"{types}\n\t\t\t\t\t}}),\n"
        '\t\t\t\t\tResult = #"Changed Types"\n'
        "\t\t\t\tin\n\t\t\t\t\tResult")


def _excel_partition(filename, columns):
    cols = ", ".join('"%s"' % c for c in columns)
    types = ",\n".join('\t\t\t\t\t\t{"%s", type text}' % c for c in columns)
    return (
        "\t\t\t\tlet\n"
        "\t\t\t\t\tSource = try let\n"
        f'\t\t\t\t\t\t_src = Excel.Workbook(File.Contents(DataFolder & "\\\\{filename}"), null, true),\n'
        '\t\t\t\t\t\t_nav = _src{[Item="Sheet",Kind="Sheet"]}[Data]\n'
        "\t\t\t\t\tin _nav otherwise #table({" + cols + "}, {}),\n"
        '\t\t\t\t\t#"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n'
        '\t\t\t\t\t#"Changed Types" = Table.TransformColumnTypes(#"Promoted Headers", {\n'
        f"{types}\n\t\t\t\t\t}}),\n"
        '\t\t\t\t\tResult = #"Changed Types"\n'
        "\t\t\t\tin\n\t\t\t\t\tResult")


def _read(project, table):
    with open(os.path.join(project, f"{NAME}.SemanticModel", "definition",
                           "tables", f"{table}.tmdl"), encoding="utf-8") as f:
        return f.read()


class TestReconcileCsvPartitions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_semicolon_file_gets_semicolon_delimiter(self):
        project = _project(self.root, {"T": _csv_partition("a.csv", ["x", "y"])})
        _write(os.path.join(project, "Data", "a.csv"), "x;y\n1;2\n")
        stats = migrate._reconcile_csv_partitions(project, NAME)
        text = _read(project, "T")
        self.assertIn('Delimiter=";"', text)
        self.assertIn("QuoteStyle=QuoteStyle.Csv", text)
        self.assertEqual(stats["options_fixed"], 1)

    def test_windows_1252_file_gets_matching_encoding(self):
        project = _project(self.root, {"T": _csv_partition("a.csv", ["Année"])})
        _write(os.path.join(project, "Data", "a.csv"), "Année\n2024\n".encode("cp1252"))
        migrate._reconcile_csv_partitions(project, NAME)
        self.assertIn("Encoding=1252", _read(project, "T"))

    def test_fixed_column_count_is_removed_so_wide_files_are_not_truncated(self):
        project = _project(self.root, {"T": _csv_partition("a.csv", ["z"], count=1)})
        _write(os.path.join(project, "Data", "a.csv"), "a,b,z\n1,2,3\n")
        migrate._reconcile_csv_partitions(project, NAME)
        self.assertNotIn("Columns=", _read(project, "T"))

    def test_joined_column_caption_is_renamed_from_file_header(self):
        project = _project(self.root, {
            "T": _csv_partition("a.csv", ["id", "name (other.csv)"])})
        _write(os.path.join(project, "Data", "a.csv"), "id,name\n1,x\n")
        stats = migrate._reconcile_csv_partitions(project, NAME)
        text = _read(project, "T")
        self.assertIn('Table.TransformColumnTypes(Table.RenameColumns(#"Promoted Headers", '
                      '{{"name", "name (other.csv)"}})', text)
        self.assertEqual(stats["columns_renamed"], 1)

    def test_column_absent_from_file_becomes_empty_column_and_is_counted(self):
        project = _project(self.root, {"T": _csv_partition("a.csv", ["id", "ghost"])})
        _write(os.path.join(project, "Data", "a.csv"), "id\n1\n")
        stats = migrate._reconcile_csv_partitions(project, NAME)
        text = _read(project, "T")
        self.assertEqual(stats["columns_unmatched"], 1)
        self.assertIn('Table.AddColumn(#"Promoted Headers", "ghost", each null)', text)
        self.assertNotIn("RenameColumns", text)

    def test_missing_file_leaves_partition_untouched(self):
        body = _csv_partition("absent.csv", ["x"])
        project = _project(self.root, {"T": body})
        migrate._reconcile_csv_partitions(project, NAME)
        self.assertIn('Delimiter=","', _read(project, "T"))
        self.assertIn("Columns=1", _read(project, "T"))


class TestHyperCsvRouting(unittest.TestCase):
    def test_pick_requires_full_column_coverage(self):
        headers = {"a.csv": {"x", "y"}, "b.csv": {"x", "z"}}
        self.assertEqual(migrate._pick_csv_for_columns(["X", "Z"], headers), "b.csv")
        self.assertIsNone(migrate._pick_csv_for_columns(["x", "q"], headers))

    def test_pick_refuses_equal_ambiguity_but_prefers_tighter_header(self):
        self.assertIsNone(migrate._pick_csv_for_columns(
            ["x"], {"a.csv": {"x", "y"}, "b.csv": {"x", "z"}}))
        self.assertEqual(migrate._pick_csv_for_columns(
            ["x"], {"a.csv": {"x"}, "b.csv": {"x", "z"}}), "a.csv")

    def test_each_excel_partition_is_routed_to_its_own_extract_csv(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {
                "Sales": _excel_partition("sales.xlsx", ["Region", "Amount"]),
                "Stock": _excel_partition("stock.xlsx", ["Item", "Qty"]),
            })
            data = os.path.join(project, "Data")
            _write(os.path.join(data, "sales_extract.csv"), "Region,Amount\nN,1\n")
            _write(os.path.join(data, "stock_extract.csv"), "Item,Qty\nA,2\n")
            _write(os.path.join(data, "sales_extract.hyper"), b"x")
            _write(os.path.join(data, "stock_extract.hyper"), b"x")
            migrate._convert_hyper_to_csv_in_data(data, NAME, project)
            self.assertIn("sales_extract.csv", _read(project, "Sales"))
            self.assertIn("stock_extract.csv", _read(project, "Stock"))
            self.assertNotIn("stock_extract.csv", _read(project, "Sales"))

    def test_packaged_excel_file_is_kept(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"Sales": _excel_partition("sales.xlsx", ["Region"])})
            data = os.path.join(project, "Data")
            _write(os.path.join(data, "sales.xlsx"), b"xlsx")
            _write(os.path.join(data, "e.csv"), "Region\nN\n")
            _write(os.path.join(data, "e.hyper"), b"x")
            migrate._convert_hyper_to_csv_in_data(data, NAME, project)
            self.assertIn("Excel.Workbook", _read(project, "Sales"))

    def test_extract_csv_name_follows_hyper_file_not_inner_table(self):
        import csv
        with tempfile.TemporaryDirectory() as out:
            hyper = os.path.join(out, "federated_abc.hyper")
            _write(hyper, b"not a real hyper")
            name = os.path.splitext(os.path.basename(hyper))[0]
            self.assertTrue(migrate._hyper_to_csv_files(hyper, out, NAME, csv) == {}
                            or all(k.startswith(name) for k in
                                   migrate._hyper_to_csv_files(hyper, out, NAME, csv)))


def _geo_partition(filename, columns):
    from tableau_export.m_query_builder import _gen_m_geojson, wrap_source_with_try_otherwise
    cols = [{"name": c, "datatype": "string"} for c in columns]
    m = _gen_m_geojson({"filename": filename}, "T", cols)
    m = wrap_source_with_try_otherwise(m, columns)
    return "\n".join("\t\t\t\t" + line for line in m.splitlines())


def _dbf(fields, rows):
    """Minimal dBase III file: fields [(name, length)], rows of strings."""
    import struct
    record_len = 1 + sum(length for _n, length in fields)
    header_len = 32 + 32 * len(fields) + 1
    out = bytearray(struct.pack("<BBBBIHH20x", 3, 124, 1, 1, len(rows), header_len, record_len))
    for name, length in fields:
        out += name.encode("ascii").ljust(11, b"\x00") + b"C" + b"\x00" * 4
        out += bytes([length, 0]) + b"\x00" * 14
    out += b"\x0d"
    for row in rows:
        out += b" " + b"".join(v.encode("utf-8").ljust(length)[:length]
                              for v, (_n, length) in zip(row, fields))
    return bytes(out + b"\x1a")


class TestReconcileHeaderVariants(unittest.TestCase):
    def test_case_only_difference_is_renamed(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"T": _csv_partition("a.csv", ["INSEE"])})
            _write(os.path.join(project, "Data", "a.csv"), "insee\n1\n")
            migrate._reconcile_csv_partitions(project, NAME)
            self.assertIn('Table.RenameColumns(#"Promoted Headers", {{"insee", "INSEE"}})',
                          _read(project, "T"))

    def test_one_file_column_feeding_two_captions_is_copied(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"T": _csv_partition(
                "a.csv", ["geometry", "geometry (a.geojson)", "geometry (b.geojson)"])})
            _write(os.path.join(project, "Data", "a.csv"), "geometry\nx\n")
            migrate._reconcile_csv_partitions(project, NAME)
            text = _read(project, "T")
            self.assertIn('"geometry", "geometry (a.geojson)")', text)
            self.assertIn('"geometry", "geometry (b.geojson)")', text)
            self.assertNotIn("RenameColumns", text)


class TestRestrictToModelColumns(unittest.TestCase):
    def test_partition_ends_on_declared_source_columns_only(self):
        with tempfile.TemporaryDirectory() as root:
            body = _csv_partition("a.csv", ["x", "y"])
            project = _project(root, {"T": body})
            path = os.path.join(project, f"{NAME}.SemanticModel", "definition", "tables", "T.tmdl")
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n\tcolumn x\n\t\tsourceColumn: x\n\n\tcolumn 'a b'\n\t\tsourceColumn: a b\n")
            self.assertEqual(migrate._restrict_file_partitions_to_model(project, NAME), 1)
            text = _read(project, "T")
            self.assertIn('#"Model Columns" = Table.SelectColumns(Result, {"x", "a b"}, '
                          'MissingField.UseNull)', text)
            self.assertIn('in\n\t\t\t\t\t#"Model Columns"', text)
            self.assertEqual(migrate._restrict_file_partitions_to_model(project, NAME), 0)


class TestSpatialRouting(unittest.TestCase):
    def test_single_workbook_runs_coordinate_wiring_after_csv_routing(self):
        order = []
        names = ('_fix_twb_data_folder', '_collect_local_data_files',
                 '_convert_hyper_to_csv_in_data', '_route_spatial_partitions',
                 '_reconcile_csv_partitions', '_wire_shapefile_map_coordinates',
                 '_restrict_file_partitions_to_model', '_shorten_report_artifact_path')
        with mock.patch.multiple(migrate, **{
            name: mock.Mock(side_effect=lambda *args, step=name: order.append(step))
            for name in names
        }):
            migrate._extract_twbx_data_files(
                SimpleNamespace(tableau_file='sample.twb', output_dir='out',
                                output_format='pbip'), NAME)
        self.assertLess(order.index('_route_spatial_partitions'),
                        order.index('_wire_shapefile_map_coordinates'))
        self.assertLess(order.index('_reconcile_csv_partitions'),
                        order.index('_wire_shapefile_map_coordinates'))
        self.assertLess(order.index('_wire_shapefile_map_coordinates'),
                        order.index('_restrict_file_partitions_to_model'))

    def test_wgs84_polygon_points_join_to_map_fact_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {
                'Facts': _csv_partition('facts.csv', ['code_insee', 'Amount']),
                'Places': _csv_partition('zones.shp.csv', ['insee']),
            })
            definition = os.path.join(project, f'{NAME}.SemanticModel', 'definition')
            shp = os.path.join(project, 'Data', 'zones.shp')
            ring = [(-1., -1.), (1., -1.), (1., 1.), (-1., 1.), (-1., -1.)]
            polygon = (struct.pack('<I4dII', 5, -1., -1., 1., 1., 1, len(ring))
                       + struct.pack('<I', 0)
                       + b''.join(struct.pack('<2d', *point) for point in ring))
            header = bytearray(100)
            struct.pack_into('>I', header, 0, 9994)
            struct.pack_into('<I', header, 32, 5)
            _write(shp, bytes(header) + struct.pack('>2I', 1, len(polygon) // 2) + polygon)
            _write(os.path.join(project, 'Data', 'zones.prj'), 'GEOGCS["GCS_WGS_1984"]')
            _write(os.path.join(project, 'Data', 'zones.shp.csv'), 'insee\n001\n')
            _write(os.path.join(project, 'Data', 'facts.csv'),
                   'code_insee,Amount\n001,12\n002,13\n')
            fact_table_path = os.path.join(definition, 'tables', 'Facts.tmdl')
            with open(fact_table_path, 'a', encoding='utf-8') as stream:
                stream.write('\n\tcolumn code_insee\n\t\tsourceColumn: code_insee\n'
                             '\n\tcolumn Amount\n\t\tsourceColumn: Amount\n')
            self.assertEqual(migrate._restrict_file_partitions_to_model(project, NAME), 1)
            _write(os.path.join(definition, 'relationships.tmdl'),
                   'relationship link\n\tfromColumn: Facts.code_insee\n'
                   '\ttoColumn: Places.insee\n')
            visual_path = os.path.join(project, f'{NAME}.Report', 'definition',
                                       'pages', 'page', 'visuals', 'visual', 'visual.json')
            visual = {
                'visual': {'visualType': 'map', 'query': {'queryState': {
                    'Size': {'projections': [{
                        'field': {'Aggregation': {'Expression': {'Column': {
                            'Expression': {'SourceRef': {'Entity': 'Facts'}},
                            'Property': 'Amount'}}}},
                        'queryRef': 'Facts.Amount'}]}}}},
                'annotations': [{'name': 'MigrationNote',
                                 'value': 'Tableau-generated map coordinates require spatial geometry; no geocodable Location is bound.'}],
            }
            _write(visual_path, json.dumps(visual))
            first = migrate._wire_shapefile_map_coordinates(project, NAME)
            self.assertEqual(first, {'mapped': 1, 'unmatched': 1})
            with open(os.path.join(project, 'Data', 'facts.csv'), newline='', encoding='utf-8') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['MigrationLatitude'], '0.0000000000')
            self.assertEqual(rows[1]['MigrationLatitude'], '')
            self.assertIn('dataCategory: Latitude', _read(project, 'Facts'))
            self.assertIn('"Amount", "MigrationLatitude", "MigrationLongitude"',
                          _read(project, 'Facts'))
            with open(visual_path, encoding='utf-8') as stream:
                bound = json.load(stream)['visual']
            self.assertEqual(bound['visualType'], 'azureMap')
            roles = bound['query']['queryState']
            self.assertEqual(set(roles), {'Category', 'Y', 'X', 'Size'})
            for role, column in (('Y', 'MigrationLatitude'),
                                 ('X', 'MigrationLongitude')):
                self.assertEqual(len(roles[role]['projections']), 1)
                self.assertEqual(roles[role]['projections'][0]['field'], {
                    'Column': {'Expression': {'SourceRef': {'Entity': 'Facts'}},
                               'Property': column},
                })
            self.assertEqual(roles['Category']['projections'], roles['Y']['projections'])
            self.assertEqual(roles['Size'],
                             visual['visual']['query']['queryState']['Size'])
            self.assertEqual(migrate._wire_shapefile_map_coordinates(project, NAME),
                             {'mapped': 0, 'unmatched': 0})

    def test_catalog_shapefile_map_promotes_label_and_retains_extra_tooltip(self):
        with tempfile.TemporaryDirectory() as root:
            columns = ['RecordKey', 'Amount', 'Record Label', 'Record Note']
            project = _project(root, {
                'Facts': _csv_partition('facts.csv', columns),
                'Places': _csv_partition('zones.shp.csv', ['GeoKey']),
            })
            definition = os.path.join(project, f'{NAME}.SemanticModel', 'definition')
            shp_path = _write(os.path.join(project, 'Data', 'zones.shp'), b'synthetic')
            fact_rows = [[f'{index:03d}', str(index + 1),
                          f'Record {index}', f'Note {index}'] for index in range(22)]
            coordinates = [(10.0 + index / 100, 20.0 + index / 100)
                           for index in range(len(fact_rows))]
            for filename, headers, records in (
                    ('facts.csv', columns, fact_rows),
                    ('zones.shp.csv', ['GeoKey'], [[row[0]] for row in fact_rows])):
                with open(os.path.join(project, 'Data', filename), 'w',
                          encoding='utf-8', newline='') as stream:
                    writer = csv.writer(stream)
                    writer.writerow(headers)
                    writer.writerows(records)
            fact_table_path = os.path.join(definition, 'tables', 'Facts.tmdl')
            with open(fact_table_path, 'a', encoding='utf-8') as stream:
                for column in columns:
                    stream.write(f"\n\tcolumn '{column}'\n\t\tsourceColumn: {column}\n")
            _write(os.path.join(definition, 'relationships.tmdl'),
                   'relationship link\n\tfromColumn: Facts.RecordKey\n'
                   '\ttoColumn: Places.GeoKey\n')
            label = {
                'field': {'Column': {'Expression': {'SourceRef': {'Entity': 'Facts'}},
                                     'Property': 'Record Label'}},
                'queryRef': 'Facts.Record Label', 'active': False,
            }
            note = {
                'field': {'Column': {'Expression': {'SourceRef': {'Entity': 'Facts'}},
                                     'Property': 'Record Note'}},
                'queryRef': 'Facts.Record Note', 'active': True,
            }
            size = {'projections': [{
                'field': {'Aggregation': {
                    'Expression': {'Column': {
                        'Expression': {'SourceRef': {'Entity': 'Facts'}},
                        'Property': 'Amount',
                    }},
                    'Function': 0,
                }},
                'queryRef': 'Facts.Amount', 'active': True,
            }]}
            query_state = {'Size': size, 'Tooltips': {'projections': [label, note, label]}}
            for obsolete_role in ('Location', 'Latitude', 'Longitude', 'Color'):
                query_state[obsolete_role] = {'projections': [label]}
            visual_path = os.path.join(project, f'{NAME}.Report', 'definition',
                                       'pages', 'page', 'visuals', 'visual', 'visual.json')
            _write(visual_path, json.dumps({
                'visual': {'visualType': 'map', 'query': {'queryState': query_state}},
                'annotations': [{'name': 'MigrationNote',
                                 'value': 'Tableau-generated map coordinates require spatial geometry.'}],
            }))
            with mock.patch.object(migrate, '_shp_polygon_centers',
                                   return_value=coordinates) as centers:
                self.assertEqual(migrate._wire_shapefile_map_coordinates(project, NAME),
                                 {'mapped': len(fact_rows), 'unmatched': 0})
                centers.assert_called_once_with(shp_path)
            with open(visual_path, encoding='utf-8') as stream:
                bound = json.load(stream)['visual']
            self.assertEqual(bound['visualType'], 'azureMap')
            roles = bound['query']['queryState']
            self.assertEqual(set(roles), {'Category', 'Y', 'X', 'Size', 'Tooltips'})
            self.assertEqual(roles['Category']['projections'], [{**label, 'active': True}])
            self.assertEqual(roles['Tooltips']['projections'], [note])
            self.assertEqual(roles['Size'], size)
            for role, column in (('Y', 'MigrationLatitude'),
                                 ('X', 'MigrationLongitude')):
                self.assertEqual(roles[role]['projections'], [{
                    'field': {'Column': {'Expression': {'SourceRef': {'Entity': 'Facts'}},
                                         'Property': column}},
                    'queryRef': f'Facts.{column}', 'nativeQueryRef': column,
                    'active': True,
                }])
            with open(os.path.join(project, 'Data', 'facts.csv'),
                      encoding='utf-8', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), len(fact_rows))
            for row, original, (latitude, longitude) in zip(rows, fact_rows, coordinates):
                self.assertEqual([row[column] for column in columns], original)
                self.assertEqual(row['MigrationLatitude'], f'{latitude:.10f}')
                self.assertEqual(row['MigrationLongitude'], f'{longitude:.10f}')

    def test_polygon_reader_returns_inside_point_and_refuses_other_crs(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "a.shp")
            ring = [(-1., -1.), (1., -1.), (1., 1.), (-1., 1.), (-1., -1.)]
            polygon = (struct.pack('<I4dII', 5, -1., -1., 1., 1., 1, len(ring))
                       + struct.pack('<I', 0)
                       + b''.join(struct.pack('<2d', *point) for point in ring))
            header = bytearray(100)
            struct.pack_into('>I', header, 0, 9994)
            struct.pack_into('<I', header, 32, 5)
            _write(path, bytes(header) + struct.pack('>2I', 1, len(polygon) // 2) + polygon)
            _write(os.path.join(root, 'a.prj'), 'GEOGCS["GCS_WGS_1984"]')
            self.assertEqual(list(migrate._shp_polygon_centers(path)), [(0., 0.)])
            _write(os.path.join(root, 'a.prj'), 'PROJCS["Lambert_93"]')
            with self.assertRaisesRegex(ValueError, 'WGS84'):
                list(migrate._shp_polygon_centers(path))

    def test_generated_geojson_survives_a_missing_file(self):
        self.assertIn("features = try Source[features] otherwise {}",
                      _geo_partition("a.geojson", ["Name"]))

    def test_missing_geojson_is_served_by_its_extract_csv(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"T": _geo_partition("zone.geojson", ["Name", "Geometry"])})
            data = os.path.join(project, "Data")
            _write(os.path.join(data, "federated_x.csv"), "Name,Geometry\nA,POINT\n")
            _write(os.path.join(data, "federated_y.csv"), "Other\n1\n")
            stats = migrate._route_spatial_partitions(project, NAME)
            text = _read(project, "T")
            self.assertEqual(stats["extract_csv"], 1)
            self.assertIn('Csv.Document(File.Contents(DataFolder & "\\federated_x.csv")', text)
            self.assertNotIn("Json.Document", text)
            self.assertIn('{"Geometry", type text}', text)

    def test_shapefile_attributes_are_read_from_dbf(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"T": _geo_partition("c.shp", ["nom", "geometry"])})
            data = os.path.join(project, "Data")
            _write(os.path.join(data, "c.shp"), b"\x00\x00\x27\x0a")
            _write(os.path.join(data, "c.dbf"), _dbf([("nom", 10)], [["Paris"], ["Lyon"]]))
            stats = migrate._route_spatial_partitions(project, NAME)
            migrate._reconcile_csv_partitions(project, NAME)
            text = _read(project, "T")
            with open(os.path.join(data, "c.shp.csv"), encoding="utf-8") as f:
                self.assertEqual(f.read().split(), ["nom", "Paris", "Lyon"])
            self.assertEqual(stats["shapefile_csv"], 1)
            self.assertIn('c.shp.csv"), [Delimiter=","', text)
            self.assertNotIn("Json.Document", text)
            self.assertIn('"geometry", each null', text)

    def test_joined_suffix_columns_match_extract_on_base_name(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"T": _geo_partition(
                "a.geojson", ["site", "site (b.geojson)"])})
            _write(os.path.join(project, "Data", "federated_x.csv"), "site\n1\n")
            migrate._route_spatial_partitions(project, NAME)
            migrate._reconcile_csv_partitions(project, NAME)
            text = _read(project, "T")
            self.assertIn("federated_x.csv", text)
            self.assertIn('"site", "site (b.geojson)")', text)

    def test_dbf_is_fetched_for_a_shapefile_already_in_data(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"T": _geo_partition("c.shp", ["nom"])})
            _write(os.path.join(project, "Data", "c.shp"), b"shp")
            src = os.path.join(root, "src")
            _write(os.path.join(src, "c.shp"), b"shp")
            _write(os.path.join(src, "c.dbf"), _dbf([("nom", 5)], [["A"]]))
            workbook = _write(os.path.join(src, "w.twb"), "<workbook/>")
            migrate._collect_local_data_files(workbook, project, NAME)
            self.assertTrue(os.path.isfile(os.path.join(project, "Data", "c.dbf")))

    def test_present_geojson_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"T": _geo_partition("a.geojson", ["Name"])})
            _write(os.path.join(project, "Data", "a.geojson"), '{"features": []}')
            _write(os.path.join(project, "Data", "e.csv"), "Name\nA\n")
            migrate._route_spatial_partitions(project, NAME)
            self.assertIn("Json.Document", _read(project, "T"))


if __name__ == "__main__":
    unittest.main()
