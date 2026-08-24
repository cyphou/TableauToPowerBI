"""Safe XML access helpers for Tableau extractors.

These wrappers avoid hard failures when elements are missing or malformed.
"""

from dataclasses import dataclass
from enum import Enum


class ExtractionWarningCode(str, Enum):
    """Warning codes used by extraction guardrails."""

    UNSAFE_ZIP_ENTRY = 'unsafe_zip_entry'
    MISSING_XML_NODE = 'missing_xml_node'
    MALFORMED_XML_ACCESS = 'malformed_xml_access'


@dataclass
class ExtractionWarning:
    """Structured extraction warning emitted by safe guards."""

    code: str
    message: str
    context: str = ''

    def as_dict(self):
        return {
            'code': self.code,
            'message': self.message,
            'context': self.context,
        }


def safe_get_attr(elem, attr, default=''):
    """Return an XML attribute safely.

    Returns *default* when *elem* is None or access fails.
    """
    if elem is None:
        return default
    try:
        return elem.get(attr, default)
    except AttributeError:
        return default


def safe_find(elem, path):
    """Return the first matching child element safely."""
    if elem is None:
        return None
    try:
        return elem.find(path)
    except (AttributeError, SyntaxError):
        return None


def safe_findall(elem, path):
    """Return matching child elements safely."""
    if elem is None:
        return []
    try:
        return elem.findall(path)
    except (AttributeError, SyntaxError):
        return []


def safe_findtext(elem, path, default=''):
    """Return text from the first matching child safely."""
    if elem is None:
        return default
    try:
        return elem.findtext(path, default)
    except (AttributeError, SyntaxError):
        return default
