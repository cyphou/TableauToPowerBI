"""Advanced relationship inference engine v2.

Detects candidate relationships using multiple signals:
- Name similarity (exact, substring, semantic)
- Data type compatibility
- Key markers (id/key/pk/fk)
- Cardinality hints
- Cycle prevention
"""

from collections import defaultdict
import re
from typing import Dict, List, Set, Tuple


class RelationshipInferenceEngine:
    """High-fidelity relationship detection from table metadata."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.relationships: List[Dict] = []
        self.relationship_paths = defaultdict(set)

    def infer_relationships(self, tables: List[Dict]) -> List[Dict]:
        """Infer relationships from table metadata.

        Args:
            tables: list of table dicts with keys: name, columns[]

        Returns:
            relationship list with fromTable/fromColumn/toTable/toColumn/cardinality/confidence
        """
        self.relationships = []

        table_meta = {}
        for table in tables:
            tname = table.get("name", "")
            columns = table.get("columns", []) or []
            table_meta[tname] = {
                "row_count": int(table.get("row_count", 0) or 0),
                "columns": {c.get("name", ""): c for c in columns},
                "col_names": {c.get("name", "").lower() for c in columns},
            }

        candidates = []
        table_names = list(table_meta.keys())
        for i, from_table in enumerate(table_names):
            for to_table in table_names[i + 1 :]:
                score_ab = self._score_relationship(table_meta, from_table, to_table)
                score_ba = self._score_relationship(table_meta, to_table, from_table)

                if score_ab[0] > 0:
                    candidates.append(
                        {
                            "from_table": from_table,
                            "to_table": to_table,
                            "score": score_ab[0],
                            "from_col": score_ab[1],
                            "to_col": score_ab[2],
                            "cardinality": score_ab[3],
                        }
                    )
                if score_ba[0] > 0:
                    candidates.append(
                        {
                            "from_table": to_table,
                            "to_table": from_table,
                            "score": score_ba[0],
                            "from_col": score_ba[1],
                            "to_col": score_ba[2],
                            "cardinality": score_ba[3],
                        }
                    )

        candidates.sort(key=lambda c: c["score"], reverse=True)

        for candidate in candidates:
            if candidate["score"] < 0.45:
                continue
            if self._would_create_cycle(candidate["from_table"], candidate["to_table"]):
                continue

            exists = any(
                (r["fromTable"], r["toTable"]) == (candidate["from_table"], candidate["to_table"])
                or (r["fromTable"], r["toTable"]) == (candidate["to_table"], candidate["from_table"])
                for r in self.relationships
            )
            if exists:
                continue

            rel = {
                "name": f"inferred_{candidate['from_table']}_{candidate['to_table']}",
                "fromTable": candidate["from_table"],
                "fromColumn": candidate["from_col"],
                "toTable": candidate["to_table"],
                "toColumn": candidate["to_col"],
                "cardinality": candidate["cardinality"],
                "crossFilteringBehavior": "oneDirection",
                "isActive": True,
                "confidence": round(candidate["score"], 3),
            }
            self.relationships.append(rel)

            if self.verbose:
                print(
                    f"  + {rel['fromTable']}[{rel['fromColumn']}] -> "
                    f"{rel['toTable']}[{rel['toColumn']}] "
                    f"({rel['cardinality']}, {rel['confidence']})"
                )

        return self.relationships

    def _score_relationship(
        self,
        table_meta: Dict[str, Dict],
        from_table: str,
        to_table: str,
    ) -> Tuple[float, str, str, str]:
        from_cols = table_meta[from_table]["columns"]
        to_cols = table_meta[to_table]["columns"]

        best_score = 0.0
        best_from_col = ""
        best_to_col = ""

        for from_col_name, from_col_meta in from_cols.items():
            for to_col_name, to_col_meta in to_cols.items():
                score = self._score_column_pair(
                    from_col_name,
                    to_col_name,
                    from_col_meta,
                    to_col_meta,
                )
                if score > best_score:
                    best_score = score
                    best_from_col = from_col_name
                    best_to_col = to_col_name

        if best_score <= 0:
            return 0.0, "", "", ""

        cardinality = self._infer_cardinality(
            from_cols.get(best_from_col, {}),
            to_cols.get(best_to_col, {}),
        )

        return best_score, best_from_col, best_to_col, cardinality

    def _score_column_pair(
        self,
        from_col: str,
        to_col: str,
        from_meta: Dict,
        to_meta: Dict,
    ) -> float:
        score = 0.0

        # 1) Name similarity (40%)
        if from_col.lower() == to_col.lower():
            score += 0.40
        elif self._is_substring_match(from_col, to_col):
            score += 0.30
        elif self._is_semantic_match(from_col, to_col):
            score += 0.20

        # 2) Type compatibility (20%)
        from_type = str(from_meta.get("datatype", "string")).lower()
        to_type = str(to_meta.get("datatype", "string")).lower()
        if self._types_compatible(from_type, to_type):
            score += 0.20

        # 3) Key markers (30%)
        fk_markers = ("id", "key", "pk", "fk", "code", "num", "number")
        from_key = any(marker in from_col.lower() for marker in fk_markers)
        to_key = any(marker in to_col.lower() for marker in fk_markers)
        if from_key and to_key:
            score += 0.30
        elif from_key or to_key:
            score += 0.15

        # 4) Cardinality hint (10%)
        from_card = from_meta.get("cardinality")
        to_card = to_meta.get("cardinality")
        if isinstance(from_card, int) and isinstance(to_card, int) and from_card > 0 and to_card > 0:
            ratio = min(from_card, to_card) / max(from_card, to_card)
            score += 0.10 * ratio

        return min(score, 1.0)

    @staticmethod
    def _is_substring_match(col1: str, col2: str) -> bool:
        a = col1.lower()
        b = col2.lower()
        return len(a) > 2 and len(b) > 2 and (a in b or b in a)

    @staticmethod
    def _is_semantic_match(col1: str, col2: str) -> bool:
        suffixes = ("_id", "_key", "_pk", "_fk", "_ref")

        def normalize(name: str) -> str:
            n = name.lower()
            for suffix in suffixes:
                if n.endswith(suffix):
                    return n[: -len(suffix)]
            return n

        n1 = normalize(col1)
        n2 = normalize(col2)
        if n1 == n2:
            return True
        if n1 and n2 and (f"{n1}_id" == col2.lower() or f"{n2}_id" == col1.lower()):
            return True
        return False

    @staticmethod
    def _types_compatible(type1: str, type2: str) -> bool:
        numeric = {"int", "integer", "long", "float", "double", "decimal", "numeric", "int64"}
        text = {"string", "text", "varchar", "char"}

        if type1 == type2:
            return True
        if type1 in numeric and type2 in numeric:
            return True
        if type1 in text and type2 in text:
            return True
        if (type1 in numeric and type2 in text) or (type1 in text and type2 in numeric):
            return True
        return False

    @staticmethod
    def _infer_cardinality(from_col_meta: Dict, to_col_meta: Dict) -> str:
        from_pk = bool(from_col_meta.get("is_primary_key") or from_col_meta.get("is_unique"))
        to_pk = bool(to_col_meta.get("is_primary_key") or to_col_meta.get("is_unique"))

        if from_pk and to_pk:
            return "oneToOne"
        if to_pk:
            return "manyToOne"
        if from_pk:
            return "oneToMany"
        return "manyToMany"

    def _would_create_cycle(self, from_table: str, to_table: str) -> bool:
        graph: Dict[str, Set[str]] = defaultdict(set)
        for rel in self.relationships:
            graph[rel["fromTable"]].add(rel["toTable"])

        stack = [to_table]
        visited = set()
        while stack:
            node = stack.pop()
            if node == from_table:
                return True
            if node in visited:
                continue
            visited.add(node)
            stack.extend(graph.get(node, set()) - visited)
        return False

    @staticmethod
    def detect_many_to_many(table1_columns: Set[str], table2_columns: Set[str]) -> float:
        """Compute Jaccard similarity for many-to-many detection support."""
        if not table1_columns and not table2_columns:
            return 0.0
        union = table1_columns | table2_columns
        if not union:
            return 0.0
        intersection = table1_columns & table2_columns
        return len(intersection) / len(union)
