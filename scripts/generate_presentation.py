#!/usr/bin/env python3
"""Compatibility entrypoint for the French technical PowerPoint guide.

    python scripts\\generate_presentation.py [--output PATH]

The canonical content and layout live in docs\\generate_pptx.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docs.generate_pptx import DOCS_DIR, GUIDE_NAME, generate_technical_guide, main


def build_presentation():
    """Keep the original no-argument guide builder and output filename."""
    return generate_technical_guide(DOCS_DIR / GUIDE_NAME)


if __name__ == "__main__":
    main(default_deck="guide")
