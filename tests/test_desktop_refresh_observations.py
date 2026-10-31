"""Owned-engine partition freshness and refresh deadlines, without Windows/GUI."""

import base64
import copy
import json
import re
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from powerbi_import import desktop_accessibility as accessibility
from powerbi_import import desktop_probe as probe

OLD = "2026-10-28T10:00:00.1234567Z"
FRESH = "2026-10-28T10:00:00.1234568Z"
PARTITIONS = (
    ("Customers", "Customers", "11", "201"),
    ("Orders", "Current", "10", "101"),
    ("Orders", "Archive", "10", "102"),
)


def _records(stamp=OLD):
    return [
        {"table": table, "partition": partition, "table_id": table_id,
         "partition_id": partition_id, "refreshed_time": stamp,
         "state": 1, "has_error": False}
        for table, partition, table_id, partition_id in PARTITIONS
    ]


def _observation(stamp=OLD, **overrides):
    result = {
        "status": "observed", "note": "metadata_observed",
        "expected_partitions": 3, "owner_pid": 4242, "engine_pid": 9001,
        "port": 50001, "partitions": _records(stamp), "missing": [], "unexpected": [],
    }
    result.update(overrides)
    return result


def _raw_observation(**overrides):
    result = _observation()
    for key in ("expected_partitions", "missing"):
        result.pop(key)
    result.update(overrides)
    return result


def _complete_counts():
    return {
        "status": "partial", "tables_checked": 2, "tables_nonempty": 1,
        "tables_empty": 1, "tables_failed": 0, "total_rows": 7,
    }


def _powershell_code(script):
    """Decode emitted EncodedCommand without executing it or querying Windows."""
    script = base64.b64decode(script).decode("utf-16le")
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#"))
    return re.sub(r"`[ \t]*\r?\n[ \t]*", " ", code)


def _state3_first_load_baseline():
    """Shape observed on a metadata-only PBIP before its first native refresh."""
    result = _observation(
        "1699-12-31T00:00:00.0000000", expected_partitions=1)
    result["partitions"] = [dict(result["partitions"][1], state=3)]
    return result


class PartitionObserverFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pbip = self.root / "P.pbip"
        self.pbip.write_text("{}", encoding="utf-8")
        self.tables = self.root / "P.SemanticModel" / "definition" / "tables"
        self.tables.mkdir(parents=True)
        self.orders = self.write_table("Orders", """
table Orders
    partition Current = m
        mode: import
        source = let Data = #table({"Public"}, {{1}}) in Data
    partition Archive = m
        mode: import
        source = let Data = #table({"Public"}, {}) in Data
""")
        self.write_table("Customers", """
table Customers
    partition Customers = m
        mode: import
        source = let Data = #table({"Public"}, {{1}}) in Data
""")
        self.write_table("Calendar", """
table Calendar
    partition Calendar = calculated
        source = CALENDAR(DATE(2020, 1, 1), DATE(2030, 1, 1))
""")
        self.process = SimpleNamespace(
            returncode=0, stdout=json.dumps(_raw_observation()), stderr="")
        self.enterContext(mock.patch.object(probe.desktop_window, "IS_WINDOWS", True))
        self.enterContext(mock.patch.object(
            accessibility, "powershell_preflight",
            return_value={"status": "ready", "executable": "powershell.exe"}))
        self.enterContext(mock.patch.object(probe.time, "monotonic", return_value=100.0))
        self.enterContext(mock.patch.object(
            probe.subprocess, "Popen",
            side_effect=AssertionError("Regression tests must not start processes")))
        self.run = self.enterContext(mock.patch.object(
            probe.subprocess, "run", return_value=self.process))

    def write_table(self, name, text):
        path = self.tables / f"{name}.tmdl"
        path.write_text(text, encoding="utf-8")
        return path

    def observe(self, raw=None, **options):
        if raw is not None:
            self.process.stdout = json.dumps(raw)
        return probe.observe_desktop_partitions(str(self.pbip), 4242, **options)


