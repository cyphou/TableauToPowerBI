"""Plan ordinary, Desktop-readable paths before writing a PBIP project.

Do not use extended Windows/device paths here: they neither lift Desktop's
limits nor belong in artifact references or source-confinement checks.
"""
import hashlib
import os


class DesktopPathError(ValueError):
    """The output cannot be represented within Desktop's path budget."""


def path_units(path):
    """Windows/Desktop count UTF-16 code units, not Python Unicode characters."""
    return len(os.fspath(path).encode('utf-16-le', errors='surrogatepass')) // 2


def _ordinary_root(output_dir):
    root = os.fspath(output_dir)
    if '\x00' in root or root.replace('/', '\\').startswith(('\\\\?\\', '\\\\.\\')):
        raise DesktopPathError(
            "Desktop output requires an ordinary path, not a Windows device "
            "or extended-length path. Use a shorter --output-dir.")
    return os.path.abspath(root)


def _validate_name(name):
    if (not name or name in ('.', '..')
            or any(c in name for c in '/\\\x00:')):
        raise DesktopPathError("Project name must be a single filename component.")


def _predicted_paths(root, name):
    project = os.path.join(root, name)
    # Longest generated page identifier (mobile/drillthrough), plus visual.
    # Both copies of the project name count against the budget.
    return [
        os.path.join(project, name + '.Report', 'definition', 'pages',
                     'Drillthrough_12345678', 'visuals', '12345678', 'visual.json'),
        os.path.join(project, name + '.SemanticModel', 'definition', 'tables',
                     '12345678.tmdl'),
        os.path.join(project, name + '.SemanticModel', 'definition',
                     'relationships.tmdl'),
        os.path.join(project, name + '.pbip'),
    ]


def _fits(root, name):
    return all(path_units(p) < 260 and path_units(os.path.dirname(p)) < 248
               for p in _predicted_paths(root, name))


def validate_desktop_root(output_dir):
    """Validate only the root, without assuming a nested full PBIP layout."""
    root = _ordinary_root(output_dir)
    if path_units(root) >= 248:
        raise DesktopPathError(
            "Power BI Desktop output directory must be <248 UTF-16 units. "
            "Use a shorter --output-dir.")
    return root


def validate_desktop_report(output_dir, name):
    """Validate the flat report-only layout used by shared/Fabric reports."""
    root = validate_desktop_root(output_dir)
    _validate_name(name)
    report = os.path.join(root, name + '.Report')
    # Check required fixed files here; content-only generation can have much
    # shorter pages than the full project's worst-case drillthrough forecast.
    # Its actual dynamic page/visual paths are checked before publishing PBIP.
    paths = [
        os.path.join(report, 'definition', 'pages', 'pages.json'),
        os.path.join(report, 'definition.pbir'),
        os.path.join(root, name + '.pbip'),
    ]
    if any(path_units(p) >= 260 or path_units(os.path.dirname(p)) >= 248
           for p in paths):
        raise DesktopPathError(
            f"Power BI Desktop path budget exceeded for thin report {name!r}. "
            "Use a shorter --output-dir or report name.")
    return report


def validate_desktop_project(output_dir, name):
    """Reject impossible roots before mkdir; never silently rename artifacts."""
    root = _ordinary_root(output_dir)
    _validate_name(name)
    if not _fits(root, name):
        raise DesktopPathError(
            f"Power BI Desktop path budget exceeded for project {name!r} "
            f"under {root!r}. Use a shorter --output-dir or project name "
            "(files must be <260 characters, directories <248). "
            "Windows long-path support does not remove Desktop's limit.")
    return os.path.join(root, name)


def plan_desktop_project_name(output_dir, name):
    """Keep the source name when possible, otherwise shorten the whole identity.

    Callers must pass the returned name to generation AND every postprocessor.
    No independent Report/SemanticModel renaming is performed.
    """
    root = _ordinary_root(output_dir)
    _validate_name(name)
    if _fits(root, name):
        return name
    short = 'P_' + hashlib.sha256(name.encode('utf-8')).hexdigest()[:12]
    validate_desktop_project(root, short)
    return short


def validate_desktop_tree(project_dir):
    """Last unconditional guard, including optional/generated auxiliary files."""
    _ordinary_root(project_dir)
    for root, dirs, files in os.walk(project_dir):
        for name in dirs + files:
            path = os.path.abspath(os.path.join(root, name))
            limit = 248 if name in dirs else 260
            if path_units(path) >= limit:
                raise DesktopPathError(
                    f"Power BI Desktop path budget exceeded ({path_units(path)} >= "
                    f"{limit}): {path}. Use a shorter --output-dir.")
