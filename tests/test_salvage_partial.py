"""Offline regressions for default diagnostic recovery, never source execution."""

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest import mock

import migrate
from powerbi_import import pbip_generator, tmdl_generator
from powerbi_import.config.migration_config import MigrationConfig
from powerbi_import.errors import ConfigurationError, ExtractionError, GenerationError
from tableau_export.extract_tableau_data import TableauExtractor
from tests.prep_flow_fixtures import clean_chain, write_flow
from tests.test_native_database_preference import (
    IGNORED,
    UNREADABLE_HYPER,
    _hyper_io_guard,
    _native_database_xml,
)

ROOT = Path(__file__).resolve().parents[1]


def _workbook_xml(*, refused=False, composite=False):
    root = ET.Element("workbook", {"version": "2024.1"})
    sources = ET.SubElement(root, "datasources")
    if refused:
        bad = _native_database_xml(name="refused-first")
        for connection in bad.findall("./connection//named-connection/connection"):
            connection.attrib.pop("server")
        sources.append(bad)
    good = _native_database_xml(name="recoverable", snapshot=False)
    if composite:
        comparison = good.find("./connection/relation/clause/expression")
        comparison.clear()
        comparison.set("op", "AND")
        for left, right in (
            ("order_region_id", "region_id"), ("order_id", "region_id"),
        ):
            equality = ET.SubElement(comparison, "expression", {"op": "="})
            ET.SubElement(equality, "expression", {
                "op": f"[Orders Model].[{left}]",
            })
            ET.SubElement(equality, "expression", {
                "op": f"[Regions Model].[{right}]",
            })
        graph = good.find("./object-graph")
        graph.clear()
        objects = ET.SubElement(graph, "objects")
        for key, alias in (("orders-object", "Orders Model"),
                           ("regions-object", "Regions Model")):
            ET.SubElement(objects, "object", {"id": key, "caption": alias})
        relationship = ET.SubElement(ET.SubElement(graph, "relationships"),
                                     "relationship")
        ET.SubElement(relationship, "first-end-point", {"object-id": "orders-object"})
        ET.SubElement(relationship, "second-end-point", {"object-id": "regions-object"})
        relationship.append(ET.fromstring(ET.tostring(comparison)))
    calculation = ET.SubElement(good, "column", {
        "name": "[Calculation_Total]", "caption": "Recovered Total",
        "datatype": "real", "role": "measure", "type": "quantitative",
    })
    ET.SubElement(calculation, "calculation", {
        "class": "tableau", "formula": "SUM([amount])",
    })
    sources.append(good)
    sheet = ET.SubElement(ET.SubElement(root, "worksheets"), "worksheet", {
        "name": "Recovered Overview",
    })
    table = ET.SubElement(sheet, "table")
    view = ET.SubElement(table, "view")
    dependencies = ET.SubElement(view, "datasource-dependencies", {
        "datasource": "recoverable",
    })
    for name, datatype, role in (
        ("region_name", "string", "dimension"), ("amount", "real", "measure"),
    ):
        ET.SubElement(dependencies, "column", {
            "name": f"[{name}]", "datatype": datatype, "role": role,
        })
    ET.SubElement(table, "cols").text = "[recoverable].[none:region_name:nk]"
    ET.SubElement(table, "rows").text = "[recoverable].[sum:amount:qk]"
    pane = ET.SubElement(ET.SubElement(table, "panes"), "pane")
    ET.SubElement(pane, "mark", {"class": "Bar"})
    return root


def _package(directory, *, refused=False, composite=False, extra_members=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "Recovery.twbx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Workbook.twb", ET.tostring(
            _workbook_xml(refused=refused, composite=composite), encoding="utf-8"))
        if refused:
            archive.writestr(IGNORED, UNREADABLE_HYPER)
        for name, content in (extra_members or {}).items():
            archive.writestr(name, content)
    return path


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class SalvageFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sources = self.root / "sources"
        self.output = self.root / "output"
        self.project = self.output / "Recovery"
        self.salvage = self.project / "Salvage"

    def _cli(self, *options, batch_config=None):
        environment = os.environ.copy()
        for name in (
            "TTPBI_FORCE", "TTPBI_UPLOAD_ROOT", "TTPBI_EXTRACT_DIR",
            "TTPBI_MODEL_MODE", "TTPBI_HYPER_EXPORT_MAX_ROWS",
            "TTPBI_MAX_ZIP_ENTRY_MB", "TTPBI_LOGICAL_RELATIONSHIP_POLICIES",
        ):
            environment.pop(name, None)
        environment["PYTHONIOENCODING"] = "utf-8"
        source_args = (["--batch-config", str(batch_config)] if batch_config
                       else ["--batch", str(self.sources)])
        result = subprocess.run(
            [sys.executable, str(ROOT / "migrate.py"), *source_args,
             "--output-dir", str(self.output), *options],
            cwd=self.root, env=environment, capture_output=True, text=True,
            encoding="utf-8", timeout=90,
        )
        self.cli_output = result.stdout + result.stderr
        return result

    def _batch(self, **options):
        with mock.patch.object(socket.socket, "connect") as connect, \
                mock.patch.object(subprocess, "Popen") as launch, \
                _hyper_io_guard(no_hyper=True), \
                redirect_stdout(io.StringIO()):
            result = migrate.run_batch_migration(
                str(self.sources), output_dir=str(self.output), quiet=True,
                verify_open=False, **options)
        connect.assert_not_called()
        launch.assert_not_called()
        return result

    def _assert_diagnostics(self):
        manifest = _read_json(self.salvage / "salvage_manifest.json")
        self.assertEqual(manifest["status"], "incomplete")
        self.assertEqual(manifest["validation_status"], "FAIL")
        self.assertTrue(manifest["diagnostic_only"])
        self.assertEqual(manifest["completion_marker"], "withheld")
        self.assertEqual(manifest["not_attempted"],
                         ["source_execution", "desktop", "deployment"])
        inventory = _read_json(self.salvage / "inventory.json")
        self.assertEqual(inventory, manifest["semantic_fragments"])
        self.assertEqual(inventory["validation"], "unvalidated")
        self.assertEqual(inventory["source_execution"], "not_attempted")
        dax = list(self.salvage.glob("*.dax"))
        queries = list(self.salvage.glob("*.pq"))
        self.assertTrue(dax, inventory)
        self.assertTrue(queries, inventory)
        self.assertTrue(any("SUM(" in path.read_text(encoding="utf-8") for path in dax))
        self.assertTrue(any("PostgreSQL.Database" in path.read_text(encoding="utf-8")
                            for path in queries))
        self.assertFalse(list(self.project.glob("*.pbip")))
        metadata = _read_json(self.salvage / "model_metadata.json")
        self.assertTrue(metadata["diagnostic_only"])
        self.assertEqual(metadata["validation"], "unvalidated")
        return manifest, metadata["extracted_objects"]


