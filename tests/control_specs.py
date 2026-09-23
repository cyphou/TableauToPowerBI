"""Deliberate defects, and the test that must notice each one.

Every entry was run by hand first and observed to fail; they are recorded here
so they keep being run. A control that stops firing means the test protecting
that behaviour has quietly become unable to fail.

Keep ``old`` narrow enough to match exactly once. ``verify_controls.py``
confirms the edit reaches disk before judging, because a harness that silently
skipped its own writes once reported every control as passing.
"""

CONTROLS = [
    # ── Inert CLI flags ────────────────────────────────────────────────
    {
        "name": "--optimize-dax stops reaching generation",
        "file": "migrate.py",
        "old": "optimize_dax=getattr(args, 'optimize_dax', False),",
        "new": "optimize_dax=False,",
        # Two call sites; breaking one leaves the other satisfying the test.
        "all": True,
        "test": "tests/test_automation.py",
    },
    {
        "name": "--server-assess stops routing to its handler",
        "file": "migrate.py",
        "old": ")) or getattr(args, 'server_assess', None) is not None",
        "new": "))",
        "test": "tests/test_cli_flag_wiring.py",
    },
    {
        "name": "--live-connection stops reaching the thin report generator",
        "file": "powerbi_import/import_to_powerbi.py",
        "old": "thin_gen = ThinReportGenerator(model_name, thin_report_dir,\n"
               "                                       live_connection=live_connection)",
        "new": "thin_gen = ThinReportGenerator(model_name, thin_report_dir)",
        "test": "tests/test_cli_flag_wiring.py",
    },
    {
        "name": "published datasource resolution stops running",
        "file": "migrate.py",
        "old": "        if getattr(args, 'resolve_published_ds', False):\n"
               "            _resolve_published_datasources(args)\n",
        "new": "",
        "test": "tests/test_published_ds_wiring.py",
    },
    {
        "name": "--no-ds-cache stops being honoured",
        "file": "migrate.py",
        "old": "no_cache = getattr(args, 'no_ds_cache', False)",
        "new": "no_cache = False",
        "test": "tests/test_published_ds_wiring.py",
    },
    {
        "name": "--merge-preview stops short-circuiting the migration",
        "file": "migrate.py",
        "old": "        if preview_only:\n"
               "            return _print_merge_preview(all_converted, workbook_names)\n\n",
        "new": "",
        "test": "tests/test_merge_preview_wiring.py",
    },
    {
        "name": "merge preview reads a key the assessment does not expose",
        "file": "migrate.py",
        "old": "assessment.get('merge_score', 0)",
        "new": "assessment.get('overall_score', None)",
        "test": "tests/test_merge_preview_wiring.py",
    },
    {
        "name": "--multi-tenant stops being acted on",
        "file": "migrate.py",
        "old": "        if exit_code == ExitCode.SUCCESS and getattr(args, 'multi_tenant', None):",
        "new": "        if False and getattr(args, 'multi_tenant', None):",
        "test": "tests/test_multi_tenant_wiring.py",
    },
    {
        "name": "multi-tenant stops validating its config",
        "file": "migrate.py",
        "old": "        errors = config.validate()\n"
               "        if errors:",
        "new": "        errors = []\n"
               "        if errors:",
        "test": "tests/test_multi_tenant_wiring.py",
    },

    # ── Preceptorship cadence ──────────────────────────────────────────
    {
        "name": "preceptorship goes back to opt-in",
        "file": "migrate.py",
        "old": "        '--preceptor',\n        action='store_true',\n        default=True,",
        "new": "        '--preceptor',\n        action='store_true',\n        default=False,",
        "test": "tests/test_preceptor_cli_wiring.py",
    },
    {
        "name": "review becomes a blocking gate by default",
        "file": "migrate.py",
        "old": "        '--preceptor-block',\n        action='store_true',\n        default=False,",
        "new": "        '--preceptor-block',\n        action='store_true',\n        default=True,",
        "test": "tests/test_preceptor_cli_wiring.py",
    },

    # ── Advisory boundary ──────────────────────────────────────────────
    {
        "name": "advisory code gains a path to the artifact generators",
        "file": "powerbi_import/remediation.py",
        "old": '"""Natural-language remediation',
        "new": "from powerbi_import.tmdl_generator import generate_tmdl\n"
               '"""Natural-language remediation',
        "test": "tests/test_advisory_boundary.py",
    },

    # ── Documented surface ─────────────────────────────────────────────
    {
        "name": "the doc guard stops recognising pre-parse intercepts",
        "file": "scripts/check_doc_claims.py",
        "old": "    flags |= intercepted_flags(handle.read())",
        "new": "    flags |= set()",
        "test": "tests/test_doc_claims.py",
    },
    {
        "name": "the doc guard stops scoping to migrate.py commands",
        "file": "scripts/check_doc_claims.py",
        "old": '            if "migrate.py" not in line:\n                continue\n',
        "new": "",
        "test": "tests/test_doc_claims.py",
    },
]