class TestOwnedPartitionObservation(PartitionObserverFixture):
    def test_request_covers_every_declared_m_partition_and_no_calculated_tables(self):
        result = self.observe(timeout=12)
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["expected_partitions"], 3)
        self.assertEqual(result["partitions"], _records())
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["unexpected"], [])
        self.assertEqual((result["owner_pid"], result["engine_pid"], result["port"]),
                         (4242, 9001, 50001))
        request = json.loads(base64.b64decode(self.run.call_args.kwargs["input"]))
        self.assertEqual(request, {
            "operation": "partitions", "owner_pid": 4242,
            "tables": ["Customers", "Orders"],
            "partitions": [
                {"table": table, "partition": partition}
                for table, partition, _, _ in PARTITIONS
            ],
        })
        self.assertGreater(self.run.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(self.run.call_args.kwargs["timeout"], 12)
        self.assertTrue(self.run.call_args.kwargs["capture_output"])
        self.assertNotIn("source =", json.dumps(request))

    def test_missing_expected_table_or_partition_is_mismatch_not_partial_success(self):
        for missing_index in (0, 2):
            with self.subTest(missing_index=missing_index):
                raw = _raw_observation()
                missing = raw["partitions"].pop(missing_index)
                result = self.observe(raw)
                self.assertEqual(result["status"], "mismatch")
                self.assertEqual(result["note"], "expected_partition_mismatch")
                self.assertEqual(result["expected_partitions"], 3)
                self.assertEqual(result["missing"], [{
                    "table": missing["table"], "partition": missing["partition"],
                }])
                self.assertEqual(result["partitions"], raw["partitions"])

    def test_added_partition_or_duplicate_identity_is_mismatch(self):
        added = _raw_observation(unexpected=[{"table_id": "10", "partition_id": "999"}])
        duplicate = _raw_observation()
        duplicate["partitions"].append(copy.deepcopy(duplicate["partitions"][0]))
        for raw in (added, duplicate):
            with self.subTest(raw=raw):
                result = self.observe(raw)
                self.assertEqual(result["status"], "mismatch")
                self.assertEqual(result["note"], "expected_partition_mismatch")

    def test_malformed_observation_fields_are_invalid_and_do_not_leak_raw_details(self):
        secret = "PUBLIC_TEST_ONLY_CONNECTION_DETAIL"
        cases = []
        for overrides in (
                {"status": "verified"}, {"note": secret},
                {"owner_pid": 9999}, {"owner_pid": True}, {"owner_pid": "4242"},
                {"engine_pid": 0}, {"engine_pid": False}, {"port": 0},
                {"port": 65536}, {"partitions": None}, {"unexpected": {}},
        ):
            cases.append(_raw_observation(**overrides))
        missing_key = _raw_observation()
        del missing_key["engine_pid"]
        cases.append(missing_key)
        for overrides in (
                {"state": True}, {"state": "1"}, {"has_error": 0},
                {"has_error": "false"}, {"refreshed_time": 123},
                {"partition_id": "01"}, {"partition_id": True},
                {"partition_id": "1.0"}, {"table_id": "-1"},
                {"table": "An unrelated table"},
        ):
            raw = _raw_observation()
            raw["partitions"][0].update(overrides)
            cases.append(raw)
        for index, raw in enumerate(cases):
            with self.subTest(case=index):
                result = self.observe(raw)
                self.assertEqual(result["status"], "invalid")
                self.assertEqual(result["note"], "observer_result_invalid")
                self.assertEqual(result["expected_partitions"], 3)
                self.assertEqual(result["partitions"], [])
                self.assertNotIn(secret, json.dumps(result))

    def test_bad_json_and_nonobject_results_cannot_be_observations(self):
        for text in ("", "not JSON", "[]", "null"):
            with self.subTest(text=text):
                self.process.stdout = text
                result = self.observe()
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["cause"], "powershell_malformed_json")
                self.assertEqual(result["note"], "powershell_malformed_json")
                self.assertIs(result["retryable"], False)
                self.assertEqual(result["partitions"], [])
        self.process.stdout = '{"status": "observed"}'
        result = self.observe()
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["note"], "observer_result_invalid")
        self.assertEqual(result["partitions"], [])

    def test_error_text_and_unrequested_source_metadata_are_never_returned(self):
        raw = _raw_observation()
        raw["partitions"][0].update(
            error_message="PUBLIC_TEST_ONLY_QUERY_ERROR",
            expression="PUBLIC_TEST_ONLY_M_EXPRESSION")
        result = self.observe(raw)
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["partitions"], _records())
        self.assertNotIn("PUBLIC_TEST_ONLY", json.dumps(result))

    def test_metadata_query_failure_is_distinct_from_observer_unavailability(self):
        result = self.observe(_raw_observation(
            status="query_failed", note="metadata_query_failed", partitions=[]))
        self.assertEqual(result["status"], "query_failed")
        self.assertEqual(result["note"], "metadata_query_failed")
        self.assertEqual(result["partitions"], [])

    def test_unavailable_or_ambiguous_engine_never_becomes_an_empty_success(self):
        for note in ("engine_unavailable_or_ambiguous",
                     "port_unavailable_or_ambiguous", "adomd_client_unavailable"):
            with self.subTest(note=note):
                result = self.observe(_raw_observation(
                    status="unavailable", note=note, engine_pid=0, port=0,
                    partitions=[]))
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["note"], note)
                self.assertEqual(result["expected_partitions"], 3)
                self.assertEqual(result["partitions"], [])

    def test_process_failure_timeout_and_nonzero_exit_keep_distinct_redacted_native_failures(self):
        for exception, status, cause in (
                (OSError("PUBLIC_TEST_ONLY_OS_DETAIL"),
                 "tooling_unavailable", "powershell_launch_failed"),
                (subprocess.TimeoutExpired("PUBLIC_TEST_ONLY_COMMAND", 12),
                 "timed_out", "powershell_timeout"),
        ):
            with self.subTest(exception=exception):
                self.run.side_effect = exception
                result = self.observe()
                self.assertEqual(result["status"], status)
                self.assertEqual(result["cause"], cause)
                self.assertEqual(result["note"], cause)
                self.assertIs(result["retryable"], False)
                self.assertEqual(result["partitions"], [])
                self.assertNotIn("PUBLIC_TEST_ONLY", json.dumps(result))
        self.run.side_effect = None
        self.process.returncode = 1
        self.process.stderr = "PUBLIC_TEST_ONLY_STDERR"
        result = self.observe()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["cause"], "powershell_nonzero_exit")
        self.assertEqual(result["note"], "powershell_nonzero_exit")
        self.assertIs(result["retryable"], False)
        self.assertEqual(result["returncode"], 1)
        self.assertEqual(result["partitions"], [])
        self.assertNotIn("PUBLIC_TEST_ONLY", json.dumps(result))

    def test_invalid_missing_owner_and_nonwindows_never_start_the_observer(self):
        for owner in (None, 0, False, -1, "bad-pid"):
            with self.subTest(owner=owner):
                result = probe.observe_desktop_partitions(str(self.pbip), owner)
                self.assertEqual(result["status"], "unavailable")
        with mock.patch.object(probe.desktop_window, "IS_WINDOWS", False):
            result = self.observe()
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["note"], "windows_unavailable")
        self.run.assert_not_called()

    def test_ambiguous_model_or_duplicate_declared_partition_never_starts_observer(self):
        second = self.root / "Other.SemanticModel"
        second.mkdir()
        self.assertEqual(self.observe()["status"], "unavailable")
        second.rmdir()
        with self.orders.open("a", encoding="utf-8") as handle:
            handle.write("\n    partition current = m\n        source = 1\n")
        self.assertEqual(self.observe()["status"], "unavailable")
        self.run.assert_not_called()

    def test_calculated_only_model_is_no_m_tables_not_observed_success(self):
        self.orders.unlink()
        (self.tables / "Customers.tmdl").unlink()
        result = self.observe()
        self.assertEqual(result["status"], "no_m_tables")
        self.assertEqual(result["expected_partitions"], 0)
        self.run.assert_not_called()

    def test_fractional_observer_budget_is_not_rounded_up_or_floored(self):
        result = self.observe(timeout=0.25)
        self.assertEqual(result["status"], "observed")
        self.assertGreater(self.run.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(self.run.call_args.kwargs["timeout"], 0.25)

    def test_late_metadata_result_cannot_be_returned_as_an_observed_success(self):
        clock = _Clock()

        def delayed(*args, **kwargs):
            clock.advance(0.5)
            return self.process

        self.run.side_effect = delayed
        with mock.patch.object(probe.time, "monotonic", side_effect=clock.time):
            result = self.observe(timeout=0.25)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["cause"], "powershell_timeout")
        self.assertEqual(result["note"], "powershell_timeout")
        self.assertIs(result["retryable"], False)
        self.assertEqual(result["partitions"], [])


class TestOwnedTableCheckCoverage(PartitionObserverFixture):
    def test_complete_row_counts_cover_every_expected_table_not_every_partition(self):
        self.process.stdout = json.dumps(_complete_counts())
        result = probe.verify_desktop_data(str(self.pbip), 4242, timeout=0.25)
        self.assertEqual(result, _complete_counts())
        self.assertEqual(result["tables_checked"], 2)
        request = json.loads(base64.b64decode(self.run.call_args.kwargs["input"]))
        self.assertEqual(request["tables"], ["Customers", "Orders"])
        self.assertGreater(self.run.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(self.run.call_args.kwargs["timeout"], 0.25)

    def test_positive_rows_from_only_one_of_two_expected_tables_are_not_verified(self):
        self.process.stdout = json.dumps({
            "status": "verified", "tables_checked": 1, "tables_nonempty": 1,
            "tables_empty": 0, "tables_failed": 0, "total_rows": 9999,
        })
        result = probe.verify_desktop_data(str(self.pbip), 4242)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["tables_checked"], 0)

    def test_late_row_counts_cannot_be_returned_as_a_successful_check(self):
        clock = _Clock()
        self.process.stdout = json.dumps(_complete_counts())

        def delayed(*args, **kwargs):
            clock.advance(0.5)
            return self.process

        self.run.side_effect = delayed
        with mock.patch.object(probe.time, "monotonic", side_effect=clock.time):
            result = probe.verify_desktop_data(str(self.pbip), 4242, timeout=0.25)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["cause"], "powershell_timeout")
        self.assertIs(result["retryable"], False)
        self.assertEqual(result["tables_checked"], 0)


