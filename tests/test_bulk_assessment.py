"""Tests for --bulk-assess CLI mode — portfolio assessment on local folders.

Covers:
  - run_bulk_assessment_mode() with workbooks only
  - run_bulk_assessment_mode() with no files (error)
  - run_bulk_assessment_mode() with nonexistent directory (error)
  - Output file generation (HTML, JSON)
"""

import io
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _make_twbx_stub(directory, name):
    """Create a minimal .twbx stub (ZIP containing a .twb XML)."""
    import zipfile

    twb_xml = f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2024.1.0' source-platform='win' version='18.1'>
  <datasources>
    <datasource caption='{name} Data' inline='true' name='federated.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='localhost' name='conn.1'>
            <connection class='postgres' dbname='test_db'
                        port='5432' server='localhost' />
          </named-connection>
        </named-connections>
        <relation connection='conn.1' name='orders' table='[public].[orders]'
                  type='table' />
        <cols>
          <map key='[order_id]' value='[orders].[order_id]' />
          <map key='[amount]' value='[orders].[amount]' />
          <map key='[date]' value='[orders].[date]' />
        </cols>
      </connection>
      <column caption='Order ID' datatype='integer' name='[order_id]'
              role='dimension' type='ordinal' />
      <column caption='Amount' datatype='real' name='[amount]'
              role='measure' type='quantitative' />
      <column caption='Date' datatype='date' name='[date]'
              role='dimension' type='ordinal' />
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Sheet 1'>
      <table>
        <view>
          <datasources>
            <datasource caption='{name} Data' name='federated.1' />
          </datasources>
          <datasource-dependencies datasource='federated.1'>
            <column datatype='real' name='[amount]' role='measure'
                    type='quantitative' />
          </datasource-dependencies>
        </view>
      </table>
    </worksheet>
  </worksheets>
  <dashboards>
    <dashboard name='Dashboard 1'>
      <zones>
        <zone h='100000' id='1' type='layout-basic' w='100000' x='0' y='0'>
          <zone h='50000' id='2' name='Sheet 1' type='text' w='50000'
                x='0' y='0' />
        </zone>
      </zones>
    </dashboard>
  </dashboards>
</workbook>"""

    twbx_path = os.path.join(directory, f'{name}.twbx')
    with zipfile.ZipFile(twbx_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f'{name}.twb', twb_xml)
    return twbx_path


class TestBulkAssessmentMode(unittest.TestCase):
    """Test run_bulk_assessment_mode CLI handler."""

    def test_nonexistent_directory(self):
        """--bulk-assess with nonexistent directory returns error."""
        from migrate import run_bulk_assessment_mode, ExitCode

        args = MagicMock()
        args.bulk_assess = '/nonexistent/path/xyz'
        args.output_dir = None

        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            result = run_bulk_assessment_mode(args)
        finally:
            sys.stdout = old_stdout

        self.assertEqual(result, ExitCode.GENERAL_ERROR)

    def test_empty_directory(self):
        """--bulk-assess with empty directory returns error."""
        from migrate import run_bulk_assessment_mode, ExitCode

        with tempfile.TemporaryDirectory() as td:
            args = MagicMock()
            args.bulk_assess = td
            args.output_dir = None

            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                result = run_bulk_assessment_mode(args)
            finally:
                sys.stdout = old_stdout

            self.assertEqual(result, ExitCode.GENERAL_ERROR)

    def test_portfolio_assessment_with_workbooks(self):
        """--bulk-assess with 2+ workbooks runs portfolio + merge analysis."""
        from migrate import run_bulk_assessment_mode, ExitCode

        with tempfile.TemporaryDirectory() as td:
            # Create two stub workbooks
            _make_twbx_stub(td, 'SalesReport')
            _make_twbx_stub(td, 'MarketingReport')

            out_dir = os.path.join(td, 'assessment_output')
            args = MagicMock()
            args.bulk_assess = td
            args.output_dir = out_dir

            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                result = run_bulk_assessment_mode(args)
            finally:
                sys.stdout = old_stdout

            self.assertEqual(result, ExitCode.SUCCESS)

            # Portfolio assessment outputs
            self.assertTrue(os.path.exists(
                os.path.join(out_dir, 'portfolio_assessment.html')))
            self.assertTrue(os.path.exists(
                os.path.join(out_dir, 'portfolio_assessment.json')))

            # Global merge analysis outputs
            self.assertTrue(os.path.exists(
                os.path.join(out_dir, 'global_assessment.html')))
            self.assertTrue(os.path.exists(
                os.path.join(out_dir, 'global_assessment.json')))

    def test_single_workbook_no_merge(self):
        """--bulk-assess with 1 workbook produces portfolio but no merge report."""
        from migrate import run_bulk_assessment_mode, ExitCode

        with tempfile.TemporaryDirectory() as td:
            _make_twbx_stub(td, 'OnlyOne')

            out_dir = os.path.join(td, 'out')
            args = MagicMock()
            args.bulk_assess = td
            args.output_dir = out_dir

            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                result = run_bulk_assessment_mode(args)
            finally:
                sys.stdout = old_stdout

            self.assertEqual(result, ExitCode.SUCCESS)

            # Portfolio assessment should exist
            self.assertTrue(os.path.exists(
                os.path.join(out_dir, 'portfolio_assessment.html')))

            # No global merge report (need 2+)
            self.assertFalse(os.path.exists(
                os.path.join(out_dir, 'global_assessment.html')))

    def test_default_output_dir(self):
        """--bulk-assess with no --output-dir uses default artifacts path."""
        from migrate import run_bulk_assessment_mode, ExitCode

        with tempfile.TemporaryDirectory() as td:
            _make_twbx_stub(td, 'WB1')
            _make_twbx_stub(td, 'WB2')

            args = MagicMock()
            args.bulk_assess = td
            args.output_dir = None

            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                result = run_bulk_assessment_mode(args)
                captured = sys.stdout.getvalue()
            finally:
                sys.stdout = old_stdout

            self.assertEqual(result, ExitCode.SUCCESS)
            self.assertIn('assessments', captured)

    def test_recursive_discovery(self):
        """--bulk-assess discovers workbooks in subdirectories."""
        from migrate import run_bulk_assessment_mode, ExitCode

        with tempfile.TemporaryDirectory() as td:
            sub = os.path.join(td, 'dept', 'sales')
            os.makedirs(sub)
            _make_twbx_stub(td, 'TopLevel')
            _make_twbx_stub(sub, 'SubDir')

            out_dir = os.path.join(td, 'out')
            args = MagicMock()
            args.bulk_assess = td
            args.output_dir = out_dir

            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                result = run_bulk_assessment_mode(args)
            finally:
                sys.stdout = old_stdout

            self.assertEqual(result, ExitCode.SUCCESS)
            self.assertTrue(os.path.exists(
                os.path.join(out_dir, 'portfolio_assessment.json')))


if __name__ == '__main__':
    unittest.main()
