"""Fixed Calendar mode is explicit and reaches the ordinary batch CLI."""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

import migrate


@pytest.mark.parametrize("mode,start,end,expected", [
    ("source", None, None, (None, None)),
    ("source", 2018, None, (2018, None)),
    ("fixed", None, None, (2020, 2030)),
    ("fixed", 2018, None, (2018, 2030)),
    ("fixed", None, 2035, (2020, 2035)),
    ("fixed", 2018, 2035, (2018, 2035)),
])
def test_calendar_bound_resolution(mode, start, end, expected):
    assert migrate._resolve_calendar_bounds(start, end, mode) == expected


@pytest.mark.parametrize("mode,start,end", [
    ("unknown", None, None), ("fixed", 2035, 2020),
    ("fixed", True, None), ("fixed", 0, None), ("fixed", None, 10000),
    ("source", "2020", None),
])
def test_invalid_calendar_range_is_explicitly_rejected(mode, start, end):
    with pytest.raises(migrate.ConfigurationError):
        migrate._resolve_calendar_bounds(start, end, mode)


def test_fixed_calendar_cli_reaches_batch_without_native_diagnostics(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    with mock.patch.object(sys, "argv", [
            "migrate.py", "batch", str(sources), "--calendar-mode", "fixed"]), \
            mock.patch.object(migrate, "run_batch_migration",
                              return_value=migrate.ExitCode.SUCCESS) as batch, \
            redirect_stdout(io.StringIO()):
        assert migrate.main() == migrate.ExitCode.SUCCESS
    assert batch.call_args.kwargs["calendar_start"] == 2020
    assert batch.call_args.kwargs["calendar_end"] == 2030
    assert batch.call_args.kwargs["calendar_mode"] == "fixed"
    assert batch.call_args.kwargs["desktop_options"] is None


def test_manifest_can_choose_fixed_bounds_per_workbook(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    for name in ("Fixed", "Dynamic"):
        (sources / f"{name}.twb").write_text("<workbook/>", encoding="utf-8")
    output = tmp_path / "output"
    seen = {}

    def generate(**kwargs):
        name = kwargs["basename"]
        seen[name] = (kwargs["wb_cal_start"], kwargs["wb_cal_end"])
        project = Path(kwargs["workbook_output_dir"], name)
        project.mkdir(parents=True, exist_ok=True)
        (project / f"{name}.pbip").write_text("{}", encoding="utf-8")
        return {"success": True, "report_name": name}

    with mock.patch.object(migrate, "_migrate_single_workbook", side_effect=generate), \
            mock.patch.object(migrate, "run_batch_html_dashboard"), \
            redirect_stdout(io.StringIO()):
        result = migrate.run_batch_migration(
            str(sources), output_dir=str(output),
            manifest=[{"file": "Fixed.twb", "calendar_mode": "fixed",
                       "calendar_start": None, "calendar_end": 2035}])
    assert result == migrate.ExitCode.SUCCESS
    assert seen == {"Fixed": (2020, 2035), "Dynamic": (None, None)}
