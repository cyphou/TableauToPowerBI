"""Domain exception hierarchy for the migration engine.

Raising a domain error instead of a bare ``ValueError``/``RuntimeError`` lets
callers distinguish *what stage* failed without resorting to string matching,
and lets resilience boundaries catch ``MigrationError`` specifically rather
than swallowing every ``Exception``.

Every error carries optional context so reports and logs can point at the
offending artifact without re-deriving it::

    raise ConversionError("unbalanced brackets", item="Sales YTD",
                          source="dax_converter")
"""

__all__ = [
    "MigrationError",
    "ExtractionError",
    "ConversionError",
    "GenerationError",
    "ValidationError",
    "ConfigurationError",
    "DeploymentError",
]


class MigrationError(Exception):
    """Base class for every expected migration failure.

    Resilience boundaries should catch this rather than ``Exception`` so that
    genuine defects (``AttributeError``, ``ImportError``, ...) still surface.
    """

    #: Short, stable stage label used in reports.
    stage = "migration"

    def __init__(self, message, *, item=None, source=None):
        super().__init__(message)
        self.message = message
        self.item = item
        self.source = source

    def __str__(self):
        parts = [self.message]
        if self.item:
            parts.append(f"item={self.item}")
        if self.source:
            parts.append(f"source={self.source}")
        return " | ".join(parts)

    def to_dict(self):
        """Return a redaction-safe payload for evidence and quality reports."""
        return {
            "stage": self.stage,
            "error": type(self).__name__,
            "message": self.message,
            "item": self.item,
            "source": self.source,
        }


class ExtractionError(MigrationError):
    """Tableau source could not be parsed or read."""

    stage = "extraction"


class ConversionError(MigrationError):
    """A Tableau formula could not be converted to DAX or Power Query M."""

    stage = "conversion"


class GenerationError(MigrationError):
    """TMDL, PBIR, or Fabric artifact generation failed."""

    stage = "generation"


class ValidationError(MigrationError):
    """A generated artifact failed a validation or openability gate."""

    stage = "validation"


class ConfigurationError(MigrationError):
    """Invalid or missing migration configuration / CLI input."""

    stage = "configuration"


class DeploymentError(MigrationError):
    """Deployment to Power BI Service or Fabric failed."""

    stage = "deployment"