class TestProjectedObserverBindingRequests(PartitionObserverFixture):
    def test_partition_and_row_checks_share_projected_process_binding_queries(self):
        self.assertEqual(self.observe()["status"], "observed")
        partition_script = self.run.call_args.args[0][-1]
        self.process.stdout = json.dumps(_complete_counts())
        self.assertEqual(
            probe.verify_desktop_data(str(self.pbip), 4242), _complete_counts())
        row_script = self.run.call_args.args[0][-1]
        self.assertEqual(partition_script, row_script)
        code = _powershell_code(partition_script)
        query, = re.findall(r"Get-CimInstance\s+Win32_Process\b[^\r\n]*", code)
        projection, = re.findall(r"-Property\s+(.+?)\s+-ErrorAction\b", query)
        self.assertCountEqual(
            [name.strip() for name in projection.split(",")],
            ["ProcessId", "ParentProcessId", "Name", "ExecutablePath"])
        # Projection must not replace the exact owner/descendant-engine guards
        # with an arbitrary running Desktop or msmdsrv.exe process.
        self.assertRegex(
            code, r"\$byId\[\$ownerPid\]\.Name\s+-ine\s+'PBIDesktop\.exe'")
        self.assertRegex(
            code, r"\$_\.Name\s+-ieq\s+'msmdsrv\.exe'\s+-and\s+"
            r"\$descendants\.Contains\(\[int\]\$_\.ProcessId\)")
        self.assertRegex(code, r"if\s*\(\$engines\.Count\s+-ne\s+1\)\s*\{\s*throw\b")

    def test_tcp_request_projects_only_the_unique_listening_port_of_the_owned_engine(self):
        self.assertEqual(self.observe()["status"], "observed")
        code = _powershell_code(self.run.call_args.args[0][-1])
        query, = re.findall(
            r"Get-CimInstance\b[^\r\n]*\bMSFT_NetTCPConnection\b[^\r\n]*", code)
        self.assertRegex(query, r"-Namespace\s+root/StandardCimv2\b")
        self.assertRegex(query, r"-ClassName\s+MSFT_NetTCPConnection\b")
        tcp_filter, = re.findall(r'-Filter\s+"([^"]+)"', query)
        self.assertEqual(
            " ".join(tcp_filter.split()),
            "State = 2 AND OwningProcess = $([int]$engine.ProcessId)")
        projection, = re.findall(r"-Property\s+(.+?)\s+-ErrorAction\b", query)
        self.assertEqual(projection.strip(), "LocalPort")
        self.assertRegex(
            code, r"Select-Object\s+-ExpandProperty\s+LocalPort\s+-Unique\b")
        self.assertRegex(code, r"if\s*\(\$ports\.Count\s+-ne\s+1\)\s*\{\s*throw\b")
        self.assertLess(code.index("$engines.Count -ne 1"), code.index(query))
        self.assertLess(
            code.index("$ports.Count -ne 1"),
            code.index("Microsoft.AnalysisServices.AdomdClient.AdomdConnection"))
        self.assertIn("Data Source=localhost:$($ports[0])", code)
        self.assertNotIn("Get-NetTCPConnection", code)


class TestStablePartitionFreshness(unittest.TestCase):
    def compare(self, before=None, after=None):
        return probe._compare_partition_freshness(
            before if before is not None else _observation(),
            after if after is not None else _observation(FRESH))

    def test_one_hundred_nanosecond_advancement_of_every_partition_is_fresh(self):
        result = self.compare()
        self.assertEqual(result["status"], "advanced")
        self.assertEqual(len(result["changed"]), 3)
        for key in ("unchanged", "missing", "added", "failed", "regressed",
                    "invalid_timestamps"):
            self.assertEqual(result[key], [])

    def test_one_unchanged_partition_prevents_all_partition_freshness(self):
        after = _observation(FRESH)
        after["partitions"][1]["refreshed_time"] = OLD
        result = self.compare(after=after)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(len(result["changed"]), 2)
        self.assertEqual(result["unchanged"], [{"table_id": "10", "partition_id": "101"}])

    def test_semantically_equal_fraction_and_timezone_spellings_are_not_advancement(self):
        result = self.compare(
            _observation("2026-10-28T10:00:00.0000000Z"),
            _observation("2026-10-28T11:00:00+01:00"))
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(len(result["unchanged"]), 3)
        self.assertEqual(result["changed"], [])

    def test_zero_dotnet_timestamp_requires_strict_advancement_too(self):
        zero = _observation("0001-01-01T00:00:00.0000000")
        self.assertEqual(self.compare(zero, copy.deepcopy(zero))["status"], "unchanged")
        result = self.compare(zero, _observation("2026-10-28T10:00:00.0000001"))
        self.assertEqual(result["status"], "advanced")
        self.assertEqual(len(result["changed"]), 3)

    def test_missing_invalid_or_clock_kind_ambiguous_timestamps_are_not_fresh(self):
        for stamp in (
                None, "", "not-a-date", "2026-02-30T10:00:00Z",
                "2026-10-28T10:00:00.12345678Z",
                "2026-10-28T10:00:00+25:00",
                "2026-10-28T10:00:00.1234568",
        ):
            with self.subTest(stamp=stamp):
                result = self.compare(after=_observation(stamp))
                self.assertEqual(result["status"], "invalid_timestamp")
                self.assertEqual(len(result["invalid_timestamps"]), 3)

    def test_regressed_timestamp_is_not_fresh_even_if_other_partitions_advance(self):
        after = _observation(FRESH)
        after["partitions"][0]["refreshed_time"] = "2026-10-27T10:00:00Z"
        result = self.compare(after=after)
        self.assertEqual(result["status"], "regressed")
        self.assertEqual(result["regressed"], [{"table_id": "11", "partition_id": "201"}])

    def test_failed_or_nonready_current_partition_blocks_freshness(self):
        for overrides in ({"state": 0}, {"state": 2}, {"state": 3},
                          {"has_error": True}):
            with self.subTest(overrides=overrides):
                after = _observation(FRESH)
                after["partitions"][0].update(overrides)
                result = self.compare(after=after)
                self.assertEqual(result["status"], "partition_failed")
                self.assertEqual(result["failed"], [
                    {"table_id": "11", "partition_id": "201"},
                ])

    def test_unprocessed_or_errored_baseline_can_recover_to_all_ready_fresh_partitions(self):
        for stamp, fresh, overrides in (
                ("0001-01-01T00:00:00.0000000", "2026-10-28T10:00:00.1234568",
                 {"state": 4}),
                ("1699-12-31T00:00:00.0000000", "2026-10-28T10:00:00.1234568",
                 {"state": 3}),
                (OLD, FRESH, {"state": 2, "has_error": True}),
        ):
            with self.subTest(overrides=overrides):
                before, after = _observation(stamp), _observation(fresh)
                for partition in before["partitions"]:
                    partition.update(overrides)
                result = self.compare(before, after)
                self.assertEqual(result["status"], "advanced")
                self.assertEqual(len(result["changed"]), 3)
                self.assertEqual(result["failed"], [])
                self.assertEqual(result["before"], before)
                self.assertEqual(len(result["baseline_failed"]), 3)

    def test_missing_added_or_replaced_id_cannot_be_hidden_by_equal_partition_counts(self):
        for mutation in ("missing", "added", "replaced"):
            with self.subTest(mutation=mutation):
                after = _observation(FRESH)
                if mutation == "missing":
                    after["partitions"].pop()
                elif mutation == "added":
                    added = dict(after["partitions"][0], partition="Added",
                                 partition_id="999")
                    after["partitions"].append(added)
                else:
                    after["partitions"][0]["partition_id"] = "999"
                result = self.compare(after=after)
                self.assertEqual(result["status"], "mismatch")
                self.assertTrue(result["missing"] or result["added"])
                if mutation == "replaced":
                    self.assertEqual(len(result["missing"]), 1)
                    self.assertEqual(len(result["added"]), 1)

    def test_same_id_with_renamed_partition_is_not_a_stable_identity(self):
        after = _observation(FRESH)
        after["partitions"][0]["partition"] = "Renamed"
        self.assertEqual(self.compare(after=after)["status"], "mismatch")

    def test_changed_owner_engine_or_port_cannot_mix_independent_refreshes(self):
        for overrides in ({"owner_pid": 4243}, {"engine_pid": 9002}, {"port": 50002}):
            with self.subTest(overrides=overrides):
                self.assertEqual(
                    self.compare(after=_observation(FRESH, **overrides))["status"],
                    "binding_changed")

    def test_query_failed_invalid_mismatch_and_unavailable_keep_distinct_statuses(self):
        for status in ("query_failed", "invalid", "mismatch", "unavailable", "no_m_tables"):
            with self.subTest(status=status):
                after = probe._partition_observation_result(status, "test_result")
                result = self.compare(after=after)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["after"], after)


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


