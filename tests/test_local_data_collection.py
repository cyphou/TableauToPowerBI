"""Data files kept beside a Tableau workbook are collected into Data/."""

import os
import tempfile
import unittest
import zipfile

import migrate

NAME = "Report"


def _write(path, text="x", mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding=None if "b" in mode else "utf-8") as handle:
        handle.write(text)
    return path


def _project(root, refs, data_folder=r"C:\\missing\\folder"):
    project = os.path.join(root, "out", NAME)
    definition = os.path.join(project, f"{NAME}.SemanticModel", "definition")
    _write(os.path.join(definition, "expressions.tmdl"),
           f'expression DataFolder = "{data_folder}" meta [IsParameterQuery=true]\n')
    body = "table T\n\tpartition T = m\n\t\tsource =\n" + "".join(
        f'\t\t\t\tS{i} = File.Contents(DataFolder & "\\{ref}"),\n'
        for i, ref in enumerate(refs))
    _write(os.path.join(definition, "tables", "T.tmdl"), body)
    _write(os.path.join(project, f"{NAME}.pbip"), "{}")
    return project


def _data_folder(project):
    _, folder = migrate._read_data_folder(os.path.join(
        project, f"{NAME}.SemanticModel", "definition", "expressions.tmdl"))
    return folder


