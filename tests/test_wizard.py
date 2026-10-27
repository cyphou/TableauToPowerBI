"""Tests for powerbi_import.wizard — interactive CLI wizard.

Covers _input(), _yes_no(), _choose(), wizard_to_args(), and run_wizard().
Most functions use builtins.input which is mocked for testing.
"""

import argparse
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))

from wizard import (
    _choose,
    _input,
    _yes_no,
    run_wizard,
    wizard_to_args,
)


class TestInputHelper(unittest.TestCase):
    """Test _input() function."""

    @patch('builtins.input', return_value='hello')
    def test_returns_user_input(self, mock_in):
        result = _input("prompt")
        self.assertEqual(result, 'hello')

    @patch('builtins.input', return_value='')
    def test_returns_default_when_empty(self, mock_in):
        result = _input("prompt", default='fallback')
        self.assertEqual(result, 'fallback')

    @patch('builtins.input', return_value='custom')
    def test_user_overrides_default(self, mock_in):
        result = _input("prompt", default='fallback')
        self.assertEqual(result, 'custom')

    @patch('builtins.input', return_value='  spaced  ')
    def test_strips_whitespace(self, mock_in):
        result = _input("prompt")
        self.assertEqual(result, 'spaced')

    @patch('builtins.input', side_effect=EOFError)
    def test_eof_exits(self, mock_in):
        with self.assertRaises(SystemExit):
            _input("prompt")

    @patch('builtins.input', side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt_exits(self, mock_in):
        with self.assertRaises(SystemExit):
            _input("prompt")


class TestYesNo(unittest.TestCase):
    """Test _yes_no() function."""

    @patch('builtins.input', return_value='y')
    def test_yes(self, mock_in):
        self.assertTrue(_yes_no("Continue?"))

    @patch('builtins.input', return_value='yes')
    def test_yes_full(self, mock_in):
        self.assertTrue(_yes_no("Continue?"))

    @patch('builtins.input', return_value='n')
    def test_no(self, mock_in):
        self.assertFalse(_yes_no("Continue?"))

    @patch('builtins.input', return_value='')
    def test_default_true(self, mock_in):
        self.assertTrue(_yes_no("Continue?", default=True))

    @patch('builtins.input', return_value='')
    def test_default_false(self, mock_in):
        self.assertFalse(_yes_no("Continue?", default=False))

    @patch('builtins.input', return_value='1')
    def test_one_is_true(self, mock_in):
        self.assertTrue(_yes_no("Continue?"))

    @patch('builtins.input', return_value='true')
    def test_true_string(self, mock_in):
        self.assertTrue(_yes_no("Continue?"))

    @patch('builtins.input', return_value='nope')
    def test_other_is_false(self, mock_in):
        self.assertFalse(_yes_no("Continue?"))


class TestChoose(unittest.TestCase):
    """Test _choose() function."""

    @patch('builtins.input', return_value='1')
    def test_first_option(self, mock_in):
        idx = _choose("Pick one:", ['A', 'B', 'C'])
        self.assertEqual(idx, 0)

    @patch('builtins.input', return_value='3')
    def test_last_option(self, mock_in):
        idx = _choose("Pick:", ['A', 'B', 'C'])
        self.assertEqual(idx, 2)

    @patch('builtins.input', return_value='')
    def test_default_option(self, mock_in):
        idx = _choose("Pick:", ['A', 'B', 'C'], default=1)
        self.assertEqual(idx, 1)

    @patch('builtins.input', side_effect=['invalid', '2'])
    def test_retries_on_invalid(self, mock_in):
        idx = _choose("Pick:", ['A', 'B'])
        self.assertEqual(idx, 1)

    @patch('builtins.input', side_effect=['0', '1'])
    def test_out_of_range_retries(self, mock_in):
        idx = _choose("Pick:", ['A', 'B'])
        self.assertEqual(idx, 0)


class TestWizardToArgs(unittest.TestCase):
    """Test wizard_to_args() conversion."""

    def test_basic_conversion(self):
        config = {
            'batch': 'test_workbooks',
            'prep': None,
            'output_dir': 'out/',
            'output_format': 'pbip',
            'mode': 'import',
            'calendar_start': 2020,
            'calendar_end': 2030,
            'culture': 'en-US',
            'paginated': False,
            'rollback': True,
            'verbose': False,
            'assess': True,
        }
        args = wizard_to_args(config)
        self.assertIsInstance(args, argparse.Namespace)
        self.assertEqual(args.tableau_file, None)
        self.assertEqual(args.mode, 'import')
        self.assertEqual(args.calendar_start, 2020)
        self.assertTrue(args.assess)
        self.assertFalse(args.dry_run)
        self.assertEqual(args.batch, 'test_workbooks')

    def test_directquery_mode(self):
        config = {
            'batch': 'workbooks',
            'mode': 'directquery',
        }
        args = wizard_to_args(config)
        self.assertEqual(args.mode, 'directquery')
        self.assertEqual(args.batch, 'workbooks')

    def test_defaults_for_missing_keys(self):
        config = {'batch': 'workbooks'}
        args = wizard_to_args(config)
        self.assertEqual(args.output_format, 'pbip')
        self.assertEqual(args.mode, 'auto')
        self.assertFalse(args.paginated)

    def test_single_file_generation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'single-file generation is retired'):
            wizard_to_args({'tableau_file': 'workbook.twbx'})


class TestRunWizard(unittest.TestCase):
    """Test run_wizard() with fully mocked input."""

    @patch('wizard.glob.glob', return_value=[])  # no auto-detected workbooks
    def test_full_wizard_flow(self, mock_glob):
        with tempfile.TemporaryDirectory() as source_folder:
            inputs = [
                source_folder,  # Step 1: source folder (no found list)
                'n',           # Step 2: prep? no
                '',            # Step 3: output dir
                '1',           # Step 3: format (pbip)
                '1',           # Step 4: model mode (import)
                'y',           # Step 5: auto calendar?
                '2020',        # start year
                '2030',        # end year
                '',            # Step 6: culture (default)
                'n',           # paginated
                'y',           # rollback
                'n',           # verbose
                'y',           # assess
                'y',           # proceed?
            ]
            with patch('builtins.input', side_effect=inputs):
                config = run_wizard()
            self.assertIsNotNone(config)
            self.assertEqual(config['batch'], source_folder)
            self.assertIsNone(config['prep'])

    @patch('wizard.glob.glob', return_value=[])  # no auto-detected workbooks
    @patch('builtins.input', side_effect=['/nonexistent/folder'])
    def test_wizard_file_not_found(self, mock_in, mock_glob):
        config = run_wizard()
        self.assertIsNone(config)


if __name__ == '__main__':
    unittest.main()