class TestRefreshSnapshotRequestBudgets(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.completed = SimpleNamespace(
            returncode=0, stdout=json.dumps({"status": "clear"}), stderr="")
        self.enterContext(mock.patch.object(probe.desktop_window, "IS_WINDOWS", True))
        self.enterContext(mock.patch.object(
            accessibility, "powershell_preflight",
            return_value={"status": "ready", "executable": "powershell.exe"}))
        self.enterContext(mock.patch.object(
            probe.time, "monotonic", side_effect=self.clock.time))
        self.dialogs = self.enterContext(mock.patch.object(
            probe.desktop_window, "dialog_windows", return_value=[]))
        self.enterContext(mock.patch.object(
            probe.subprocess, "Popen",
            side_effect=AssertionError("Snapshot tests must not start processes")))
        self.run = self.enterContext(mock.patch.object(
            probe.subprocess, "run", return_value=self.completed))

    def test_read_only_snapshot_caps_default_and_large_budgets_at_thirty_seconds(self):
        for options in ({}, {"timeout": 300}, {"timeout": 120}, {"timeout": 30}):
            with self.subTest(options=options):
                self.run.reset_mock()
                self.assertEqual(probe._refresh_ui_state(101, 4242, **options), "clear")
                self.run.assert_called_once()
                self.assertEqual(self.run.call_args.kwargs["timeout"], 30.0)
        self.assertTrue(all(call.args == (4242,) for call in self.dialogs.call_args_list))

    def test_ui_provider_inventory_projects_only_process_identity_and_parent(self):
        self.assertEqual(probe._refresh_ui_state(101, 4242), "clear")
        code = _powershell_code(self.run.call_args.args[0][-1])
        query, = re.findall(r"Get-CimInstance\s+Win32_Process\b[^\r\n]*", code)
        projection, = re.findall(r"-Property\s+(.+?)\s+-ErrorAction\b", query)
        self.assertCountEqual(
            [name.strip() for name in projection.split(",")],
            ["ProcessId", "ParentProcessId"])
        self.assertIn(
            "$root.Current.ProcessId -ne [int]$request.owner_pid", code)
        self.assertIn("$providers.Add([int]$request.owner_pid)", code)
        self.assertIn("$providers.Contains([int]$e.Current.ProcessId)", code)
        self.assertNotIn(".Invoke(", code)
        request = json.loads(base64.b64decode(self.run.call_args.kwargs["input"]))
        self.assertEqual(request["hwnd"], 101)
        self.assertEqual(request["owner_pid"], 4242)
        self.assertIs(request["refresh_dialog"], False)

    def test_fractional_budget_is_shared_by_the_report_and_owned_dialog_snapshots(self):
        dialogs = [
            SimpleNamespace(hwnd=202, title="Refresh"),
            SimpleNamespace(hwnd=303, title="Other dialog"),
        ]

        def find_owned(owner):
            self.assertEqual(owner, 4242)
            self.clock.advance(0.1)
            return dialogs

        def read(*args, **kwargs):
            self.clock.advance(0.125)
            return self.completed

        self.dialogs.side_effect = find_owned
        self.run.side_effect = read
        self.assertEqual(probe._refresh_ui_state(101, 4242, timeout=0.5), "clear")
        self.assertEqual(self.run.call_count, 3)
        requests = [
            json.loads(base64.b64decode(call.kwargs["input"]))
            for call in self.run.call_args_list]
        self.assertEqual([request["hwnd"] for request in requests], [101, 202, 303])
        self.assertEqual([request["owner_pid"] for request in requests], [4242] * 3)
        self.assertEqual([request["refresh_dialog"] for request in requests],
                         [False, True, False])
        for call, remaining in zip(self.run.call_args_list, (0.4, 0.275, 0.15)):
            self.assertAlmostEqual(call.kwargs["timeout"], remaining)
            self.assertGreater(call.kwargs["timeout"], 0)
        self.assertAlmostEqual(self.clock.now, 0.475)
        self.assertLess(self.clock.now, 0.5)

    def test_expired_fractional_snapshot_does_not_restart_the_budget_for_each_dialog(self):
        self.dialogs.return_value = [
            SimpleNamespace(hwnd=202, title="Refresh"),
            SimpleNamespace(hwnd=303, title="Other dialog"),
        ]

        def timeout(command, **kwargs):
            self.clock.advance(kwargs["timeout"])
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        self.run.side_effect = timeout
        with self.assertRaises(probe.PowerShellFailure) as raised:
            probe._refresh_ui_state(101, 4242, timeout=0.25)
        self.assertEqual(raised.exception.observation, {
            "status": "timed_out", "cause": "powershell_timeout",
            "note": "powershell_timeout", "retryable": False})
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.kwargs["timeout"], 0.25)
        self.assertEqual(self.clock.now, 0.25)


