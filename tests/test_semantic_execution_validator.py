import unittest

from powerbi_import.semantic_execution_validator import SemanticExecutionValidator


class TestSemanticExecutionValidator(unittest.TestCase):
    def setUp(self):
        self.validator = SemanticExecutionValidator()
        self.columns = {"Region": "Sales", "Product": "Products"}

    def test_fixed_lod_with_known_dimension_passes(self):
        issues = self.validator.validate_lod_grain_compatibility(
            "{FIXED 'Sales'[Region] : SUM('Sales'[Amount])}",
            self.columns,
            [{
                "fromTable": "Sales",
                "fromColumn": "ProductId",
                "toTable": "Products",
                "toColumn": "ProductId",
                "cardinality": "manyToOne",
            }],
        )
        self.assertEqual(issues, [])

    def test_lod_reports_unknown_dimension(self):
        issues = self.validator.validate_lod_grain_compatibility(
            "{FIXED 'Sales'[Territory] : SUM('Sales'[Amount])}",
            self.columns,
        )
        self.assertEqual(len(issues), 1)
        self.assertIn("not present", issues[0])

    def test_lod_reports_many_to_many_grain_risk(self):
        issues = self.validator.validate_lod_grain_compatibility(
            "{INCLUDE 'Products'[Product] : SUM('Sales'[Amount])}",
            self.columns,
            [{
                "from_table": "Sales",
                "from_column": "ProductId",
                "to_table": "Products",
                "to_column": "Product",
                "cardinality": "many-to-many",
            }],
        )
        self.assertEqual(len(issues), 1)
        self.assertIn("many-to-many", issues[0])

    def test_lod_reports_table_resolution_mismatch(self):
        issues = self.validator.validate_lod_grain_compatibility(
            "{EXCLUDE 'Products'[Region] : SUM('Sales'[Amount])}",
            self.columns,
        )
        self.assertEqual(len(issues), 1)
        self.assertIn("resolves to table", issues[0])


if __name__ == "__main__":
    unittest.main()
