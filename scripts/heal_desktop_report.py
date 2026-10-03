"""Reconcile an existing Desktop opening report with generated PBIP projects.

Run from VS Code's terminal after probe_projects.py has captured screenshots:
    python scripts/heal_desktop_report.py path/to/opening_report.json
The input report and screenshots are never changed. Output stays beside the input.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from powerbi_import.desktop_feedback_healing import heal_from_desktop_evidence
from powerbi_import.healing import AutoHealer, check_openability
from powerbi_import.recovery_report import RecoveryReport
from powerbi_import.self_healing_report import load_report, run_report_healers, write_report


def _preserves_content(before, after):
    if isinstance(before, dict):
        return isinstance(after, dict) and all(
            key in after and _preserves_content(value, after[key])
            for key, value in before.items()
        )
    if isinstance(before, list):
        return isinstance(after, list) and len(after) >= len(before) and all(
            _preserves_content(item, after[index]) for index, item in enumerate(before)
        )
    return True


def _run_report_healers(project_dir, recovery):
    reports = [os.path.join(project_dir, name) for name in os.listdir(project_dir)
               if name.endswith('.Report') and os.path.isdir(os.path.join(project_dir, name))]
    if len(reports) != 1:
        return 0
    state = load_report(reports[0])
    if state is None:
        return 0
    before = copy.deepcopy(state)
    staged = RecoveryReport(recovery.report_name)
    repairs = run_report_healers(state, staged)
    if not _preserves_content(before['report_json'], state['report_json']) or not all(
        _preserves_content(old['json'], new['json']) and
        all(_preserves_content(old_visual['json'], new_visual['json'])
            for old_visual, new_visual in zip(old['visuals'], new['visuals'])) and
        len(new['visuals']) >= len(old['visuals'])
        for old, new in zip(before['pages'], state['pages'])
    ) or len(state['pages']) < len(before['pages']) or not _preserves_content(
        before['pages_metadata'], state['pages_metadata']):
        recovery.record('visual', 'report_healing_preservation_guard',
                        description='Report healer proposed removing existing report content',
                        action='Rejected the entire report-healing pass', severity='error',
                        follow_up='Inspect the report healer before allowing a destructive edit')
        return 0
    dirty_count = len(state['_dirty_files'])
    written = write_report(state)
    if written != dirty_count:
        recovery.record('visual', 'report_healing_write_failure',
                        description='Report healer could not write all staged changes',
                        action='Inspect files before re-running', severity='error')
    recovery.repairs.extend(staged.repairs if written == dirty_count else [])
    return repairs if written == dirty_count else 0


def _save_recovery(project_dir, recovery):
    filename = recovery.report_name.replace(' ', '_').replace('/', '_') + '_recovery.json'
    path = os.path.join(project_dir, filename)
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as handle:
            previous = json.load(handle)
        existing = previous.get('repairs', [])
        seen = {json.dumps(entry, sort_keys=True, ensure_ascii=False) for entry in existing}
        additions = [entry for entry in recovery.repairs
                     if json.dumps(entry, sort_keys=True, ensure_ascii=False) not in seen]
        history = RecoveryReport(recovery.report_name)
        history.repairs = existing + additions
        history.created_at = previous.get('created_at', history.created_at)
        if history.has_repairs:
            history.save(project_dir)
    elif recovery.has_repairs:
        recovery.save(project_dir)


def heal_opening_report(report_path):
    with open(report_path, encoding='utf-8') as handle:
        entries = json.load(handle)
    if not isinstance(entries, list):
        raise ValueError('Opening report must contain a list of probe records')
    results = []
    for evidence in entries:
        if not isinstance(evidence, dict) or not evidence.get('pbip_path'):
            raise ValueError('Opening report contains an invalid probe record')
        pbip = os.path.abspath(evidence['pbip_path'])
        if not os.path.isfile(pbip):
            results.append({'pbip_path': pbip, 'status': 'missing_project'})
            continue
        project_dir = os.path.dirname(pbip)
        recovery = RecoveryReport(os.path.splitext(os.path.basename(pbip))[0])
        report_repairs = _run_report_healers(project_dir, recovery)
        autoheal = AutoHealer().heal_project(project_dir)
        for action in autoheal.actions:
            recovery.record('m_query' if action.artifact == 'm' else
                            'visual' if action.artifact == 'visual' else 'tmdl',
                            'desktop_static_autoheal', item_name=action.location,
                            description='Deterministic expression or visual repair',
                            action=action.source, severity='warning',
                            follow_up='Re-run Desktop validation')
        feedback = heal_from_desktop_evidence(project_dir, evidence, recovery)
        openability = check_openability(project_dir)
        _save_recovery(project_dir, recovery)
        results.append({
            'pbip_path': pbip,
            'status': 'needs_review' if not openability.openable or autoheal.remaining_errors or
                      feedback['remaining_invalid_visuals'] or
                      (evidence.get('data_load') or {}).get('status') != 'verified' or
                      any(record['severity'] == 'error' or
                          record['repair_type'] == 'visual_off_canvas_unresolved'
                          for record in recovery.repairs)
                      else 'static_clean_reprobe_required',
            'report_repairs': report_repairs,
            'static_repairs': len(autoheal.actions),
            'feedback_repairs': feedback['repairs'],
            'remaining_invalid_visuals': feedback['remaining_invalid_visuals'],
            'remaining_static_errors': len(autoheal.remaining_errors),
            'openable': openability.openable,
            'evidence_findings': recovery.get_summary(),
        })
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('opening_report', help='existing opening_report.json from probe_projects.py')
    args = parser.parse_args(argv)
    path = os.path.abspath(args.opening_report)
    results = heal_opening_report(path)
    output = os.path.join(os.path.dirname(path), 'desktop_healing_report.json')
    with open(output, 'w', encoding='utf-8') as handle:
        json.dump(results, handle, indent=2, ensure_ascii=False)
    print(f'Processed {len(results)} projects; repaired {sum(row.get("report_repairs", 0) + row.get("static_repairs", 0) + row.get("feedback_repairs", 0) for row in results)} items; '
          f'{sum(row.get("status") == "needs_review" for row in results)} need review. '
          'A fresh Desktop probe is required to verify repairs.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())