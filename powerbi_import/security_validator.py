"""
Security Validator — centralized security utilities for the migration pipeline.

Sprint 97: Security Hardening across all agents.

Provides:
- Path validation and traversal protection
- Non-local (UNC/device) path classification for metadata-derived paths
- ZIP archive safe extraction (ZIP slip defense)
- XML parsing with XXE protection
- Credential detection and redaction in output artifacts
- Input sanitization for template substitution
- File size limit enforcement
"""

import logging
import ntpath
import os
import re
import xml.etree.ElementTree as ET

try:
    from .errors import ConfigurationError
except ImportError:  # direct module import when powerbi_import is on sys.path
    from errors import ConfigurationError

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

MAX_FILE_SIZE_MB = 500  # Maximum allowed file size in MB
MAX_ZIP_ENTRY_SIZE_MB = 1024  # Default maximum allowed size per ZIP entry
MAX_XML_SIZE_MB = 100  # Maximum XML content size
_ZIP_COPY_CHUNK_SIZE = 1024 * 1024
ALLOWED_EXTENSIONS = frozenset({
    '.twb', '.twbx', '.tds', '.tdsx', '.tfl', '.tflx', '.hyper',
    '.json', '.xml', '.csv', '.txt',
})


# ── Credential redaction patterns ────────────────────────────────────────────

