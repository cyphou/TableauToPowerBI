"""Tests for Sprint 168 — Incremental & Live Sync depth."""

import os
import json
import unittest
import tempfile
from powerbi_import.incremental import FileWatcher, LiveSyncEngine


class TestFileWatcher(unittest.TestCase):
    """Test file change detection."""

    def setUp(self):
        """Create temp directory with test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.temp_dir, '.watch_state.json')

    def tearDown(self):
        """Clean up temp files."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_initial_scan_detects_new_files(self):
        """First scan reports all files as 'added'."""
        # Create a .twbx file
        twbx_path = os.path.join(self.temp_dir, 'test.twbx')
        with open(twbx_path, 'w') as f:
            f.write('dummy')

        watcher = FileWatcher([self.temp_dir], state_file=self.state_file)
        changes = watcher.scan_for_changes()
        self.assertIn(twbx_path, changes['added'])
        self.assertEqual(len(changes['changed']), 0)
        self.assertEqual(len(changes['deleted']), 0)

    def test_no_changes_on_second_scan(self):
        """Second scan with no modifications reports no changes."""
        twbx_path = os.path.join(self.temp_dir, 'test.twbx')
        with open(twbx_path, 'w') as f:
            f.write('dummy')

        watcher = FileWatcher([self.temp_dir], state_file=self.state_file)
        watcher.scan_for_changes()  # First scan
        changes = watcher.scan_for_changes()  # Second scan
        self.assertEqual(len(changes['added']), 0)
        self.assertEqual(len(changes['changed']), 0)

    def test_modified_file_detected(self):
        """Modified file appears in 'changed' list."""
        twbx_path = os.path.join(self.temp_dir, 'test.twbx')
        with open(twbx_path, 'w') as f:
            f.write('original')

        watcher = FileWatcher([self.temp_dir], state_file=self.state_file)
        watcher.scan_for_changes()  # First scan

        # Modify the file (ensure mtime changes)
        import time
        time.sleep(0.1)
        with open(twbx_path, 'w') as f:
            f.write('modified content')
        os.utime(twbx_path, (os.path.getmtime(twbx_path) + 1,
                              os.path.getmtime(twbx_path) + 1))

        changes = watcher.scan_for_changes()
        self.assertIn(twbx_path, changes['changed'])

    def test_deleted_file_detected(self):
        """Deleted file appears in 'deleted' list."""
        twbx_path = os.path.join(self.temp_dir, 'test.twbx')
        with open(twbx_path, 'w') as f:
            f.write('dummy')

        watcher = FileWatcher([self.temp_dir], state_file=self.state_file)
        watcher.scan_for_changes()  # First scan

        os.unlink(twbx_path)
        changes = watcher.scan_for_changes()
        self.assertIn(twbx_path, changes['deleted'])

    def test_only_tableau_extensions_tracked(self):
        """Non-Tableau files (.txt, .py) are ignored."""
        txt_path = os.path.join(self.temp_dir, 'readme.txt')
        with open(txt_path, 'w') as f:
            f.write('text')

        watcher = FileWatcher([self.temp_dir], state_file=self.state_file)
        changes = watcher.scan_for_changes()
        self.assertNotIn(txt_path, changes['added'])

    def test_state_persists_across_instances(self):
        """State file allows new watcher instance to detect changes."""
        twbx_path = os.path.join(self.temp_dir, 'test.twbx')
        with open(twbx_path, 'w') as f:
            f.write('dummy')

        watcher1 = FileWatcher([self.temp_dir], state_file=self.state_file)
        watcher1.scan_for_changes()

        # New watcher instance loads persisted state
        watcher2 = FileWatcher([self.temp_dir], state_file=self.state_file)
        changes = watcher2.scan_for_changes()
        self.assertEqual(len(changes['added']), 0)  # Already known


class TestLiveSyncEngine(unittest.TestCase):
    """Test live sync orchestration."""

    def setUp(self):
        """Create temp source and output dirs."""
        self.source_dir = tempfile.mkdtemp()
        self.output_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Clean up."""
        import shutil
        shutil.rmtree(self.source_dir, ignore_errors=True)
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_no_changes_returns_zero(self):
        """Empty directory reports no sync needed."""
        engine = LiveSyncEngine(self.source_dir, self.output_dir)
        engine.watcher.scan_for_changes()  # Initialize state
        result = engine.check_and_sync()
        self.assertFalse(result['changes_detected'])
        self.assertEqual(result['files_synced'], 0)

    def test_new_file_triggers_sync(self):
        """New .twbx file triggers sync."""
        # Create source file
        twbx_path = os.path.join(self.source_dir, 'new_report.twbx')
        with open(twbx_path, 'w') as f:
            f.write('workbook data')

        engine = LiveSyncEngine(self.source_dir, self.output_dir)
        result = engine.check_and_sync()
        self.assertTrue(result['changes_detected'])
        self.assertEqual(result['files_synced'], 1)

    def test_get_sync_status(self):
        """Status returns expected fields."""
        engine = LiveSyncEngine(self.source_dir, self.output_dir)
        status = engine.get_sync_status()
        self.assertIn('source_dir', status)
        self.assertIn('output_dir', status)
        self.assertIn('tracked_files', status)
        self.assertEqual(status['source_dir'], self.source_dir)


if __name__ == '__main__':
    unittest.main()
