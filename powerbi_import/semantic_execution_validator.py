"""Static semantic-context checks for migrated Tableau calculations.

This module does not execute DAX. It validates the model assumptions that most
commonly affect converted LOD expressions before an execution-capable target is
available.
"""

import re
from typing import Dict, List, Mapping, Sequence, Tuple


_LOD_RE = re.compile(
    r"\{\s*(FIXED|INCLUDE|EXCLUDE)\s+(.*?)\s*:\s*",
    re.IGNORECASE | re.DOTALL,
)
_COLUMN_RE = re.compile(r"(?:'((?:[^']|'')+)'|([A-Za-z_][\w ]*))\s*\[([^\]]+)\]")


class SemanticExecutionValidator:
    """Perform deterministic, execution-independent semantic checks."""

    @staticmethod
    def validate_table_calc_partition(
        calculation: Mapping[str, object],
        column_table_map: Mapping[str, str],
    ) -> List[str]:
        """Check extracted table-calculation partition fields exist in the model."""
        issues: List[str] = []
        fields = calculation.get("table_calc_partitioning", []) or []
        for field in fields:
            field_name = str(field).strip()
            if field_name.startswith("[") and field_name.endswith("]"):
                field_name = field_name[1:-1]
            if field_name and field_name not in column_table_map:
                issues.append(
                    f"Table calculation partition field '{field_name}' is not "
                    "present in the semantic model"
                )
        return issues

    def validate_lod_grain_compatibility(
        self,
        expression: str,
        column_table_map: Mapping[str, str],
        relationships: Sequence[Mapping[str, object]] = (),
    ) -> List[str]:
        """Check LOD dimensions against model columns and relationship grain.

        Relationship dictionaries may use either the generated model keys
        (``fromTable``, ``toTable``, ``fromColumn``, ``toColumn``,
        ``cardinality``) or the common snake_case equivalents.
        """
        issues: List[str] = []
        for match in _LOD_RE.finditer(expression or ""):
            lod_type = match.group(1).upper()
            dimensions = self._extract_dimensions(match.group(2), column_table_map)
            for table, column in dimensions:
                known_table = column_table_map.get(column)
                if known_table is None:
                    issues.append(
                        f"LOD {lod_type} dimension '{column}' is not present "
                        "in the semantic model"
                    )
                    continue
                if known_table != table:
                    issues.append(
                        f"LOD {lod_type} dimension '{table}[{column}]' resolves "
                        f"to table '{known_table}'"
                    )

                for relationship in relationships:
                    if not self._touches_column(relationship, known_table, column):
                        continue
                    cardinality = self._value(
                        relationship, "cardinality", "Cardinality"
                    )
                    if str(cardinality).lower() in {
                        "manytomany", "many-to-many", "many_to_many",
                    }:
                        issues.append(
                            f"LOD {lod_type} dimension '{known_table}[{column}]' "
                            "touches a many-to-many relationship; verify filter "
                            "propagation and grain explicitly"
                        )
        return issues

    @staticmethod
    def _extract_dimensions(
        text: str, column_table_map: Mapping[str, str]
    ) -> List[Tuple[str, str]]:
        dimensions: List[Tuple[str, str]] = []
        qualified_spans = set()
        for match in _COLUMN_RE.finditer(text):
            table = (match.group(1) or match.group(2)).replace("''", "'").strip()
            column = match.group(3).strip()
            dimensions.append((table, column))
            qualified_spans.add((match.start(), match.end()))
        for match in re.finditer(r"\[([^\]]+)\]", text):
            if any(start <= match.start() < end for start, end in qualified_spans):
                continue
            column = match.group(1).strip()
            table = column_table_map.get(column)
            dimensions.append((table or "", column))
        return dimensions

    @staticmethod
    def _value(mapping: Mapping[str, object], *names: str) -> object:
        for name in names:
            if name in mapping:
                return mapping[name]
        return None

    @classmethod
    def _touches_column(
        cls, relationship: Mapping[str, object], table: str, column: str
    ) -> bool:
        endpoints = (
            (cls._value(relationship, "fromTable", "from_table"),
             cls._value(relationship, "fromColumn", "from_column")),
            (cls._value(relationship, "toTable", "to_table"),
             cls._value(relationship, "toColumn", "to_column")),
        )
        if any(
            str(endpoint_table) == table and str(endpoint_column) == column
            for endpoint_table, endpoint_column in endpoints
        ):
            return True
        for side in ("left", "right", "from", "to"):
            nested = relationship.get(side)
            if isinstance(nested, Mapping):
                nested_table = nested.get("table", nested.get("name"))
                nested_column = nested.get("column")
                if str(nested_table) == table and str(nested_column) == column:
                    return True
        return False
