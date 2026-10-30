"""Final batch diagnostics describe the retracted output, not a prior snapshot."""

import io
import json
import zipfile
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import migrate
from powerbi_import.migration_quality import (
    MigrationQualityReport,
    apply_pipeline_failure,
)
from powerbi_import.openability import _check_pbip_contract


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _publish_stale_result(workbook_output_dir, basename, **kwargs):
    project = Path(workbook_output_dir, basename)
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{basename}.pbip").write_text("{}", encoding="utf-8")
    report_dir = project / f"{basename}.Report"
    report_dir.mkdir(exist_ok=True)
    (report_dir / "definition.pbir").write_text("{}", encoding="utf-8")
    (project / "openability_report.json").write_text(
        json.dumps({"openable": True, "blocking_count": 0}), encoding="utf-8")
    quality = MigrationQualityReport(
        basename, {}, {}, {}, {}, {"openable": True}, "PASS",
        openability_confidence={"level": "STATIC_PASS"}, handoff_status="PASS",
        evidence_manifest={"artifacts": {"families": {"pbip": True}}})
    quality.save_json(str(Path(workbook_output_dir, f"migration_quality_{basename}.json")))
    return {
        "success": False, "error": "validation", "report_name": basename,
        "error_details": "Synthetic post-generation rejection",
        "stages": {"extraction": "completed", "generation": "completed",
                   "validation": "failed"},
    }


@pytest.mark.parametrize("parallel", [None, 2])
def test_failed_batch_rewrites_quality_openability_and_evidence(tmp_path, parallel):
    sources = tmp_path / "sources"
    sources.mkdir()
    names = ("First", "Second") if parallel else ("Synthetic",)
    for name in names:
        (sources / f"{name}.twb").write_text("<workbook/>", encoding="utf-8")
    output = tmp_path / "output"
    with mock.patch.object(migrate, "_migrate_single_workbook",
                           side_effect=_publish_stale_result), \
            mock.patch.object(migrate, "run_batch_html_dashboard"), \
            redirect_stdout(io.StringIO()):
        code = migrate.run_batch_migration(
            str(sources), output_dir=str(output), parallel=parallel)
    assert code == migrate.ExitCode.BATCH_PARTIAL_FAIL
    states = _read(output / ".migration_batch_state.json")
    for name in names:
        project = output / name
        assert not (project / f"{name}.pbip").exists()
        assert not _check_pbip_contract(str(project)).ok
        openability = _read(project / "openability_report.json")
        assert openability["openable"] is False
        assert openability["blocking_count"] > 0
        assert openability["refresh_verification"] == "not_run"
        assert any(check["name"] == "pipeline_completion"
                   for check in openability["checks"])
        failure = _read(project / "migration_failure.json")
        assert failure["completion_marker"] == "withheld"
        assert failure["failure"]["message"] == "Synthetic post-generation rejection"
        assert Path(failure["extraction_snapshot"]["path"]).is_dir()
        quality = _read(output / f"migration_quality_{name}.json")
        assert quality["status"] == "FAIL"
        assert quality["handoff_status"] == "BLOCKED"
        assert quality["openability"]["openable"] is False
        assert quality["openability_confidence"]["level"] == "UNVERIFIED"
        assert quality["evidence_manifest"]["artifacts"]["families"]["pbip"] is False
        assert states[f"{name}.twb"]["status"] == "failed"
        assert states[f"{name}.twb"]["result"]["quality_status"] == "FAIL"
        assert Path(states[f"{name}.twb"]["result"]["failure_report"]).is_file()
        with zipfile.ZipFile(output / f"evidence_package_{name}.zip") as archive:
            packaged = json.loads(archive.read("evidence_package.json"))
            assert packaged["status"] == "FAIL"
            assert packaged["handoff_status"] == "BLOCKED"
            assert packaged["pipeline_failure"]["message"] == failure["failure"]["message"]
            snapshot = json.loads(archive.read("quality/openability_report.json"))
            assert snapshot["openable"] is False
            assert "quality/migration_failure.json" in archive.namelist()
        assert "Synthetic post-generation rejection" in (
            output / f"migration_quality_{name}.html").read_text(encoding="utf-8")


def test_generation_rejection_retains_stage_type_and_original_message(tmp_path):
    from import_to_powerbi import PowerBIImporter

    message = "A model equality relationship cannot represent a logical range predicate"
    migrate._stats.reset()
    with mock.patch.object(PowerBIImporter, "import_all", side_effect=ValueError(message)), \
            redirect_stdout(io.StringIO()):
        assert not migrate.run_generation("Synthetic", output_dir=str(tmp_path))
    assert migrate._stats.failure == {
        "stage": "generation", "error": "ValueError", "message": message,
    }


