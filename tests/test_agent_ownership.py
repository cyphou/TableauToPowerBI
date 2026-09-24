"""Guard the agent file-ownership contract.

Every source module must have exactly one owning agent so the "one owner per
file" rule in `.github/agents/shared.instructions.md` stays enforceable.
Intentional sharing must be declared explicitly with "co-owned"; delegation
must say "owned by @other".
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scripts.check_agent_ownership import analyse

# Modules deliberately shared, and the number of agents each may carry.
# tmdl_generator was the historical case (@semantic structure + @dax
# post-processing); the DAX half moved to tmdl_dax_postprocess, so it is sole
# @semantic now and this map shrank. Shrink it further, never grow it.
CO_OWNED_LIMITS = {
    'merge_assessment.py': 2,
    'merge_report_html.py': 2,
    'prep_lineage.py': 2,
    'prep_lineage_report.py': 2,
    'shared_model.py': 2,
}


class TestAgentOwnership(unittest.TestCase):
    def setUp(self):
        (self.mods, self.owners, self.shared,
         self.unowned, self.multi, self.asymmetric) = analyse()

    def _agents_for(self, module):
        """Every agent recorded against a module, however it was declared."""
        return (self.multi.get(module)
                or self.shared.get(module)
                or self.owners.get(module)
                or [])

    def test_co_ownership_is_declared_on_both_sides(self):
        """Half-declared sharing leaves one agent's file claiming exclusivity."""
        self.assertEqual(
            self.asymmetric, {},
            "co-ownership declared by only one side:\n  "
            + "\n  ".join(f"{m} -> {', '.join(a)}"
                          for m, a in sorted(self.asymmetric.items())))

    def test_every_module_has_an_owner(self):
        self.assertEqual(
            self.unowned, [],
            "modules with no owning agent:\n  " + "\n  ".join(self.unowned))

    def test_no_undeclared_multi_ownership(self):
        self.assertEqual(
            self.multi, {},
            "modules claimed by several agents without a 'co-owned' marker:\n  "
            + "\n  ".join(f"{m} -> {', '.join(a)}"
                          for m, a in sorted(self.multi.items())))

    def test_sharing_stays_on_the_allow_list(self):
        """A newly shared module is a decision, not a side effect."""
        self.assertEqual(
            sorted(self.shared), sorted(CO_OWNED_LIMITS),
            "declared co-ownership no longer matches the allow-list")

    def test_owner_count_does_not_grow(self):
        for module, limit in CO_OWNED_LIMITS.items():
            agents = self._agents_for(module)
            self.assertLessEqual(
                len(agents), limit,
                f"{module} is now owned by {len(agents)} agents "
                f"({', '.join(agents)}), limit is {limit}")

    def test_sole_ownership_is_sole(self):
        """The limit above is only meaningful if unshared files carry one agent."""
        for module, agents in self.owners.items():
            self.assertEqual(
                len(agents), 1,
                f"{module} is claimed by {', '.join(agents)} without 'co-owned'")

    def test_modules_are_discovered(self):
        self.assertGreater(len(self.mods), 100)


if __name__ == '__main__':
    unittest.main()
