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

# tmdl_generator is the one historically co-owned file: @semantic owns the
# structure and @dax owns DAX post-processing. @wiring was removed once the M
# conversion surface moved to tmdl_m_conversion. Shrink this set, never grow it.
KNOWN_MULTI_OWNED = {'tmdl_generator.py'}
MAX_OWNERS = {'tmdl_generator.py': 2}


class TestAgentOwnership(unittest.TestCase):
    def setUp(self):
        (self.mods, self.owners, self.shared,
         self.unowned, self.multi) = analyse()

    def test_every_module_has_an_owner(self):
        self.assertEqual(
            self.unowned, [],
            "modules with no owning agent:\n  " + "\n  ".join(self.unowned))

    def test_no_undeclared_multi_ownership(self):
        unexpected = {m: a for m, a in self.multi.items()
                      if m not in KNOWN_MULTI_OWNED}
        self.assertEqual(
            unexpected, {},
            "modules claimed by several agents without a 'co-owned' marker:\n  "
            + "\n  ".join(f"{m} -> {', '.join(a)}"
                          for m, a in sorted(unexpected.items())))

    def test_known_multi_owned_does_not_grow(self):
        still_multi = set(self.multi) & KNOWN_MULTI_OWNED
        self.assertTrue(
            still_multi <= KNOWN_MULTI_OWNED,
            f"co-ownership grew beyond the allow-list: {still_multi}")

    def test_owner_count_does_not_grow(self):
        for module, limit in MAX_OWNERS.items():
            agents = self.multi.get(module, self.owners.get(module, []))
            self.assertLessEqual(
                len(agents), limit,
                f"{module} is now owned by {len(agents)} agents "
                f"({', '.join(agents)}), limit is {limit}")

    def test_modules_are_discovered(self):
        self.assertGreater(len(self.mods), 100)


if __name__ == '__main__':
    unittest.main()