def test_extraction_rejection_retains_physical_binding_reason(tmp_path):
    from extract_tableau_data import TableauExtractor

    source = tmp_path / "Synthetic.twb"
    source.write_text("<workbook/>", encoding="utf-8")
    message = "Synthetic leaf columns do not cover declared physical fields"
    preflight = SimpleNamespace(warnings=[], advisories=[], blockers=[])
    migrate._stats.reset()
    with mock.patch("powerbi_import.preflight.run_preflight", return_value=preflight), \
            mock.patch.object(TableauExtractor, "extract_all", side_effect=ValueError(message)), \
            redirect_stdout(io.StringIO()):
        assert not migrate.run_extraction(str(source), output_dir=str(tmp_path / "extract"))
    assert migrate._stats.failure == {
        "stage": "extraction", "error": "ValueError", "message": message,
    }


def test_prep_rejection_retains_its_original_diagnostic(tmp_path):
    source = tmp_path / "Empty.tfl"
    source.write_text(json.dumps({"nodes": {}}), encoding="utf-8")
    migrate._stats.reset()
    with redirect_stdout(io.StringIO()):
        assert not migrate.run_prep_flow(str(source))
    assert migrate._stats.phase == "prep"
    assert migrate._stats.failure["stage"] == "prep"
    assert migrate._stats.failure["error"] == "ValueError"
    assert "no nodes; no outputs can be produced" in migrate._stats.failure["message"]


def test_failure_diagnostic_redacts_credentials_without_erasing_reason():
    diagnostic = migrate._failure_diagnostic(
        "generation", ValueError("Synthetic source refused password=not-a-real-secret"))
    assert diagnostic["stage"] == "generation"
    assert diagnostic["error"] == "ValueError"
    assert "Synthetic source refused" in diagnostic["message"]
    assert "not-a-real-secret" not in diagnostic["message"]


def test_pipeline_failure_preserves_root_blockers_without_duplicate_summary():
    report = MigrationQualityReport(
        "Synthetic", {}, {}, {}, {}, {}, "FAIL",
        blockers=["Exact physical source binding is unresolved"])
    failure = {
        "stage": "hyper_import", "error": "GenerationError",
        "message": "Exact physical source binding is unresolved",
    }
    apply_pipeline_failure(report, failure, {"openable": False})
    assert report.blockers == ["Exact physical source binding is unresolved"]
    assert report.pipeline_failure == failure
    assert report.status == "FAIL"
    assert report.handoff_status == "BLOCKED"


def test_successful_retry_removes_obsolete_failure_artifact(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "Synthetic.twb").write_text("<workbook/>", encoding="utf-8")
    output = tmp_path / "output"
    with mock.patch.object(migrate, "_migrate_single_workbook",
                           side_effect=_publish_stale_result), \
            mock.patch.object(migrate, "run_batch_html_dashboard"), \
            redirect_stdout(io.StringIO()):
        assert migrate.run_batch_migration(
            str(sources), output_dir=str(output)) == migrate.ExitCode.BATCH_PARTIAL_FAIL

    def succeed(**kwargs):
        project = output / kwargs["basename"]
        (project / f"{kwargs['basename']}.pbip").write_text("{}", encoding="utf-8")
        return {"success": True, "report_name": kwargs["basename"]}

    with mock.patch.object(migrate, "_migrate_single_workbook", side_effect=succeed), \
            mock.patch.object(migrate, "run_batch_html_dashboard"), \
            redirect_stdout(io.StringIO()):
        assert migrate.run_batch_migration(
            str(sources), output_dir=str(output), resume=True) == migrate.ExitCode.SUCCESS
    assert not (output / "Synthetic" / "migration_failure.json").exists()


@pytest.fixture
def batch_config_run(tmp_path):
    source = tmp_path / "Synthetic.twb"
    source.write_text("<workbook/>", encoding="utf-8")
    output = tmp_path / "output"
    config = tmp_path / "batch.json"
    config.write_text(json.dumps([{"file": str(source)}]), encoding="utf-8")
    args = migrate._build_argument_parser().parse_args([
        "--batch-config", str(config), "--output-dir", str(output)])
    with ExitStack() as stack:
        patches = {}
        for name, value in {
            "run_extraction": True, "run_prep_flow": True,
            "run_generation": True, "run_migration_report": {},
            "_stage_declared_local_sources": {},
            "_collect_local_data_files": {}, "_convert_hyper_to_csv_in_data": {},
            "_sync_final_calendar_source_expressions": None,
            "_export_power_query_files": None, "_shorten_report_artifact_path": None,
            "_require_hyper_import": None, "_run_openability_gate": True,
            "run_batch_html_dashboard": None,
        }.items():
            patches[name] = stack.enter_context(
                mock.patch.object(migrate, name, return_value=value))
        stack.enter_context(mock.patch(
            "powerbi_import.import_to_powerbi.PowerBIImporter._load_converted_objects",
            return_value={}))
        stack.enter_context(redirect_stdout(io.StringIO()))
        yield args, output, patches


