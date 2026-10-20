"""Persistent, offline regressions for the second source/quality bug bash."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import migrate
from powerbi_import import migration_quality
from powerbi_import.mcp_server import MigrationTools
from powerbi_import.tmdl_generator import generate_tmdl


def _datasource():
    connection = {"type": "CSV", "details": {"filename": "missing.csv"}}
    return {
        "name": "Source",
        "connection": connection,
        "tables": [{
            "name": "Orders", "source_table": "missing.csv",
            "connection_details": connection,
            "columns": [{"name": "ID", "datatype": "integer"}],
        }],
        "calculations": [], "columns": [], "relationships": [],
    }


class _TemporaryProject(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write_json(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def generate(self, datasource, output, name="SourceQuality"):
        with redirect_stdout(io.StringIO()):
            return generate_tmdl([datasource], name, None, str(output))


class TestAuthoredPartitionProvenance(_TemporaryProject):
    OPTIONAL_SOURCE = (
        'let\n'
        '    Source = try Csv.Document(File.Contents(DataFolder & "\\missing.csv")) '
        'otherwise #table({"ID"}, {}),\n'
        '    Result = Source\n'
        'in\n'
        '    Result'
    )

    def assert_written_expression(self, output, expression):
        written = (output / "definition" / "tables" / "Orders.tmdl").read_text("utf-8")
        # Check the actual writer output, not the in-memory partition alone.
        serialized = "\n".join("\t\t\t\t" + line for line in expression.splitlines())
        self.assertIn("\t\tsource =\n" + serialized + "\n", written)
        self.assertNotIn("Migration.SourceUnavailable", written)
        self.assertNotIn("_expression_origin", written)
        self.assertNotIn("authored_override", written)

    def test_authored_try_missing_file_survives_public_generation_and_writer(self):
        # Both supported override entry points must retain authored provenance.
        for key in ("m_query_override", "m_query_overrides"):
            with self.subTest(key=key):
                datasource = _datasource()
                datasource[key] = (
                    {"Orders": self.OPTIONAL_SOURCE}
                    if key == "m_query_overrides" else self.OPTIONAL_SOURCE
                )
                output = self.root / key
                stats = self.generate(datasource, output)
                self.assertEqual(stats["source_issues"], [{
                    "table": "Orders", "source": "missing.csv",
                    "issue": "file_not_found",
                }])
                self.assert_written_expression(output, self.OPTIONAL_SOURCE)

    def test_autonomous_authored_query_survives_stale_connection_metadata(self):
        datasource = _datasource()
        datasource["tables"][0]["source_table"] = "old-relation.csv"
        expression = 'let\n    Source = #table({"ID"}, {{42}})\nin\n    Source'
        datasource["m_query_overrides"] = {"Orders": expression}
        output = self.root / "autonomous"
        stats = self.generate(datasource, output)
        self.assertEqual(len(stats["source_issues"]), 1)
        self.assertEqual(stats["source_issues"][0]["issue"], "source_relation_mismatch")
        self.assertEqual(stats["source_issues"][0]["source"], "old-relation.csv")
        self.assert_written_expression(output, expression)

    def test_generated_missing_source_with_try_is_not_exempt(self):
        # Simulate a generated connector containing try: syntax is not provenance.
        output = self.root / "generated-try"
        with patch("powerbi_import.tmdl_generator.generate_power_query_m",
                   return_value=self.OPTIONAL_SOURCE) as emitter:
            stats = self.generate(_datasource(), output)
        emitter.assert_called_once()
        self.assertEqual(stats["source_issues"], [{
            "table": "Orders", "source": "missing.csv", "issue": "file_not_found",
        }])
        written = (output / "definition" / "tables" / "Orders.tmdl").read_text("utf-8")
        self.assertIn('error Error.Record("Migration.SourceUnavailable"', written)
        self.assertIn('Source = "missing.csv"', written)
        self.assertNotIn("otherwise #table", written)
        self.assertNotIn("_expression_origin", written)


class TestQualityProjectIdentity(_TemporaryProject):
    def test_classic_project_and_shortened_project_resolve_actual_identity(self):
        for name in ("Sales", "P_0123456789ab"):
            with self.subTest(name=name):
                project = self.root / name
                project.mkdir()
                self.write_json(project / f"{name}.pbip", {})
                (project / f"{name}.Report").mkdir()
                (project / f"{name}.SemanticModel").mkdir()
                self.write_json(project / "migration_metadata.json", {"report_name": name})
                self.assertEqual(
                    migration_quality.resolve_quality_report_name(str(project)), name)

    def test_single_named_artifact_or_metadata_can_supply_identity(self):
        for suffix in (".pbip", ".Report", ".SemanticModel", ".Lakehouse", "metadata"):
            with self.subTest(suffix=suffix):
                project = self.root / ("project_" + suffix.lstrip("."))
                project.mkdir()
                if suffix == "metadata":
                    self.write_json(project / "migration_metadata.json",
                                    {"report_name": "Sales"})
                elif suffix == ".pbip":
                    self.write_json(project / "Sales.pbip", {})
                else:
                    (project / f"Sales{suffix}").mkdir()
                self.assertEqual(
                    migration_quality.resolve_quality_report_name(str(project)), "Sales")

    def test_no_identity_is_explicit_error_not_directory_basename(self):
        with self.assertRaisesRegex(ValueError, "identity unavailable"):
            migration_quality.resolve_quality_report_name(str(self.root))

    def test_multiple_reports_are_ambiguous(self):
        for name in ("First", "Second"):
            (self.root / f"{name}.Report").mkdir()
        with self.assertRaisesRegex(ValueError, "Ambiguous.*multiple .Report"):
            migration_quality.resolve_quality_report_name(str(self.root))

    def test_conflicting_artifact_identities_are_ambiguous(self):
        self.write_json(self.root / "First.pbip", {})
        (self.root / "Second.SemanticModel").mkdir()
        with self.assertRaisesRegex(ValueError, "artifacts/metadata disagree"):
            migration_quality.resolve_quality_report_name(str(self.root))

    def test_metadata_contradicting_artifacts_is_explicit_error(self):
        (self.root / "P_0123456789ab.Report").mkdir()
        self.write_json(self.root / "migration_metadata.json", {"report_name": "Original"})
        with self.assertRaisesRegex(ValueError, "artifacts/metadata disagree"):
            migration_quality.resolve_quality_report_name(str(self.root))

    def test_invalid_metadata_does_not_silently_fall_back_to_artifacts(self):
        (self.root / "Sales.Report").mkdir()
        for metadata in ({}, {"report_name": ""}, {"report_name": "  "},
                         {"report_name": 12}, []):
            with self.subTest(metadata=metadata):
                self.write_json(self.root / "migration_metadata.json", metadata)
                with self.assertRaisesRegex(ValueError, "no valid report_name"):
                    migration_quality.resolve_quality_report_name(str(self.root))

    def test_unsafe_metadata_identity_is_rejected(self):
        for name in (".", "..", "../Sales", "folder\\Sales", "C:Sales", "Sales\x00"):
            with self.subTest(name=name):
                self.write_json(self.root / "migration_metadata.json", {"report_name": name})
                with self.assertRaisesRegex(ValueError, "Invalid project identity"):
                    migration_quality.resolve_quality_report_name(str(self.root))


class TestQualityConsumers(_TemporaryProject):
    def setUp(self):
        super().setUp()
        # Deliberately unrelated source, container, and generated artifact names.
        self.source = self.root / "Original_workbook_name.twb"
        self.source.write_text("<workbook/>", encoding="utf-8")
        self.output = self.root / "output"
        self.project = self.output / "migration_container"
        self.project.mkdir(parents=True)
        self.extracted = {"datasources": [_datasource()]}
        self.extracted["datasources"][0]["m_query_overrides"] = {
            "Orders": 'let\n    Source = #table({"ID"}, {{42}})\nin\n    Source'
        }

    def make_project(self, name):
        self.write_json(self.project / f"{name}.pbip", {})
        (self.project / f"{name}.Report").mkdir()
        self.generate(copy.deepcopy(self.extracted["datasources"][0]),
                      self.project / f"{name}.SemanticModel", name)
        self.write_json(self.project / "migration_metadata.json", {"report_name": name})

    def assert_real_coverage(self, report, name):
        self.assertEqual(report["report_name"], name)
        self.assertEqual(report["data"]["summary"]["source_tables"], 1)
        self.assertEqual(report["data"]["summary"]["tables_found"], 1)
        self.assertNotIn("One or more extracted source tables are missing from the target model.",
                         report["blockers"])

    def test_mcp_shortened_identity_reaches_real_table_comparison(self):
        name = "P_0123456789ab"
        self.make_project(name)
        tools = MigrationTools()
        with patch("powerbi_import.mcp_server._extract_to_dir",
                   return_value=(None, self.extracted)), \
             patch.object(migration_quality, "build_quality_report",
                          wraps=migration_quality.build_quality_report) as build:
            result = tools.quality_report({
                "file": str(self.source), "project_dir": str(self.project),
            })
        self.assertTrue(result["ok"], result)
        self.assertEqual(build.call_args.args[2], name)
        self.assert_real_coverage(result["report"], name)
        self.assertEqual(tools.report_store["quality"], result["report"])
        self.assertEqual(tools._quality_context["report_name"], name)

    def test_mcp_classic_identity_also_reaches_real_table_comparison(self):
        self.make_project("Sales")
        with patch("powerbi_import.mcp_server._extract_to_dir",
                   return_value=(None, self.extracted)):
            result = MigrationTools().quality_report({
                "file": str(self.source), "project_dir": str(self.project),
            })
        self.assertTrue(result["ok"], result)
        self.assert_real_coverage(result["report"], "Sales")

    def test_mcp_identity_errors_stop_before_extraction_or_quality_build(self):
        for case in ("missing", "ambiguous", "contradictory"):
            with self.subTest(case=case):
                project = self.root / case
                project.mkdir()
                if case != "missing":
                    (project / "First.Report").mkdir()
                    if case == "ambiguous":
                        (project / "Second.Report").mkdir()
                    else:
                        self.write_json(project / "migration_metadata.json",
                                        {"report_name": "Second"})
                tools = MigrationTools()
                with patch("powerbi_import.mcp_server._extract_to_dir") as extract, \
                     patch.object(migration_quality, "build_quality_report") as build:
                    result = tools.quality_report({
                        "file": str(self.source), "project_dir": str(project),
                    })
                self.assertFalse(result["ok"])
                self.assertIn("identity", result["error"])
                extract.assert_not_called()
                build.assert_not_called()
                self.assertNotIn("quality", tools.report_store)

    def test_cli_quality_uses_generated_identity_and_persists_real_coverage(self):
        name = "P_0123456789ab"
        self.make_project(name)
        extract_dir = self.root / "extracted"
        self.write_json(extract_dir / "datasources.json", self.extracted["datasources"])
        args = SimpleNamespace(output_dir=str(self.output), tableau_file=str(self.source),
                               quality_ai=False, quality_policy="report")
        with patch.object(migrate, "_get_extract_dir", return_value=str(extract_dir)), \
             patch.object(migration_quality, "build_quality_report",
                          wraps=migration_quality.build_quality_report) as build, \
             redirect_stdout(io.StringIO()):
            report = migrate._run_quality_report(args, self.project.name)
        self.assertIsNotNone(report)
        self.assertEqual(build.call_args.args[2], name)
        self.assert_real_coverage(report.to_dict(), name)
        saved = self.output / f"migration_quality_{self.project.name}.json"
        self.assert_real_coverage(json.loads(saved.read_text("utf-8")), name)
        self.assertTrue(saved.with_suffix(".html").is_file())

    def test_cli_identity_error_does_not_publish_misleading_quality_report(self):
        self.make_project("P_0123456789ab")
        self.write_json(self.project / "migration_metadata.json",
                        {"report_name": "Original_workbook_name"})
        args = SimpleNamespace(output_dir=str(self.output), quality_ai=False)
        with patch.object(migrate, "_get_extract_dir", return_value=str(self.root)), \
             patch.object(migration_quality, "build_quality_report") as build, \
             patch.object(migrate.logger, "error") as log_error, \
             redirect_stdout(io.StringIO()) as stdout:
            report = migrate._run_quality_report(args, self.project.name)
        self.assertIsNone(report)
        self.assertIn("artifacts/metadata disagree", stdout.getvalue())
        log_error.assert_called_once()
        build.assert_not_called()
        self.assertFalse(list(self.output.glob("migration_quality_*")))


if __name__ == "__main__":
    unittest.main()
