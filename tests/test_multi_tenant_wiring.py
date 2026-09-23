"""--multi-tenant was declared, fully backed, and inert.

`deploy_multi_tenant` validates tenants, substitutes per-tenant connection
strings, deploys each copy, and supports dry_run. None of it was reachable:
the documented `--shared-model ... --multi-tenant tenants.json` produced a
model and stopped.

The wiring sits behind `exit_code == ExitCode.SUCCESS`, which matters more
than it looks — see TestDeploymentGating.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import migrate  # noqa: E402

GUID_A = "11111111-2222-3333-4444-555555555555"
GUID_B = "66666666-7777-8888-9999-aaaaaaaaaaaa"


def _config(tenants):
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
    json.dump({"tenants": tenants}, handle)
    handle.close()
    return handle.name


def _model_dir(root):
    """Minimal model tree: the pre-deploy gate rejects an empty directory."""
    model = os.path.join(root, "Shared.SemanticModel", "definition")
    os.makedirs(model, exist_ok=True)
    with open(os.path.join(model, "model.tmdl"), "w", encoding="utf-8") as fh:
        fh.write("model Model\n\tculture: en-US\n")
    return root


class _Args:
    multi_tenant = None
    bundle_refresh = False
    dry_run = True


class TestFlag(unittest.TestCase):

    def setUp(self):
        self.parser = migrate._build_argument_parser()

    def test_absent_by_default(self):
        self.assertIsNone(self.parser.parse_args(['wb.twbx']).multi_tenant)

    def test_takes_a_config_path(self):
        args = self.parser.parse_args(['wb.twbx', '--multi-tenant', 't.json'])
        self.assertEqual('t.json', args.multi_tenant)


class TestRunner(unittest.TestCase):

    def _run(self, path, project_dir=None):
        args = _Args()
        args.multi_tenant = path
        return migrate._run_multi_tenant_deploy(
            args, project_dir or tempfile.gettempdir())

    def test_a_missing_config_is_an_error_not_a_crash(self):
        code = self._run(os.path.join('no', 'such', 'tenants.json'))
        self.assertEqual(migrate.ExitCode.GENERAL_ERROR, code)

    def test_an_empty_tenant_list_fails_validation(self):
        path = _config([])
        try:
            self.assertEqual(migrate.ExitCode.VALIDATION_FAILED, self._run(path))
        finally:
            os.unlink(path)

    def test_a_non_guid_workspace_fails_validation(self):
        """Caught during wiring: the validator rejects ids that only look real."""
        path = _config([{"name": "A", "workspace_id": "ws-contoso-0001"}])
        try:
            self.assertEqual(migrate.ExitCode.VALIDATION_FAILED, self._run(path))
        finally:
            os.unlink(path)

    def test_duplicate_workspaces_fail_validation(self):
        path = _config([
            {"name": "A", "workspace_id": GUID_A},
            {"name": "B", "workspace_id": GUID_A},
        ])
        try:
            self.assertEqual(migrate.ExitCode.VALIDATION_FAILED, self._run(path))
        finally:
            os.unlink(path)

    def test_a_valid_config_dry_runs_to_success(self):
        path = _config([
            {"name": "Contoso", "workspace_id": GUID_A,
             "connection_overrides": {"${TENANT_SERVER}": "contoso.example.net"}},
            {"name": "Fabrikam", "workspace_id": GUID_B,
             "connection_overrides": {"${TENANT_SERVER}": "fabrikam.example.net"}},
        ])
        with tempfile.TemporaryDirectory() as project:
            _model_dir(project)
            try:
                self.assertEqual(migrate.ExitCode.SUCCESS,
                                 self._run(path, project))
                report = os.path.join(project, 'multi_tenant_deployment.json')
                self.assertTrue(os.path.isfile(report))
                with open(report, encoding='utf-8') as fh:
                    data = json.load(fh)
                self.assertEqual(2, data['total'])
                self.assertEqual(0, data['failed'])
            finally:
                os.unlink(path)

    def test_a_bad_placeholder_is_rejected(self):
        """Overrides must be ${UPPER_NAME}; a bare key is not substituted."""
        path = _config([
            {"name": "A", "workspace_id": GUID_A,
             "connection_overrides": {"server": "contoso.example.net"}},
        ])
        with tempfile.TemporaryDirectory() as project:
            _model_dir(project)
            try:
                self.assertEqual(migrate.ExitCode.GENERAL_ERROR,
                                 self._run(path, project))
            finally:
                os.unlink(path)


class TestWiring(unittest.TestCase):

    def test_the_flag_reaches_the_runner(self):
        import inspect
        source = inspect.getsource(migrate)
        self.assertRegex(
            source,
            r"getattr\(args, 'multi_tenant', None\):\n\s+model_name",
            "--multi-tenant is never acted on after the shared model is built",
        )


class TestDeploymentGating(unittest.TestCase):
    """Both post-shared-model deploy steps require a SUCCESS exit code.

    That coupling is load-bearing and currently unmet: --shared-model returns
    VALIDATION_FAILED because the openability gate assumes the single-project
    layout, so --deploy-bundle and --multi-tenant are both unreachable in a
    default run. The check is correct; the gate it depends on is the defect.
    Pinned so the coupling is visible rather than discovered again.
    """

    def test_both_deploy_paths_share_the_success_gate(self):
        import inspect
        source = inspect.getsource(migrate)
        for flag in ("deploy_bundle", "multi_tenant"):
            with self.subTest(flag=flag):
                self.assertIn(
                    f"if exit_code == ExitCode.SUCCESS and getattr(args, '{flag}'",
                    source)


if __name__ == "__main__":
    unittest.main()
