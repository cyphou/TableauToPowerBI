"""Windows executable launcher for the Tableau to Power BI desktop UI."""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

import migrate as _bundled_engine


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
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    for root in _candidate_roots():
        if (root / "run_light_ui.ps1").is_file() and (root / "migrate.py").is_file():
            return root
    raise FileNotFoundError(
        "Could not find the TableauToPowerBI repository. Set TTPBI_ENGINE_ROOT "
        "to the checkout containing run_light_ui.ps1 and migrate.py."
    )


def main() -> int:
    root = find_engine_root()
    if len(sys.argv) > 1 and sys.argv[1] == "--run-engine":
        sys.path.insert(0, str(root))
        sys.argv = ["migrate.py", *sys.argv[3:]]
        return int(_bundled_engine.main())

    if getattr(sys, "frozen", False):
        ui_script = root / "web" / "light_ui.py"
        sys.path.insert(0, str(root))
        sys.argv = [str(ui_script)]
        runpy.run_path(str(ui_script), run_name="__main__")
        return 0

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