class TestRefreshPartitionCompletion(unittest.TestCase):
    def refresh(self, *, before=None, after=None, states=None, data=None,
                timeout=1, poll=0.1, observer_time=0, verification_time=0):
        before = _observation() if before is None else before
        after = _observation(FRESH) if after is None else after
        data = _complete_counts() if data is None else data
        state_iter = iter(states or ["clear"])
        last_state = (states or ["clear"])[-1]
        clock = _Clock()
        events = []
        observations = []

        def observe(*args, **kwargs):
            events.append("observe")
            clock.advance(observer_time)
            result = before if not observations else after
            observations.append(result)
            return copy.deepcopy(result)

        def counts(*args, **kwargs):
            events.append("counts")
            clock.advance(verification_time)
            return copy.deepcopy(data)

        def invoke(*args, **kwargs):
            events.append("refresh")
            return 1

        with mock.patch.object(probe, "observe_desktop_partitions",
                               side_effect=observe) as observer, \
                mock.patch.object(probe, "verify_desktop_data",
                                  side_effect=counts) as verifier, \
                mock.patch.object(probe, "_refresh_ui_state",
                                  side_effect=lambda *args, **kwargs:
                                  next(state_iter, last_state)), \
                mock.patch.object(probe, "_invoke_schema_refresh",
                                  side_effect=invoke) as request, \
                mock.patch.object(probe, "_click_button", return_value=0), \
                mock.patch.object(probe, "_invoke_button", return_value=0), \
                mock.patch.object(probe.time, "time", side_effect=clock.time), \
                mock.patch.object(probe.time, "monotonic", side_effect=clock.time), \
                mock.patch.object(probe.time, "sleep", side_effect=clock.sleep):
            result = probe.refresh_report(
                101, "P.pbip", 4242, timeout=timeout, poll=poll)
        return result, events, observer, verifier, request, clock

    def test_fast_clear_ui_uses_pre_request_baseline_and_strict_partition_freshness(self):
        result, events, observer, verifier, request, clock = self.refresh()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["completion_basis"], "partition_freshness")
        self.assertEqual(result["partition_freshness"]["status"], "advanced")
        self.assertFalse(result["ui_state"]["observed_progress"])
        self.assertEqual(result["data_load"], _complete_counts())
        self.assertEqual(events[:2], ["observe", "refresh"])
        self.assertEqual(observer.call_count, 2)
        verifier.assert_called_once()
        request.assert_called_once()
        self.assertLess(clock.now, 1)

    def test_existing_completed_ui_is_not_required_to_flicker_if_every_partition_advances(self):
        result, *_ = self.refresh(states=["completed"])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["completion_basis"], "partition_freshness")
        self.assertFalse(result["ui_state"]["observed_progress"])

    def test_metadata_only_unprocessed_baseline_verifies_after_first_real_load(self):
        before = _observation("0001-01-01T00:00:00.0000000")
        for partition in before["partitions"]:
            partition["state"] = 4
        after = _observation("2026-10-28T10:00:00.1234568")
        result, *_ = self.refresh(before=before, after=after)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["completion_basis"], "partition_freshness")
        self.assertEqual(result["partition_freshness"]["failed"], [])
        self.assertEqual(result["data_load"], _complete_counts())

    def test_state3_native_first_load_verifies_without_any_observed_ui_progress(self):
        before = _state3_first_load_baseline()
        after = copy.deepcopy(before)
        after["partitions"][0].update(
            state=1, refreshed_time="2026-10-28T11:00:00.1234568")
        data = {
            "status": "verified", "tables_checked": 1, "tables_nonempty": 1,
            "tables_empty": 0, "tables_failed": 0, "total_rows": 3,
        }
        result, events, observer, verifier, request, clock = self.refresh(
            before=before, after=after, data=data,
            timeout=300, verification_time=74.5)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["completion_basis"], "partition_freshness")
        self.assertFalse(result["ui_state"]["observed_progress"])
        self.assertEqual(result["data_load"], data)
        freshness = result["partition_freshness"]
        self.assertEqual(freshness["status"], "advanced")
        self.assertEqual(freshness["baseline_failed"],
                         [{"table_id": "10", "partition_id": "101"}])
        self.assertEqual(freshness["changed"],
                         [{"table_id": "10", "partition_id": "101"}])
        self.assertEqual(freshness["failed"], [])
        self.assertEqual(freshness["before"], before)
        self.assertEqual(freshness["after"], after)
        self.assertEqual(events, ["observe", "refresh", "counts", "observe"])
        self.assertEqual(observer.call_count, 2)
        verifier.assert_called_once()
        request.assert_called_once()
        self.assertAlmostEqual(clock.now, 74.6)
        self.assertLess(result["waited_s"], 300)

    def test_state3_recovery_still_rejects_stale_failed_missing_or_replaced_terminal_evidence(self):
        before = _state3_first_load_baseline()
        data = {
            "status": "verified", "tables_checked": 1, "tables_nonempty": 1,
            "tables_empty": 0, "tables_failed": 0, "total_rows": 3,
        }
        for mutation, expected in (
                ("unchanged_time", "unchanged"), ("state3", "partition_failed"),
                ("has_error", "partition_failed"), ("missing", "mismatch"),
                ("replaced_id", "mismatch"),
        ):
            with self.subTest(mutation=mutation):
                after = copy.deepcopy(before)
                partition = after["partitions"][0]
                partition.update(state=1, refreshed_time="2026-10-28T11:00:00.1234568")
                if mutation == "unchanged_time":
                    partition["refreshed_time"] = before["partitions"][0]["refreshed_time"]
                elif mutation == "state3":
                    partition["state"] = 3
                elif mutation == "has_error":
                    partition["has_error"] = True
                elif mutation == "missing":
                    after["partitions"].clear()
                else:
                    partition["partition_id"] = "102"
                result, *_ = self.refresh(before=before, after=after, data=data)
                self.assertNotEqual(result["status"], "verified")
                self.assertEqual(result["completion_basis"], "none")
                self.assertFalse(result["ui_state"]["observed_progress"])
                self.assertEqual(result["partition_freshness"]["status"], expected)
                self.assertEqual(result["data_load"], data)

    def test_unchanged_existing_rows_and_completed_ui_cannot_verify_a_new_request(self):
        for states in (["clear"], ["completed"]):
            with self.subTest(states=states):
                result, *_ = self.refresh(after=_observation(), states=states)
                self.assertNotEqual(result["status"], "verified")
                self.assertEqual(result["partition_freshness"]["status"], "unchanged")
                self.assertEqual(result["completion_basis"], "none")
                self.assertEqual(result["data_load"], _complete_counts())

    def test_ui_error_pending_busy_or_unavailable_overrides_fresh_partition_evidence(self):
        for state in ("refresh_error", "pending_changes", "busy", "unavailable"):
            with self.subTest(state=state):
                result, events, observer, *_ = self.refresh(
                    states=["clear", "clear", state])
                self.assertEqual(
                    result["status"],
                    "timed_out" if state in {"pending_changes", "busy"} else state)
                self.assertEqual(result["ui_state"]["after"], state)
                self.assertNotEqual(result["status"], "verified")
                self.assertEqual(result["data_load"], _complete_counts())
                # Both the baseline and a genuinely advanced current observation
                # were available before the adverse post-verification UI state.
                self.assertEqual(observer.call_count, 2)

    def test_initial_error_does_not_observe_or_request_refresh(self):
        result, events, observer, verifier, request, clock = self.refresh(
            states=["refresh_error"])
        self.assertEqual(result["status"], "refresh_error")
        observer.assert_not_called()
        verifier.assert_not_called()
        request.assert_not_called()
        self.assertEqual(events, [])

    def test_incomplete_or_failed_counts_block_even_all_fresh_partitions(self):
        for overrides in (
                {"tables_checked": 3}, {"tables_failed": 1},
                {"status": "query_failed"},
                {"status": "empty", "tables_checked": 0,
                 "tables_nonempty": 0, "tables_empty": 0, "total_rows": 0},
        ):
            with self.subTest(overrides=overrides):
                data = dict(_complete_counts(), **overrides)
                result, *_ = self.refresh(data=data)
                self.assertNotEqual(result["status"], "verified")
                self.assertEqual(result["partition_freshness"]["status"], "advanced")
                self.assertEqual(result["data_load"], data)

    def test_complete_counts_for_fewer_tables_cannot_certify_fresh_partition_coverage(self):
        data = {
            "status": "verified", "tables_checked": 1, "tables_nonempty": 1,
            "tables_empty": 0, "tables_failed": 0, "total_rows": 9999,
        }
        result, *_ = self.refresh(data=data)
        self.assertNotEqual(result["status"], "verified")
        self.assertEqual(result["partition_freshness"]["status"], "advanced")
        self.assertEqual(result["data_load"], data)

    def test_recovering_baseline_does_not_allow_missing_replaced_or_failed_current_partitions(self):
        before = _observation("0001-01-01T00:00:00.0000000")
        for partition in before["partitions"]:
            partition["state"] = 4
        for mutation, expected in (
                ("missing", "mismatch"), ("replaced", "mismatch"),
                ("failed", "partition_failed"),
        ):
            with self.subTest(mutation=mutation):
                after = _observation("2026-10-28T10:00:00.1234568")
                if mutation == "missing":
                    after["partitions"].pop()
                elif mutation == "replaced":
                    after["partitions"][0]["partition_id"] = "999"
                else:
                    after["partitions"][0]["has_error"] = True
                result, *_ = self.refresh(before=before, after=after)
                self.assertNotEqual(result["status"], "verified")
                self.assertEqual(result["partition_freshness"]["status"], expected)

    def test_unavailable_observer_preserves_the_existing_ui_lifecycle_path(self):
        unavailable = probe._partition_observation_result(
            "unavailable", "test_observer_unavailable")
        result, *_ = self.refresh(
            before=unavailable, after=unavailable,
            states=["clear", "busy", "clear", "clear", "clear"])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["completion_basis"], "ui_lifecycle")
        self.assertTrue(result["ui_state"]["observed_progress"])
        self.assertEqual(result["partition_freshness"]["status"], "unavailable")

    def test_counts_alone_do_not_succeed_when_observer_is_unavailable(self):
        unavailable = probe._partition_observation_result(
            "unavailable", "test_observer_unavailable")
        result, *_ = self.refresh(before=unavailable, after=unavailable)
        self.assertNotEqual(result["status"], "verified")
        self.assertFalse(result["ui_state"]["observed_progress"])
        self.assertEqual(result["data_load"], _complete_counts())

    def test_explicit_metadata_failures_cannot_fall_back_to_successful_ui_lifecycle(self):
        failed_partition = _observation(FRESH)
        failed_partition["partitions"][0]["has_error"] = True
        cases = [
            (probe._partition_observation_result("query_failed", "metadata_query_failed"),
             "query_failed"),
            (probe._partition_observation_result("invalid", "observer_result_invalid"),
             "invalid"),
            (probe._partition_observation_result("mismatch", "expected_partition_mismatch"),
             "mismatch"),
            (failed_partition, "partition_failed"),
        ]
        for after, expected in cases:
            with self.subTest(status=expected):
                result, *_ = self.refresh(
                    after=after, states=["clear", "busy", "clear", "clear", "clear"])
                self.assertNotEqual(result["status"], "verified")
                self.assertTrue(result["ui_state"]["observed_progress"])
                self.assertEqual(result["partition_freshness"]["status"], expected)

    def test_poll_sleep_is_bounded_by_remaining_refresh_deadline(self):
        result, events, observer, verifier, request, clock = self.refresh(
            after=_observation(), timeout=0.25, poll=10)
        self.assertNotEqual(result["status"], "verified")
        self.assertLessEqual(clock.now, 0.25)
        self.assertTrue(all(0 <= seconds <= 0.25 for seconds in clock.sleeps))

    def test_slow_count_check_cannot_verify_with_fresh_evidence_after_deadline(self):
        result, events, observer, verifier, request, clock = self.refresh(
            timeout=1, verification_time=1.1)
        self.assertNotEqual(result["status"], "verified")
        verifier.assert_called_once()
        self.assertGreater(clock.now, 1)

    def test_baseline_that_exhausts_deadline_cannot_launch_a_late_refresh_request(self):
        result, events, observer, verifier, request, clock = self.refresh(
            timeout=1, observer_time=1.1)
        self.assertNotEqual(result["status"], "verified")
        request.assert_not_called()
        verifier.assert_not_called()
        self.assertEqual(events, ["observe"])


