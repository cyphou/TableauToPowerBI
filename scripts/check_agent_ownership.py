"""Check agent file-ownership coverage and overlaps.

Ownership is declared before the ``## Constraints`` heading; anything after it
is a delegation reference, not a claim. Matching uses a boundary so that
``validator.py`` does not match ``dax_validator.py``.

A claim line is not counted as ownership when it delegates ("owned by @other").
Lines marked "co-owned" are reported separately as intentional sharing.
"""

import collections
import glob
import os
import re

MODULE_DIRS = ("powerbi_import", "tableau_export")
AGENT_GLOB = ".github/agents/*.agent.md"

_DELEGATES = re.compile(r"owned by \*{0,2}@", re.I)
_SHARED = re.compile(r"co-owned", re.I)


def _modules():
    found = []
    for d in MODULE_DIRS:
        for root, _, files in os.walk(d):
            for fn in files:
                if fn.endswith(".py") and fn != "__init__.py":
                    found.append(fn)
    return sorted(set(found))


def analyse():
    mods = _modules()
    owners = collections.defaultdict(list)
    shared = collections.defaultdict(list)
    for path in sorted(glob.glob(AGENT_GLOB)):
        agent = os.path.basename(path).replace(".agent.md", "")
        text = open(path, encoding="utf-8", errors="replace").read()
        head = re.split(r"^## Constraints", text, flags=re.M)[0]
        # Co-ownership may be declared on the claim line itself or by the
        # section heading it sits under ("## Shared Files (Co-owned with X)"),
        # so a claim is read together with its nearest preceding heading.
        section = ""
        claims = collections.defaultdict(list)
        for line in head.splitlines():
            if line.startswith("#"):
                section = line
                continue
            for fn in mods:
                if re.search(r"(?<!\w)" + re.escape(fn), line):
                    claims[fn].append(section + "\n" + line)

        for fn, claim_lines in claims.items():
            if all(_DELEGATES.search(ln) for ln in claim_lines):
                continue  # explicitly points at another agent
            if any(_SHARED.search(ln) for ln in claim_lines):
                shared[fn].append(agent)
                continue
            owners[fn].append(agent)
    unowned = [m for m in mods if m not in owners and m not in shared]
    multi = {m: a for m, a in owners.items() if len(a) > 1}
    # One side says "co-owned", the other claims the file outright: the sharing
    # is real but only half-declared, so neither agent's file tells the truth.
    asymmetric = {m: sorted(shared[m]) + sorted(owners[m])
                  for m in set(shared) & set(owners)}
    return mods, owners, shared, unowned, multi, asymmetric


def main():
    mods, _owners, shared, unowned, multi, asymmetric = analyse()
    print("modules                :", len(mods))
    print("sans proprietaire      :", len(unowned))
    print("co-propriete declaree  :", len(shared))
    print("conflits reels         :", len(multi))
    print("declaree d'un seul cote:", len(asymmetric))
    if unowned:
        print("\n=== SANS PROPRIETAIRE ===")
        for m in unowned:
            print("  ", m)
    if shared:
        print("\n=== CO-PROPRIETE DECLAREE (intentionnel) ===")
        for m, agents in sorted(shared.items()):
            print("   {:38} -> {}".format(m, ", ".join(agents)))
    if asymmetric:
        print("\n=== CO-PROPRIETE DECLAREE D'UN SEUL COTE ===")
        for m, agents in sorted(asymmetric.items()):
            print("   {:38} -> {}".format(m, ", ".join(agents)))
    if multi:
        print("\n=== CONFLITS REELS ===")
        for m, agents in sorted(multi.items()):
            print("   {:38} -> {}".format(m, ", ".join(agents)))


if __name__ == "__main__":
    main()
