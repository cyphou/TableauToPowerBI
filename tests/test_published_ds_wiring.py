"""--resolve-published-ds and --no-ds-cache were declared, backed, and inert.

A published datasource carries no tables or columns in the workbook XML, so
the generator sees an empty source unless the real definition is fetched.
`resolve_all_published` did that correctly and had tests, but nothing in the
pipeline ever called it: complete, covered, unreachable.

These tests seed a real cache and drive the resolution step end to end, so a
passing assertion means the stub was genuinely filled.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tableau_export"))

from tableau_export.datasource_extractor import (  # noqa: E402
    cache_published_datasource,
)

PUBLISHED_NAME = "SalesPublished"


def _stub_datasource():
    """What the workbook XML gives us: a pointer, with no content."""
    return {
        "name": "Sales (published)",
        "connection": {
            "type": "Tableau Server",
            "details": {"server_ds_name": PUBLISHED_NAME},
        },
        "tables": [],
        "columns": [],
    }


def _resolved_datasource():
    """What the server or cache gives back: the real thing."""
    return {
        "name": PUBLISHED_NAME,
        "connection": {"type": "Tableau Server", "details": {}},
        "tables": [{"name": "Orders"}, {"name": "Customers"}],
        "columns": [{"name": "OrderID"}, {"name": "Amount"}],
        "relationships": [],
        "connection_map": {},
    }


class _Args:
    server = None
    token_name = None
    token_secret = None
    site = ""
    resolve_published_ds = True
    ds_cache_dir = None
    no_ds_cache = False


class TestPublishedDatasourceResolution(unittest.TestCase):

    def setUp(self):
        self._extract = tempfile.TemporaryDirectory()
        self._cache = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("TTPBI_EXTRACT_DIR")
        os.environ["TTPBI_EXTRACT_DIR"] = self._extract.name

        self.json_path = os.path.join(self._extract.name, "datasources.json")
        with open(self.json_path, "w", encoding="utf-8") as fh:
            json.dump([_stub_datasource()], fh)

        cache_published_datasource(_resolved_datasource(), self._cache.name)

        self.args = _Args()
        self.args.ds_cache_dir = self._cache.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TTPBI_EXTRACT_DIR", None)
        else:
            os.environ["TTPBI_EXTRACT_DIR"] = self._prev
        self._extract.cleanup()
        self._cache.cleanup()

    def _run(self):
        import migrate
        migrate._resolve_published_datasources(self.args)
        with open(self.json_path, encoding="utf-8") as fh:
            return json.load(fh)[0]

    def test_the_stub_is_filled_from_cache(self):
        ds = self._run()
        self.assertTrue(ds.get("_published_resolved"))
        self.assertEqual("cache", ds.get("_published_source"))
        self.assertEqual(["Orders", "Customers"],
                         [t["name"] for t in ds["tables"]])

    def test_the_result_is_written_back_to_disk(self):
        """Resolving in memory and not persisting would leave generation blind."""
        self._run()
        with open(self.json_path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertTrue(on_disk[0]["tables"])

    def test_no_ds_cache_skips_the_primary_read(self):
        """The flag disables the cache as a *source*, not as a last resort.

        With no server reachable the offline fallback still applies, so the
        proof is the provenance tag, not the absence of data.
        """
        self.args.no_ds_cache = True
        ds = self._run()
        self.assertEqual("cache_fallback", ds.get("_published_source"))

    def test_a_workbook_with_no_published_sources_is_untouched(self):
        with open(self.json_path, "w", encoding="utf-8") as fh:
            json.dump([{"name": "Local", "connection": {"type": "excel"},
                        "tables": [{"name": "Sheet1"}]}], fh)
        ds = self._run()
        self.assertNotIn("_published_resolved", ds)
        self.assertEqual([{"name": "Sheet1"}], ds["tables"])

    def test_missing_datasources_json_is_not_fatal(self):
        os.remove(self.json_path)
        import migrate
        self.assertTrue(migrate._resolve_published_datasources(self.args))

    def test_a_failing_server_falls_back_instead_of_aborting(self):
        self.args.server = "https://tableau.example.com"
        with mock.patch("tableau_export.server_client.TableauServerClient",
                        side_effect=RuntimeError("no network")):
            ds = self._run()
        self.assertTrue(ds.get("_published_resolved"))


class TestResolutionIsWired(unittest.TestCase):
    """The step has to run, not merely exist."""

    def test_the_flag_triggers_the_step(self):
        import inspect
        import re

        import migrate
        source = inspect.getsource(migrate)

        # "def _resolve_published_datasources(args):" also contains the call
        # text, so counting bare occurrences would pass with no call site.
        calls = [m for m in re.finditer(r"(?<!def )_resolve_published_datasources\(args\)",
                                        source)]
        self.assertTrue(calls, "the resolution step is never invoked")
        self.assertRegex(
            source,
            r"resolve_published_ds[^\n]*\n\s*_resolve_published_datasources\(args\)",
            "resolution must be gated on --resolve-published-ds",
        )

    def test_both_flags_parse(self):
        import migrate
        parser = migrate._build_argument_parser()
        args = parser.parse_args(['wb.twbx', '--resolve-published-ds',
                                  '--no-ds-cache'])
        self.assertTrue(args.resolve_published_ds)
        self.assertTrue(args.no_ds_cache)

    def test_both_default_off(self):
        import migrate
        args = migrate._build_argument_parser().parse_args(['wb.twbx'])
        self.assertFalse(args.resolve_published_ds)
        self.assertFalse(args.no_ds_cache)


if __name__ == "__main__":
    unittest.main()