class TestSalvageBatchCLI(SalvageFixture):
    def test_batch_config_refusal_uses_same_diagnostic_recovery_path(self):
        workbook = _package(self.sources, refused=True)
        config = self.root / "batch.json"
        config.write_text(json.dumps([{"file": str(workbook)}]), encoding="utf-8")
        result = self._cli(batch_config=config)
        self.assertEqual(result.returncode, migrate.ExitCode.BATCH_PARTIAL_FAIL,
                         self.cli_output)
        manifest, objects = self._assert_diagnostics()
        self.assertEqual(manifest["extraction"]["status"], "incomplete")
        self.assertTrue(any(item["name"] == "refused-first"
                            for item in manifest["extraction"]["blocked"]))
        self.assertEqual([source["name"] for source in objects["datasources"]],
                         ["recoverable"])
        self.assertEqual(objects["calculations"][0]["formula"], "SUM([amount])")
        self.assertEqual(objects["worksheets"][0]["name"], "Recovered Overview")
        self.assertFalse(list(self.project.rglob("*.tmdl")))

    def test_refused_first_datasource_recovers_later_objects_without_project(self):
        _package(self.sources, refused=True)
        result = self._cli()
        self.assertEqual(result.returncode, migrate.ExitCode.BATCH_PARTIAL_FAIL,
                         self.cli_output)
        manifest, objects = self._assert_diagnostics()
        extraction = manifest["extraction"]
        self.assertEqual(extraction["status"], "incomplete")
        self.assertTrue(any(item["name"] == "refused-first"
                            and item["kind"] == "datasource"
                            for item in extraction["blocked"]))
        self.assertTrue(any(item["name"] == "recoverable"
                            and item["kind"] == "datasource"
                            for item in extraction["recovered"]))
        self.assertTrue(any(item["name"] == "hyper_files"
                            for item in extraction["not_attempted"]))
        self.assertEqual([source["name"] for source in objects["datasources"]],
                         ["recoverable"])
        self.assertTrue(any(calc["formula"] == "SUM([amount])"
                            for calc in objects["calculations"]))
        self.assertEqual([sheet["name"] for sheet in objects["worksheets"]],
                         ["Recovered Overview"])
        self.assertTrue(extraction["diagnostics"]["datasources"])
        self.assertFalse(list(self.project.rglob("*.tmdl")))

    def test_strict_refusal_does_not_create_salvage(self):
        _package(self.sources, refused=True)
        result = self._cli("--strict-generation")
        self.assertEqual(result.returncode, migrate.ExitCode.BATCH_PARTIAL_FAIL,
                         self.cli_output)
        self.assertFalse(self.salvage.exists())
        self.assertFalse(list(self.output.rglob("extraction_salvage.json")))
        self.assertFalse(list(self.project.glob("*.pbip")))

    def test_natural_composite_guard_emits_diagnostics_without_tmdl(self):
        _package(self.sources, composite=True)
        result = self._cli()
        self.assertEqual(result.returncode, migrate.ExitCode.BATCH_PARTIAL_FAIL,
                         self.cli_output)
        manifest, objects = self._assert_diagnostics()
        self.assertIsNone(manifest["extraction"])
        self.assertTrue(objects["datasources"][0]["relationships"])
        with self.subTest(boundary="prewrite"):
            self.assertFalse(list(self.project.rglob("*.tmdl")))
        with self.subTest(boundary="original failure"):
            self.assertIn("complete predicate has multiple equality terms",
                          manifest["failure"]["message"])

    def test_complete_default_publishes_success_without_salvage(self):
        _package(self.sources)
        result = self._cli()
        self.assertEqual(result.returncode, migrate.ExitCode.SUCCESS, self.cli_output)
        self.assertTrue((self.project / "Recovery.pbip").is_file())
        self.assertTrue(list(self.project.rglob("*.tmdl")))
        self.assertFalse(self.salvage.exists())
        extraction = _read_json(self.output / ".migration_extract" /
                                "Recovery.twbx" / "extraction_salvage.json")
        self.assertEqual(extraction["status"], "complete")
        self.assertEqual(extraction["blocked"], [])
        self.assertEqual(extraction["not_attempted"], [])

    def test_strict_natural_semantic_refusal_has_no_salvage_or_tmdl(self):
        _package(self.sources, composite=True)
        result = self._cli("--strict-generation")
        self.assertEqual(result.returncode, migrate.ExitCode.BATCH_PARTIAL_FAIL,
                         self.cli_output)
        self.assertIn("complete predicate has multiple equality terms", self.cli_output)
        self.assertFalse(self.salvage.exists())
        self.assertFalse(list(self.output.rglob("extraction_salvage.json")))
        self.assertFalse(list(self.project.rglob("*.tmdl")))
        self.assertFalse(list(self.project.glob("*.pbip")))

    def test_previous_success_is_archived_not_reused_for_failed_attempt(self):
        _package(self.sources)
        first = self._cli()
        self.assertEqual(first.returncode, migrate.ExitCode.SUCCESS, self.cli_output)
        marker = self.project / "Recovery.pbip"
        previous_marker = marker.read_bytes()
        stale = self.project / "successful-only.dax"
        stale.write_text("STALE_SUCCESS_SENTINEL", encoding="utf-8")
        _package(self.sources, refused=True)
        second = self._cli()
        self.assertEqual(second.returncode, migrate.ExitCode.BATCH_PARTIAL_FAIL,
                         self.cli_output)
        self._assert_diagnostics()
        self.assertFalse(stale.exists())
        self.assertFalse(list(self.project.rglob("*.tmdl")))
        archives = self.output / ".migration_previous"
        old_markers = list(archives.rglob("Recovery.pbip"))
        self.assertEqual(len(old_markers), 1)
        self.assertEqual(old_markers[0].read_bytes(), previous_marker)
        self.assertEqual([path.read_text(encoding="utf-8")
                          for path in archives.rglob("successful-only.dax")],
                         ["STALE_SUCCESS_SENTINEL"])
        for path in self.salvage.iterdir():
            self.assertNotIn("STALE_SUCCESS_SENTINEL", path.read_text(encoding="utf-8"))