class TestLocalDataCollection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.src_dir = os.path.join(self.root, "sources", "team")
        self.workbook = _write(os.path.join(self.src_dir, f"{NAME}.twb"), "<workbook/>")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, refs, **kwargs):
        project = _project(self.root, refs, **kwargs)
        stats = migrate._collect_local_data_files(self.workbook, project, NAME)
        return project, stats

    def test_file_in_same_folder_is_copied_and_datafolder_points_to_data(self):
        _write(os.path.join(self.src_dir, "orders.csv"), "a,b\n1,2\n")
        project, stats = self._run(["orders.csv"])
        data = os.path.join(project, "Data")
        self.assertEqual(stats["copied"], 1)
        self.assertTrue(os.path.isfile(os.path.join(data, "orders.csv")))
        self.assertEqual(os.path.normcase(_data_folder(project)),
                         os.path.normcase(os.path.abspath(data)))

    def test_file_in_subfolder_is_found(self):
        _write(os.path.join(self.src_dir, "inputs", "2025", "sales.xlsx"), "xlsx")
        project, stats = self._run(["sales.xlsx"])
        self.assertEqual(stats["copied"], 1)
        self.assertTrue(os.path.isfile(os.path.join(project, "Data", "sales.xlsx")))

    def test_file_in_parent_folder_is_found(self):
        _write(os.path.join(self.root, "sources", "shared.geojson"), "{}")
        _, stats = self._run(["shared.geojson"])
        self.assertEqual(stats["copied"], 1)

    def test_filename_match_is_case_insensitive(self):
        _write(os.path.join(self.src_dir, "ORDERS.CSV"), "a\n")
        project, stats = self._run(["orders.csv"])
        self.assertEqual(stats["copied"], 1)
        self.assertTrue(os.path.isfile(os.path.join(project, "Data", "orders.csv")))

    def test_nearest_folder_wins_over_parent(self):
        _write(os.path.join(self.src_dir, "orders.csv"), "near\n")
        _write(os.path.join(self.root, "sources", "orders.csv"), "far\n")
        project, stats = self._run(["orders.csv"])
        self.assertEqual(stats["copied"], 1)
        with open(os.path.join(project, "Data", "orders.csv"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "near\n")

    def test_identical_duplicates_are_copied(self):
        _write(os.path.join(self.src_dir, "a", "orders.csv"), "same\n")
        _write(os.path.join(self.src_dir, "b", "orders.csv"), "same\n")
        _, stats = self._run(["orders.csv"])
        self.assertEqual(stats["copied"], 1)
        self.assertEqual(stats["ambiguous"], 0)

    def test_conflicting_duplicates_are_not_guessed(self):
        _write(os.path.join(self.src_dir, "a", "orders.csv"), "one\n")
        _write(os.path.join(self.src_dir, "b", "orders.csv"), "two\n")
        project, stats = self._run(["orders.csv"])
        self.assertEqual(stats["ambiguous"], 1)
        self.assertFalse(os.path.exists(os.path.join(project, "Data", "orders.csv")))

    def test_file_not_on_disk_is_reported(self):
        project, stats = self._run(["absent.csv"])
        self.assertEqual(stats["not_found"], 1)
        self.assertEqual(_data_folder(project), "C:\\missing\\folder")

    def test_generated_output_is_never_searched(self):
        _write(os.path.join(self.src_dir, "old_run", "Old.pbip"), "{}")
        _write(os.path.join(self.src_dir, "old_run", "Data", "orders.csv"), "stale\n")
        _, stats = self._run(["orders.csv"])
        self.assertEqual(stats["not_found"], 1)

    def test_embedded_twbx_file_already_in_data_is_kept(self):
        project = _project(self.root, ["embedded.csv"])
        _write(os.path.join(project, "Data", "embedded.csv"), "packaged\n")
        _write(os.path.join(self.src_dir, "embedded.csv"), "loose\n")
        stats = migrate._collect_local_data_files(self.workbook, project, NAME)
        self.assertEqual(stats["already_present"], 1)
        self.assertEqual(stats["copied"], 0)
        with open(os.path.join(project, "Data", "embedded.csv"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "packaged\n")

    def test_twbx_embedded_plus_linked_external_file(self):
        twbx = os.path.join(self.src_dir, f"{NAME}.twbx")
        with zipfile.ZipFile(twbx, "w") as z:
            z.writestr(f"{NAME}.twb", "<workbook/>")
            z.writestr("Data/Extracts/embedded.csv", "packaged\n")
        _write(os.path.join(self.src_dir, "linked.geojson"), "{}")
        project = _project(self.root, ["embedded.csv", "linked.geojson"])
        migrate._process_twbx_post_generation(twbx, project, NAME)
        stats = migrate._collect_local_data_files(twbx, project, NAME)
        data = os.path.join(project, "Data")
        self.assertTrue(os.path.isfile(os.path.join(data, "embedded.csv")))
        self.assertTrue(os.path.isfile(os.path.join(data, "linked.geojson")))
        self.assertEqual(stats["copied"], 1)
        self.assertEqual(os.path.normcase(_data_folder(project)),
                         os.path.normcase(os.path.abspath(data)))

    def test_existing_author_folder_files_are_consolidated(self):
        author = os.path.join(self.root, "author_share")
        _write(os.path.join(author, "orders.csv"), "a\n")
        _write(os.path.join(self.src_dir, "regions.csv"), "b\n")
        project, stats = self._run(["orders.csv", "regions.csv"],
                                   data_folder=author.replace("\\", "\\\\"))
        data = os.path.join(project, "Data")
        self.assertEqual(stats["copied"], 2)
        self.assertEqual(os.path.normcase(_data_folder(project)),
                         os.path.normcase(os.path.abspath(data)))

    def test_no_references_changes_nothing(self):
        project, stats = self._run([])
        self.assertEqual(stats["referenced"], 0)
        self.assertEqual(_data_folder(project), "C:\\missing\\folder")

    def test_batch_root_is_searched_last(self):
        _write(os.path.join(self.root, "shared_data", "deep", "ref.csv"), "r\n")
        project = _project(self.root, ["ref.csv"])
        stats = migrate._collect_local_data_files(self.workbook, project, NAME)
        self.assertEqual(stats["not_found"], 1)
        stats = migrate._collect_local_data_files(self.workbook, project, NAME,
                                                  search_root=self.root)
        self.assertEqual(stats["copied"], 1)
        self.assertTrue(os.path.isfile(os.path.join(project, "Data", "ref.csv")))


if __name__ == "__main__":
    unittest.main()
