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
               "                                       live_connection=live_connection,\n"
               "                                       model_context=model_context)",
        "new": "thin_gen = ThinReportGenerator(model_name, thin_report_dir,\n"
               "                                       model_context=model_context)",
        "test": "tests/test_cli_flag_wiring.py",
    },
    {
        "name": "published datasource resolution stops running",
        "file": "migrate.py",
        "old": "        if getattr(args, 'resolve_published_ds', False):\n"
               "            _resolve_published_datasources(args)\n",
        "new": "",
        "test": "tests/test_published_ds_wiring.py",
        "all": True,  # Disable resolution in both batch and inspection paths.
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

    # ── Data validation ────────────────────────────────────────────────
    {
        "name": "the equivalence suite stops reading the generated model",
        "file": "powerbi_import/equivalence_tester_v2.py",
        "old": "    model = _read_generated_model(pbi_artifact)",
        "new": "    model = None",
        "test": "tests/test_data_validation.py",
    },
    {
        "name": "tests that never ran count as passes again",
        "file": "powerbi_import/equivalence_tester_v2.py",
        "old": "        comparable = [r for r in test_results if r.get('status') != 'not_run']",
        "new": "        comparable = list(test_results)",
        "test": "tests/test_data_validation.py",
    },
    {
        "name": "field coverage compares Tableau against itself again",
        "file": "powerbi_import/equivalence_tester_v2.py",
        "old": "        passed, coverage = tester.test_visual_field_coverage(\n"
               "            tableau_fields, model_names, ws_name\n"
               "        )",
        "new": "        passed, coverage = tester.test_visual_field_coverage(\n"
               "            tableau_fields, tableau_fields, ws_name\n"
               "        )",
        "test": "tests/test_data_validation.py",
    },
    {
        "name": "--validate-data stops being acted on",
        "file": "migrate.py",
        "old": "        _run_data_validation(args, source_basename)",
        "new": "        pass",
        "test": "tests/test_data_validation.py",
    },

    # ── Shared-model measure routing ───────────────────────────────────
    {
        "name": "merged calculations keep a dead datasource name",
        "file": "powerbi_import/shared_model.py",
        "old": "        for calc in merged_datasource.get('calculations', []):\n"
               "            calc['datasource_name'] = merged_ds_name",
        "new": "        pass",
        "test": "tests/test_merge_measure_routing.py",
    },
    {
        "name": "measures stop being attributed to their source table",
        "file": "powerbi_import/shared_model.py",
        "old": "    _attribute_calculations_to_source_tables(\n"
               "        merged, merged_datasource, all_extracted, workbook_names)",
        "new": "    pass",
        "test": "tests/test_merge_measure_routing.py",
    },
    {
        "name": "the generator ignores explicit table attribution",
        "file": "powerbi_import/tmdl_generator.py",
        "old": "        attributed = [c for c in all_calculations if c.get('table') == table_name]",
        "new": "        attributed = []",
        "test": "tests/test_merge_measure_routing.py",
    },

    # ── Openability gate ───────────────────────────────────────────────
    {
        "name": "visual bindings check only the first report again",
        "file": "powerbi_import/openability.py",
        "old": "    for report_dir in report_dirs:\n"
               "        report_name = os.path.basename(report_dir)\n"
               "        report_state = load_report(report_dir)",
        "new": "    for report_dir in report_dirs[:1]:\n"
               "        report_name = os.path.basename(report_dir)\n"
               "        report_state = load_report(report_dir)",
        "test": "tests/test_openability_bundle.py",
    },
    {
        "name": "the contract derives the report name instead of reading it",
        "file": "powerbi_import/openability.py",
        "old": "        if report_name not in declared_names:",
        "new": "        if report_name not in declared_names and False:",
        "test": "tests/test_openability_bundle.py",
    },
    {
        "name": "the model explorer report loses its pages.json",
        "file": "powerbi_import/import_to_powerbi.py",
        "old": "        _write_json_file(os.path.join(model_report_dir, 'definition', 'pages', 'pages.json'), {",
        "new": "        _skip = lambda *a, **k: None\n"
               "        _skip(os.path.join(model_report_dir, 'definition', 'pages', 'pages.json'), {",
        "test": "tests/test_shared_model.py",
    },

    # ── Thin report model context ──────────────────────────────────────
    {
        "name": "shared-model thin reports lose the model's name index",
        "file": "powerbi_import/import_to_powerbi.py",
        "old": "live_connection=live_connection,\n"
               "                                       model_context=model_context)",
        "new": "live_connection=live_connection)",
        "test": "tests/test_thin_report_model_context.py",
    },
    {
        "name": "the Fabric project report loses the model's name index",
        "file": "powerbi_import/fabric_project_generator.py",
        "old": "                    item_id=item_registry['Report'],\n"
               "                    model_context=sm_stats,",
        "new": "                    item_id=item_registry['Report'],",
        "test": "tests/test_thin_report_model_context.py",
    },
    {
        "name": "--add-to-model reports lose the regenerated model's name index",
        "file": "migrate.py",
        "old": "model_context=gen.export_model_context() if sm_dir else None,",
        "new": "model_context=None,",
        "test": "tests/test_thin_report_model_context.py",
    },
    {
        "name": "the thin report generator stops applying its model context",
        "file": "powerbi_import/thin_report_generator.py",
        "old": "        if self.model_context:\n"
               "            generator.apply_model_context(self.model_context)",
        "new": "        pass",
        "test": "tests/test_thin_report_model_context.py",
    },
    {
        "name": "the PBIP generator stops recording its model's name index",
        "file": "powerbi_import/pbip_generator.py",
        "old": "            self.apply_model_context(stats)\n",
        "new": "",
        "test": "tests/test_thin_report_model_context.py",
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

    # ── Agent ownership ────────────────────────────────────────────────
    {
        # Retargeted from tmdl_generator, which became sole @semantic once the
        # DAX half moved to tmdl_dax_postprocess.
        "name": "co-ownership of shared_model stops being declared",
        "file": ".github/agents/semantic.agent.md",
        "old": "Co-owned with @merger (you own the semantic model merge logic)",
        "new": "the semantic model merge logic",
        "test": "tests/test_agent_ownership.py",
    },
    {
        "name": "a third agent claims a file without declaring the sharing",
        "file": ".github/agents/visual.agent.md",
        "old": "## Constraints",
        "new": "- `powerbi_import/tmdl_generator.py`\n\n## Constraints",
        "test": "tests/test_agent_ownership.py",
    },

    # ── Module reachability ────────────────────────────────────────────
    {
        "name": "a wired module loses its only import",
        "file": "migrate.py",
        "old": "from goals_generator import generate_goals_json, write_goals_artifact",
        "new": "generate_goals_json = write_goals_artifact = None",
        "test": "tests/test_reachability.py",
    },
    {
        "name": "the analyser stops resolving bare module names",
        "file": "scripts/check_reachability.py",
        "old": '    for directory in MODULE_DIRS:\n'
               '        found |= _with_packages(f"{directory}.{name}")\n',
        "new": "    for directory in MODULE_DIRS:\n        pass\n",
        "test": "tests/test_reachability.py",
    },
    {
        "name": "a declared entry point names a caller that does not exist",
        "file": "scripts/check_reachability.py",
        "old": '    "powerbi_import.api_server": "Dockerfile",',
        "new": '    "powerbi_import.api_server": "Dockerfile.removed",',
        "test": "tests/test_reachability.py",
    },

    # ── Retired flags ──────────────────────────────────────────────────
    {
        "name": "a retired flag stops applying its replacement",
        "file": "migrate.py",
        "old": "        args.validate_data = True\n"
               "        retired.append(('--parallel-run', '--validate-data'))",
        "new": "        retired.append(('--parallel-run', '--validate-data'))",
        "test": "tests/test_cli_flag_wiring.py",
    },
    {
        "name": "retired flags are applied to everything, not just what was passed",
        "file": "migrate.py",
        "old": "    if getattr(args, 'sync', False):\n        args.incremental = True",
        "new": "    if True:\n        args.incremental = True",
        "test": "tests/test_cli_flag_wiring.py",
    },
    {
        "name": "main stops applying retired flags",
        "file": "migrate.py",
        "old": "    _apply_retired_flags(args)\n\n    # Load configuration file if specified",
        "new": "    # Load configuration file if specified",
        "test": "tests/test_cli_flag_wiring.py",
    },

    # ── Healing facade ─────────────────────────────────────────────────
    {
        "name": "a consumer bypasses the healing facade",
        "file": "migrate.py",
        "old": "        from healing import check_openability\n"
               "    except ImportError:\n"
               "        from powerbi_import.healing import check_openability",
        "new": "        from openability import check_openability\n"
               "    except ImportError:\n"
               "        from powerbi_import.openability import check_openability",
        "test": "tests/test_healing_facade.py",
    },

    # ── Copilot readiness ──────────────────────────────────────────────
    {
        "name": "columns stop carrying a description",
        "file": "powerbi_import/tmdl_writers.py",
        "old": "    column_desc = _generate_column_description(column)",
        "new": "    column_desc = ''",
        "test": "tests/test_copilot_readiness.py",
    },

    # ── Extraction seams ───────────────────────────────────────────────
    {
        "name": "an extracted module stops being re-exported",
        "file": "powerbi_import/tmdl_generator.py",
        "old": "    detect_refresh_policy,\n",
        "new": "",
        "test": "tests/test_incremental_refresh.py",
    },
    {
        "name": "a test patches a name its target only re-exports",
        "file": "tests/test_filter_apostrophe_escaping.py",
        "old": "            with patch.object(tw, '_write_table_tmdl',",
        "new": "            with patch.object(tg, '_write_table_tmdl',",
        "test": "tests/test_patch_targets.py",
    },

    # ── Credential scanning ────────────────────────────────────────────
    {
        "name": "the scan flags every word after Basic again",
        "file": "powerbi_import/security_validator.py",
        "old": "    if _decodes_to_user_password(match.group(1)):\n"
               "        return True\n",
        "new": "    return True\n",
        "test": "tests/test_security.py",
    },
    {
        "name": "the scan stops catching tokens on an Authorization line",
        "file": "powerbi_import/security_validator.py",
        "old": "    return bool(_BASIC_AUTH_CONTEXT.search(prefix))",
        "new": "    return False",
        "test": "tests/test_security.py",
    },
    {
        "name": "the scan ignores the Basic precision rule",
        "file": "powerbi_import/security_validator.py",
        "old": "            if cred_type == 'basic_auth' and not _is_basic_credential(found):\n"
               "                continue\n",
        "new": "",
        "test": "tests/test_security.py",
    },
    {
        "name": "redaction corrupts prose after Basic again",
        "file": "powerbi_import/security_validator.py",
        "old": "    (_BASIC_AUTH_PATTERN, _redact_basic_auth),",
        "new": "    (re.compile(r'(Basic\\s+)([A-Za-z0-9+/]+=*)', re.IGNORECASE), r'\\1***REDACTED***'),",
        "test": "tests/test_security.py",
    },
    {
        "name": "URL-safe Basic credentials stop being recognised",
        "file": "powerbi_import/security_validator.py",
        "old": "    raw = token.rstrip('=').replace('-', '+').replace('_', '/')",
        "new": "    raw = token.rstrip('=')",
        "test": "tests/test_security.py",
    },
    {
        "name": "unpadded Basic credentials stop being recognised",
        "file": "powerbi_import/security_validator.py",
        "old": "base64.b64decode(raw + '=' * (-len(raw) % 4), validate=True)",
        "new": "base64.b64decode(raw, validate=True)",
        "test": "tests/test_security.py",
    },

    # ── Corpus gate ────────────────────────────────────────────────────
    {
        "name": "the gate stops failing an artefact nobody listed",
        "file": "scripts/check_corpus_gate.py",
        "old": "        if limitation is None:\n",
        "new": "        if limitation is None:\n            continue\n",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "the gate stops flagging a listed artefact that now migrates",
        "file": "scripts/check_corpus_gate.py",
        "old": "            if limitation:\n",
        "new": "            if False:\n",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "the gate stops flagging a listed artefact the corpus lost",
        "file": "scripts/check_corpus_gate.py",
        "old": "    for artefact in sorted(set(known) - set(expected)):\n",
        "new": "    for artefact in []:\n",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "the gate stops comparing the error category",
        "file": "scripts/check_corpus_gate.py",
        "old": "    if category != limitation['category']:\n",
        "new": "    if False:\n",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "the gate tolerates blockers nobody reviewed",
        "file": "scripts/check_corpus_gate.py",
        "old": "(observed - recorded).elements()",
        "new": "(observed - observed).elements()",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "the gate tolerates a reviewed blocker that vanished",
        "file": "scripts/check_corpus_gate.py",
        "old": "(recorded - observed).elements()",
        "new": "(recorded - recorded).elements()",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "the gate excuses blockers of artefacts that are not listed",
        "file": "scripts/check_corpus_gate.py",
        "old": "        if owner.get(name) not in judged:\n",
        "new": "        if False:\n",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "the gate stops failing a project that will not open",
        "file": "scripts/check_corpus_gate.py",
        "old": "        for project, issues in evidence['not_openable']\n",
        "new": "        for project, issues in []\n",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "the gate stops cross-checking the migration exit code",
        "file": "scripts/check_corpus_gate.py",
        "old": "    if exit_code != expected_code:\n",
        "new": "    if False:\n",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "the gate passes with failures on the list",
        "file": "scripts/check_corpus_gate.py",
        "old": "    if failures:\n",
        "new": "    if False:\n",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "a missing corpus directory is only printed",
        "file": "scripts/check_corpus_gate.py",
        "old": "            failures.append(f'{label}: corpus directory {batch_dir} not found')\n",
        "new": "            print(f'{label}: corpus directory {batch_dir} not found')\n",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "the gate runs without the Hyper reader",
        "file": "scripts/check_corpus_gate.py",
        "old": "    if problem:\n        print(f'CORPUS GATE CANNOT RUN: {problem}')\n",
        "new": "    if False:\n        print(f'CORPUS GATE CANNOT RUN: {problem}')\n",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "a Hyper reader that cannot be imported goes unnoticed",
        "file": "scripts/check_corpus_gate.py",
        "old": "        import tableauhyperapi  # noqa: F401\n",
        "new": "        pass\n",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "the baseline accepts a repeated key",
        "file": "scripts/check_corpus_gate.py",
        "old": "object_pairs_hook=_reject_duplicate_keys",
        "new": "object_pairs_hook=dict",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "the baseline accepts fields nobody reviewed",
        "file": "scripts/check_corpus_gate.py",
        "old": "        if not isinstance(entry, dict) or set(entry) != fields:\n",
        "new": "        if not isinstance(entry, dict):\n",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "a baseline entry stops having to name a real artefact",
        "file": "examples/corpus_known_limitations.json",
        "old": '"real_world/SampleFlow.tfl": {',
        "new": '"real_world/NoSuchFlow.tfl": {',
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "a baseline entry stops having to explain itself",
        "file": "examples/corpus_known_limitations.json",
        "old": "it declares no nodes, so there is nothing to convert.",
        "new": "empty.",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "the gate and migrate.py disagree on the partial-failure exit code",
        "file": "scripts/check_corpus_gate.py",
        "old": "EXIT_BATCH_PARTIAL_FAIL = 7",
        "new": "EXIT_BATCH_PARTIAL_FAIL = 6",
        "test": "tests/test_corpus_gate.py",
    },
    {
        "name": "the gate and migrate.py disagree on which files a batch picks up",
        "file": "scripts/check_corpus_gate.py",
        "old": "if name.lower().endswith(ARTEFACT_SUFFIXES) and not name.startswith('~'):",
        "new": "if name.lower().endswith(ARTEFACT_SUFFIXES):",
        "test": "tests/test_corpus_gate.py",
    },
]
