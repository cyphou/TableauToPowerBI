"""Tests for the unified deterministic migration quality report."""

import html
import json
import os
import shutil
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, unquote, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.migration_quality import (
    MigrationQualityReport,
    add_ai_summary,
    apply_desktop_probe,
    build_consolidated_quality_html,
    build_quality_prompt,
    build_quality_report,
    refresh_desktop_comparison_links,
)


class _ComparisonAnchors(HTMLParser):
    """Read actual browser href values, not HTML-escaped source strings."""

    def __init__(self, document):
        super().__init__()
        self.hrefs = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href", "")
        if tag == "a" and href.endswith("/comparison.html"):
            self.hrefs.append(href)


class TestMigrationQualityDesktopComparisonLinks(unittest.TestCase):
    """Offline regression coverage for portable, non-verdict capture links."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.report = MigrationQualityReport(
            report_name="Sales & café",
            assessment={}, parity={"parity_score": 73}, data={}, interface={},
            openability={}, status="WARN", warnings=["Keep this warning"],
        )
        self.single = self.output / "division & café" / "Sales #100%" / "migration_quality_Sales.html"
        self.consolidated = self.output / "migration_quality.html"
        self.quality_json = self.single.with_suffix(".json")
        self.report.save_json(str(self.quality_json))
        self.render_quality()

    def render_quality(self):
        self.report.save_html(str(self.single))
        build_consolidated_quality_html(
            [str(self.quality_json)], str(self.consolidated))

    def comparison(self, name="run-001", status="captured", manifest_text=None):
        directory = self.output / "desktop_comparisons" / name
        directory.mkdir(parents=True, exist_ok=True)
        report = directory / "comparison.html"
        report.write_text("<html>Separate capture evidence</html>", encoding="utf-8")
        payload = {
            "status": status,
            # Discovery must use artifacts, not paths from the original host.
            "run_dir": "Z:/missing original machine/run",
            "output_root": "Z:/missing original machine",
        }
        (directory / "manifest.json").write_text(
            json.dumps(payload) if manifest_text is None else manifest_text,
            encoding="utf-8")
        return report

    def document(self, path):
        return path.read_text(encoding="utf-8")

    def assert_links(self, path, reports):
        document = self.document(path)
        hrefs = _ComparisonAnchors(document).hrefs
        expected = [
            quote(os.path.relpath(report, path.parent).replace("\\", "/"), safe="/.")
            for report in reports
        ]
        self.assertCountEqual(hrefs, expected)
        self.assertEqual(len(hrefs), len(set(hrefs)), "Duplicate comparison links")
        for href in hrefs:
            parsed = urlsplit(href)
            self.assertEqual((parsed.scheme, parsed.netloc, parsed.query, parsed.fragment),
                             ("", "", "", ""))
            self.assertNotIn("\\", href)
            self.assertFalse(href.startswith("/"))
            self.assertTrue((path.parent / unquote(href)).is_file(), href)
        return document

    def test_no_comparison_directory_or_report_never_creates_broken_links(self):
        for empty_directory in (False, True):
            with self.subTest(empty_directory=empty_directory):
                if empty_directory:
                    # A manifest alone does not imply a browsable comparison.
                    orphan = self.comparison()
                    orphan.unlink()
                self.render_quality()
                before = {p: p.read_bytes() for p in (self.single, self.consolidated)}
                self.assertEqual(refresh_desktop_comparison_links(str(self.output)), [])
                for path in before:
                    self.assert_links(path, [])
                    self.assertNotIn("quality-desktop-comparisons", self.document(path))
                    self.assertEqual(path.read_bytes(), before[path])

    def test_relative_urls_escape_special_characters_and_nested_paths(self):
        name = "run café & #'100%"
        comparison = self.comparison(name)
        self.render_quality()
        for path, prefix in ((self.single, "../../"), (self.consolidated, "")):
            with self.subTest(path=path):
                document = self.assert_links(path, [comparison])
                expected = prefix + "desktop_comparisons/" + quote(name, safe="") + "/comparison.html"
                self.assertEqual(_ComparisonAnchors(document).hrefs, [expected])
                # Apostrophes need not be escaped in link text (only in URLs).
                self.assertIn(html.escape(name, quote=False), document)
                self.assertNotIn("Z:/missing", document)
                self.assertNotIn(str(self.output), document)

    def test_links_remain_valid_when_output_bundle_is_moved(self):
        comparison = self.comparison("run café & #100%")
        self.render_quality()
        moved = self.root / "downloaded bundle & #2"
        shutil.copytree(self.output, moved)
        shutil.rmtree(self.output)
        for original in (self.single, self.consolidated):
            path = moved / original.relative_to(self.output)
            self.assert_links(path, [moved / comparison.relative_to(self.output)])
        self.assertEqual(refresh_desktop_comparison_links(str(moved)), [])

    def test_sibling_and_legacy_runs_without_new_manifest_fields_remain_discoverable(self):
        (self.output / "Synthetic.pbip").write_text("{}", encoding="utf-8")
        legacy = self.comparison("legacy-run")
        sibling = self.output.with_name(self.output.name + ".desktop_comparisons")
        external = sibling / "external-run" / "comparison.html"
        external.parent.mkdir(parents=True)
        external.write_text("<html>Separate external evidence</html>", encoding="utf-8")
        (external.parent / "manifest.json").write_text(
            '{"schema_version":3,"status":"captured",'
            '"run_dir":"Z:/synthetic-original-location"}', encoding="utf-8")
        original_artifacts = {
            p: p.read_bytes() for p in (legacy, legacy.parent / "manifest.json",
                                       external, external.parent / "manifest.json")}
        self.render_quality()
        for path in (self.single, self.consolidated):
            self.assert_links(path, [legacy, external])
            self.assertNotIn("Z:/synthetic", self.document(path))
        for path, content in original_artifacts.items():
            self.assertEqual(path.read_bytes(), content)

        # Move the project and its sibling together. Old recorded machine
        # locations must not override discovery from the actual artifact tree.
        relocated = self.root / "relocated"
        relocated.mkdir()
        moved_output = relocated / self.output.name
        moved_sibling = relocated / sibling.name
        shutil.copytree(self.output, moved_output)
        shutil.copytree(sibling, moved_sibling)
        for original in (self.single, self.consolidated):
            moved = moved_output / original.relative_to(self.output)
            self.assert_links(moved, [
                moved_output / legacy.relative_to(self.output),
                moved_sibling / external.relative_to(sibling),
            ])
        self.assertEqual(refresh_desktop_comparison_links(str(moved_output)), [])

    def test_failed_and_successful_captures_are_not_parity_verdicts(self):
        failed = self.comparison("run-failed", status="failed")
        captured = self.comparison("run-captured")
        self.render_quality()
        for path in (self.single, self.consolidated):
            document = self.assert_links(path, [failed, captured])
            self.assertIn("Capture failure / incomplete — not parity success", document)
            self.assertIn("Captures recorded — manual review required", document)
            self.assertIn("not a parity verdict", document)
            self.assertIn("even successful captures do not verify", document)
            self.assertIn("Keep this warning", document)
        self.assertEqual(json.loads(self.quality_json.read_text("utf-8")),
                         self.report.to_dict())

    def test_partial_and_interrupted_diagnostics_are_not_reported_as_success(self):
        partial = self.comparison("run-partial", status="in_progress")
        interrupted = self.comparison("run-interrupted", status="interrupted")
        self.render_quality()
        for path in (self.single, self.consolidated):
            document = self.assert_links(path, [partial, interrupted])
            self.assertIn("Partial diagnostic — completion not established", document)
            self.assertIn("Interrupted diagnostic — incomplete", document)
            self.assertNotIn("Captures recorded — manual review required", document)

    def test_missing_malformed_or_unknown_manifest_status_is_unverified(self):
        for payload in (None, "{broken", "[]", '{"status": "PASS"}',
                        '{"status": ["captured"]}'):
            with self.subTest(payload=payload):
                report = self.comparison(manifest_text=payload)
                if payload is None:
                    (report.parent / "manifest.json").unlink()
                self.render_quality()
                for path in (self.single, self.consolidated):
                    document = self.assert_links(path, [report])
                    self.assertIn("not_run — capture status unavailable", document)
                    self.assertNotIn("Captures recorded — manual review required", document)

    def test_refresh_is_idempotent_preserves_quality_and_ignores_other_html(self):
        originals = {p: p.read_bytes() for p in (self.single, self.consolidated)}
        json_before = self.quality_json.read_bytes()
        unrelated = self.output / "assessment.html"
        unrelated.write_bytes(originals[self.single])
        comparison = self.comparison()
        comparison_before = comparison.read_bytes()
        changed = refresh_desktop_comparison_links(str(self.output))
        self.assertCountEqual(changed, [str(self.single), str(self.consolidated)])
        refreshed = {p: p.read_bytes() for p in originals}
        for _ in range(3):
            self.assertEqual(refresh_desktop_comparison_links(str(self.output)), [])
            for path in originals:
                document = self.assert_links(path, [comparison])
                self.assertEqual(document.count('id="quality-desktop-comparisons"'), 1)
                self.assertEqual(path.read_bytes(), refreshed[path])
        self.assertEqual(self.quality_json.read_bytes(), json_before)
        self.assertEqual(unrelated.read_bytes(), originals[self.single])
        self.assertEqual(comparison.read_bytes(), comparison_before)
        # Removing the managed block must recover the original document exactly.
        comparison.unlink()
        self.assertCountEqual(refresh_desktop_comparison_links(str(self.output)),
                              [str(self.single), str(self.consolidated)])
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)

    def test_regeneration_discovers_existing_comparisons_without_refresh(self):
        comparison = self.comparison()
        for _ in range(2):
            self.render_quality()
            for path in (self.single, self.consolidated):
                self.assert_links(path, [comparison])
            self.assertEqual(refresh_desktop_comparison_links(str(self.output)), [])

    def test_refresh_removes_only_stale_reports_then_removes_empty_section(self):
        first = self.comparison("run-001")
        second = self.comparison("run-002")
        self.render_quality()
        first.unlink()  # Its manifest remains, but must no longer be linked.
        self.assertCountEqual(refresh_desktop_comparison_links(str(self.output)),
                              [str(self.single), str(self.consolidated)])
        for path in (self.single, self.consolidated):
            self.assert_links(path, [second])
            self.assertNotIn("run-001", self.document(path))
        second.unlink()
        self.assertCountEqual(refresh_desktop_comparison_links(str(self.output)),
                              [str(self.single), str(self.consolidated)])
        for path in (self.single, self.consolidated):
            self.assert_links(path, [])
            self.assertNotIn("quality-desktop-comparisons", self.document(path))
        self.assertEqual(refresh_desktop_comparison_links(str(self.output)), [])

    def test_compare_finalization_refreshes_existing_quality_after_artifacts_exist(self):
        from powerbi_import import desktop_comparison
        from powerbi_import.tableau_desktop_probe import TableauProbeReport

        source = self.root / "source"
        source.mkdir()
        workbook = source / "Sales.twb"
        workbook.write_text("<workbook/>", encoding="utf-8")
        pbip = self.output / "Sales" / "Sales.pbip"
        pbip.parent.mkdir()
        pbip.write_text("{}", encoding="utf-8")
        json_before = self.quality_json.read_bytes()
        finalized = []

        def observed_refresh(output_root):
            runs = list((self.output / "desktop_comparisons").iterdir())
            latest = next(p for p in runs if p not in finalized)
            self.assertTrue((latest / "comparison.html").is_file())
            saved = json.loads((latest / "manifest.json").read_text("utf-8"))
            self.assertEqual(saved["status"], "failed")
            finalized.append(latest)
            return refresh_desktop_comparison_links(output_root)

        with patch.object(desktop_comparison, "probe_tableau_open",
                          return_value=TableauProbeReport(str(workbook), status="unavailable")), \
             patch.object(desktop_comparison, "_pbi_capture",
                          return_value={"status": "unavailable", "screenshot": None}), \
             patch.object(desktop_comparison, "_cleanup_comparison_processes"), \
             patch("subprocess.Popen", side_effect=AssertionError("Never launch Desktop")), \
             patch("powerbi_import.migration_quality.refresh_desktop_comparison_links",
                   side_effect=observed_refresh) as refresh:
            for _ in range(2):
                manifest = desktop_comparison.compare_desktop_reports(str(source), str(self.output))
                self.assertEqual(manifest["status"], "failed")
                self.assertIsNone(manifest["parity_score"])
                for path in (self.single, self.consolidated):
                    document = self.assert_links(
                        path, [run / "comparison.html" for run in finalized])
                    self.assertIn("Capture failure / incomplete — not parity success", document)
            self.assertEqual(refresh.call_count, 2)
            refresh.assert_called_with(str(self.output.resolve()))
        self.assertEqual(self.quality_json.read_bytes(), json_before)
        self.assertEqual(refresh_desktop_comparison_links(str(self.output)), [])


class _Assessment:
    overall_score = "GREEN"

    def to_dict(self):
        return {"overall_score": self.overall_score}


class _Openability:
    openable = True
    blocking_issues = []

    def to_dict(self):
        return {"openable": self.openable}


class _FabricValidator:
    result = {"valid": True, "errors": [], "warnings": [], "artifacts_checked": 6}

    @classmethod
    def validate(cls, project_dir, project_name, include_report=True):
        return cls.result


class _LLMResult:
    text = "Outcome: PASS\nHighest priority actions: none\nResidual risks: none"
    source = "llm"


class _Gateway:
    def __init__(self):
        self.calls = []

    def complete(self, prompt, system=None):
        self.calls.append((prompt, system))
        return _LLMResult()


class TestMigrationQuality(unittest.TestCase):
    def _build(self, *, parity=None, data=None, interface=None, openability=None,
               assessment=None, project_dir='project', quality_policy='report',
               extracted=None, semantic_queries=None, semantic_executor=None,
               semantic_fixture_path=None):
        with patch('powerbi_import.migration_quality.run_assessment',
                   return_value=assessment or _Assessment()), \
             patch('powerbi_import.migration_quality.scan_project') as scan, \
             patch('powerbi_import.migration_quality.compare_report_tables',
                   return_value=data or {
                       'summary': {'source_tables': 1, 'tables_found': 1}
                   }), \
             patch('powerbi_import.migration_quality.compare_report_interface',
                   return_value=interface or {
                       'filters': {'covered': True},
                       'parameters': {'covered': True},
                   }), \
             patch('powerbi_import.migration_quality.check_openability',
                   return_value=openability or _Openability()):
            scan.return_value.to_dict.return_value = parity or {'gaps': []}
            return build_quality_report(
                extracted or {}, project_dir, 'Demo',
                quality_policy=quality_policy,
                semantic_queries=semantic_queries,
                semantic_executor=semantic_executor,
                semantic_fixture_path=semantic_fixture_path,
            )

    def test_pass_when_all_checks_are_clean(self):
        report = self._build()
        self.assertEqual(report.status, 'PASS')
        self.assertEqual(report.handoff_status, 'PASS')
        self.assertFalse(report.fabric['present'])
        self.assertEqual(report.openability_confidence['level'], 'STATIC_PASS')
        self.assertEqual(report.openability_confidence['desktop']['status'], 'not_run')
        self.assertEqual(report.openability_confidence['semantic_execution'], 'not_run')
        self.assertEqual(report.evidence_manifest['manifest_version'], '1.0')
        self.assertEqual(report.evidence_manifest['validation']['status'], 'PASS')
        self.assertEqual(report.strategy['status'], 'recommended')
        self.assertEqual(report.lineage['status'], 'static_evidence')
        self.assertEqual(report.evidence_manifest['checkpoints']['status'], 'not_found')
        self.assertEqual(report.blockers, [])
        self.assertEqual(report.warnings, [])
        self.assertEqual(report.semantic_context['execution']['status'], 'not_run')
        self.assertIn('summary', report.m_emitters)
        self.assertEqual(report.m_emitters['summary']['aliases'], 98)
        self.assertIn('summary', report.visual_mappings)
        self.assertEqual(report.visual_mappings['summary']['status_counts']['approximation'], 16)

    def test_sampled_or_capped_hyper_manifest_cannot_receive_clean_full_data_handoff(self):
        cases = (
            {"row_count": 2, "total_rows": 37, "max_rows": 2,
             "sampled": True, "truncated": True},
            {"row_count": 2, "total_rows": None, "max_rows": None,
             "sampled": True, "truncated": None},
        )
        for limits in cases:
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as root:
                data = Path(root, "Data")
                data.mkdir()
                (data / "synthetic.csv").write_text("id\n1\n2\n", encoding="utf-8")
                record = {
                    "identity": {
                        "workbook": str(Path(root, "synthetic.twbx")),
                        "archive_member": "Data/synthetic.hyper",
                        "schema": "Extract", "table": "Facts",
                    },
                    "format": "csv", "filename": "synthetic.csv",
                    "csv_filename": "synthetic.csv", "columns": ["id"], **limits,
                }
                (data / "hyper_exports.json").write_text(
                    json.dumps({"version": 1, "exports": [record]}), encoding="utf-8",
                )
                report = self._build(project_dir=root)
                self.assertNotEqual(
                    report.status, "PASS",
                    "Static schema checks cannot certify capped/sample-only data",
                )
                serialized = json.loads(json.dumps(report.to_dict(), default=str))
                self.assertIs(
                    serialized["data"]["hyper_sampling"]["full_data_verified"], False,
                )
                self.assertIs(
                    serialized["evidence_manifest"]["hyper_sampling"]["full_data_verified"],
                    False,
                )
                self.assertIn(serialized["handoff_status"], ("WARN", "BLOCKED"))
                self.assertEqual(
                    serialized["evidence_manifest"]["handoff"]["status"],
                    serialized["handoff_status"],
                )
                diagnostics = "\n".join(report.warnings + report.blockers).lower()
                self.assertTrue(
                    any(word in diagnostics for word in ("sample", "capped", "truncated")),
                    diagnostics,
                )

    def test_production_policy_blocks_visual_approximations(self):
        report = self._build(
            quality_policy='production',
            extracted={'worksheets': [{
                'name': 'Demo', 'original_mark_class': 'Gantt Bar'
            }]},
        )
        self.assertEqual(report.status, 'FAIL')
        self.assertTrue(any('Visual mapping contains 1 explicit approximation' in item
                            for item in report.blockers))

    def test_combined_group_approximation_warns_and_strict_policy_blocks(self):
        extracted = {'groups': [{
            'name': 'Combined', 'group_type': 'combined',
            'source_fields': ['A', 'B'],
        }]}
        report = self._build(extracted=extracted)
        self.assertEqual(report.status, 'WARN')
        self.assertTrue(any('Combined group' in item and 'delimiter' in item
                            for item in report.warnings))
        strict = self._build(extracted=extracted, quality_policy='production')
        self.assertEqual(strict.status, 'FAIL')
        self.assertTrue(any('Combined group' in item for item in strict.blockers))

    def test_m_fallback_is_visible_when_source_uses_it(self):
        extracted = {
            'datasources': [{'connection': {'type': 'UnknownConnector'}}],
        }
        report = self._build(extracted=extracted)
        self.assertEqual(report.status, 'WARN')
        self.assertIn('M fallback emitters are in use', report.warnings[-1])

    def test_production_policy_blocks_m_fallback_in_use(self):
        extracted = {
            'datasources': [{'connection': {'type': 'UnknownConnector'}}],
        }
        report = self._build(extracted=extracted, quality_policy='production')
        self.assertEqual(report.status, 'FAIL')
        self.assertIn('M fallback emitters are in use', report.blockers[-1])

    def test_report_policy_warns_on_invalid_fabric_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'Demo.Lakehouse'))
            report = self._build(project_dir=tmp, quality_policy='report')
        self.assertEqual(report.status, 'WARN')
        self.assertIn('Fabric-native artifact bundle failed validation.', report.warnings)

    def test_enterprise_policy_blocks_invalid_fabric_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'Demo.Lakehouse'))
            report = self._build(project_dir=tmp, quality_policy='enterprise')
        self.assertEqual(report.status, 'FAIL')
        self.assertIn('Fabric-native artifact bundle failed validation.', report.blockers)

    def test_production_policy_requires_fabric_runtime_evidence(self):
        fabric_evidence = {
            'status': 'locally_valid',
            'confidence': 'FABRIC_STATIC_PASS',
            'validation': {'valid': True, 'errors': [], 'warnings': []},
            'artifacts': {},
            'runtime': {
                'deployment': 'not_run',
                'refresh': 'not_run',
                'semantic_execution': 'not_run',
                'post_deploy': 'not_run',
            },
        }
        with patch('powerbi_import.migration_quality.build_fabric_evidence',
                   return_value=fabric_evidence):
            report = self._build(quality_policy='production')
        self.assertEqual(report.status, 'FAIL')
        self.assertTrue(any('Fabric production evidence is incomplete' in item
                            for item in report.blockers))

    def test_semantic_runtime_executor_passes(self):
        report = self._build(
            semantic_queries=[{'name': 'Sales_total', 'dax': 'EVALUATE ROW("x", 1)'}],
            semantic_executor=lambda query: {'rows': [{'x': 1}]},
        )
        execution = report.semantic_context['execution']
        self.assertEqual(execution['status'], 'passed')
        self.assertEqual(execution['passed'], 1)
        self.assertEqual(report.status, 'PASS')

    def test_quality_report_propagates_complete_semantic_contract_without_desktop_roundtrip(self):
        queries = [
            {
                'name': 'Sales_total',
                'dax': 'EVALUATE ROW("Value", 12)',
                'expected_rows': [{'Value': 12}],
            },
            {
                'name': 'Order_count',
                'dax': 'EVALUATE ROW("Value", 7)',
                'expected_rows': [{'Value': 7}],
            },
        ]
        observed_rows = {
            query['dax']: query['expected_rows'] for query in queries
        }
        calls = []

        def execute(query):
            calls.append(query)
            return {'rows': observed_rows[query]}

        report = self._build(
            semantic_queries=queries,
            semantic_executor=execute,
        )

        execution = report.semantic_context['execution']
        self.assertEqual(calls, [query['dax'] for query in queries])
        self.assertEqual(execution['status'], 'passed')
        self.assertEqual(execution['queries_requested'], 2)
        self.assertEqual(execution['queries_run'], 2)
        self.assertEqual(execution['passed'], 2)
        self.assertEqual(execution['failed'], 0)
        self.assertEqual(execution['queries_not_run'], 0)
        self.assertEqual(len(execution['results']), 2)
        for result, query in zip(execution['results'], queries):
            with self.subTest(query=query['name']):
                self.assertEqual(result['name'], query['name'])
                self.assertEqual(result['status'], 'passed')
                self.assertEqual(result['rows'], query['expected_rows'])
                self.assertTrue(result['evidence']['comparison']['matched'])

        contract = report.validation_contract
        self.assertEqual(contract['status'], 'static_evidence')
        self.assertEqual(contract['semantic']['static_status'], 'passed')
        self.assertEqual(contract['semantic']['execution_status'], 'passed')
        self.assertEqual(contract['semantic']['runtime'], 'evidence_recorded')
        self.assertEqual(contract['m']['status'], 'passed')
        self.assertEqual(contract['recovery']['status'], 'complete')
        # This is the semantic/M/recovery sub-contract, not the full
        # OPERATIONAL_100 gate: no Desktop round-trip was performed.
        self.assertTrue(contract['release_ready'])
        self.assertEqual(
            report.evidence_manifest['artifacts']['validation']['semantic_execution'],
            'passed',
        )
        self.assertTrue(
            report.evidence_manifest['artifacts']['validation']['release_ready']
        )
        self.assertEqual(report.openability_confidence['semantic_execution'], 'not_run')
        self.assertEqual(report.roundtrip_validation['status'], 'static_evidence')
        self.assertEqual(report.roundtrip_validation['desktop']['status'], 'not_run')
        self.assertEqual(report.roundtrip_validation['desktop']['reopen'], 'not_run')

        serialized = json.loads(json.dumps(report.to_dict(), default=str))
        self.assertEqual(
            serialized['semantic_context']['execution']['status'], 'passed'
        )
        self.assertEqual(
            serialized['validation_contract']['semantic']['execution_status'],
            'passed',
        )
        self.assertTrue(serialized['validation_contract']['release_ready'])
        self.assertEqual(
            serialized['roundtrip_validation']['desktop']['status'], 'not_run'
        )

    def test_missing_semantic_executor_keeps_default_report_quality_behavior(self):
        report = self._build(
            semantic_queries=[{
                'name': 'Sales_total',
                'dax': 'EVALUATE ROW("Value", 12)',
                'expected_rows': [{'Value': 12}],
            }],
        )

        execution = report.semantic_context['execution']
        self.assertEqual(execution['status'], 'not_run')
        self.assertEqual(execution['executor']['status'], 'not_run')
        self.assertEqual(execution['queries_requested'], 1)
        self.assertEqual(execution['queries_run'], 0)
        self.assertEqual(execution['results'][0]['status'], 'not_run')
        self.assertFalse(execution['results'][0]['evidence']['executor_called'])
        self.assertEqual(
            report.validation_contract['semantic']['execution_status'], 'not_run'
        )
        self.assertEqual(report.validation_contract['semantic']['runtime'], 'not_run')
        self.assertFalse(report.validation_contract['release_ready'])
        self.assertEqual(report.status, 'PASS')
        self.assertEqual(report.evidence_manifest['validation']['status'], 'PASS')
        self.assertEqual(report.roundtrip_validation['desktop']['status'], 'not_run')

    def test_provider_not_run_with_rows_does_not_pass_semantic_contract(self):
        query = {
            'name': 'Sales_total',
            'dax': 'EVALUATE ROW("Value", 12)',
            'expected_rows': [{'Value': 12}],
        }
        calls = []

        def execute(dax):
            calls.append(dax)
            return {'status': 'not_run', 'rows': [{'Value': 12}]}

        report = self._build(
            semantic_queries=[query],
            semantic_executor=execute,
        )

        execution = report.semantic_context['execution']
        result = execution['results'][0]
        self.assertEqual(calls, [query['dax']])
        self.assertEqual(execution['status'], 'not_run')
        self.assertEqual(execution['queries_run'], 1)
        self.assertEqual(execution['queries_not_run'], 1)
        self.assertEqual(result['status'], 'not_run')
        self.assertIsNone(result['rows'])
        self.assertEqual(result['evidence']['observation'], 'not_received')
        self.assertEqual(result['evidence']['provider_status'], 'not_run')
        self.assertNotIn('comparison', result['evidence'])
        self.assertEqual(
            report.validation_contract['semantic']['execution_status'], 'not_run'
        )
        self.assertFalse(report.validation_contract['release_ready'])
        self.assertEqual(report.status, 'PASS')

    def test_malformed_semantic_wrappers_fail_contract_without_fabricated_nulls(self):
        query = {
            'name': 'Sales_total',
            'dax': 'EVALUATE ROW("Value", 12)',
            'expected_rows': [{'Value': 12}],
        }
        cases = (
            (
                'unknown provider status',
                {'status': 'future_state', 'rows': [{'Value': 12}]},
                'unknown executor response status',
            ),
            (
                'successful wrapper without rows',
                {'status': 'passed'},
                'missing rows/data',
            ),
        )
        for label, response, expected_error in cases:
            with self.subTest(case=label):
                calls = []

                def execute(dax, response=response):
                    calls.append(dax)
                    return response

                report = self._build(
                    semantic_queries=[query],
                    semantic_executor=execute,
                )
                execution = report.semantic_context['execution']
                result = execution['results'][0]

                self.assertEqual(calls, [query['dax']])
                self.assertEqual(execution['status'], 'failed')
                self.assertEqual(execution['failed'], 1)
                self.assertEqual(result['status'], 'failed')
                self.assertIn(expected_error, result['error'])
                self.assertIsNone(result['rows'])
                self.assertEqual(result['evidence']['observation'], 'not_received')
                self.assertNotIn('comparison', result['evidence'])
                self.assertEqual(
                    report.validation_contract['semantic']['execution_status'],
                    'failed',
                )
                self.assertFalse(report.validation_contract['release_ready'])

    def test_invalid_semantic_tolerance_fails_contract_before_runtime_call(self):
        calls = []

        def execute(dax):
            calls.append(dax)
            return {'rows': [{'Value': 12}]}

        report = self._build(
            semantic_queries=[{
                'name': 'Sales_total',
                'dax': 'EVALUATE ROW("Value", 12)',
                'expected_rows': [{'Value': 12}],
                'tolerance': float('nan'),
            }],
            semantic_executor=execute,
        )

        execution = report.semantic_context['execution']
        result = execution['results'][0]
        self.assertEqual(calls, [])
        self.assertEqual(execution['status'], 'failed')
        self.assertEqual(execution['failed'], 1)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('finite non-negative number', result['error'])
        self.assertIsNone(result['rows'])
        self.assertEqual(result['evidence']['observation'], 'not_received')
        self.assertFalse(result['evidence']['executor_called'])
        self.assertNotIn('comparison', result['evidence'])
        self.assertEqual(
            report.validation_contract['semantic']['execution_status'], 'failed'
        )
        self.assertFalse(report.validation_contract['release_ready'])

    def test_semantic_query_budget_preserves_requested_count_and_blocks_incomplete_contract(self):
        queries = [
            {
                'name': f'query-{index}',
                'dax': f'EVALUATE ROW("Value", {index})',
            }
            for index in range(26)
        ]
        calls = []

        def execute(dax):
            calls.append(dax)
            return {'rows': []}

        report = self._build(
            semantic_queries=queries,
            semantic_executor=execute,
        )

        execution = report.semantic_context['execution']
        self.assertEqual(calls, [query['dax'] for query in queries[:25]])
        self.assertEqual(len(calls), 25)
        self.assertEqual(execution['status'], 'not_run')
        self.assertEqual(execution['queries_requested'], 26)
        self.assertEqual(execution['queries_run'], 25)
        self.assertEqual(execution['passed'], 25)
        self.assertEqual(execution['failed'], 0)
        self.assertEqual(execution['queries_not_run'], 1)
        self.assertEqual(len(execution['results']), 26)
        self.assertEqual(execution['results'][-1]['status'], 'not_run')
        self.assertTrue(any('budget' in item.lower() for item in execution['diagnostics']))
        self.assertEqual(
            report.validation_contract['semantic']['execution_status'], 'not_run'
        )
        self.assertFalse(report.validation_contract['release_ready'])
        self.assertEqual(report.status, 'PASS')

    def test_explicit_null_expectation_fails_while_missing_expectation_is_smoke_only(self):
        dax = 'EVALUATE ROW("Value", 12)'
        null_report = self._build(
            semantic_queries=[{
                'name': 'Explicit_null',
                'dax': dax,
                'expected_rows': None,
            }],
            semantic_executor=lambda query: {'rows': [{'Value': 12}]},
        )
        null_execution = null_report.semantic_context['execution']
        null_result = null_execution['results'][0]
        self.assertEqual(null_execution['status'], 'failed')
        self.assertEqual(null_execution['failed'], 1)
        self.assertEqual(null_result['status'], 'failed')
        self.assertEqual(null_result['error'], 'semantic result mismatch')
        self.assertFalse(null_result['evidence']['comparison']['matched'])
        self.assertTrue(null_result['evidence']['comparison']['mismatches'])
        self.assertEqual(
            null_report.validation_contract['semantic']['execution_status'], 'failed'
        )
        self.assertFalse(null_report.validation_contract['release_ready'])

        smoke_report = self._build(
            semantic_queries=[{
                'name': 'Smoke_only',
                'dax': dax,
            }],
            semantic_executor=lambda query: {'rows': [{'Value': 12}]},
        )
        smoke_execution = smoke_report.semantic_context['execution']
        smoke_result = smoke_execution['results'][0]
        self.assertEqual(smoke_execution['status'], 'passed')
        self.assertEqual(smoke_result['status'], 'passed')
        self.assertNotIn('expected_rows', smoke_result)
        self.assertNotIn('comparison', smoke_result['evidence'])
        self.assertEqual(
            smoke_report.validation_contract['semantic']['execution_status'], 'passed'
        )

    def test_semantic_runtime_tolerance_accepts_numeric_difference(self):
        report = self._build(
            semantic_queries=[{
                'name': 'Sales_total',
                'dax': 'EVALUATE ROW("x", 1)',
                'expected_rows': [{'x': 10.0}],
                'tolerance': 0.1,
            }],
            semantic_executor=lambda query: {'rows': [{'x': 10.05}]},
        )
        comparison = report.semantic_context['execution']['results'][0]['evidence']['comparison']
        self.assertTrue(comparison['matched'])
        self.assertEqual(report.status, 'PASS')

    def test_semantic_runtime_mismatch_is_failure_with_evidence(self):
        report = self._build(
            quality_policy='production',
            semantic_queries=[{
                'name': 'Sales_total',
                'dax': 'EVALUATE ROW("x", 1)',
                'expected_rows': [{'x': 10.0}],
            }],
            semantic_executor=lambda query: {'rows': [{'x': 12.0}]},
        )
        result = report.semantic_context['execution']['results'][0]
        self.assertEqual(result['error'], 'semantic result mismatch')
        self.assertIn('value[0].x', result['evidence']['comparison']['mismatches'][0])
        self.assertEqual(report.status, 'FAIL')

    def test_semantic_fixture_loads_expected_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_path = os.path.join(tmpdir, 'semantic.json')
            with open(fixture_path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'version': 1,
                    'name': 'public-sales-reference',
                    'queries': [{
                        'name': 'Sales_total',
                        'dax': 'EVALUATE ROW("x", 1)',
                        'expected_rows': [{'x': 10}],
                    }],
                }, handle)
            report = self._build(
                semantic_fixture_path=fixture_path,
                semantic_executor=lambda query: {'rows': [{'x': 10}]},
            )
        self.assertEqual(report.semantic_context['execution']['status'], 'passed')

    def test_invalid_semantic_fixture_is_runtime_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_path = os.path.join(tmpdir, 'semantic.json')
            with open(fixture_path, 'w', encoding='utf-8') as handle:
                json.dump({'version': 99, 'queries': []}, handle)
            report = self._build(
                quality_policy='production', semantic_fixture_path=fixture_path,
                semantic_executor=lambda query: {'rows': []},
            )
        self.assertEqual(report.semantic_context['execution']['status'], 'failed')
        self.assertEqual(report.status, 'FAIL')

    def test_production_policy_blocks_semantic_runtime_failure(self):
        report = self._build(
            quality_policy='production',
            semantic_queries=[{'name': 'Broken', 'dax': 'EVALUATE ROW("x", 1)'}],
            semantic_executor=lambda query: {'error': 'unauthorized'},
        )
        execution = report.semantic_context['execution']
        self.assertEqual(execution['status'], 'failed')
        self.assertEqual(execution['failed'], 1)
        self.assertEqual(report.status, 'FAIL')
        self.assertIn('Semantic runtime validation failed', report.blockers[-1])

    def test_unresolved_lineage_contract_is_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, 'lineage_map.json'), 'w', encoding='utf-8') as handle:
                json.dump({
                    'contract': {
                        'status': 'partial',
                        'coverage': {'columns': {'percent': 50.0}},
                        'unresolved': [{'source_column': 'Missing'}],
                    }
                }, handle)
            report = self._build(project_dir=tmpdir)
        self.assertEqual(report.status, 'WARN')
        self.assertIn('1 unresolved source-to-target record(s)', report.warnings[-1])
        self.assertEqual(report.lineage['contract']['status'], 'partial')

    def test_production_policy_blocks_unresolved_lineage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, 'lineage_map.json'), 'w', encoding='utf-8') as handle:
                json.dump({'contract': {'status': 'partial', 'unresolved': [
                    {'source_column': 'Missing'}
                ]}}, handle)
            report = self._build(project_dir=tmpdir, quality_policy='production')
        self.assertEqual(report.status, 'FAIL')
        self.assertIn('unresolved source-to-target', report.blockers[0])
        self.assertEqual(
            report.evidence_manifest['validation']['quality_policy'], 'production'
        )

    def test_static_lod_diagnostics_are_reported_without_changing_status(self):
        extracted = {
            'datasources': [{
                'tables': [{'name': 'Sales', 'columns': [{'name': 'Territory'}]}],
                'calculations': [{
                    'caption': 'Sales by territory',
                    'formula': "{FIXED [MissingTerritory] : SUM([Amount])}",
                }],
                'relationships': [],
            }],
        }
        with patch('powerbi_import.migration_quality.run_assessment',
                   return_value=_Assessment()), \
             patch('powerbi_import.migration_quality.scan_project') as scan, \
             patch('powerbi_import.migration_quality.compare_report_tables',
                   return_value={'summary': {'source_tables': 1, 'tables_found': 1}}), \
             patch('powerbi_import.migration_quality.compare_report_interface',
                   return_value={'filters': {'covered': True}, 'parameters': {'covered': True}}), \
             patch('powerbi_import.migration_quality.check_openability',
                   return_value=_Openability()):
            scan.return_value.to_dict.return_value = {'gaps': []}
            report = build_quality_report(extracted, 'project', 'Demo')
        self.assertEqual(report.status, 'PASS')
        self.assertEqual(report.semantic_context['issue_count'], 1)
        self.assertIn('not present', report.semantic_context['lod_issues'][0]['issue'])

    def test_production_policy_blocks_semantic_diagnostics(self):
        extracted = {
            'datasources': [{
                'tables': [{'name': 'Sales', 'columns': [{'name': 'Territory'}]}],
                'calculations': [{
                    'caption': 'Sales by territory',
                    'formula': '{FIXED [MissingTerritory] : SUM([Amount])}',
                }],
            }],
        }
        report = self._build(extracted=extracted, quality_policy='production')
        self.assertEqual(report.status, 'FAIL')
        self.assertIn('static context issue(s)', report.blockers[0])

    def test_table_calc_partition_diagnostics_are_reported(self):
        extracted = {
            'datasources': [{
                'tables': [{'name': 'Sales', 'columns': [{'name': 'Region'}]}],
                'calculations': [{
                    'caption': 'Running sales',
                    'formula': 'RUNNING_SUM(SUM([Amount]))',
                    'table_calc_partitioning': ['MissingGroup'],
                }],
                'relationships': [],
            }],
        }
        with patch('powerbi_import.migration_quality.run_assessment',
                   return_value=_Assessment()), \
             patch('powerbi_import.migration_quality.scan_project') as scan, \
             patch('powerbi_import.migration_quality.compare_report_tables',
                   return_value={'summary': {'source_tables': 1, 'tables_found': 1}}), \
             patch('powerbi_import.migration_quality.compare_report_interface',
                   return_value={'filters': {'covered': True}, 'parameters': {'covered': True}}), \
             patch('powerbi_import.migration_quality.check_openability',
                   return_value=_Openability()):
            scan.return_value.to_dict.return_value = {'gaps': []}
            report = build_quality_report(extracted, 'project', 'Demo')
        self.assertEqual(report.status, 'PASS')
        self.assertEqual(report.semantic_context['issue_count'], 1)
        self.assertIn('MissingGroup', report.semantic_context['lod_issues'][0]['issue'])

    def test_target_measure_context_diagnostics_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'Demo.SemanticModel'))
            with patch('powerbi_import.validator.ArtifactValidator.validate_measure_column_context',
                       return_value=['bare column reference']):
                report = self._build(project_dir=tmp)
        self.assertEqual(report.status, 'PASS')
        self.assertEqual(report.semantic_context['measure_context']['issue_count'], 1)
        self.assertEqual(report.semantic_context['measure_context']['status'],
                         'static_diagnostics')

    def test_dax_conversion_fallbacks_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            tables = os.path.join(tmp, 'Demo.SemanticModel', 'definition', 'tables')
            os.makedirs(tables)
            with open(os.path.join(tables, 'Sales.tmdl'), 'w', encoding='utf-8') as handle:
                handle.write("table Sales\n\tmeasure 'Broken' = /* TODO: DAX conversion validation failed for Broken */ BLANK()\n")
            report = self._build(project_dir=tmp)
        context = report.semantic_context['measure_context']
        self.assertEqual(context['fallback_count'], 1)
        self.assertEqual(len(context['fallbacks']), 1)

    def test_countd_fallbacks_are_reported_without_counting_quoted_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            tables = os.path.join(tmp, 'Demo.SemanticModel', 'definition', 'tables')
            os.makedirs(tables)
            note = '/* TODO: RUNNING_COUNT of COUNTD requires Tableau mark ordering plus partitioning */'
            with open(os.path.join(tables, 'Sales.tmdl'), 'w', encoding='utf-8') as handle:
                handle.write(
                    "table Sales\n"
                    f"\tmeasure 'Broken' = {note} BLANK()\n"
                    f'\tmeasure "Quoted" = "{note}"\n'
                    f'\tannotation Copilot_Description = {note}\n'
                )
            report = self._build(project_dir=tmp)
            production = self._build(project_dir=tmp, quality_policy='production')
        context = report.semantic_context['measure_context']
        self.assertEqual(context['fallback_count'], 1)
        self.assertEqual(report.status, 'WARN')
        self.assertTrue(any('COUNTD calculation(s) could not be converted' in warning
                            for warning in report.warnings))
        self.assertEqual(production.status, 'FAIL')

    def test_generated_filter_context_diagnostics_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            tables = os.path.join(tmp, 'Demo.SemanticModel', 'definition', 'tables')
            os.makedirs(tables)
            with open(os.path.join(tables, 'Sales.tmdl'), 'w', encoding='utf-8') as handle:
                handle.write(
                    "table 'Sales'\n"
                    "\tcolumn 'Region'\n"
                    "\tmeasure 'By region' = CALCULATE([Sales], "
                    "ALLEXCEPT('Sales', 'Sales'[MissingGroup]))\n"
                )
            report = self._build(project_dir=tmp)
        self.assertEqual(report.status, 'PASS')
        filter_context = report.semantic_context['filter_context']
        self.assertEqual(filter_context['issue_count'], 1)
        self.assertIn('MissingGroup', filter_context['issues'][0])

    def test_unsupported_feature_is_blocker(self):
        report = self._build(parity={
            'gaps': [{'key': 'forecast', 'status': 'unsupported'}]
        })
        self.assertEqual(report.status, 'FAIL')
        self.assertIn('Unsupported Tableau features remain in use.', report.blockers)

    def test_missing_table_is_blocker(self):
        report = self._build(data={
            'summary': {'source_tables': 2, 'tables_found': 1}
        })
        self.assertEqual(report.status, 'FAIL')
        self.assertIn('One or more extracted source tables are missing from the target model.',
                      report.blockers)

    def test_valid_fabric_bundle_is_reported_without_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'Demo.Lakehouse'))
            with patch('powerbi_import.fabric_validator.FabricProjectValidator',
                       _FabricValidator):
                report = self._build(project_dir=tmp)
        self.assertTrue(report.fabric['present'])
        self.assertTrue(report.fabric['valid'])
        self.assertEqual(report.status, 'PASS')

    def test_invalid_fabric_bundle_is_blocker(self):
        invalid = type('InvalidFabricValidator', (), {
            'validate': classmethod(lambda cls, project_dir, project_name,
                                    include_report=True: {
                                        'valid': False,
                                        'errors': ['Missing Pipeline'],
                                        'warnings': [],
                                    })
        })
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'Demo.Lakehouse'))
            with patch('powerbi_import.fabric_validator.FabricProjectValidator', invalid):
                report = self._build(project_dir=tmp)
        self.assertEqual(report.status, 'WARN')
        self.assertIn('Fabric-native artifact bundle failed validation.', report.warnings)

    def test_openability_failure_is_blocker(self):
        failed = _Openability()
        failed.openable = False
        failed.blocking_issues = ['Dangling dataset reference']
        report = self._build(openability=failed)
        self.assertEqual(report.status, 'FAIL')
        self.assertIn('Dangling dataset reference', report.blockers)
        self.assertEqual(report.openability_confidence['level'], 'UNVERIFIED')
        self.assertEqual(report.handoff_status, 'BLOCKED')
        self.assertEqual(report.evidence_manifest['handoff']['status'], 'BLOCKED')

    def test_interface_gap_is_warning(self):
        report = self._build(interface={
            'filters': {'covered': False},
            'parameters': {'covered': True},
        })
        self.assertEqual(report.status, 'WARN')
        self.assertTrue(report.warnings)

    def test_save_json_preserves_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self._build()
            path = report.save_json(os.path.join(tmp, 'quality.json'))
            with open(path, encoding='utf-8') as fh:
                payload = json.load(fh)
        self.assertEqual(payload['report_name'], 'Demo')
        self.assertIn('parity', payload)
        self.assertIn('openability_confidence', payload)
        self.assertEqual(payload['status'], 'PASS')

    def test_save_html_contains_quality_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self._build()
            path = report.save_html(os.path.join(tmp, 'quality.html'))
            with open(path, encoding='utf-8') as fh:
                html = fh.read()
        self.assertIn('Migration quality', html)
        self.assertIn('Overall status', html)
        self.assertIn('Validation details', html)
        self.assertIn('No AI summary was requested', html)

    def test_priorities_rank_blockers_before_gaps_and_warnings(self):
        report = self._build(
            parity={'gaps': [{'key': 'forecast', 'label': 'Forecast',
                              'status': 'unsupported', 'evidence': ['forecast.json']}]},
            interface={'filters': {'covered': False}, 'parameters': {'covered': True}},
        )
        self.assertEqual([item['priority'] for item in report.priorities], ['P0', 'P1', 'P2'])
        self.assertEqual(report.priorities[1]['owner'], 'Assessor / domain owner')
        self.assertEqual(report.priorities[1]['evidence'], ['forecast.json'])

    def test_blocker_priority_is_p0(self):
        report = self._build(data={'summary': {'source_tables': 2, 'tables_found': 1}})
        self.assertEqual(report.status, 'FAIL')
        self.assertEqual(report.priorities[0]['priority'], 'P0')

    def test_untracked_feature_family_is_warning(self):
        report = self._build(parity={
            'gaps': [],
            'untracked_features': ['aliases'],
        })
        self.assertEqual(report.status, 'WARN')
        self.assertIn('aliases', report.warnings[0])
        self.assertEqual(report.priorities[0]['priority'], 'P2')

    def test_each_priority_says_what_to_do_about_it(self):
        """The queue answers "what do I fix first", not just "what is wrong"."""
        report = self._build(
            interface={'filters': {'covered': False}, 'parameters': {'covered': False}},
        )
        kinds = {item['action_kind'] for item in report.priorities}
        self.assertTrue(kinds <= {'repair', 'decide', 'verify', 'note'}, kinds)

    def test_something_broken_outranks_something_merely_different(self):
        """A missing parameter is repairable; an approximate filter needs a look."""
        report = self._build(
            interface={'filters': {'covered': False}, 'parameters': {'covered': False}},
        )
        kinds = [item['action_kind'] for item in report.priorities]
        self.assertLess(kinds.index('repair'), kinds.index('verify'))

    def test_owner_comes_from_the_finding_not_from_its_wording(self):
        """Owners used to be guessed by searching the message for 'table'."""
        report = self._build(data={'summary': {'source_tables': 2, 'tables_found': 1}})
        top = report.priorities[0]
        self.assertEqual(top['priority'], 'P0')
        self.assertEqual(top['action_kind'], 'repair')
        self.assertEqual(top['owner'], 'Semantic / Wiring')

    def test_advisory_findings_never_outrank_actionable_ones(self):
        report = self._build(
            parity={'gaps': [], 'untracked_features': ['aliases']},
            interface={'filters': {'covered': True}, 'parameters': {'covered': False}},
        )
        kinds = [item['action_kind'] for item in report.priorities]
        self.assertLess(kinds.index('repair'), kinds.index('note'))

    def test_ai_prompt_contains_verified_facts_and_guardrails(self):
        report = self._build()
        prompt = build_quality_prompt(report)
        self.assertIn('Do not invent tests', prompt)
        self.assertIn('"status": "PASS"', prompt)
        self.assertIn('"blockers": []', prompt)

    def test_ai_summary_is_attached_without_changing_status(self):
        report = self._build()
        gateway = _Gateway()
        updated = add_ai_summary(report, gateway)
        self.assertIs(updated, report)
        self.assertEqual(updated.status, 'PASS')
        self.assertIn('Outcome: PASS', updated.ai_summary)
        self.assertEqual(updated.ai_source, 'llm')
        self.assertEqual(len(gateway.calls), 1)

    def test_missing_gateway_leaves_deterministic_report_unchanged(self):
        report = self._build()
        add_ai_summary(report, None)
        self.assertEqual(report.ai_summary, '')
        self.assertEqual(report.ai_source, 'none')

    def test_opened_desktop_probe_promotes_confidence(self):
        report = self._build()
        apply_desktop_probe(report, {
            'status': 'opened',
            'executable': 'PBIDesktop.exe',
            'signals': [],
        })
        self.assertEqual(report.openability_confidence['level'],
                         'DESKTOP_SMOKE_PASS')
        self.assertEqual(report.openability_confidence['desktop']['status'], 'opened')

    def test_failed_desktop_probe_does_not_promote_confidence(self):
        report = self._build()
        apply_desktop_probe(report, {'status': 'crashed', 'signals': ['load error']})
        self.assertEqual(report.openability_confidence['level'], 'UNVERIFIED')
        self.assertEqual(report.openability_confidence['desktop']['status'], 'crashed')

    def test_reopened_desktop_probe_promotes_reopen_confidence(self):
        report = self._build()
        apply_desktop_probe(report, {'status': 'reopened', 'signals': []})
        self.assertEqual(report.openability_confidence['level'],
                         'DESKTOP_REOPEN_PASS')


if __name__ == '__main__':
    unittest.main()
