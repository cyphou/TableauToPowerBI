"""Coverage contract for registered Power Query M connector emitters."""

import unittest

from powerbi_import.m_emitter_matrix import (
    build_m_emitter_matrix,
    summarize_m_emitter_matrix,
)


class TestMEmitterMatrix(unittest.TestCase):
    def test_every_registered_alias_is_exercised(self):
        rows = build_m_emitter_matrix()
        self.assertGreaterEqual(len(rows), 80)
        self.assertTrue(all(row["connector"] for row in rows if "connector" in row))
        summary = summarize_m_emitter_matrix(rows)
        self.assertEqual(summary["aliases"], len([row for row in rows if "connector" in row]))
        self.assertEqual(summary["invalid_connectors"], [])
        self.assertEqual(summary["status_counts"].get("error", 0), 0)
        self.assertEqual(summary["fixtures"]["count"], 5)
        self.assertEqual(summary["fixtures"]["invalid_fixtures"], [])

    def test_fallback_emitters_are_explicit(self):
        rows = build_m_emitter_matrix()
        summary = summarize_m_emitter_matrix(rows)
        self.assertIn("fallback_connectors", summary)
        self.assertTrue(all(
            row["status"] == "fallback"
            for row in rows if row.get("generator") == "_gen_m_fallback"
        ))
        fallback = [row for row in rows if row.get("status") == "fallback"]
        self.assertTrue(fallback)
        self.assertTrue(all(row["remediation"].get("owner") for row in fallback))
        self.assertTrue(all(row["remediation"].get("action") for row in fallback))
        self.assertEqual(set(summary["remediation"]), {row["connector"] for row in fallback})


if __name__ == "__main__":
    unittest.main()