@pytest.mark.parametrize("stage", [
    "extraction", "prep", "source_staging", "generation",
    "postprocessing", "validation",
])
def test_batch_config_finalizes_each_failed_pipeline_stage(batch_config_run, stage):
    args, output, patches = batch_config_run
    _publish_stale_result(str(output), "Synthetic")
    message = f"Synthetic {stage} rejection"
    if stage in ("extraction", "prep", "generation"):
        def reject(*unused_args, **unused_kwargs):
            migrate._stats.failure = migrate._failure_diagnostic(
                stage, ValueError(message))
            return False

        patches[f"run_{'prep_flow' if stage == 'prep' else stage}"].side_effect = reject
        if stage == "prep":
            args.prep = "Synthetic.tfl"
    else:
        target = {
            "source_staging": "_stage_declared_local_sources",
            "postprocessing": "_collect_local_data_files",
            "validation": "_run_openability_gate",
        }[stage]
        patches[target].side_effect = ValueError(message)
    assert migrate._run_batch_config(args) == migrate.ExitCode.BATCH_PARTIAL_FAIL
    project = output / "Synthetic"
    assert not (project / "Synthetic.pbip").exists()
    failure = _read(project / "migration_failure.json")
    assert failure["failure"]["stage"] == stage
    assert failure["failure"]["error"] == "ValueError"
    assert failure["failure"]["message"] == message
    assert Path(failure["extraction_snapshot"]["path"]).is_dir()
    assert _read(project / "openability_report.json")["openable"] is False
    quality = _read(output / "migration_quality_Synthetic.json")
    assert quality["status"] == "FAIL"
    assert quality["handoff_status"] == "BLOCKED"
    with zipfile.ZipFile(output / "evidence_package_Synthetic.zip") as archive:
        assert json.loads(archive.read("evidence_package.json"))["status"] == "FAIL"
        assert "quality/migration_failure.json" in archive.namelist()


def test_batch_config_retracts_entry_and_records_interrupt(batch_config_run):
    args, output, patches = batch_config_run
    _publish_stale_result(str(output), "Synthetic")
    patches["_collect_local_data_files"].side_effect = KeyboardInterrupt(
        "Synthetic interrupt")
    with pytest.raises(KeyboardInterrupt, match="Synthetic interrupt"):
        migrate._run_batch_config(args)
    project = output / "Synthetic"
    assert not (project / "Synthetic.pbip").exists()
    failure = _read(project / "migration_failure.json")
    assert failure["failure"]["stage"] == "postprocessing"
    assert failure["failure"]["error"] == "KeyboardInterrupt"
    assert failure["failure"]["message"] == "Synthetic interrupt"
    assert _read(project / "openability_report.json")["openable"] is False


def test_batch_config_invalid_calendar_does_not_skip_other_entries(batch_config_run):
    args, output, patches = batch_config_run
    config = Path(args.batch_config)
    entries = json.loads(config.read_text(encoding="utf-8"))
    entries[0]["calendar_start"] = 2035
    entries[0]["calendar_end"] = 2020
    valid_source = config.parent / "Other.twb"
    valid_source.write_text("<workbook/>", encoding="utf-8")
    entries.append({
        "file": str(valid_source), "calendar_mode": "fixed",
        "calendar_start": None, "calendar_end": 2035,
    })
    config.write_text(json.dumps(entries), encoding="utf-8")
    assert migrate._run_batch_config(args) == migrate.ExitCode.BATCH_PARTIAL_FAIL
    patches["run_generation"].assert_called_once()
    assert patches["run_generation"].call_args.kwargs["calendar_start"] == 2020
    assert patches["run_generation"].call_args.kwargs["calendar_end"] == 2035


@pytest.mark.parametrize("stage,target", [
    ("source_staging", "_stage_declared_local_sources"),
    ("migration_report", "run_migration_report"),
    ("hyper_import", "_require_hyper_import"),
])
def test_ordinary_batch_records_the_actual_rejection_phase(batch_config_run, stage, target):
    args, output, patches = batch_config_run
    source = Path(json.loads(Path(args.batch_config).read_text(encoding="utf-8"))[0]["file"])
    sources = source.parent / "sources"
    sources.mkdir()
    source.rename(sources / source.name)
    _publish_stale_result(str(output), "Synthetic")
    message = f"Synthetic {stage} rejection"
    patches[target].side_effect = ValueError(message)
    assert migrate.run_batch_migration(
        str(sources), output_dir=str(output), skip_extraction=True,
    ) == migrate.ExitCode.BATCH_PARTIAL_FAIL
    failure = _read(output / "Synthetic" / "migration_failure.json")["failure"]
    assert failure["stage"] == stage
    assert failure["error"] == "ValueError"
    assert failure["message"] == message


def test_ordinary_worker_preserves_the_prep_rejection_payload(batch_config_run):
    args, output, patches = batch_config_run
    source = json.loads(Path(args.batch_config).read_text(encoding="utf-8"))[0]["file"]
    failure = {
        "stage": "prep", "error": "ValueError",
        "message": "Synthetic Prep operation cannot be represented",
    }

    def reject(*unused_args, **unused_kwargs):
        migrate._stats.failure = failure
        return False

    patches["run_prep_flow"].side_effect = reject
    result = migrate._migrate_single_workbook(
        source, "Synthetic", str(output), "Synthetic",
        True, "Synthetic.tfl", None, None, None)
    assert result["failure"] == failure
    assert result["error_details"] == failure["message"]
