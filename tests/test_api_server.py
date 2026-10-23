"""Model-mode contracts at the REST option and isolated-worker boundaries."""

import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from powerbi_import import api_server


class TestMigrationModeOptions(unittest.TestCase):
    def test_omitted_mode_is_auto_and_explicit_modes_are_preserved(self):
        self.assertEqual(api_server._migration_options({})["model_mode"], "auto")
        for mode in ("auto", "import", "directquery", "composite"):
            with self.subTest(mode=mode):
                params = {"model_mode": [mode], "culture": ["en-US"]}
                options = api_server._migration_options(params)
                self.assertEqual(options["model_mode"], mode)
                self.assertEqual(options["culture"], "en-US")
                self.assertEqual(params, {"model_mode": [mode], "culture": ["en-US"]})

    def test_invalid_modes_are_rejected_before_worker_dispatch(self):
        for mode in ("", "unsupported", "--output-dir=outside", None):
            with self.subTest(mode=mode), patch.object(
                    api_server, "_run_upload_worker") as worker:
                with self.assertRaises(ValueError):
                    api_server._migration_options({"model_mode": [mode]})
                worker.assert_not_called()

    def test_raw_worker_omitted_mode_is_auto_and_import_override_survives(self):
        with tempfile.TemporaryDirectory(prefix="rest_mode_") as temporary:
            root = os.path.realpath(temporary)
            source = os.path.join(root, "Synthetic.twb")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write("<workbook/>")
            output = os.path.join(root, "output")
            os.mkdir(output)
            cases = (
                (None, "auto"),
                ({}, "auto"),
                ({"model_mode": "import"}, "import"),
                ({"mode": "import"}, "import"),
                ({"model_mode": "auto", "mode": "import"}, "auto"),
            )
            for options, expected in cases:
                with self.subTest(options=options), patch.object(
                        api_server.subprocess, "run") as run:
                    run.return_value = subprocess.CompletedProcess([], 0, "", "")
                    api_server._run_upload_worker(source, root, output, options)
                    run.assert_called_once()
                    command = run.call_args.args[0]
                    self.assertEqual(command.count("--mode"), 1)
                    self.assertEqual(command[command.index("--mode") + 1], expected)
                    environment = run.call_args.kwargs["env"]
                    self.assertEqual(environment["TTPBI_UPLOAD_ROOT"], root)
                    self.assertFalse(os.path.exists(environment["TTPBI_EXTRACT_DIR"]))