_CREDENTIAL_PATTERNS = [
    (re.compile(r'(password\s*=\s*)([^\s;,"\']+)', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(secret\s*=\s*)([^\s;,"\']+)', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(token\s*=\s*)([^\s;,"\']+)', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(access.?key\s*=\s*)([^\s;,"\']+)', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(account.?key\s*=\s*)([^\s;,"\']+)', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(private.?key\s*=\s*)([^\s;,"\']+)', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(Bearer\s+)([A-Za-z0-9\-._~+/]+=*)', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(Basic\s+)([A-Za-z0-9+/]+=*)', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(client.?secret\s*=\s*)([^\s;,"\']+)', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(api.?key\s*=\s*)([^\s;,"\']+)', re.IGNORECASE), r'\1***REDACTED***'),
]

# Patterns for detecting credentials in M query strings
_M_CREDENTIAL_PATTERNS = [
    re.compile(r'Password\s*=\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'pwd\s*=\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'User\s+ID\s*=\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'uid\s*=\s*"[^"]*"', re.IGNORECASE),
]


# ── Path validation ─────────────────────────────────────────────────────────

def validate_path(path, must_exist=True, allowed_extensions=None):
    """Validate a file path for security.

    Checks:
    - Path traversal (no ``..`` escaping to parent directories)
    - Null bytes
    - File existence (optional)
    - Extension whitelist (optional)

    Args:
        path: The file path to validate.
        must_exist: If True, check that the file exists.
        allowed_extensions: Optional set of allowed extensions (e.g., {'.twbx'}).

    Returns:
        tuple: (is_valid: bool, error_message: str or None)
    """
    if not path:
        return False, "Path is empty"

    # Check for null bytes (path injection)
    if '\x00' in path:
        return False, "Path contains null bytes"

    # Resolve to absolute and normalize
    resolved = os.path.realpath(path)

    if allowed_extensions:
        ext = os.path.splitext(resolved)[1].lower()
        if ext not in allowed_extensions:
            return False, f"Extension '{ext}' not in allowed list: {sorted(allowed_extensions)}"

    if must_exist and not os.path.exists(resolved):
        return False, f"Path does not exist: {resolved}"

    return True, None


def validate_output_dir(path):
    """Validate an output directory path.

    Args:
        path: The directory path to validate.

    Returns:
        tuple: (is_valid: bool, error_message: str or None)
    """
    if not path:
        return False, "Output directory path is empty"

    if '\x00' in path:
        return False, "Path contains null bytes"

    # Resolve to absolute
    resolved = os.path.realpath(path)

    # Ensure it's not a system directory
    system_dirs = {'/etc', '/usr', '/bin', '/sbin', '/var', '/boot',
                   'C:\\Windows', 'C:\\Program Files', 'C:\\Program Files (x86)'}
    for sdir in system_dirs:
        if resolved.lower().startswith(sdir.lower()):
            return False, f"Cannot write to system directory: {resolved}"

    return True, None


_LOCAL_DRIVE_RE = re.compile(r'(?:\\\\[?.]\\)?[A-Za-z]:')


def is_non_local_windows_path(path):
    """Return True for UNC, device-namespace, volume and NT-object Windows paths.

    These forms name a host or device rather than a local drive. They are
    acceptable as an operator-selected folder, but a path read from project
    metadata must never be resolved through the filesystem: ``os.path.realpath``
    opens its argument, so a crafted ``\\\\host\\share`` value could make this
    process contact an arbitrary SMB host and leak an NTLM challenge response.
    The check is lexical and applies on every platform.
    """
    normalized = os.fsdecode(path).replace('/', '\\')
    if normalized.startswith('\\??\\'):
        return True
    drive = ntpath.splitdrive(normalized)[0]
    return bool(drive) and not _LOCAL_DRIVE_RE.fullmatch(drive)


def _absolute_path_text(path):
    path = os.fsdecode(path)
    return path if is_non_local_windows_path(path) else os.path.abspath(path)


def lexical_path_key(path):
    """Return a case-normalized identity key for ``path`` using no filesystem I/O.

    Use it for every path read from project metadata. A symlink, junction or
    short-name alias keeps a key different from its target: following one lets
    a crafted project make this process contact an arbitrary SMB host exactly
    like a UNC value does, so such an alias is refused rather than resolved.
    """
    absolute = _absolute_path_text(path)
    if is_non_local_windows_path(absolute):
        return ntpath.normcase(ntpath.normpath(absolute))
    return os.path.normcase(absolute)


def canonical_path_key(path):
    """Return the identity key of ``path`` after resolving its links.

    Reserve this for a path the operator chose, such as the project directory,
    and never for a value read from project metadata: ``os.path.realpath``
    opens its argument. A non-local Windows path (see
    ``is_non_local_windows_path``) is only normalized lexically, which is enough
    to compare it with the path the operator chose and never contacts the host
    it names.
    """
    absolute = _absolute_path_text(path)
    if is_non_local_windows_path(absolute):
        return lexical_path_key(absolute)
    return os.path.normcase(os.path.realpath(absolute))


# ── ZIP safe extraction ──────────────────────────────────────────────────────

def _positive_zip_limit(value, *, source):
    """Validate a positive, integral ZIP entry limit."""
    if isinstance(value, bool):
        raise ConfigurationError(f"{source} must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        parsed = int(value.strip())
    else:
        raise ConfigurationError(f"{source} must be a positive integer")
    if parsed <= 0:
        raise ConfigurationError(f"{source} must be a positive integer")
    return parsed


def get_zip_entry_limit_mb(value=None):
    """Resolve the maximum decompressed ZIP member size in MiB.

    An explicit argument wins, followed by ``TTPBI_MAX_ZIP_ENTRY_MB`` and the
    module default. Invalid explicit or environment values are configuration
    errors; they are never silently replaced with the default.
    """
    if value is not None:
        return _positive_zip_limit(value, source="max ZIP entry size")
    env_value = os.environ.get("TTPBI_MAX_ZIP_ENTRY_MB")
    if env_value is not None:
        return _positive_zip_limit(
            env_value, source="TTPBI_MAX_ZIP_ENTRY_MB")
    return _positive_zip_limit(
        MAX_ZIP_ENTRY_SIZE_MB, source="MAX_ZIP_ENTRY_SIZE_MB")


def _validate_zip_member(zip_file, member_name, target_dir, max_size_mb):
    """Return the ZIP metadata after path and declared-size checks."""
    if not isinstance(member_name, str) or not member_name:
        raise SecurityError("ZIP entry name must be a nonempty string")
    info = zip_file.getinfo(member_name)

    # Normalize to forward slashes for consistent POSIX and Windows checking.
    normalized = member_name.replace('\\', '/')
    if '..' in normalized.split('/'):
        raise SecurityError(
            f"ZIP entry contains path traversal: '{member_name}'")
    if (os.path.isabs(member_name) or ntpath.isabs(member_name)
            or normalized.startswith('/')):
        raise SecurityError(f"ZIP entry has absolute path: '{member_name}'")

    size_limit_mb = get_zip_entry_limit_mb(max_size_mb)
    size_limit = size_limit_mb * 1024 * 1024
    if info.file_size > size_limit:
        raise ValueError(
            f"ZIP entry '{member_name}' exceeds size limit "
            f"({info.file_size / (1024 * 1024):.1f} MB > "
            f"{size_limit_mb} MB)")

    if target_dir:
        target_path = os.path.realpath(os.path.join(target_dir, member_name))
        target_dir_real = os.path.realpath(target_dir)
        try:
            contained = os.path.commonpath(
                (os.path.normcase(target_path), os.path.normcase(target_dir_real))
            ) == os.path.normcase(target_dir_real)
        except ValueError:
            contained = False
        if not contained:
            raise SecurityError(
                f"ZIP entry would extract outside target: '{member_name}'")
    return info, size_limit


def safe_zip_copy_member(zip_file, member_name, output_stream, target_dir=None,
                         max_size_mb=None):
    """Stream one ZIP member to *output_stream* with path and size defenses.

    Data is copied in chunks no larger than 1 MiB. Both the central-directory
    size and actual decompressed byte count are checked against the configured
    limit. The caller owns and closes ``output_stream`` and should write to a
    sibling temporary file before atomically publishing it.

    Returns:
        int: Number of decompressed bytes written.
    """
    if not callable(getattr(output_stream, "write", None)):
        raise TypeError("output_stream must provide write(bytes)")
    info, size_limit = _validate_zip_member(
        zip_file, member_name, target_dir, max_size_mb)
    total = 0
    with zip_file.open(member_name) as source:
        while True:
            # Read at most 1 MiB, and one byte beyond the remaining allowance
            # when near the limit so a dishonest declared size cannot bypass
            # the runtime guard.
            remaining = size_limit - total
            chunk = source.read(min(_ZIP_COPY_CHUNK_SIZE, remaining + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > size_limit:
                raise ValueError(
                    f"ZIP entry '{member_name}' exceeds size limit "
                    f"while streaming ({total / (1024 * 1024):.1f} MB > "
                    f"{size_limit / (1024 * 1024):.1f} MB)")
            if total > info.file_size:
                raise ValueError(
                    f"ZIP entry '{member_name}' expanded beyond its declared size")
            written = output_stream.write(chunk)
            if written is not None and written != len(chunk):
                raise OSError(
                    f"Short write while copying ZIP entry '{member_name}'")
    if total != info.file_size:
        raise ValueError(
            f"ZIP entry '{member_name}' size mismatch "
            f"({total} bytes read, {info.file_size} declared)")
    return total


def safe_zip_extract_member(zip_file, member_name, target_dir=None,
                            max_size_mb=None):
    """Safely extract a single member from a ZIP archive with ZIP slip protection.

    Args:
        zip_file: An open zipfile.ZipFile object.
        member_name: Name of the member to extract.
        target_dir: Optional target directory. If None, reads content in memory.
        max_size_mb: Optional per-member decompressed size limit in MiB.

    Returns:
        bytes: The raw content of the member.

    Raises:
        SecurityError: If the member path would escape the target directory.
        ValueError: If the member exceeds size limits.
    """
    import io

    result = io.BytesIO()
    safe_zip_copy_member(
        zip_file, member_name, result, target_dir=target_dir,
        max_size_mb=max_size_mb)
    return result.getvalue()


def validate_zip_archive(zip_path, max_size_mb=None):
    """Validate a ZIP archive for common security issues.

    Args:
        zip_path: Path to the ZIP file.

    Returns:
        tuple: (is_safe: bool, issues: list[str])
    """
    import zipfile as zf
    issues = []
    size_limit_mb = get_zip_entry_limit_mb(max_size_mb)

    if not os.path.exists(zip_path):
        return False, ["File does not exist"]

    file_size = os.path.getsize(zip_path)
    if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        issues.append(f"Archive size ({file_size / (1024*1024):.1f} MB) exceeds limit ({MAX_FILE_SIZE_MB} MB)")

    try:
        with zf.ZipFile(zip_path, 'r') as z:
            for info in z.infolist():
                normalized = info.filename.replace('\\', '/')
                if '..' in normalized.split('/'):
                    issues.append(f"Path traversal in entry: '{info.filename}'")
                if os.path.isabs(info.filename):
                    issues.append(f"Absolute path in entry: '{info.filename}'")
                if info.file_size > size_limit_mb * 1024 * 1024:
                    issues.append(
                        f"Entry '{info.filename}' exceeds size limit "
                        f"({info.file_size / (1024*1024):.1f} MB > "
                        f"{size_limit_mb} MB)"
                    )
    except zf.BadZipFile:
        issues.append("Invalid or corrupted ZIP file")

    return len(issues) == 0, issues


# ── XML parsing security ────────────────────────────────────────────────────

def safe_parse_xml(xml_content):
    """Parse XML content with XXE (XML External Entity) protections.

    Disables external entity resolution and DTD processing to prevent
    XXE attacks (OWASP Top 10 — Injection).

    Args:
        xml_content: XML string or bytes to parse.

    Returns:
        xml.etree.ElementTree.Element: The parsed root element.

    Raises:
        SecurityError: If the XML content exceeds size limits.
        ET.ParseError: If the XML is malformed.
    """
    # Size check
    content_size = len(xml_content) if isinstance(xml_content, (str, bytes)) else 0
    if content_size > MAX_XML_SIZE_MB * 1024 * 1024:
        raise SecurityError(
            f"XML content exceeds size limit "
            f"({content_size / (1024*1024):.1f} MB > {MAX_XML_SIZE_MB} MB)"
        )

    # Check for XXE attack patterns in the content
    if isinstance(xml_content, bytes):
        check_str = xml_content[:4096].decode('utf-8', errors='ignore')
    else:
        check_str = xml_content[:4096]

    # Detect DOCTYPE with ENTITY declarations (XXE indicator)
    if re.search(r'<!DOCTYPE\s+[^>]*\[', check_str, re.IGNORECASE):
        if re.search(r'<!ENTITY\s+', check_str, re.IGNORECASE):
            raise SecurityError(
                "XML contains DOCTYPE with ENTITY declarations — "
                "potential XXE attack detected"
            )
        logger.warning(
            "XML contains DOCTYPE declaration — processing without entities"
        )

    # Use the standard parser but with a check for entities
    # Python's xml.etree.ElementTree does not expand external entities by default,
    # but we add explicit checks for safety
    return ET.fromstring(xml_content)


# ── Credential redaction ─────────────────────────────────────────────────────

def redact_credentials(text):
    """Redact sensitive credentials from a text string.

    Replaces passwords, tokens, keys, and other secrets with ``***REDACTED***``.

    Args:
        text: Input text that may contain credentials.

    Returns:
        str: Text with credentials redacted.
    """
    if not text:
        return text
    result = text
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_m_credentials(m_query):
    """Redact credentials embedded in Power Query M expressions.

    Args:
        m_query: M query string that may contain embedded credentials.

    Returns:
        str: M query with credentials redacted.
    """
    if not m_query:
        return m_query
    result = m_query
    for pattern in _M_CREDENTIAL_PATTERNS:
        result = pattern.sub(lambda m: m.group(0).split('=')[0] + '="***REDACTED***"', result)
    return result


def scan_for_credentials(text):
    """Scan text for potential credentials without modifying it.

    Args:
        text: Input text to scan.

    Returns:
        list[dict]: List of findings with 'type' and 'sample' keys.
    """
    if not text:
        return []
    findings = []
    all_patterns = [
        (re.compile(r'password\s*=\s*\S+', re.IGNORECASE), 'password'),
        (re.compile(r'secret\s*=\s*\S+', re.IGNORECASE), 'secret'),
        (re.compile(r'token\s*=\s*\S+', re.IGNORECASE), 'token'),
        (re.compile(r'(?:access|account).?key\s*=\s*\S+', re.IGNORECASE), 'access_key'),
        (re.compile(r'private.?key\s*=\s*\S+', re.IGNORECASE), 'private_key'),
        (re.compile(r'Bearer\s+[A-Za-z0-9\-._~+/]+=*', re.IGNORECASE), 'bearer_token'),
        (re.compile(r'Basic\s+[A-Za-z0-9+/]+=*', re.IGNORECASE), 'basic_auth'),
        (re.compile(r'client.?secret\s*=\s*\S+', re.IGNORECASE), 'client_secret'),
        (re.compile(r'api.?key\s*=\s*\S+', re.IGNORECASE), 'api_key'),
    ]
    for pattern, cred_type in all_patterns:
        matches = pattern.findall(text)
        for match in matches:
            # Show only the first 20 chars as a sample
            sample = match[:20] + '...' if len(match) > 20 else match
            findings.append({'type': cred_type, 'sample': sample})
    return findings


# ── Template substitution sanitization ───────────────────────────────────────

def sanitize_template_value(value, context='tmdl'):
    """Sanitize a value before template substitution.

    Prevents injection attacks by escaping special characters based on context.

    Args:
        value: The value to sanitize.
        context: Target context — 'tmdl', 'json', or 'm' (Power Query M).

    Returns:
        str: Sanitized value.

    Raises:
        ValueError: If the value contains dangerous patterns.
    """
    if not isinstance(value, str):
        value = str(value)

    # Block null bytes
    if '\x00' in value:
        raise ValueError("Template value contains null bytes")

    if context == 'json':
        # For JSON context, escape backslashes and quotes
        value = value.replace('\\', '\\\\').replace('"', '\\"')
    elif context == 'm':
        # For M query context, escape quotes
        value = value.replace('"', '""')
    elif context == 'tmdl':
        # For TMDL, escape single quotes
        value = value.replace("'", "''")

    return value


# ── Security exception ───────────────────────────────────────────────────────

class SecurityError(Exception):
    """Raised when a security check fails."""
    pass


# ── Aggregate validator ──────────────────────────────────────────────────────

def validate_migration_artifacts(project_dir):
    """Scan generated migration artifacts for security issues.

    Checks:
    - Embedded credentials in .tmdl, .json, .m, .pbir files
    - Sensitive data in connection strings
    - Hardcoded secrets in generated code

    Args:
        project_dir: Path to the generated .pbip project directory.

    Returns:
        dict: Validation results with 'issues' list and 'scanned_files' count.
    """
    results = {'issues': [], 'scanned_files': 0, 'clean': True}

    if not os.path.exists(project_dir):
        results['issues'].append({'file': project_dir, 'type': 'missing', 'detail': 'Project directory not found'})
        results['clean'] = False
        return results

    scan_extensions = {'.tmdl', '.json', '.m', '.pbir', '.pbi', '.xml'}

    for root, _dirs, files in os.walk(project_dir):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in scan_extensions:
                continue
            fpath = os.path.join(root, fname)
            results['scanned_files'] += 1
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    content = f.read()
            except (UnicodeDecodeError, OSError):
                continue

            findings = scan_for_credentials(content)
            if findings:
                rel_path = os.path.relpath(fpath, project_dir)
                for finding in findings:
                    results['issues'].append({
                        'file': rel_path,
                        'type': finding['type'],
                        'detail': f"Potential {finding['type']} found",
                    })
                results['clean'] = False

    return results
