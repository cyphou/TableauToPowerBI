"""Tests for the domain exception hierarchy."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.errors import (
    ConfigurationError,
    ConversionError,
    DeploymentError,
    ExtractionError,
    GenerationError,
    MigrationError,
    ValidationError,
)

_SUBCLASSES = (
    ExtractionError, ConversionError, GenerationError,
    ValidationError, ConfigurationError, DeploymentError,
)


class TestHierarchy(unittest.TestCase):
    def test_all_inherit_migration_error(self):
        for cls in _SUBCLASSES:
            self.assertTrue(issubclass(cls, MigrationError), cls.__name__)

    def test_migration_error_is_an_exception(self):
        self.assertTrue(issubclass(MigrationError, Exception))

    def test_stages_are_unique_and_non_empty(self):
        stages = [cls.stage for cls in _SUBCLASSES]
        self.assertEqual(len(stages), len(set(stages)))
        for stage in stages:
            self.assertTrue(stage)

    def test_catching_base_catches_subclasses(self):
        for cls in _SUBCLASSES:
            with self.assertRaises(MigrationError):
                raise cls("boom")

    def test_generic_exception_not_swallowed_by_base(self):
        with self.assertRaises(AttributeError):
            try:
                raise AttributeError("real defect")
            except MigrationError:  # pragma: no cover - must not match
                self.fail("MigrationError must not catch AttributeError")


class TestContext(unittest.TestCase):
    def test_message_only(self):
        err = ConversionError("unbalanced brackets")
        self.assertEqual(err.message, "unbalanced brackets")
        self.assertIsNone(err.item)
        self.assertIsNone(err.source)
        self.assertEqual(str(err), "unbalanced brackets")

    def test_str_includes_context(self):
        err = ConversionError("bad formula", item="Sales YTD",
                              source="dax_converter")
        text = str(err)
        self.assertIn("bad formula", text)
        self.assertIn("Sales YTD", text)
        self.assertIn("dax_converter", text)

    def test_to_dict_payload(self):
        err = ValidationError("dangling reference", item="Orders.tmdl")
        payload = err.to_dict()
        self.assertEqual(payload["stage"], "validation")
        self.assertEqual(payload["error"], "ValidationError")
        self.assertEqual(payload["message"], "dangling reference")
        self.assertEqual(payload["item"], "Orders.tmdl")
        self.assertIsNone(payload["source"])

    def test_to_dict_keys_are_stable(self):
        payload = GenerationError("x").to_dict()
        self.assertEqual(
            sorted(payload), ["error", "item", "message", "source", "stage"])


if __name__ == "__main__":
    unittest.main()
