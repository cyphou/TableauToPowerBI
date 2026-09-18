"""What the Desktop probe verifies, and what it cannot.

Measured against deliberately broken projects on a machine with Power BI
Desktop installed: a corrupted report.json, a deleted SemanticModel and invalid
DAX all still returned ``opened``, because Desktop surfaces content errors in a
dialog and keeps running. Every corpus run also lasted exactly the settle
window, confirming the verdict is "survived N seconds" and nothing more.

These tests pin that limitation so the verdict cannot quietly be read — or
documented — as proof that a project loaded correctly.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.desktop_probe import (  # noqa: E402
    OPENED_LIMITATION,
    VERIFIED_SCOPE,
    DesktopProbeReport,
)
from powerbi_import.migration_quality import _openability_confidence  # noqa: E402


class TestProbeStatesItsScope(unittest.TestCase):
    def test_scope_is_process_survival(self):
        self.assertEqual(VERIFIED_SCOPE, 'process_survival')

    def test_opened_report_carries_the_limitation(self):
        data = DesktopProbeReport(pbip_path='p.pbip', status='opened').to_dict()
        self.assertTrue(data['opened'])
        self.assertEqual(data['verified'], 'process_survival')
        self.assertEqual(data['limitation'], OPENED_LIMITATION)

    def test_limitation_says_it_does_not_prove_loading(self):
        self.assertIn('does not prove', OPENED_LIMITATION)

    def test_non_opened_report_claims_no_limitation(self):
        data = DesktopProbeReport(pbip_path='p.pbip', status='crashed').to_dict()
        self.assertNotIn('limitation', data)
        self.assertEqual(data['verified'], 'process_survival')


class TestConfidenceDoesNotOverstateTheProbe(unittest.TestCase):
    _OPENABLE = {'openable': True, 'checks': []}
    _FABRIC = {'present': False}

    def test_opened_is_labelled_a_smoke_pass_only(self):
        confidence = _openability_confidence(
            self._OPENABLE, self._FABRIC,
            {'status': 'opened', 'verified': 'process_survival'})
        self.assertEqual(confidence['level'], 'DESKTOP_SMOKE_PASS')

    def test_confidence_records_what_was_verified(self):
        confidence = _openability_confidence(
            self._OPENABLE, self._FABRIC,
            {'status': 'opened', 'verified': 'process_survival'})
        self.assertEqual(confidence['desktop']['verified'], 'process_survival')

    def test_scope_is_reported_even_when_the_probe_did_not_run(self):
        confidence = _openability_confidence(self._OPENABLE, self._FABRIC)
        self.assertEqual(confidence['desktop']['status'], 'not_run')
        self.assertEqual(confidence['desktop']['verified'], 'process_survival')

    def test_a_failing_static_check_is_never_rescued_by_the_probe(self):
        """Desktop surviving cannot make an unopenable project verified."""
        confidence = _openability_confidence(
            {'openable': False, 'checks': []}, self._FABRIC,
            {'status': 'opened', 'verified': 'process_survival'})
        self.assertEqual(confidence['level'], 'UNVERIFIED')

    def test_runtime_signals_stay_not_run(self):
        confidence = _openability_confidence(
            self._OPENABLE, self._FABRIC, {'status': 'opened'})
        self.assertEqual(confidence['semantic_execution'], 'not_run')
        self.assertEqual(confidence['refresh'], 'not_run')
        self.assertEqual(confidence['deployment'], 'not_run')


if __name__ == '__main__':
    unittest.main()
