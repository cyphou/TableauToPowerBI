"""Windows executable launcher for the Tableau to Power BI desktop UI."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _candidate_roots() -> list[Path]:
    executable_dir = Path(sys.executable).resolve().parent
    source_dir = Path(__file__).resolve().parent
    candidates = [
        Path(os.environ["TTPBI_ENGINE_ROOT"]) if os.environ.get("TTPBI_ENGINE_ROOT") else None,
        executable_dir,
        executable_dir.parent,
        source_dir,
        Path.cwd(),
    ]
    return [path for path in candidates if path is not None]


def find_engine_root() -> Path:
    for root in _candidate_roots():
        if (root / "run_light_ui.ps1").is_file() and (root / "migrate.py").is_file():
            return root
    raise FileNotFoundError(
        "Could not find the TableauToPowerBI repository. Set TTPBI_ENGINE_ROOT "
        "to the checkout containing run_light_ui.ps1 and migrate.py."
    )


def main() -> int:
    try:
        root = find_engine_root()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    script = root / "run_light_ui.ps1"
    command = [
        "powershell.exe",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-NoVenvCreate",
    ]
    return subprocess.call(command, cwd=str(root))


if __name__ == "__main__":
    raise SystemExit(main())