class TestSalvageGenerationGuard(SalvageFixture):
    def test_strict_extraction_refusal_never_builds_or_recovers_semantics(self):
        _package(self.sources, refused=True)
        with mock.patch.object(
            pbip_generator.tmdl_generator, "_build_semantic_model",
        ) as build, mock.patch.object(
            tmdl_generator, "recover_diagnostic_semantics",
        ) as recover:
            result = self._batch(strict_generation=True)
        self.assertEqual(result, migrate.ExitCode.BATCH_PARTIAL_FAIL)
        build.assert_not_called()
        recover.assert_not_called()
        self.assertFalse(self.salvage.exists())
        self.assertFalse(list(self.output.rglob("extraction_salvage.json")))
        self.assertFalse(list(self.project.glob("*.pbip")))

    def test_prewrite_guard_recovers_from_source_independently_of_model_build(self):
        for strict in (False, True):
            with self.subTest(strict_generation=strict):
                self.output = self.root / f"output-{strict}"
                self.project = self.output / "Recovery"
                self.salvage = self.project / "Salvage"
                _package(self.sources)
                semantic = pbip_generator.tmdl_generator
                with mock.patch.object(
                    semantic, "_build_semantic_model",
                    side_effect=GenerationError("Synthetic pre-write semantic refusal"),
                ) as build, mock.patch.object(
                    semantic, "_write_tmdl_files",
                ) as write, mock.patch.object(
                    tmdl_generator, "recover_diagnostic_semantics",
                    wraps=tmdl_generator.recover_diagnostic_semantics,
                ) as recover:
                    result = self._batch(strict_generation=strict)
                self.assertEqual(result, migrate.ExitCode.BATCH_PARTIAL_FAIL)
                build.assert_called_once()
                write.assert_not_called()
                if strict:
                    recover.assert_not_called()
                    self.assertFalse(self.salvage.exists())
                    self.assertFalse(list(self.project.glob("*.pbip")))
                else:
                    recover.assert_called_once()
                    manifest, objects = self._assert_diagnostics()
                    self.assertIn("Synthetic pre-write semantic refusal",
                                  manifest["failure"]["message"])
                    self.assertEqual(objects["calculations"][0]["formula"], "SUM([amount])")
                self.assertFalse(list(self.project.rglob("*.tmdl")))

    def test_strict_overrides_explicit_recovery_for_generation_failure(self):
        _package(self.sources)
        semantic = pbip_generator.tmdl_generator
        with mock.patch.object(
            semantic, "_build_semantic_model",
            side_effect=GenerationError("Synthetic default pre-write refusal"),
        ) as build, mock.patch.object(
            tmdl_generator, "recover_diagnostic_semantics",
        ) as recover:
            result = self._batch(salvage_partial=True, strict_generation=True)
        self.assertEqual(result, migrate.ExitCode.BATCH_PARTIAL_FAIL)
        build.assert_called_once()
        recover.assert_not_called()
        self.assertFalse(self.salvage.exists())
        self.assertFalse(list(self.project.rglob("*.tmdl")))
        self.assertFalse(list(self.project.glob("*.pbip")))

    def test_dry_run_does_not_archive_or_recover_or_mutate_existing_output(self):
        _package(self.sources, refused=True)
        self.project.mkdir(parents=True)
        marker = self.project / "Recovery.pbip"
        marker.write_bytes(b'{"previous": true}\r\n')
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        with mock.patch.object(migrate, "_prepare_salvage_attempt") as prepare, \
                mock.patch.object(migrate, "_migrate_single_workbook") as worker, \
                mock.patch.object(tmdl_generator, "recover_diagnostic_semantics") as recover:
            result = self._batch(salvage_partial=True, dry_run=True)
        self.assertEqual(result, migrate.ExitCode.SUCCESS)
        prepare.assert_not_called()
        worker.assert_not_called()
        recover.assert_not_called()
        self.assertEqual(
            {str(path.relative_to(self.root)): path.read_bytes()
             for path in self.root.rglob("*") if path.is_file()}, before)
        self.assertFalse((self.output / ".migration_previous").exists())

    def test_incompatible_options_rejected_before_extraction_or_desktop(self):
        _package(self.sources)
        for options in (
            {"skip_extraction": True},
            {"desktop_options": migrate.DesktopDiagnosticOptions()},
            {"output_format": "fabric"},
        ):
            with self.subTest(options=options), \
                    mock.patch.object(migrate, "_migrate_single_workbook") as worker:
                with self.assertRaisesRegex(ConfigurationError, "fresh PBIP extraction"):
                    self._batch(salvage_partial=True, **options)
                worker.assert_not_called()
        self.assertFalse(self.output.exists())