class TestRefreshInitialReadiness(unittest.TestCase):
    """Startup UI retries must precede, not replace, refresh verification."""

    def run_refresh(self, initial_states, *, before=None, after=None, data=None,
                    post_states=("clear",), timeout=1.0, poll=0.05,
                    baseline_time=0, ready_at_deadline=False):
        run = SimpleNamespace(
            clock=_Clock(), deadline=timeout, events=[], requested=False,
            last_ui=None,
            before=copy.deepcopy(_observation() if before is None else before),
            after=copy.deepcopy(_observation(FRESH) if after is None else after),
            data=copy.deepcopy(_complete_counts() if data is None else data),
        )
        initial = iter(initial_states)
        post = iter(post_states)

        def record(kind, **fields):
            event = dict(kind=kind, at=run.clock.now,
                         requested=run.requested, **fields)
            run.events.append(event)
            return event

        def budgeted(kind, kwargs, **fields):
            seconds = kwargs["timeout"]
            self.assertGreater(seconds, 0)
            self.assertLessEqual(seconds, run.deadline - run.clock.now + 1e-12)
            return record(kind, timeout=seconds, **fields)

        def read_ui(hwnd, owner, **kwargs):
            self.assertEqual((hwnd, owner), (101, 4242))
            state = (next(post, post_states[-1]) if run.requested
                     else next(initial, initial_states[-1]))
            event = budgeted("ui", kwargs, state=state)
            elapsed = (event["timeout"] if ready_at_deadline
                       and not run.requested and state in {"clear", "completed"}
                       else min(0.01, event["timeout"]))
            run.clock.advance(elapsed)
            run.last_ui = state
            return state

        def observe(pbip, owner, **kwargs):
            self.assertEqual((pbip, owner), ("P.pbip", 4242))
            self.assertIn(run.last_ui, {"clear", "completed"})
            kind = "current" if run.requested else "baseline"
            budgeted(kind, kwargs)
            if not run.requested:
                run.clock.advance(baseline_time)
            return copy.deepcopy(run.after if run.requested else run.before)

        def schema(hwnd, **kwargs):
            self.assertEqual(hwnd, 101)
            self.assertFalse(run.requested)
            self.assertIn(run.last_ui, {"clear", "completed"})
            self.assertTrue(any(event["kind"] == "baseline" for event in run.events))
            budgeted("schema", kwargs)
            run.requested = True
            return 1

        def counts(pbip, owner, **kwargs):
            self.assertEqual((pbip, owner), ("P.pbip", 4242))
            self.assertTrue(run.requested)
            budgeted("counts", kwargs)
            return copy.deepcopy(run.data)

        def control(kind, hwnd, names, **kwargs):
            self.assertEqual(hwnd, 101)
            self.assertTrue(run.requested, "No controls may be invoked during readiness")
            budgeted(kind, kwargs, names=tuple(names))
            return 0

        def sleep(seconds):
            self.assertGreaterEqual(seconds, 0)
            self.assertLessEqual(seconds, run.deadline - run.clock.now + 1e-12)
            record("sleep", seconds=seconds)
            run.clock.sleep(seconds)

        with ExitStack() as patches:
            for name in ("run", "Popen"):
                patches.enter_context(mock.patch.object(
                    probe.subprocess, name,
                    side_effect=AssertionError("Readiness tests must not start processes")))
            patches.enter_context(mock.patch.object(
                probe.time, "monotonic", side_effect=run.clock.time))
            patches.enter_context(mock.patch.object(
                probe.time, "time", side_effect=run.clock.time))
            patches.enter_context(mock.patch.object(
                probe.time, "sleep", side_effect=sleep))
            run.reader = patches.enter_context(mock.patch.object(
                probe, "_refresh_ui_state", side_effect=read_ui))
            run.observer = patches.enter_context(mock.patch.object(
                probe, "observe_desktop_partitions", side_effect=observe))
            run.schema = patches.enter_context(mock.patch.object(
                probe, "_invoke_schema_refresh", side_effect=schema))
            run.verifier = patches.enter_context(mock.patch.object(
                probe, "verify_desktop_data", side_effect=counts))
            run.click = patches.enter_context(mock.patch.object(
                probe, "_click_button",
                side_effect=lambda *args, **kwargs: control("click", *args, **kwargs)))
            run.invoke = patches.enter_context(mock.patch.object(
                probe, "_invoke_button",
                side_effect=lambda *args, **kwargs: control("invoke", *args, **kwargs)))
            run.result = probe.refresh_report(
                101, "P.pbip", 4242, timeout=timeout, poll=poll)
        return run

    def assert_no_request(self, run, status):
        self.assertEqual(run.result["status"], status)
        self.assertEqual(run.result["mode"], "none")
        self.assertEqual(run.result["clicks"], 0)
        self.assertEqual(run.result["completion_basis"], "none")
        self.assertEqual(run.result["partition_freshness"]["status"], "not_observed")
        self.assertEqual(run.result["data_load"], probe._data_load_result("unavailable"))
        self.assertNotIn("before", run.result["ui_state"])
        self.assertNotIn("after", run.result["ui_state"])
        for action in (run.observer, run.schema, run.verifier, run.click, run.invoke):
            action.assert_not_called()

    def assert_safe_baseline_before_request(self, run):
        baseline_indices = [index for index, event in enumerate(run.events)
                            if event["kind"] == "baseline"]
        request_indices = [index for index, event in enumerate(run.events)
                           if event["kind"] == "schema"]
        self.assertEqual(len(baseline_indices), 1)
        self.assertEqual(len(request_indices), 1)
        baseline_index, = baseline_indices
        request_index, = request_indices
        self.assertLess(baseline_index, request_index)
        pre_baseline_ui = [
            event for event in run.events[:baseline_index] if event["kind"] == "ui"]
        self.assertTrue(pre_baseline_ui)
        self.assertIn(pre_baseline_ui[-1]["state"], {"clear", "completed"})
        self.assertFalse(run.events[baseline_index]["requested"])
        self.assertFalse(run.events[request_index]["requested"])
        self.assertEqual(run.result["partition_freshness"]["before"], run.before)

    def test_transient_unavailable_recovers_fast_refresh_from_safe_pre_request_baseline(self):
        before = _state3_first_load_baseline()
        after = copy.deepcopy(before)
        after["partitions"][0].update(
            state=1, refreshed_time="2026-10-28T11:00:00.1234568")
        data = {
            "status": "verified", "tables_checked": 1, "tables_nonempty": 1,
            "tables_empty": 0, "tables_failed": 0, "total_rows": 3,
        }
        run = self.run_refresh(
            ["unavailable", "clear"], before=before, after=after, data=data)
        self.assertEqual(run.result["status"], "verified")
        self.assertEqual(run.result["completion_basis"], "partition_freshness")
        self.assertEqual(run.result["ui_state"]["initial"], "unavailable")
        self.assertEqual(run.result["ui_state"]["readiness"], "clear")
        self.assertEqual(run.result["ui_state"]["readiness_checks"], 2)
        self.assertFalse(run.result["ui_state"]["observed_progress"])
        self.assertEqual(run.result["data_load"], data)
        freshness = run.result["partition_freshness"]
        self.assertEqual(freshness["status"], "advanced")
        self.assertEqual(freshness["failed"], [])
        self.assertEqual(freshness["baseline_failed"],
                         [{"table_id": "10", "partition_id": "101"}])
        self.assertEqual(freshness["after"], after)
        self.assert_safe_baseline_before_request(run)
        self.assertEqual(run.observer.call_count, 2)
        run.schema.assert_called_once()
        run.verifier.assert_called_once()
        self.assertLess(run.clock.now, run.deadline)

    def test_busy_pending_and_mixed_readiness_wait_for_safe_ui_before_any_action(self):
        for states in (
                ["busy", "clear"], ["pending_changes", "clear"],
                ["unavailable", "busy", "pending_changes", "clear"],
        ):
            with self.subTest(states=states):
                run = self.run_refresh(states)
                self.assertEqual(run.result["status"], "verified")
                self.assertEqual(run.result["completion_basis"], "partition_freshness")
                self.assertEqual(run.result["ui_state"]["initial"], states[0])
                self.assertEqual(run.result["ui_state"]["readiness"], "clear")
                self.assertEqual(run.result["ui_state"]["readiness_checks"], len(states))
                self.assertFalse(run.result["ui_state"]["observed_progress"])
                self.assertEqual(
                    [event["state"] for event in run.events
                     if event["kind"] == "ui" and not event["requested"]], states)
                self.assert_safe_baseline_before_request(run)
                run.schema.assert_called_once()

    def test_pre_request_busy_is_not_progress_when_requested_partitions_stay_unchanged(self):
        run = self.run_refresh(["busy", "clear"], after=_observation())
        self.assertEqual(run.result["status"], "timed_out")
        self.assertEqual(run.result["completion_basis"], "none")
        self.assertFalse(run.result["ui_state"]["observed_progress"])
        self.assertEqual(run.result["partition_freshness"]["status"], "unchanged")
        self.assertEqual(run.result["data_load"], _complete_counts())
        self.assert_safe_baseline_before_request(run)
        run.schema.assert_called_once()
        self.assertGreater(run.verifier.call_count, 0)

    def test_only_real_post_request_lifecycle_verifies_when_partition_observer_is_unavailable(self):
        unavailable = probe._partition_observation_result(
            "unavailable", "test_observer_unavailable")
        for states, expected, basis, progress in (
                (["clear"], "timed_out", "none", False),
                (["busy", "clear", "clear"], "verified", "ui_lifecycle", True),
                (["clear", "completed"], "verified", "ui_lifecycle", False),
        ):
            with self.subTest(post_states=states):
                run = self.run_refresh(
                    ["unavailable", "clear"], before=unavailable, after=unavailable,
                    post_states=states)
                self.assertEqual(run.result["status"], expected)
                self.assertEqual(run.result["completion_basis"], basis)
                self.assertEqual(run.result["ui_state"]["observed_progress"], progress)
                self.assertEqual(run.result["partition_freshness"]["status"], "unavailable")
                self.assertEqual(run.result["data_load"], _complete_counts())
                self.assert_safe_baseline_before_request(run)

    def test_recovered_stale_completed_readiness_does_not_certify_the_new_request(self):
        unavailable = probe._partition_observation_result(
            "unavailable", "test_observer_unavailable")
        for initial in ("unavailable", "busy", "pending_changes"):
            with self.subTest(initial=initial):
                run = self.run_refresh(
                    [initial, "completed"], before=unavailable, after=unavailable,
                    post_states=["completed"])
                self.assertEqual(run.result["status"], "timed_out")
                self.assertEqual(run.result["ui_state"]["initial"], initial)
                self.assertEqual(run.result["ui_state"]["readiness"], "completed")
                self.assertEqual(run.result["ui_state"]["readiness_checks"], 2)
                self.assertFalse(run.result["ui_state"]["observed_progress"])
                self.assertEqual(run.result["completion_basis"], "none")
                self.assertEqual(run.result["data_load"], _complete_counts())
                self.assert_safe_baseline_before_request(run)

    def test_readiness_error_aborts_immediately_without_baseline_or_any_click(self):
        for preceding in ([], ["unavailable"], ["busy", "pending_changes"]):
            with self.subTest(preceding=preceding):
                states = preceding + ["refresh_error", "clear"]
                run = self.run_refresh(states)
                self.assert_no_request(run, "refresh_error")
                self.assertEqual(run.result["ui_state"]["initial"], states[0])
                self.assertEqual(run.result["ui_state"]["readiness"], "refresh_error")
                self.assertEqual(
                    run.result["ui_state"]["readiness_checks"], len(preceding) + 1)
                self.assertEqual(run.reader.call_count, len(preceding) + 1)
                self.assertLess(run.clock.now, run.deadline)

    def test_fractional_deadline_without_safe_ui_blocks_all_refresh_actions(self):
        for state in ("unavailable", "busy", "pending_changes"):
            for poll in (0.05, 10.0):
                with self.subTest(state=state, poll=poll):
                    run = self.run_refresh([state], timeout=0.25, poll=poll)
                    self.assert_no_request(run, "timed_out")
                    self.assertEqual(run.result["note"], "refresh_timeout")
                    self.assertEqual(run.result["ui_state"]["initial"], state)
                    self.assertEqual(run.result["ui_state"]["readiness"], state)
                    self.assertFalse(run.result["ui_state"]["observed_progress"])
                    self.assertAlmostEqual(run.clock.now, 0.25)
                    self.assertTrue(all(0 <= seconds <= 0.25
                                        for seconds in run.clock.sleeps))

    def test_readiness_retry_sleeps_cap_at_one_second_and_keep_fractional_remainder(self):
        run = self.run_refresh(["unavailable"], timeout=2.25, poll=10.0)
        self.assert_no_request(run, "timed_out")
        self.assertGreaterEqual(len(run.clock.sleeps), 2)
        self.assertEqual(run.clock.sleeps[0], 1.0)
        self.assertTrue(all(0 < seconds <= 1.0 for seconds in run.clock.sleeps))
        self.assertGreater(run.clock.sleeps[-1], 0)
        self.assertLess(run.clock.sleeps[-1], 1.0)
        self.assertAlmostEqual(run.clock.now, 2.25)

    def test_safe_ui_at_deadline_cannot_create_a_late_baseline_or_click(self):
        for states in (["clear"], ["unavailable", "clear"], ["busy", "completed"]):
            with self.subTest(states=states):
                run = self.run_refresh(
                    states, timeout=0.25, ready_at_deadline=True)
                self.assert_no_request(run, "timed_out")
                self.assertEqual(run.result["ui_state"]["readiness"], states[-1])
                self.assertEqual(run.result["ui_state"]["readiness_checks"], len(states))
                self.assertAlmostEqual(run.clock.now, 0.25)

    def test_slow_safe_baseline_after_readiness_retry_cannot_invoke_a_late_refresh(self):
        run = self.run_refresh(
            ["unavailable", "clear"], timeout=0.5, baseline_time=0.6)
        self.assertEqual(run.result["status"], "timed_out")
        self.assertEqual(run.result["clicks"], 0)
        self.assertEqual(run.result["completion_basis"], "none")
        self.assertEqual(run.result["data_load"], probe._data_load_result("unavailable"))
        self.assertEqual(run.result["ui_state"]["readiness"], "clear")
        run.observer.assert_called_once()
        for action in (run.schema, run.verifier, run.click, run.invoke):
            action.assert_not_called()
        self.assertGreater(run.clock.now, run.deadline)
        self.assertEqual(
            [event["kind"] for event in run.events if event["kind"] != "sleep"],
            ["ui", "ui", "baseline"])

    def test_readiness_baseline_requests_and_checks_share_one_fractional_budget(self):
        run = self.run_refresh(
            ["unavailable", "busy", "pending_changes", "clear"],
            timeout=0.95, baseline_time=0.125)
        self.assertEqual(run.result["status"], "verified")
        self.assert_safe_baseline_before_request(run)
        calls = [event for event in run.events if "timeout" in event]
        self.assertEqual(calls[0]["timeout"], 0.95)
        for event in calls:
            self.assertAlmostEqual(event["at"] + event["timeout"], run.deadline)
        for kind in ("baseline", "schema", "counts", "current"):
            event, = [event for event in calls if event["kind"] == kind]
            self.assertGreater(event["at"], 0)
            self.assertLess(event["timeout"], 0.95)
        self.assertLess(run.clock.now, run.deadline)

    def test_recovered_readiness_does_not_bypass_adverse_post_refresh_ui(self):
        for state in ("refresh_error", "pending_changes", "busy", "unavailable"):
            with self.subTest(state=state):
                run = self.run_refresh(
                    ["unavailable", "clear"], post_states=["clear", state])
                self.assertEqual(
                    run.result["status"],
                    "timed_out" if state in {"pending_changes", "busy"} else state)
                self.assertEqual(run.result["completion_basis"], "none")
                self.assertEqual(run.result["ui_state"]["after"], state)
                self.assertEqual(run.result["data_load"], _complete_counts())
                self.assert_safe_baseline_before_request(run)
                # The first current observation was genuinely fresh; a later
                # adverse UI state must still revoke its eligibility.
                self.assertEqual(run.observer.call_count, 2)
                run.schema.assert_called_once()

    def test_missing_pre_request_observation_is_not_replaced_by_a_late_baseline(self):
        unavailable = probe._partition_observation_result(
            "unavailable", "test_observer_unavailable")
        run = self.run_refresh(
            ["unavailable", "clear"], before=unavailable, after=_observation(FRESH))
        self.assertEqual(run.result["status"], "timed_out")
        self.assertEqual(run.result["completion_basis"], "none")
        self.assertFalse(run.result["ui_state"]["observed_progress"])
        self.assertEqual(run.result["partition_freshness"]["status"], "unavailable")
        self.assertEqual(run.result["partition_freshness"]["after"], _observation(FRESH))
        self.assertEqual(run.result["data_load"], _complete_counts())
        self.assert_safe_baseline_before_request(run)
        self.assertGreater(run.observer.call_count, 1)