class TestSalvageRecursiveArchive(SalvageFixture):
    def test_nested_distinct_workbooks_archive_only_their_owned_extractions(self):
        workbooks = []
        for folder, name in (("north", "Orders"), ("south", "Regions")):
            workbook = _package(self.sources / folder)
            renamed = workbook.with_name(name + ".twbx")
            workbook.rename(renamed)
            workbooks.append(renamed)
        originals = {path: path.read_bytes() for path in workbooks}
        self.assertEqual(self._batch(), migrate.ExitCode.SUCCESS)
        extraction_root = self.output / ".migration_extract"
        previous = {}
        for workbook in workbooks:
            relative = workbook.relative_to(self.sources)
            extraction = extraction_root / relative
            sentinel = extraction / "previous-only.json"
            sentinel.write_bytes(b'{"previous": true}\r\n')
            previous[relative] = (extraction / "datasources.json").read_bytes()
        unrelated = extraction_root / "unrelated" / "keep.json"
        unrelated.parent.mkdir()
        unrelated.write_bytes(b'{"unrelated": true}\r\n')

        self.assertEqual(self._batch(), migrate.ExitCode.SUCCESS)

        for workbook in workbooks:
            relative = workbook.relative_to(self.sources)
            project = self.output / relative.parent / workbook.stem
            self.assertTrue((project / (workbook.stem + ".pbip")).is_file())
            extraction = extraction_root / relative
            self.assertTrue((extraction / "datasources.json").is_file())
            self.assertFalse((extraction / "previous-only.json").exists())
            history = project.parent / ".migration_previous"
            archived = list(history.glob("attempt-*/*-" + workbook.name))
            self.assertEqual(len(archived), 1)
            self.assertEqual((archived[0] / "datasources.json").read_bytes(),
                             previous[relative])
            self.assertEqual((archived[0] / "previous-only.json").read_bytes(),
                             b'{"previous": true}\r\n')
        self.assertEqual(unrelated.read_bytes(), b'{"unrelated": true}\r\n')
        self.assertEqual({path: path.read_bytes() for path in workbooks}, originals)

    def _task(self, folder="north"):
        workbook = _package(self.sources / folder)
        workbook_output = self.output / folder
        project = workbook_output / "Recovery"
        project.mkdir(parents=True)
        (project / "Recovery.pbip").write_bytes(b'{"previous": true}\r\n')
        extraction_root = self.output / ".migration_extract"
        extraction = extraction_root / workbook.relative_to(self.sources)
        extraction.mkdir(parents=True)
        (extraction / "datasources.json").write_bytes(b"[]\r\n")
        return {
            "workbook_output_dir": str(workbook_output),
            "basename": "Recovery", "tableau_file": str(workbook),
            "search_root": str(self.sources),
            "extraction_root": str(extraction_root),
            "extract_dir": str(extraction),
        }

    def _assert_refused_without_mutation(self, task):
        before = {path: path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        with self.assertRaisesRegex(ConfigurationError, "Unsafe salvage"):
            migrate._prepare_salvage_attempt(task)
        self.assertEqual({path: path.read_bytes()
                          for path in self.root.rglob("*") if path.is_file()}, before)
        self.assertFalse((Path(task["workbook_output_dir"]) /
                          ".migration_previous").exists())

    def test_rejects_external_missing_owner_mismatched_and_source_targets(self):
        external = self.root / "external"
        external.mkdir()
        (external / "preserve.json").write_bytes(b'{"external": true}\r\n')
        (external / "Recovery.twbx").mkdir()
        (external / "Recovery.twbx" / "datasources.json").write_bytes(b"[]\r\n")
        mismatched = self.output / ".migration_extract" / "south" / "Other.twbx"
        mismatched.mkdir(parents=True)
        (mismatched / "preserve.json").write_bytes(b'{"other": true}\r\n')
        for index, reason in enumerate((
                "outside owner", "external owner", "missing owner",
                "wrong task under owner", "source directory", "source file")):
            with self.subTest(reason=reason):
                task = self._task(f"case-{index}")
                unsafe = dict(task)
                if reason == "outside owner":
                    unsafe["extract_dir"] = str(external)
                elif reason == "external owner":
                    unsafe.update(extraction_root=str(external),
                                  extract_dir=str(external / "Recovery.twbx"))
                elif reason == "missing owner":
                    del unsafe["extraction_root"]
                    unowned = Path(task["workbook_output_dir"]) / "extracted"
                    unowned.mkdir()
                    (unowned / "datasources.json").write_bytes(b"[]\r\n")
                    unsafe["extract_dir"] = str(unowned)
                elif reason == "wrong task under owner":
                    unsafe["extract_dir"] = str(mismatched)
                else:
                    unsafe.update(
                        extraction_root=str(self.sources),
                        extract_dir=(str(Path(task["tableau_file"]).parent)
                                     if reason == "source directory"
                                     else task["tableau_file"]))
                self._assert_refused_without_mutation(unsafe)

    def test_rejects_link_and_junction_parents_before_archiving_any_target(self):
        task = self._task()
        parent = str(Path(task["extract_dir"]).parent)
        for kind in ("islink", "isjunction"):
            real_check = getattr(os.path, kind)

            def is_redirected(path):
                return os.path.abspath(path) == parent or real_check(path)

            with self.subTest(kind=kind), mock.patch.object(
                    migrate.os.path, kind, side_effect=is_redirected):
                self._assert_refused_without_mutation(task)

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_real_junction_parent_to_source_is_rejected_without_mutation(self):
        task = self._task()
        parent = Path(task["extract_dir"]).parent
        retained = parent.with_name("retained-extraction")
        parent.rename(retained)
        result = subprocess.run(
            [os.environ["COMSPEC"], "/c", "mklink", "/J", str(parent),
             str(self.sources / "north")],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.addCleanup(parent.rmdir)
        self.assertTrue(os.path.isjunction(parent))
        self._assert_refused_without_mutation(task)
        self.assertEqual((retained / "Recovery.twbx" / "datasources.json").read_bytes(),
                         b"[]\r\n")


class TestSalvageSecurity(SalvageFixture):
    def test_direct_recovery_scrubbing_preserves_inventory_protocol(self):
        for secret in ("recovered", "blocked", "not_attempted", "status", "items",
                       "counts", "source_file", "000001.json"):
            with self.subTest(secret=secret):
                objects = {
                    "password": secret,
                    "description": f"password={secret}",
                    "datasources": [{
                        "name": "Synthetic",
                        "connection": {"type": "hyper"},
                        "calculations": [
                            {"name": "Constant", "formula": "1"},
                            {"name": "Missing formula", "formula": ""},
                        ],
                        "tables": [{"name": "Snapshot"}],
                    }],
                }
                original = deepcopy(objects)
                output = self.root / secret.replace(".", "_")
                with mock.patch.object(socket.socket, "connect") as connect, \
                        mock.patch.object(subprocess, "Popen") as launch:
                    inventory = tmdl_generator.recover_diagnostic_semantics(
                        objects, str(output))
                connect.assert_not_called()
                launch.assert_not_called()
                self.assertEqual(objects, original)
                self.assertEqual(_read_json(output / "inventory.json"), inventory)
                self.assertEqual(inventory["counts"],
                                 {"recovered": 1, "blocked": 1, "not_attempted": 1})
                self.assertEqual(inventory["metadata_file"], "model_metadata.json")
                self.assertEqual([item["status"] for item in inventory["items"]],
                                 ["recovered", "blocked", "not_attempted"])
                self.assertEqual([item["key"] for item in inventory["items"]],
                                 ["000001", "000002", "000003"])
                self.assertEqual(inventory["items"][0]["fragment_file"], "000001.dax")
                for index, item in enumerate(inventory["items"], 1):
                    self.assertEqual(item["source_file"], f"{index:06d}.json")
                    self.assertTrue((output / item["source_file"]).is_file())
                    self.assertEqual(item["validation"], "unvalidated")
                self.assertTrue((output / "000001.dax").is_file())
                metadata = _read_json(output / "model_metadata.json")["extracted_objects"]
                self.assertEqual(metadata["password"], "***REDACTED***")
                self.assertNotIn(f"password={secret}", metadata["description"])
                self.assertIn("***REDACTED***", metadata["description"])

    def _assert_unsafe_rejected(self):
        for options, flags in (({}, ()), ({"salvage_partial": True}, ("--salvage-partial",))):
            with self.subTest(options=options), mock.patch.object(
                tmdl_generator, "recover_diagnostic_semantics",
            ) as recover, mock.patch.object(migrate, "run_generation") as generate:
                result = self._batch(**options)
            self.assertEqual(result, migrate.ExitCode.BATCH_PARTIAL_FAIL)
            recover.assert_not_called()
            generate.assert_not_called()
            self.assertFalse(self.salvage.exists())
            self.assertFalse(list(self.output.rglob("extraction_salvage.json")))
            self.assertFalse(list(self.project.glob("*.pbip")))
            cli = self._cli(*flags)
            self.assertEqual(cli.returncode, migrate.ExitCode.BATCH_PARTIAL_FAIL,
                             self.cli_output)
            self.assertFalse(self.salvage.exists())
            self.assertFalse(list(self.output.rglob("extraction_salvage.json")))
            self.assertFalse(list(self.project.glob("*.pbip")))

    def test_xxe_is_rejected_before_diagnostic_recovery(self):
        self.sources.mkdir()
        sentinel = self.root / "external-entity.txt"
        sentinel.write_text("EXTERNAL_ENTITY_MUST_NOT_BE_RECOVERED", encoding="utf-8")
        (self.sources / "Recovery.twb").write_text(
            '<!DOCTYPE workbook [<!ENTITY external SYSTEM "'
            + sentinel.as_uri() + '">]><workbook>&external;</workbook>',
            encoding="utf-8")
        self._assert_unsafe_rejected()
        self.assertEqual(sentinel.read_text(encoding="utf-8"),
                         "EXTERNAL_ENTITY_MUST_NOT_BE_RECOVERED")
        for path in self.project.rglob("*"):
            if path.is_file():
                self.assertNotIn("EXTERNAL_ENTITY_MUST_NOT_BE_RECOVERED",
                                 path.read_text(encoding="utf-8"))

    def test_traversal_after_valid_workbook_is_rejected_not_silently_skipped(self):
        escaped = self.root / "escaped.txt"
        _package(self.sources, extra_members={
            "../escaped.txt": b"ZIP_TRAVERSAL_MUST_NOT_BE_WRITTEN",
            "SourceScript.py": b"raise RuntimeError('SOURCE_SCRIPT_MUST_NOT_EXECUTE')",
        })
        self._assert_unsafe_rejected()
        self.assertFalse(escaped.exists())
        self.assertFalse(list(self.output.rglob("SourceScript.py")))

    def test_archive_preparation_cannot_move_outside_output_or_move_input(self):
        workbook = _package(self.sources)
        outside = self.root / "outside.json"
        outside.write_bytes(b'{"public": "preserve"}')
        self.project.mkdir(parents=True)
        marker = self.project / "Recovery.pbip"
        marker.write_bytes(b'{"previous": true}')
        task = {
            "workbook_output_dir": str(self.output), "basename": "Recovery",
            "tableau_file": str(workbook), "extract_dir": str(outside),
            "extraction_root": str(self.output / ".migration_extract"),
        }
        with self.assertRaisesRegex(ConfigurationError, "Unsafe salvage output"):
            migrate._prepare_salvage_attempt(task)
        self.assertEqual(outside.read_bytes(), b'{"public": "preserve"}')
        self.assertEqual(marker.read_bytes(), b'{"previous": true}')
        self.assertFalse((self.output / ".migration_previous").exists())
        task.update(workbook_output_dir=str(self.root), basename="sources",
                    extract_dir=None)
        original = workbook.read_bytes()
        with self.assertRaisesRegex(ConfigurationError, "Unsafe salvage output"):
            migrate._prepare_salvage_attempt(task)
        self.assertEqual(workbook.read_bytes(), original)
        self.assertFalse((self.root / ".migration_previous").exists())


class TestSalvageExtractionCheckpoints(SalvageFixture):
    def test_datasource_checkpoints_scrub_only_new_payload_and_reuse_string_work(self):
        root = _workbook_xml()
        sources = root.find("./datasources")
        first = sources[0]
        credential = "synthetic-checkpoint-secret-47"
        formula = '"SYNTHETIC_SHARED_LITERAL_47"'
        for connection in first.findall("./connection//named-connection/connection"):
            connection.set("password", credential)
            connection.set("username", "amount")
        literal = ET.SubElement(first, "column", {
            "name": "[SharedLiteral]", "datatype": "string", "role": "measure",
        })
        ET.SubElement(literal, "calculation", {
            "class": "tableau", "formula": formula,
        })
        for index in range(1, 4):
            source = deepcopy(first)
            source.set("name", f"source-{index}")
            sources.append(source)
        names = [source.get("name") for source in sources]
        workbook = self.root / "checkpoint-complexity.twb"
        workbook.write_bytes(ET.tostring(root, encoding="utf-8"))
        extracted = self.root / "extracted"
        extractor = TableauExtractor(str(workbook), str(extracted))
        real_extract = extractor._extract_datasource_with_selection
        real_checkpoint = extractor._save_salvage_checkpoint
        real_scrub = extractor._scrub_salvage
        checkpoint_payloads = []
        seen = []

        def scrub(value, **kwargs):
            if isinstance(value, dict) and "tables" in value and "connection" in value:
                checkpoint_payloads.append(value["name"])
            return real_scrub(value, **kwargs)

        def assert_checkpoint(expected):
            text = (extracted / "datasources.json").read_text(encoding="utf-8")
            checkpoint = json.loads(text)
            self.assertEqual([source["name"] for source in checkpoint], expected)
            self.assertNotIn(credential, text)
            for source in checkpoint:
                self.assertIn("amount", [column["name"] for table in source["tables"]
                                        for column in table["columns"]])
                self.assertIn("SUM([amount])",
                              [calc["formula"] for calc in source["calculations"]])
                self.assertIn(formula, [calc["formula"]
                                        for calc in source["calculations"]])

        def extract(source):
            if seen:
                assert_checkpoint(seen)
            result = real_extract(source)
            seen.append(result["name"])
            return result

        def checkpoint(*args, **kwargs):
            before = len(checkpoint_payloads)
            result = real_checkpoint(*args, **kwargs)
            if kwargs.get("datasource") is not None:
                self.assertEqual(checkpoint_payloads[before:],
                                 [kwargs["datasource"]["name"]])
                assert_checkpoint(seen)
            return result

        module = sys.modules[TableauExtractor.__module__]
        with mock.patch.object(module, "redact_credentials",
                               wraps=module.redact_credentials) as redact, \
                mock.patch.object(extractor, "_scrub_salvage", side_effect=scrub), \
                mock.patch.object(extractor, "_save_salvage_checkpoint",
                                  side_effect=checkpoint), \
                mock.patch.object(extractor, "_extract_datasource_with_selection",
                                  side_effect=extract), \
                redirect_stdout(io.StringIO()):
            self.assertTrue(extractor.extract_all())
        self.assertEqual(seen, names)
        assert_checkpoint(names)
        self.assertEqual(sum(call.args == (formula,) for call in redact.call_args_list), 1)
        self.assertEqual(extractor._scrub_salvage("[amount]", preserve_references=True),
                         "[amount]")
        self.assertEqual(extractor._scrub_salvage("[amount]"), "[***REDACTED***]")
        self.assertEqual(extractor._scrub_salvage("[amount]", preserve_references=True),
                         "[amount]")

    def test_reused_extractor_resets_scrubbing_after_source_secrets_change(self):
        root = _workbook_xml()
        source = root.find("./datasources/datasource")
        future_secret = "synthetic-new-source-secret-83"
        formula = f'"{future_secret}"'
        literal = ET.SubElement(source, "column", {
            "name": "[ChangingSecret]", "datatype": "string", "role": "measure",
        })
        ET.SubElement(literal, "calculation", {
            "class": "tableau", "formula": formula,
        })
        workbook = self.root / "reuse.twb"
        workbook.write_bytes(ET.tostring(root, encoding="utf-8"))
        extracted = self.root / "extracted"
        extractor = TableauExtractor(str(workbook), str(extracted))
        with redirect_stdout(io.StringIO()):
            self.assertTrue(extractor.extract_all())
        self.assertIn(future_secret, (extracted / "datasources.json").read_text(
            encoding="utf-8"))
        for connection in source.findall("./connection//named-connection/connection"):
            connection.set("password", future_secret)
        workbook.write_bytes(ET.tostring(root, encoding="utf-8"))
        with redirect_stdout(io.StringIO()):
            self.assertTrue(extractor.extract_all())
        for path in extracted.glob("*.json"):
            text = path.read_text(encoding="utf-8")
            json.loads(text)
            self.assertNotIn(future_secret, text, path.name)
        self.assertEqual(len(_read_json(extracted / "datasources.json")), 1)
        self.assertIn(formula, [calc["formula"]
                               for calc in extractor.workbook_data["calculations"]])

    def test_next_datasource_sees_scrubbed_checkpoint_without_live_model_mutation(self):
        credential = "synthetic-source-credential-9f2"
        assignment = "synthetic-assignment-literal-7c4"
        root = _workbook_xml()
        first = root.find("./datasources/datasource")
        for connection in first.findall("./connection//named-connection/connection"):
            connection.set("password", credential)
            connection.set("username", "amount")
        literal = ET.SubElement(first, "column", {
            "name": "[DiagnosticLiteral]", "datatype": "string", "role": "measure",
        })
        formula = f'"password={assignment}; copied={credential}"'
        ET.SubElement(literal, "calculation", {"class": "tableau", "formula": formula})
        second = deepcopy(first)
        second.set("name", "next-datasource")
        root.find("./datasources").append(second)
        workbook = self.root / "checkpoint.twb"
        workbook.write_bytes(ET.tostring(root, encoding="utf-8"))
        extracted = self.root / "extracted"
        extractor = TableauExtractor(str(workbook), str(extracted), salvage_partial=True)
        real_extract = extractor._extract_datasource_with_selection
        live = []
        snapshots = []

        def assert_scrubbed_json():
            paths = list(extracted.glob("*.json"))
            self.assertTrue(paths)
            for path in paths:
                text = path.read_text(encoding="utf-8")
                json.loads(text)
                self.assertNotIn(credential, text, path.name)
                self.assertNotIn(assignment, text, path.name)

        def extract(datasource):
            if datasource.get("name") == "next-datasource":
                checkpoint = _read_json(extracted / "datasources.json")
                self.assertEqual([source["name"] for source in checkpoint], ["recoverable"])
                inventory = _read_json(extracted / "extraction_salvage.json")
                self.assertEqual(inventory["status"], "incomplete")
                self.assertTrue(any(item["name"] == "datasources"
                                    and item["kind"] == "stage"
                                    for item in inventory["not_attempted"]))
                self.assertTrue(any(item["name"] == "recoverable"
                                    for item in inventory["recovered"]))
                self.assertEqual(live[0], snapshots[0])
                self.assertIn("amount", [column["name"]
                                        for table in checkpoint[0]["tables"]
                                        for column in table["columns"]])
                self.assertIn("SUM([amount])",
                              [calc["formula"] for calc in checkpoint[0]["calculations"]])
                self.assertIn(formula, [calc["formula"] for calc in live[0]["calculations"]])
                assert_scrubbed_json()
            result = real_extract(datasource)
            live.append(result)
            snapshots.append(deepcopy(result))
            return result

        with mock.patch.object(extractor, "_extract_datasource_with_selection",
                               side_effect=extract) as extract_call, \
                redirect_stdout(io.StringIO()):
            self.assertTrue(extractor.extract_all())
        self.assertEqual(extract_call.call_count, 2)
        assert_scrubbed_json()
        self.assertIn(formula, [calc["formula"]
                               for calc in extractor.workbook_data["calculations"]])

    def test_supported_stage_refusal_preserves_later_calculations(self):
        workbook = _package(self.sources)
        extracted = self.root / "extracted"
        extractor = TableauExtractor(str(workbook), str(extracted))
        with mock.patch.object(
            extractor, "extract_worksheets",
            side_effect=ExtractionError("Synthetic worksheet-only refusal"),
        ), redirect_stdout(io.StringIO()):
            self.assertFalse(extractor.extract_all())
        inventory = _read_json(extracted / "extraction_salvage.json")
        self.assertEqual(inventory["status"], "incomplete")
        blocked = next(item for item in inventory["blocked"]
                       if item["name"] == "worksheets")
        self.assertEqual(blocked["error"]["type"], "ExtractionError")
        self.assertEqual(blocked["error"]["message"], "Synthetic worksheet-only refusal")
        self.assertTrue(any(item["name"] == "calculations"
                            for item in inventory["recovered"]))
        self.assertEqual(_read_json(extracted / "calculations.json")[0]["formula"],
                         "SUM([amount])")
        self.assertEqual(inventory["diagnostics"]["worksheets"][0]["name"],
                         "Recovered Overview")

    def test_default_refused_datasource_preserves_later_datasource(self):
        workbook = _package(self.sources, refused=True)
        extracted = self.root / "extracted"
        extractor = TableauExtractor(str(workbook), str(extracted))
        with _hyper_io_guard(no_hyper=True), redirect_stdout(io.StringIO()):
            self.assertFalse(extractor.extract_all())
        self.assertEqual(
            [source["name"] for source in _read_json(extracted / "datasources.json")],
            ["recoverable"])
        self.assertEqual(_read_json(extracted / "calculations.json")[0]["formula"],
                         "SUM([amount])")
        self.assertEqual(_read_json(extracted / "extraction_salvage.json")["status"],
                         "incomplete")

    def test_explicit_false_stops_at_datasource_refusal_without_later_extraction(self):
        workbook = _package(self.sources, refused=True)
        extracted = self.root / "extracted"
        extractor = TableauExtractor(str(workbook), str(extracted), salvage_partial=False)
        with mock.patch.object(extractor, "extract_worksheets") as later, \
                _hyper_io_guard(no_hyper=True), redirect_stdout(io.StringIO()):
            with self.assertRaises(ExtractionError):
                extractor.extract_all()
        later.assert_not_called()
        self.assertFalse((extracted / "extraction_salvage.json").exists())
        self.assertFalse((extracted / "calculations.json").exists())

    def test_extractor_rejects_non_boolean_recovery_options(self):
        workbook = _package(self.sources)
        for value in (None, "false", "true", 0, 1, [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ConfigurationError, "salvage_partial must be a boolean"):
                    TableauExtractor(str(workbook), str(self.root / "extracted"),
                                     salvage_partial=value)

    def test_wrapped_security_failure_is_not_a_supported_stage_refusal(self):
        workbook = _package(self.sources)
        extractor = TableauExtractor(
            str(workbook), str(self.root / "extracted"), salvage_partial=True)

        def unsafe_stage(root):
            try:
                raise ValueError("Synthetic unsafe source boundary")
            except ValueError as cause:
                raise ExtractionError("Wrapped security failure") from cause

        with mock.patch.object(extractor, "extract_worksheets", side_effect=unsafe_stage), \
                mock.patch.object(extractor, "extract_calculations") as later, \
                redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ExtractionError, "Wrapped security failure"):
                extractor.extract_all()
        later.assert_not_called()


class TestSalvagePropagation(SalvageFixture):
    def test_unsupported_entry_points_refuse_before_generation(self):
        workbook = _package(self.sources)
        for options in (
            ["--shared-model", str(workbook), str(workbook)],
            ["--batch", str(self.sources), "--assess"],
            [str(workbook), "--quality-report"],
            [str(workbook), "--parity"],
        ):
            with self.subTest(options=options), \
                    mock.patch.object(sys, "argv",
                                      ["migrate.py", *options, "--salvage-partial"]), \
                    mock.patch.object(migrate, "_install_upload_copy_guard"), \
                    mock.patch.object(migrate, "run_batch_migration") as batch, \
                    mock.patch.object(migrate, "run_generation") as generation, \
                    mock.patch.object(migrate, "run_shared_model_migration") as shared, \
                    mock.patch.object(tmdl_generator, "recover_diagnostic_semantics") as recover, \
                    mock.patch.object(migrate, "_desktop_options_from_args") as desktop, \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as error:
                with self.assertRaises(SystemExit) as refused:
                    migrate.main()
                self.assertEqual(refused.exception.code, 2)
                self.assertIn("--salvage-partial requires folder batch", error.getvalue())
                batch.assert_not_called()
                generation.assert_not_called()
                shared.assert_not_called()
                recover.assert_not_called()
                desktop.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_batch_resolves_default_explicit_and_strict_policies(self):
        _package(self.sources)
        for requested, strict, enabled in (
            (None, False, True), (False, False, False), (True, False, True),
            (None, True, False), (True, True, False),
        ):
            with self.subTest(salvage_partial=requested, strict=strict), mock.patch.object(
                migrate, "_migrate_single_workbook",
                return_value={"success": False, "error": "Synthetic refused migration"},
            ) as worker:
                result = self._batch(salvage_partial=requested, strict_generation=strict)
                self.assertEqual(result, migrate.ExitCode.BATCH_PARTIAL_FAIL)
                worker.assert_called_once()
                self.assertIs(worker.call_args.kwargs["salvage_partial"], enabled)
                self.assertIs(worker.call_args.kwargs["strict_generation"], strict)

    def _dispatch(self, *options):
        argv = ["migrate.py", "--batch", str(self.sources),
                "--output-dir", str(self.output), *options]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(migrate, "setup_logging"), \
                mock.patch.object(migrate, "_install_upload_copy_guard"), \
                mock.patch.object(migrate, "run_batch_migration",
                                  return_value=migrate.ExitCode.SUCCESS) as batch, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(migrate.main(), migrate.ExitCode.SUCCESS)
        batch.assert_called_once()
        return batch.call_args.kwargs

    def test_cli_dispatch_preserves_auto_and_separate_explicit_flags(self):
        _package(self.sources)
        default = self._dispatch()
        self.assertIsNone(default["salvage_partial"])
        self.assertFalse(default["strict_generation"])
        enabled = self._dispatch("--salvage-partial")
        self.assertTrue(enabled["salvage_partial"])
        self.assertFalse(enabled["strict_generation"])
        strict = self._dispatch("--strict-generation")
        self.assertFalse(strict["salvage_partial"])
        self.assertTrue(strict["strict_generation"])

    def test_explicit_config_opt_in_reaches_batch_dispatch(self):
        _package(self.sources)
        for shape in (
            {"salvage_partial": True},
            {"migration": {"salvage_partial": True}},
        ):
            with self.subTest(shape=shape):
                config = self.root / "config.json"
                config.write_text(json.dumps(shape), encoding="utf-8")
                options = self._dispatch("--config", str(config))
                self.assertTrue(options["salvage_partial"])

    def test_config_cannot_disable_explicit_cli_opt_in(self):
        _package(self.sources)
        config = self.root / "config.json"
        config.write_text('{"migration": {"salvage_partial": false}}', encoding="utf-8")
        options = self._dispatch("--config", str(config), "--salvage-partial")
        self.assertTrue(options["salvage_partial"])

    def test_cli_recovery_and_strict_flags_are_mutually_exclusive(self):
        _package(self.sources)
        for flags in (
            ("--salvage-partial", "--strict-generation"),
            ("--strict-generation", "--salvage-partial"),
        ):
            with self.subTest(flags=flags), \
                    mock.patch.object(migrate, "run_batch_migration") as batch, \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as error:
                with self.assertRaises(SystemExit) as refused:
                    self._dispatch(*flags)
                self.assertEqual(refused.exception.code, 2)
                self.assertIn("not allowed with argument", error.getvalue())
                batch.assert_not_called()

    def test_explicit_cli_overrides_opposing_flat_and_nested_config(self):
        _package(self.sources)
        for nested in (False, True):
            for flag, configured, expected in (
                ("--strict-generation",
                 {"salvage_partial": True, "strict_generation": False}, (False, True)),
                ("--salvage-partial",
                 {"salvage_partial": False, "strict_generation": True}, (True, False)),
            ):
                with self.subTest(nested=nested, flag=flag):
                    config = self.root / "config.json"
                    config.write_text(json.dumps(
                        {"migration": configured} if nested else configured),
                        encoding="utf-8")
                    options = self._dispatch("--config", str(config), flag)
                    self.assertIs(options["salvage_partial"], expected[0])
                    self.assertIs(options["strict_generation"], expected[1])

    def test_config_false_and_strict_are_not_overwritten_by_cli_defaults(self):
        _package(self.sources)
        for values, expected in (
            ({"salvage_partial": False}, (False, False)),
            ({"strict_generation": True}, (False, True)),
            ({"salvage_partial": True, "strict_generation": True}, (False, True)),
        ):
            with self.subTest(values=values):
                config = self.root / "config.json"
                config.write_text(json.dumps({"migration": values}), encoding="utf-8")
                options = self._dispatch("--config", str(config))
                self.assertIs(options["salvage_partial"], expected[0])
                self.assertIs(options["strict_generation"], expected[1])

    def test_config_rejects_non_boolean_flags_before_batch_dispatch(self):
        _package(self.sources)
        for key in ("salvage_partial", "strict_generation"):
            for value in (None, "false", "true", 0, 1, [], {}):
                for nested in (False, True):
                    with self.subTest(key=key, value=value, nested=nested):
                        config = self.root / "config.json"
                        values = {key: value}
                        config.write_text(json.dumps(
                            {"migration": values} if nested else values), encoding="utf-8")
                        with mock.patch("sys.stderr", new_callable=io.StringIO) as error:
                            with self.assertRaises(SystemExit) as refused:
                                self._dispatch("--config", str(config))
                        self.assertEqual(refused.exception.code, 2)
                        self.assertIn(f"{key} must be a boolean", error.getvalue())

    def test_config_api_merge_preserves_explicit_boolean_overrides(self):
        parser = migrate._build_argument_parser()
        for configured, cli, expected in (
            ({}, [], (True, False)),
            ({"salvage_partial": False}, [], (False, False)),
            ({"salvage_partial": True}, ["--strict-generation"], (False, True)),
            ({"strict_generation": True}, ["--salvage-partial"], (True, False)),
        ):
            with self.subTest(configured=configured, cli=cli):
                config = MigrationConfig({"migration": configured}).merge_with_args(
                    parser.parse_args(["--batch", str(self.sources), *cli]))
                self.assertIs(config.salvage_partial, expected[0])
                self.assertIs(config.strict_generation, expected[1])

    def test_automatic_policy_disables_recovery_for_incompatible_batch_modes(self):
        _package(self.sources)
        for options in (
            {"skip_extraction": True},
            {"desktop_options": migrate.DesktopDiagnosticOptions()},
            {"output_format": "fabric"},
        ):
            with self.subTest(options=options), mock.patch.object(
                migrate, "_migrate_single_workbook",
                return_value={"success": False, "error": "Synthetic refused migration"},
            ) as worker, mock.patch.object(
                migrate, "_run_batch_desktop_diagnostics",
                side_effect=lambda *args, **kwargs: args[4],
            ), mock.patch.object(migrate, "_prepare_salvage_attempt") as prepare, \
                    mock.patch.object(tmdl_generator, "recover_diagnostic_semantics") as recover:
                result = self._batch(**options)
            self.assertEqual(result, migrate.ExitCode.BATCH_PARTIAL_FAIL)
            worker.assert_called_once()
            self.assertIs(worker.call_args.kwargs["salvage_partial"], False)
            prepare.assert_not_called()
            recover.assert_not_called()

    def test_default_shared_and_inspection_routes_do_not_request_recovery(self):
        workbook = _package(self.sources)
        for options, handler in (
            (["--shared-model", str(workbook), str(workbook)], "run_shared_model_migration"),
            ([str(workbook), "--assess"], "_run_single_inspection"),
            ([str(workbook), "--quality-report"], "_run_single_inspection"),
            ([str(workbook), "--parity"], "_run_single_inspection"),
        ):
            with self.subTest(options=options), \
                    mock.patch.object(sys, "argv", ["migrate.py", *options]), \
                    mock.patch.object(migrate, "setup_logging"), \
                    mock.patch.object(migrate, "_install_upload_copy_guard"), \
                    mock.patch.object(migrate, handler, return_value=migrate.ExitCode.SUCCESS) as dispatch, \
                    mock.patch.object(migrate, "run_batch_migration") as batch, \
                    mock.patch.object(migrate, "_prepare_salvage_attempt") as prepare, \
                    mock.patch.object(tmdl_generator, "recover_diagnostic_semantics") as recover, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(migrate.main(), migrate.ExitCode.SUCCESS)
            dispatch.assert_called_once()
            batch.assert_not_called()
            prepare.assert_not_called()
            recover.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_default_prep_batch_exports_m_without_workbook_recovery(self):
        self.sources.mkdir()
        write_flow(self.sources, clean_chain(), "Recovery.tfl")
        with mock.patch.object(migrate, "_prepare_salvage_attempt") as prepare, \
                mock.patch.object(tmdl_generator, "recover_diagnostic_semantics") as recover:
            result = self._batch()
        self.assertEqual(result, migrate.ExitCode.SUCCESS)
        prepare.assert_not_called()
        recover.assert_not_called()
        self.assertTrue(list((self.project / "PowerQuery").glob("*.pq")))
        self.assertTrue(list((self.project / "Sources").glob("*.json")))
        self.assertFalse(self.salvage.exists())
        self.assertFalse(list(self.project.rglob("*.pbip")))

    def test_explicit_recovery_refuses_standalone_prep_before_worker(self):
        self.sources.mkdir()
        write_flow(self.sources, clean_chain(), "Recovery.tfl")
        with mock.patch.object(migrate, "_migrate_single_workbook") as worker:
            with self.assertRaisesRegex(ConfigurationError, "workbook batch input"):
                self._batch(salvage_partial=True)
        worker.assert_not_called()
        self.assertFalse(self.project.exists())


class TestSalvagePolicy(unittest.TestCase):
    def test_strict_wins_even_when_recovery_is_explicit_and_mode_is_incompatible(self):
        for options in (
            {}, {"skip_extraction": True}, {"output_format": "fabric"},
            {"desktop_options": migrate.DesktopDiagnosticOptions()},
        ):
            with self.subTest(options=options):
                self.assertIs(migrate._resolve_salvage_policy(
                    salvage_partial=True, strict_generation=True, **options), False)

    def test_policy_rejects_truthy_and_falsey_non_boolean_options(self):
        for key in ("salvage_partial", "strict_generation"):
            for value in ("false", "true", 0, 1, [], {}):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ConfigurationError, f"{key} must be a boolean"):
                        migrate._resolve_salvage_policy(**{key: value})
        with self.assertRaisesRegex(ConfigurationError, "strict_generation must be a boolean"):
            migrate._resolve_salvage_policy(strict_generation=None)
