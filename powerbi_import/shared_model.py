"""
Shared Semantic Model — Core Merge Engine.

Merges multiple Tableau workbook extractions into a single unified
semantic model, enabling the Power BI "thin report" pattern where
N reports reference one shared semantic model.

Pipeline:
    1. Build table fingerprints (connection + physical name)
    2. Detect merge candidates (tables appearing in multiple workbooks)
    3. Merge tables (column union), measures, relationships, parameters, RLS
    4. Return a single merged converted_objects dict

Usage (programmatic)::

    from powerbi_import.shared_model import assess_merge, merge_semantic_models

    assessment = assess_merge(all_extracted, workbook_names)
    merged = merge_semantic_models(all_extracted, assessment, "SharedModel")

Usage (CLI)::

    python migrate.py --shared-model wb1.twbx wb2.twbx --model-name "Sales Analytics"
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  Data classes
# ═══════════════════════════════════════════════════════════════════

@dataclass
class TableFingerprint:
    """Uniquely identifies a physical table across workbooks."""
    connection_type: str
    server: str
    database: str
    schema: str
    table_name: str

    def fingerprint(self) -> str:
        """Normalized hash key for matching."""
        raw = '\x00'.join([
            self.connection_type.lower().strip(),
            self.server.lower().strip(),
            self.database.lower().strip(),
            self.schema.lower().strip(),
            self.table_name.lower().strip(),
        ])
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]

    def __eq__(self, other):
        if not isinstance(other, TableFingerprint):
            return NotImplemented
        return self.fingerprint() == other.fingerprint()

    def __hash__(self):
        return hash(self.fingerprint())


@dataclass
class MergeCandidate:
    """A table that appears in multiple workbooks."""
    fingerprint: TableFingerprint
    table_name: str
    sources: List[Tuple[str, dict, dict]]  # [(workbook_name, table_dict, connection_dict)]
    column_overlap: float = 0.0
    conflicts: List[str] = field(default_factory=list)


@dataclass
class MeasureConflict:
    """A measure with the same name but different DAX across workbooks."""
    name: str
    table: str
    variants: Dict[str, str]  # {workbook_name: formula}


@dataclass
class MergeAssessment:
    """Complete analysis of merge feasibility."""
    workbooks: List[str]
    merge_candidates: List[MergeCandidate] = field(default_factory=list)
    unique_tables: Dict[str, List[str]] = field(default_factory=dict)
    measure_conflicts: List[MeasureConflict] = field(default_factory=list)
    measure_duplicates_removed: int = 0
    relationship_duplicates_removed: int = 0
    parameter_conflicts: List[dict] = field(default_factory=list)
    parameter_duplicates_removed: int = 0
    rls_conflicts: List[dict] = field(default_factory=list)
    total_tables: int = 0
    unique_table_count: int = 0
    merge_score: int = 0
    recommendation: str = "separate"
    # Tables unique to one workbook but linked (via relationships or shared columns)
    linked_unique_tables: Dict[str, List[str]] = field(default_factory=dict)
    # Tables with no links to any other table in the merged model
    isolated_tables: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize to a JSON-friendly dictionary."""
        return {
            "workbooks": self.workbooks,
            "total_tables": self.total_tables,
            "unique_table_count": self.unique_table_count,
            "tables_saved": self.total_tables - self.unique_table_count,
            "merge_candidates": [
                {
                    "table_name": mc.table_name,
                    "workbooks": [s[0] for s in mc.sources],
                    "column_overlap": round(mc.column_overlap, 2),
                    "conflicts": mc.conflicts,
                }
                for mc in self.merge_candidates
            ],
            "unique_tables": self.unique_tables,
            "measure_conflicts": [
                {"name": mc.name, "table": mc.table, "variants": mc.variants}
                for mc in self.measure_conflicts
            ],
            "measure_duplicates_removed": self.measure_duplicates_removed,
            "relationship_duplicates_removed": self.relationship_duplicates_removed,
            "parameter_conflicts": self.parameter_conflicts,
            "parameter_duplicates_removed": self.parameter_duplicates_removed,
            "rls_conflicts": self.rls_conflicts,
            "merge_score": self.merge_score,
            "recommendation": self.recommendation,
            "linked_unique_tables": self.linked_unique_tables,
            "isolated_tables": self.isolated_tables,
        }


@dataclass
class MergeManifest:
    """Tracks merge provenance for incremental add/remove workflows.

    Written to ``merge_manifest.json`` after a merge completes, enabling
    subsequent ``--add-to-model`` and ``--remove-from-model`` operations
    without re-extracting all original workbooks.
    """
    model_name: str
    workbooks: List[dict] = field(default_factory=list)
    # workbook entry: {"name", "path", "hash", "tables", "measures", "exclusive_tables"}
    table_fingerprints: Dict[str, str] = field(default_factory=dict)
    # {table_name: fingerprint_hex}
    artifact_counts: dict = field(default_factory=dict)
    # {"tables", "measures", "relationships", "rls_roles", "parameters"}
    merge_config_snapshot: dict = field(default_factory=dict)
    validation_score: int = 0
    merge_score: int = 0
    timestamp: str = ""
    # None means a legacy/invalid manifest cannot prove whether scopes existed.
    # TMDL reverse-engineering cannot reconstruct retained source join scopes.
    physical_join_provenance_required: Optional[bool] = False

    def to_dict(self) -> dict:
        return {
            "schema_version": "1.0",
            "model_name": self.model_name,
            "workbooks": self.workbooks,
            "table_fingerprints": self.table_fingerprints,
            "artifact_counts": self.artifact_counts,
            "merge_config_snapshot": self.merge_config_snapshot,
            "validation_score": self.validation_score,
            "merge_score": self.merge_score,
            "timestamp": self.timestamp,
            "physical_join_provenance_required": self.physical_join_provenance_required,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'MergeManifest':
        return cls(
            model_name=data.get("model_name", ""),
            workbooks=data.get("workbooks", []),
            table_fingerprints=data.get("table_fingerprints", {}),
            artifact_counts=data.get("artifact_counts", {}),
            merge_config_snapshot=data.get("merge_config_snapshot", {}),
            validation_score=data.get("validation_score", 0),
            merge_score=data.get("merge_score", 0),
            timestamp=data.get("timestamp", ""),
            physical_join_provenance_required=(
                data["physical_join_provenance_required"]
                if isinstance(data.get("physical_join_provenance_required"), bool)
                else None
            ),
        )

    def save(self, output_dir: str) -> str:
        """Write merge_manifest.json to *output_dir*. Returns file path."""
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, "merge_manifest.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        return path

    @classmethod
    def load(cls, path: str) -> 'MergeManifest':
        """Load manifest from a JSON file or directory containing one."""
        if os.path.isdir(path):
            path = os.path.join(path, "merge_manifest.json")
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls.from_dict(data)

    def find_workbook(self, name: str) -> Optional[dict]:
        """Find a workbook entry by name (case-insensitive)."""
        lower = name.lower()
        for wb in self.workbooks:
            if wb.get("name", "").lower() == lower:
                return wb
        return None

    def exclusive_tables(self, workbook_name: str) -> List[str]:
        """Tables contributed solely by *workbook_name*."""
        wb = self.find_workbook(workbook_name)
        if not wb:
            return []
        return list(wb.get("exclusive_tables", []))


def build_merge_manifest(model_name: str,
                         all_extracted: List[dict],
                         workbook_names: List[str],
                         workbook_paths: Optional[List[str]],
                         merged: dict,
                         assessment: 'MergeAssessment',
                         validation_score: int = 0,
                         merge_config: Optional[dict] = None) -> MergeManifest:
    """Build a :class:`MergeManifest` from merge results.

    Args:
        model_name: Name of the shared semantic model.
        all_extracted: Per-workbook converted_objects list.
        workbook_names: Parallel list of workbook names.
        workbook_paths: Parallel list of workbook file paths (may be ``None``).
        merged: The merged ``converted_objects`` dict.
        assessment: The :class:`MergeAssessment`.
        validation_score: 0–100 from post-merge validation.
        merge_config: Optional merge config dict snapshot.

    Returns:
        A populated :class:`MergeManifest`.
    """
    # Collect per-workbook info
    wb_entries = []
    # Build set of shared table names (appear in >1 workbook)
    all_wb_tables: Dict[str, List[str]] = {}  # table_name -> [wb_names]
    for wb_name, extracted in zip(workbook_names, all_extracted):
        fps = build_table_fingerprints(extracted.get('datasources', []))
        for tname in fps:
            all_wb_tables.setdefault(tname, []).append(wb_name)

    shared_table_names = {t for t, wbs in all_wb_tables.items() if len(wbs) > 1}

    for idx, (wb_name, extracted) in enumerate(zip(workbook_names, all_extracted)):
        fps = build_table_fingerprints(extracted.get('datasources', []))
        wb_tables = list(fps.keys())
        exclusive = [t for t in wb_tables if t not in shared_table_names]

        # Count measures from this workbook's calculations
        wb_measures = [
            c for c in extracted.get('calculations', [])
            if c.get('_classification') == 'measure' or c.get('role') == 'measure'
        ]

        wb_path = ""
        if workbook_paths and idx < len(workbook_paths):
            wb_path = workbook_paths[idx]
        file_hash = _file_hash(wb_path) if wb_path and os.path.isfile(wb_path) else ""

        wb_entries.append({
            "name": wb_name,
            "path": wb_path,
            "hash": file_hash,
            "tables": wb_tables,
            "measures": [c.get('caption', c.get('name', '')) for c in wb_measures],
            "exclusive_tables": exclusive,
        })

    # Build table fingerprint map from merged datasources
    fp_map = {}
    for ds in merged.get('datasources', []):
        fps = build_table_fingerprints([ds])
        for tname, (fp, _, _) in fps.items():
            fp_map[tname] = fp.fingerprint()

    # Count artifacts in merged model
    merged_ds = merged.get('datasources', [{}])[0] if merged.get('datasources') else {}
    counts = {
        "tables": len(merged_ds.get('tables', [])),
        "measures": len([c for c in merged.get('calculations', [])
                        if c.get('_classification') == 'measure' or c.get('role') == 'measure']),
        "relationships": len(merged_ds.get('relationships', [])),
        "rls_roles": len(merged.get('user_filters', [])),
        "parameters": len(merged.get('parameters', [])),
    }

    return MergeManifest(
        model_name=model_name,
        workbooks=wb_entries,
        table_fingerprints=fp_map,
        artifact_counts=counts,
        merge_config_snapshot=merge_config or {},
        validation_score=validation_score,
        merge_score=assessment.merge_score,
        timestamp=datetime.now(timezone.utc).isoformat(),
        physical_join_provenance_required=_has_retained_physical_join_scopes(merged),
    )


def _has_retained_physical_join_scopes(merged: dict) -> bool:
    """Whether regeneration needs physical provenance not present in TMDL."""
    from powerbi_import.tmdl_relationships import _physical_join_scopes

    return any(_physical_join_scopes(ds) for ds in merged.get('datasources', []))


def _require_incremental_physical_provenance(manifest: MergeManifest) -> None:
    """Do not regenerate a scoped model from provenance-free TMDL."""
    if manifest.physical_join_provenance_required is False:
        return
    from powerbi_import.errors import PartialGenerationError

    if manifest.physical_join_provenance_required is None:
        detail = ("The merge manifest does not prove whether retained physical "
                  "source-join scopes existed.")
    else:
        detail = ("TMDL reconstruction does not retain the physical source-join "
                  "scopes needed to prevent inferred relationships.")
    raise PartialGenerationError(
        "Cannot incrementally modify this shared model: " + detail +
        " Regenerate from the original workbooks and their complete datasource "
        "provenance instead.",
        item='merge_manifest.physical_join_provenance_required',
        source=manifest.model_name,
        recovery_kind='physical_join_predicate',
        required_evidence=[
            'Original source join trees and unambiguous table/field bindings',
            'A provenance-preserving incremental regeneration path',
        ],
    )


def _file_hash(path: str) -> str:
    """SHA-256 hash of a file (first 16 hex chars)."""
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return ""


# ═══════════════════════════════════════════════════════════════════
#  Table fingerprinting
# ═══════════════════════════════════════════════════════════════════

_SCHEMA_PATTERN = re.compile(r'^\[?([^\]]*)\]?\.\[?([^\]]*)\]?$')


def _parse_table_name(raw_name: str) -> Tuple[str, str]:
    """Parse '[schema].[table]' or 'table' into (schema, table)."""
    m = _SCHEMA_PATTERN.match(raw_name.strip())
    if m:
        return m.group(1), m.group(2)
    # No schema prefix
    clean = raw_name.strip().strip('[]')
    return 'dbo', clean


def build_table_fingerprints(datasources: list) -> Dict[str, Tuple[TableFingerprint, dict, dict]]:
    """Build fingerprints for all tables in a workbook's datasources.

    Handles both physical tables (connection+schema+name) and custom SQL
    tables (connection+normalized SQL hash).

    Returns:
        {physical_table_name: (fingerprint, table_dict, connection_dict)}
    """
    result = {}
    for ds in datasources:
        conn = ds.get('connection', {})
        conn_type = conn.get('type', 'unknown')
        details = conn.get('details', {})
        server = details.get('server', details.get('host', ''))
        database = details.get('database', details.get('db', ''))

        for table in ds.get('tables', []):
            ttype = table.get('type', 'table')
            raw_name = table.get('name', '')

            # Custom SQL tables: fingerprint by normalized SQL hash
            sql = table.get('custom_sql', table.get('query', ''))
            if sql:
                sql_hash = _hash_sql(sql)
                fp = TableFingerprint(
                    connection_type=conn_type,
                    server=server,
                    database=database,
                    schema='_custom_sql',
                    table_name=sql_hash,
                )
                result[raw_name] = (fp, table, conn)
                continue

            if ttype != 'table':
                continue
            schema, tname = _parse_table_name(raw_name)
            fp = TableFingerprint(
                connection_type=conn_type,
                server=server,
                database=database,
                schema=schema,
                table_name=tname,
            )
            result[raw_name] = (fp, table, conn)
    return result


# ═══════════════════════════════════════════════════════════════════
#  Column overlap
# ═══════════════════════════════════════════════════════════════════

def compute_column_overlap(table_a: dict, table_b: dict) -> float:
    """Compute Jaccard similarity of column names between two tables.

    Returns:
        Float 0.0–1.0 representing column name overlap.
    """
    cols_a = {c.get('name', '').lower() for c in table_a.get('columns', [])}
    cols_b = {c.get('name', '').lower() for c in table_b.get('columns', [])}

    if not cols_a and not cols_b:
        return 0.0

    intersection = cols_a & cols_b
    union = cols_a | cols_b

    if not union:
        return 0.0

    return len(intersection) / len(union)


# ═══════════════════════════════════════════════════════════════════
#  Merge assessment
# ═══════════════════════════════════════════════════════════════════

def assess_merge(all_extracted: List[dict],
                 workbook_names: List[str]) -> MergeAssessment:
    """Analyze multiple extracted workbook datasets for merge potential.

    Args:
        all_extracted: List of converted_objects dicts (one per workbook)
        workbook_names: List of workbook names (parallel with all_extracted)

    Returns:
        MergeAssessment with full analysis.
    """
    assessment = MergeAssessment(workbooks=list(workbook_names))

    # 1. Build fingerprints for all tables in all workbooks
    # fingerprint_hash → [(workbook_name, table_dict, connection_dict, raw_table_name)]
    fp_map: Dict[str, list] = {}
    all_table_count = 0

    for wb_name, extracted in zip(workbook_names, all_extracted):
        datasources = extracted.get('datasources', [])
        fps = build_table_fingerprints(datasources)
        for raw_name, (fp, table, conn) in fps.items():
            all_table_count += 1
            fp_hash = fp.fingerprint()
            if fp_hash not in fp_map:
                fp_map[fp_hash] = []
            fp_map[fp_hash].append((wb_name, table, conn, raw_name, fp))

    assessment.total_tables = all_table_count

    # 2. Identify merge candidates (tables appearing in 2+ workbooks)
    seen_tables = set()
    unique_per_workbook: Dict[str, List[str]] = {wb: [] for wb in workbook_names}

    for fp_hash, entries in fp_map.items():
        unique_wbs = {e[0] for e in entries}
        table_name = entries[0][3]  # raw_name from first entry
        fp = entries[0][4]

        if len(unique_wbs) >= 2:
            # Compute column overlap (pairwise — use first pair)
            overlap = 1.0
            if len(entries) >= 2:
                overlap = compute_column_overlap(entries[0][1], entries[1][1])

            # Check for column type conflicts
            conflicts = _detect_column_conflicts(entries)

            candidate = MergeCandidate(
                fingerprint=fp,
                table_name=table_name,
                sources=[(e[0], e[1], e[2]) for e in entries],
                column_overlap=overlap,
                conflicts=conflicts,
            )
            assessment.merge_candidates.append(candidate)
            seen_tables.add(fp_hash)
        else:
            # Unique to one workbook
            wb_name = entries[0][0]
            unique_per_workbook[wb_name].append(table_name)

    assessment.unique_tables = {
        wb: tables for wb, tables in unique_per_workbook.items() if tables
    }
    assessment.unique_table_count = (
        len(assessment.merge_candidates) +
        sum(len(v) for v in assessment.unique_tables.values())
    )

    # 3. Detect measure conflicts
    measure_conflicts, measure_dupes = _detect_measure_conflicts(all_extracted, workbook_names)
    assessment.measure_conflicts = measure_conflicts
    assessment.measure_duplicates_removed = measure_dupes

    # 4. Detect relationship duplicates
    assessment.relationship_duplicates_removed = _count_relationship_duplicates(
        all_extracted
    )

    # 5. Detect parameter conflicts
    param_conflicts, param_dupes = _detect_parameter_conflicts(all_extracted, workbook_names)
    assessment.parameter_conflicts = param_conflicts
    assessment.parameter_duplicates_removed = param_dupes

    # 5b. Classify unique tables as linked vs isolated
    _classify_unique_tables(assessment, all_extracted, workbook_names, fp_map)

    # 6. Calculate merge score
    assessment.merge_score = calculate_merge_score(assessment)

    # 7. Set recommendation
    if assessment.merge_score >= 60:
        assessment.recommendation = "merge"
    elif assessment.merge_score >= 30:
        assessment.recommendation = "partial"
    else:
        assessment.recommendation = "separate"

    return assessment


def _detect_column_conflicts(entries: list) -> List[str]:
    """Detect column type mismatches across workbook instances of the same table."""
    conflicts = []
    # Build col_name → {wb_name: datatype}
    col_types: Dict[str, Dict[str, str]] = {}
    for wb_name, table, _conn, _raw, _fp in entries:
        for col in table.get('columns', []):
            cname = col.get('name', '')
            dtype = col.get('datatype', 'string')
            if cname not in col_types:
                col_types[cname] = {}
            col_types[cname][wb_name] = dtype

    for cname, wb_types in col_types.items():
        unique_types = set(wb_types.values())
        if len(unique_types) > 1:
            conflicts.append(
                f"Column '{cname}': type mismatch — " +
                ", ".join(f"{wb}={dt}" for wb, dt in wb_types.items())
            )

    return conflicts


def _detect_measure_conflicts(
    all_extracted: List[dict], workbook_names: List[str]
) -> Tuple[List[MeasureConflict], int]:
    """Detect measure conflicts and count deduplicatable measures."""
    # measure caption → {wb_name: formula}
    measure_map: Dict[str, Dict[str, str]] = {}

    for wb_name, extracted in zip(workbook_names, all_extracted):
        for ds in extracted.get('datasources', []):
            for calc in ds.get('calculations', []):
                role = calc.get('role', 'measure')
                if role != 'measure':
                    continue
                caption = calc.get('caption', calc.get('name', ''))
                formula = calc.get('formula', '').strip()
                key = caption
                if key not in measure_map:
                    measure_map[key] = {}
                measure_map[key][wb_name] = formula

        # Also check standalone calculations
        for calc in extracted.get('calculations', []):
            role = calc.get('role', 'measure')
            if role != 'measure':
                continue
            caption = calc.get('caption', calc.get('name', ''))
            formula = calc.get('formula', '').strip()
            key = caption
            if key not in measure_map:
                measure_map[key] = {}
            measure_map[key][wb_name] = formula

    conflicts = []
    duplicates = 0

    for name, wb_formulas in measure_map.items():
        if len(wb_formulas) <= 1:
            continue
        unique_formulas = set(wb_formulas.values())
        if len(unique_formulas) == 1:
            duplicates += 1
        else:
            conflicts.append(MeasureConflict(
                name=name, table='', variants=wb_formulas
            ))

    return conflicts, duplicates


def _count_relationship_duplicates(all_extracted: List[dict]) -> int:
    """Count relationship definitions that appear in multiple workbooks."""
    rel_keys = set()
    duplicates = 0
    for extracted in all_extracted:
        for ds in extracted.get('datasources', []):
            for rel in ds.get('relationships', []):
                key = _relationship_key(rel)
                if key in rel_keys:
                    duplicates += 1
                else:
                    rel_keys.add(key)
    return duplicates


def _relationship_key(rel: dict) -> Tuple[str, str, str, str]:
    """Deduplicate key for a relationship."""
    # Handle different relationship formats
    if 'left' in rel:
        return (
            rel['left'].get('table', '').lower(),
            rel['left'].get('column', '').lower(),
            rel['right'].get('table', '').lower(),
            rel['right'].get('column', '').lower(),
        )
    return (
        rel.get('from_table', '').lower(),
        rel.get('from_column', '').lower(),
        rel.get('to_table', '').lower(),
        rel.get('to_column', '').lower(),
    )


# ═══════════════════════════════════════════════════════════════════
#  Post-merge safety validation (Sprint 55)
# ═══════════════════════════════════════════════════════════════════

# Column type compatibility matrix: (from_type, to_type) → level
# Levels: "ok" (safe promotion), "warn" (lossy), "error" (incompatible)
_TYPE_COMPAT: Dict[Tuple[str, str], str] = {}

_TYPE_NAMES = ['boolean', 'integer', 'int64', 'real', 'double', 'decimal',
               'currency', 'datetime', 'string']

# Build compatibility — wider type absorbs narrower
_SAFE_PROMOTIONS = {
    ('boolean', 'integer'), ('boolean', 'int64'), ('boolean', 'real'),
    ('boolean', 'double'), ('boolean', 'decimal'), ('boolean', 'currency'),
    ('boolean', 'string'),
    ('integer', 'int64'), ('integer', 'real'), ('integer', 'double'),
    ('integer', 'decimal'), ('integer', 'currency'), ('integer', 'string'),
    ('int64', 'real'), ('int64', 'double'), ('int64', 'decimal'),
    ('int64', 'currency'), ('int64', 'string'),
    ('real', 'double'), ('real', 'decimal'), ('real', 'string'),
    ('double', 'decimal'), ('double', 'string'),
    ('decimal', 'string'), ('currency', 'string'),
    ('currency', 'real'), ('currency', 'double'), ('currency', 'decimal'),
    ('datetime', 'string'),
}

for _a in _TYPE_NAMES:
    _TYPE_COMPAT[(_a, _a)] = 'ok'
for _pair in _SAFE_PROMOTIONS:
    _TYPE_COMPAT[_pair] = 'ok'
    _TYPE_COMPAT[(_pair[1], _pair[0])] = 'ok'  # reverse is also ok (wider wins)

_WARN_PROMOTIONS = {
    ('string', 'integer'), ('string', 'int64'), ('string', 'real'),
    ('string', 'double'), ('string', 'decimal'), ('string', 'boolean'),
    ('string', 'currency'),
}
for _pair in _WARN_PROMOTIONS:
    key = (_pair[0], _pair[1])
    rev = (_pair[1], _pair[0])
    if key not in _TYPE_COMPAT:
        _TYPE_COMPAT[key] = 'warn'
    if rev not in _TYPE_COMPAT:
        _TYPE_COMPAT[rev] = 'warn'

_ERROR_PROMOTIONS = {
    ('datetime', 'boolean'), ('datetime', 'integer'), ('datetime', 'int64'),
    ('datetime', 'real'), ('datetime', 'double'), ('datetime', 'decimal'),
    ('datetime', 'currency'),
}
for _pair in _ERROR_PROMOTIONS:
    key = (_pair[0], _pair[1])
    rev = (_pair[1], _pair[0])
    if key not in _TYPE_COMPAT:
        _TYPE_COMPAT[key] = 'error'
    if rev not in _TYPE_COMPAT:
        _TYPE_COMPAT[rev] = 'error'


def check_type_compatibility(type_a: str, type_b: str) -> str:
    """Check compatibility between two column types.

    Returns:
        'ok', 'warn', or 'error'.
    """
    a = type_a.lower().strip()
    b = type_b.lower().strip()
    if a == b:
        return 'ok'
    return _TYPE_COMPAT.get((a, b), 'warn')


def detect_merge_cycles(merged: dict) -> List[List[str]]:
    """Detect circular relationships in merged model using iterative DFS.

    Args:
        merged: The merged converted_objects dict.

    Returns:
        List of cycles, each cycle is a list of table names forming the loop.
    """
    # Build directed graph from relationships
    graph: Dict[str, set] = {}
    for ds in merged.get('datasources', []):
        for rel in ds.get('relationships', []):
            if 'left' in rel:
                from_t = rel['left'].get('table', '')
                to_t = rel['right'].get('table', '')
            else:
                from_t = rel.get('from_table', '')
                to_t = rel.get('to_table', '')
            if from_t and to_t:
                graph.setdefault(from_t, set()).add(to_t)

    # Iterative DFS with explicit stack
    cycles: List[List[str]] = []
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {n: WHITE for n in graph}
    # Also include nodes that are only targets
    for targets in list(graph.values()):
        for t in targets:
            if t not in color:
                color[t] = WHITE

    parent: Dict[str, Optional[str]] = {}

    for start in list(color.keys()):
        if color[start] != WHITE:
            continue
        stack = [(start, False)]  # (node, backtrack_flag)
        while stack:
            node, backtrack = stack.pop()
            if backtrack:
                color[node] = BLACK
                continue
            if color[node] == GRAY:
                continue
            color[node] = GRAY
            stack.append((node, True))  # schedule backtrack
            for neighbor in graph.get(node, set()):
                if color.get(neighbor) == GRAY:
                    # Found cycle — reconstruct path
                    cycle = [neighbor]
                    cur = node
                    while cur != neighbor:
                        cycle.append(cur)
                        cur = parent.get(cur, neighbor)
                    cycle.append(neighbor)
                    cycle.reverse()
                    cycles.append(cycle)
                elif color.get(neighbor, WHITE) == WHITE:
                    parent[neighbor] = node
                    stack.append((neighbor, False))

    return cycles


def detect_type_conflicts(merged: dict) -> List[dict]:
    """Detect column type conflicts across merged tables.

    Compares each table's columns against the type compatibility matrix.
    Only relevant for tables that were merged (have columns from multiple
    workbooks, tracked via _merge_columns_into).

    Returns:
        List of {table, column, types, level} dicts.
    """
    warnings = []
    for ds in merged.get('datasources', []):
        for table in ds.get('tables', []):
            table_name = table.get('name', '')
            type_history = table.get('_column_type_history', {})
            for col_name, types in type_history.items():
                if len(types) < 2:
                    continue
                unique_types = list(set(types))
                if len(unique_types) < 2:
                    continue
                # Check worst compatibility across all pairs
                worst = 'ok'
                for i in range(len(unique_types)):
                    for j in range(i + 1, len(unique_types)):
                        level = check_type_compatibility(
                            unique_types[i], unique_types[j]
                        )
                        if level == 'error':
                            worst = 'error'
                        elif level == 'warn' and worst != 'error':
                            worst = 'warn'
                if worst != 'ok':
                    warnings.append({
                        'table': table_name,
                        'column': col_name,
                        'types': unique_types,
                        'level': worst,
                    })
    return warnings


# Regex to extract 'Table'[Column] references from DAX
_RE_DAX_TABLE_COL = re.compile(
    r"'([^']+)'\[([^\]]+)\]"
)
# Regex to extract bare [Column] references
_RE_DAX_BARE_COL = re.compile(
    r"(?<!')\[([^\]]+)\]"
)


def validate_merged_dax_references(merged: dict) -> List[dict]:
    """Scan all measures and calc columns for unresolved 'Table'[Column] refs.

    Args:
        merged: The merged converted_objects dict.

    Returns:
        List of {source, source_type, ref, table, column, status, suggestion} dicts.
    """
    # Build inventory of tables and columns
    table_names: set = set()
    table_columns: Dict[str, set] = {}
    for ds in merged.get('datasources', []):
        for table in ds.get('tables', []):
            tname = table.get('name', '')
            table_names.add(tname)
            cols = {c.get('name', '') for c in table.get('columns', [])}
            table_columns[tname] = cols

    # Also add parameter tables from merged parameters
    for param in merged.get('parameters', []):
        pname = param.get('caption', param.get('name', ''))
        if pname:
            table_names.add(pname)

    errors: List[dict] = []

    # Scan calculations (measures + calc columns)
    for calc in merged.get('calculations', []):
        formula = calc.get('dax_formula', calc.get('formula', ''))
        if not formula:
            continue
        calc_name = calc.get('caption', calc.get('name', ''))
        calc_type = calc.get('classification', 'measure')

        for match in _RE_DAX_TABLE_COL.finditer(formula):
            ref_table = match.group(1)
            ref_col = match.group(2)
            status = 'ok'
            suggestion = None

            if ref_table not in table_names:
                status = 'error_table'
                suggestion = _find_closest(ref_table, table_names)
            elif ref_col not in table_columns.get(ref_table, set()):
                status = 'error_column'
                suggestion = _find_closest(
                    ref_col, table_columns.get(ref_table, set())
                )

            if status != 'ok':
                errors.append({
                    'source': calc_name,
                    'source_type': calc_type,
                    'ref': f"'{ref_table}'[{ref_col}]",
                    'table': ref_table,
                    'column': ref_col,
                    'status': status,
                    'suggestion': suggestion,
                })

    return errors


def validate_dax_relationship_functions(merged: dict) -> List[dict]:
    """Verify RELATED() / LOOKUPVALUE() usage matches relationship cardinality.

    RELATED() requires a manyToOne relationship on the referenced path.
    LOOKUPVALUE() is used for manyToMany.

    Returns:
        List of {source, function, ref_table, ref_column, expected, actual, status}.
    """
    # Build relationship cardinality map: (from_table, to_table) -> cardinality
    cardinality_map: Dict[Tuple[str, str], str] = {}
    # Also build reverse map for RELATED lookups
    for ds in merged.get('datasources', []):
        for rel in ds.get('relationships', []):
            if 'left' in rel:
                ft = rel['left'].get('table', '')
                tt = rel['right'].get('table', '')
            else:
                ft = rel.get('from_table', '')
                tt = rel.get('to_table', '')
            card = rel.get('cardinality', 'manyToOne')
            if ft and tt:
                cardinality_map[(ft, tt)] = card
                cardinality_map[(tt, ft)] = card

    mismatches: List[dict] = []

    _RE_RELATED = re.compile(r"RELATED\s*\(\s*'([^']+)'\[([^\]]+)\]")
    _RE_LOOKUPVALUE = re.compile(
        r"LOOKUPVALUE\s*\(\s*'([^']+)'\[([^\]]+)\]"
    )

    for calc in merged.get('calculations', []):
        formula = calc.get('dax_formula', calc.get('formula', ''))
        if not formula:
            continue
        calc_name = calc.get('caption', calc.get('name', ''))

        # Check RELATED() calls — should be manyToOne
        for m in _RE_RELATED.finditer(formula):
            ref_table = m.group(1)
            ref_col = m.group(2)
            # Find if any relationship path exists to ref_table
            related_cards = [
                v for (k, v) in cardinality_map.items()
                if k[1] == ref_table
            ]
            if not related_cards:
                mismatches.append({
                    'source': calc_name,
                    'function': 'RELATED',
                    'ref_table': ref_table,
                    'ref_column': ref_col,
                    'expected': 'manyToOne',
                    'actual': 'no_relationship',
                    'status': 'error',
                })
            elif all(c == 'manyToMany' for c in related_cards):
                mismatches.append({
                    'source': calc_name,
                    'function': 'RELATED',
                    'ref_table': ref_table,
                    'ref_column': ref_col,
                    'expected': 'manyToOne',
                    'actual': 'manyToMany',
                    'status': 'warning',
                })

        # Check LOOKUPVALUE() calls — used when manyToMany
        for m in _RE_LOOKUPVALUE.finditer(formula):
            ref_table = m.group(1)
            ref_col = m.group(2)
            related_cards = [
                v for (k, v) in cardinality_map.items()
                if k[1] == ref_table
            ]
            if related_cards and all(c == 'manyToOne' for c in related_cards):
                mismatches.append({
                    'source': calc_name,
                    'function': 'LOOKUPVALUE',
                    'ref_table': ref_table,
                    'ref_column': ref_col,
                    'expected': 'manyToMany',
                    'actual': 'manyToOne',
                    'status': 'info',
                })

    return mismatches


def generate_merge_validation_report(merged: dict) -> dict:
    """Run all post-merge safety checks and produce a summary report.

    Returns:
        {
            'cycles': [...],
            'type_warnings': [...],
            'dax_errors': [...],
            'cardinality_mismatches': [...],
            'counts': {cycles, type_errors, type_warnings, dax_errors, cardinality},
            'score': int (0-100, higher is better),
            'passed': bool,
        }
    """
    cycles = detect_merge_cycles(merged)
    type_warnings = detect_type_conflicts(merged)
    dax_errors = validate_merged_dax_references(merged)
    cardinality = validate_dax_relationship_functions(merged)

    # Counts
    n_cycles = len(cycles)
    n_type_errors = sum(1 for w in type_warnings if w['level'] == 'error')
    n_type_warns = sum(1 for w in type_warnings if w['level'] == 'warn')
    n_dax_errors = len(dax_errors)
    n_card = sum(1 for c in cardinality if c['status'] in ('error', 'warning'))

    # Score: start at 100, deduct for issues
    score = 100
    score -= n_cycles * 25  # cycles are critical
    score -= n_type_errors * 15
    score -= n_type_warns * 5
    score -= n_dax_errors * 10
    score -= n_card * 5
    score = max(0, score)

    passed = n_cycles == 0 and n_type_errors == 0

    return {
        'cycles': cycles,
        'type_warnings': type_warnings,
        'dax_errors': dax_errors,
        'cardinality_mismatches': cardinality,
        'counts': {
            'cycles': n_cycles,
            'type_errors': n_type_errors,
            'type_warnings': n_type_warns,
            'dax_errors': n_dax_errors,
            'cardinality_mismatches': n_card,
        },
        'score': score,
        'passed': passed,
    }


def _find_closest(name: str, candidates: set) -> Optional[str]:
    """Find the closest match for *name* in *candidates* (Levenshtein-like)."""
    if not candidates:
        return None
    name_lower = name.lower()
    best = None
    best_score = 999
    for c in candidates:
        c_lower = c.lower()
        # Simple edit distance approximation: shared prefix + length diff
        common = 0
        for a, b in zip(name_lower, c_lower):
            if a == b:
                common += 1
            else:
                break
        dist = abs(len(name_lower) - len(c_lower)) + (max(len(name_lower), len(c_lower)) - common)
        if dist < best_score:
            best_score = dist
            best = c
    # Only suggest if reasonably close
    if best_score <= max(3, len(name) // 2):
        return best
    return None


def _detect_parameter_conflicts(
    all_extracted: List[dict], workbook_names: List[str]
) -> Tuple[List[dict], int]:
    """Detect parameter conflicts and count deduplicatable parameters."""
    param_map: Dict[str, Dict[str, dict]] = {}
    for wb_name, extracted in zip(workbook_names, all_extracted):
        for param in extracted.get('parameters', []):
            pname = param.get('name', param.get('caption', ''))
            if pname not in param_map:
                param_map[pname] = {}
            param_map[pname][wb_name] = param

    conflicts = []
    duplicates = 0

    for pname, wb_params in param_map.items():
        if len(wb_params) <= 1:
            continue
        # Compare domain_type and datatype
        signatures = set()
        for wb, p in wb_params.items():
            sig = (p.get('datatype', ''), p.get('domain_type', ''),
                   str(p.get('current_value', '')))
            signatures.add(sig)
        if len(signatures) == 1:
            duplicates += 1
        else:
            conflicts.append({
                "name": pname,
                "variants": {
                    wb: {
                        "datatype": p.get('datatype'),
                        "domain_type": p.get('domain_type'),
                        "current_value": p.get('current_value'),
                    }
                    for wb, p in wb_params.items()
                },
            })

    return conflicts, duplicates


# ═══════════════════════════════════════════════════════════════════
#  Table linkage classification
# ═══════════════════════════════════════════════════════════════════

def _classify_unique_tables(assessment: MergeAssessment,
                            all_extracted: List[dict],
                            workbook_names: List[str],
                            fp_map: Dict[str, list]):
    """Classify unique tables as linked (have relationships to other tables) or isolated.

    A unique table is "linked" if:
    - It participates in a relationship with another table in the merged model, OR
    - It shares column names with a shared (merge-candidate) table (suggesting a join key)

    A unique table is "isolated" if none of the above apply — meaning it has no
    connection to any other workbook's data and should NOT be included in the merged model.
    """
    # Build the set of all table names in the merged model (shared + unique)
    shared_table_names = set()
    for mc in assessment.merge_candidates:
        shared_table_names.add(_normalize_table_key(mc.table_name))

    # Collect all relationship endpoints from all workbooks
    all_rel_tables = set()  # normalized table names that appear in relationships
    rel_pairs = []  # (table_a, table_b) pairs
    for extracted in all_extracted:
        for ds in extracted.get('datasources', []):
            for rel in ds.get('relationships', []):
                if 'left' in rel:
                    t_a = _normalize_table_key(rel['left'].get('table', ''))
                    t_b = _normalize_table_key(rel['right'].get('table', ''))
                else:
                    t_a = _normalize_table_key(rel.get('from_table', ''))
                    t_b = _normalize_table_key(rel.get('to_table', ''))
                if t_a and t_b:
                    all_rel_tables.add(t_a)
                    all_rel_tables.add(t_b)
                    rel_pairs.append((t_a, t_b))

    # Collect column sets for shared tables (for column-name-based linkage detection)
    shared_columns = set()
    for mc in assessment.merge_candidates:
        for _wb_name, table, _conn in mc.sources:
            for col in table.get('columns', []):
                shared_columns.add(col.get('name', '').lower())

    # Classify each unique table
    linked: Dict[str, List[str]] = {}
    isolated: Dict[str, List[str]] = {}

    for wb_name, table_names in assessment.unique_tables.items():
        for tname in table_names:
            norm_key = _normalize_table_key(tname)
            is_linked = False

            # Check 1: Does this table participate in a relationship with another table?
            for t_a, t_b in rel_pairs:
                if t_a == norm_key and t_b != norm_key:
                    # The other table exists in the model (shared or from same workbook)
                    if t_b in shared_table_names:
                        is_linked = True
                        break
                elif t_b == norm_key and t_a != norm_key:
                    if t_a in shared_table_names:
                        is_linked = True
                        break

            # Check 2: Does this table share column names with any shared table?
            # (suggesting a potential join key like customer_id, order_id, etc.)
            if not is_linked:
                unique_cols = _get_table_columns(tname, wb_name, all_extracted, workbook_names)
                # Require at least one column name match AND the column looks like a key
                key_patterns = {'id', '_id', 'key', '_key', 'code', '_code', 'no', '_no', 'num'}
                for col in unique_cols:
                    col_lower = col.lower()
                    if col_lower in shared_columns:
                        # Check if it looks like a join key (word-boundary match)
                        if any(
                            col_lower == p
                            or col_lower.endswith(f'_{p}')
                            or col_lower.startswith(f'{p}_')
                            for p in key_patterns
                        ):
                            is_linked = True
                            break

            if is_linked:
                linked.setdefault(wb_name, []).append(tname)
            else:
                isolated.setdefault(wb_name, []).append(tname)

    assessment.linked_unique_tables = linked
    assessment.isolated_tables = isolated


def _normalize_table_key(name: str) -> str:
    """Normalize a table name for comparison."""
    # Strip schema prefix and brackets
    clean = name.strip().strip('[]')
    if '.' in clean:
        parts = clean.rsplit('.', 1)
        clean = parts[-1].strip('[]')
    return clean.lower()


def _get_table_columns(table_name: str, wb_name: str,
                       all_extracted: List[dict],
                       workbook_names: List[str]) -> List[str]:
    """Get column names for a specific table in a specific workbook."""
    idx = workbook_names.index(wb_name) if wb_name in workbook_names else -1
    if idx < 0:
        return []
    extracted = all_extracted[idx]
    norm_target = _normalize_table_key(table_name)
    for ds in extracted.get('datasources', []):
        for table in ds.get('tables', []):
            if _normalize_table_key(table.get('name', '')) == norm_target:
                return [c.get('name', '') for c in table.get('columns', [])]
    return []


# ═══════════════════════════════════════════════════════════════════
#  Merge score
# ═══════════════════════════════════════════════════════════════════

def calculate_merge_score(assessment: MergeAssessment) -> int:
    """Calculate a 0-100 merge score.

    Dimensions:
        - Table overlap ratio (0-40 points)
        - Column match quality (0-20 points)
        - Measure conflict ratio (0-20 points)
        - Connection homogeneity (0-20 points)
    """
    score = 0

    # 1. Table overlap (0-40)
    if assessment.total_tables > 0:
        tables_saved = assessment.total_tables - assessment.unique_table_count
        overlap_ratio = tables_saved / assessment.total_tables
        score += int(overlap_ratio * 40)

    # 2. Column match quality (0-20)
    if assessment.merge_candidates:
        avg_overlap = sum(
            mc.column_overlap for mc in assessment.merge_candidates
        ) / len(assessment.merge_candidates)
        score += int(avg_overlap * 20)

    # 3. Measure conflict ratio (0-20) — fewer conflicts = higher score
    total_measures = (
        assessment.measure_duplicates_removed + len(assessment.measure_conflicts)
    )
    if total_measures > 0:
        clean_ratio = assessment.measure_duplicates_removed / total_measures
        score += int(clean_ratio * 20)
    else:
        score += 20  # No measures = no conflicts

    # 4. Connection homogeneity (0-20)
    if assessment.merge_candidates:
        conn_types = set()
        for mc in assessment.merge_candidates:
            conn_types.add(mc.fingerprint.connection_type.lower())
        if len(conn_types) == 1:
            score += 20
        elif len(conn_types) == 2:
            score += 10
        else:
            score += 5

    return min(score, 100)


# ═══════════════════════════════════════════════════════════════════
#  Lineage extraction
# ═══════════════════════════════════════════════════════════════════

def extract_lineage(merged: dict) -> List[dict]:
    """Extract lineage metadata from a merged converted_objects dict.

    Returns a list of lineage records, each with:
        name, type, source_workbooks, merge_action
    """
    records: List[dict] = []

    # Tables
    for ds in merged.get('datasources', []):
        for table in ds.get('tables', []):
            records.append({
                'name': table.get('name', ''),
                'type': 'table',
                'source_workbooks': table.get('_source_workbooks', []),
                'merge_action': table.get('_merge_action', ''),
            })
        # Datasource-level calculations
        for calc in ds.get('calculations', []):
            records.append({
                'name': calc.get('caption', calc.get('name', '')),
                'type': 'ds_calculation',
                'source_workbooks': calc.get('_source_workbooks', []),
                'merge_action': calc.get('_merge_action', ''),
            })
        # Relationships
        for rel in ds.get('relationships', []):
            from_t = rel.get('from_table', '')
            to_t = rel.get('to_table', '')
            records.append({
                'name': f"{from_t} → {to_t}",
                'type': 'relationship',
                'source_workbooks': rel.get('_source_workbooks', []),
                'merge_action': rel.get('_merge_action', ''),
            })

    # Standalone calculations
    for calc in merged.get('calculations', []):
        records.append({
            'name': calc.get('caption', calc.get('name', '')),
            'type': 'calculation',
            'source_workbooks': calc.get('_source_workbooks', []),
            'merge_action': calc.get('_merge_action', ''),
        })

    # Parameters
    for param in merged.get('parameters', []):
        records.append({
            'name': param.get('name', param.get('caption', '')),
            'type': 'parameter',
            'source_workbooks': param.get('_source_workbooks', []),
            'merge_action': param.get('_merge_action', ''),
        })

    # Hierarchies
    for hier in merged.get('hierarchies', []):
        records.append({
            'name': hier.get('name', ''),
            'type': 'hierarchy',
            'source_workbooks': hier.get('_source_workbooks', []),
            'merge_action': hier.get('_merge_action', ''),
        })

    # Simple list-based artifacts
    for key in ('sets', 'groups', 'bins', 'user_filters', 'filters',
                'stories', 'actions', 'sort_orders', 'custom_sql'):
        for item in merged.get(key, []):
            records.append({
                'name': item.get('name', ''),
                'type': key.rstrip('s'),  # e.g., 'set', 'group'
                'source_workbooks': item.get('_source_workbooks', []),
                'merge_action': item.get('_merge_action', ''),
            })

    # Advanced artifacts
    for cg in merged.get('_calculation_groups', []):
        records.append({
            'name': cg.get('caption', ''),
            'type': 'calculation_group',
            'source_workbooks': cg.get('_source_workbooks', []),
            'merge_action': cg.get('_merge_action', ''),
        })

    for fp in merged.get('_field_parameters', []):
        records.append({
            'name': fp.get('caption', ''),
            'type': 'field_parameter',
            'source_workbooks': fp.get('_source_workbooks', []),
            'merge_action': fp.get('_merge_action', ''),
        })

    for persp in merged.get('_perspectives', []):
        records.append({
            'name': persp.get('name', ''),
            'type': 'perspective',
            'source_workbooks': persp.get('_source_workbooks', []),
            'merge_action': persp.get('_merge_action', ''),
        })

    for culture in merged.get('_cultures', []):
        records.append({
            'name': culture.get('locale', ''),
            'type': 'culture',
            'source_workbooks': culture.get('_source_workbooks', []),
            'merge_action': culture.get('_merge_action', ''),
        })

    for goal in merged.get('_goals', []):
        records.append({
            'name': goal.get('name', goal.get('metric_name', '')),
            'type': 'goal',
            'source_workbooks': goal.get('_source_workbooks', []),
            'merge_action': goal.get('_merge_action', ''),
        })

    return records


# ═══════════════════════════════════════════════════════════════════
#  Semantic model merging
# ═══════════════════════════════════════════════════════════════════

def merge_semantic_models(all_extracted: List[dict],
                          assessment: MergeAssessment,
                          model_name: str) -> dict:
    """Merge multiple workbook extractions into a single unified dataset.

    Returns:
        A single converted_objects dict with merged datasources, calculations,
        parameters, relationships, etc. suitable for tmdl_generator.generate_tmdl().
    """
    merged = {
        'datasources': [],
        'worksheets': [],
        'dashboards': [],
        'calculations': [],
        'parameters': [],
        'filters': [],
        'stories': [],
        'actions': [],
        'sets': [],
        'groups': [],
        'bins': [],
        'hierarchies': [],
        'sort_orders': [],
        'aliases': {},
        'custom_sql': [],
        'user_filters': [],
    }

    workbook_names = assessment.workbooks

    # Inspect original calculation identities and target scopes before either
    # calculation inventory is collapsed by the shared merge.
    calculation_scopes = _preflight_calculation_identities(
        all_extracted, workbook_names, assessment)

    # 1. Merge datasources (table-level deduplication)
    merged_datasource = _merge_datasources(
        all_extracted, workbook_names, assessment, calculation_scopes
    )
    merged['datasources'] = [merged_datasource]

    # 2. Merge calculations with conflict resolution
    merged['calculations'] = _merge_calculations(
        all_extracted, workbook_names, assessment, calculation_scopes
    )

    # The TMDL generator routes calculations to a table by datasource_name,
    # reading the copy held inside the datasource. Merging replaces the source
    # datasources with one new datasource, so any calculation still naming its
    # original one is routed nowhere and its measure is silently dropped.
    merged_ds_name = merged_datasource.get('name')
    if merged_ds_name:
        for calc in merged['calculations']:
            calc['datasource_name'] = merged_ds_name
        for calc in merged_datasource.get('calculations', []):
            calc['datasource_name'] = merged_ds_name

    # Routing by datasource alone sends every calculation to the merged
    # datasource's largest table, so a measure from one workbook lands on
    # another workbook's fact table and its thin report cannot bind to it.
    # Record the table each calculation came from instead.
    _attribute_calculations_to_source_tables(
        merged, merged_datasource, all_extracted, workbook_names)

    # 3. Merge parameters
    merged['parameters'] = _merge_parameters(all_extracted, workbook_names)

    # 4. Merge remaining objects (union, deduplicated by name)
    for key in ('sets', 'groups', 'bins', 'sort_orders',
                'custom_sql', 'user_filters', 'filters', 'stories', 'actions'):
        merged[key] = _merge_list_by_name(all_extracted, key, workbook_names=workbook_names)

    # 4b. Merge hierarchies with level-aware deduplication
    merged['hierarchies'] = _merge_hierarchies(all_extracted, workbook_names)

    # 4c. Merge calculation groups
    merged['_calculation_groups'] = _merge_calculation_groups(
        all_extracted, workbook_names
    )

    # 4d. Merge field parameters
    merged['_field_parameters'] = _merge_field_parameters(
        all_extracted, workbook_names
    )

    # 4e. Merge perspectives
    merged['_perspectives'] = _merge_perspectives(all_extracted, workbook_names)

    # 4f. Merge cultures
    merged['_cultures'] = _merge_cultures(all_extracted, workbook_names)

    # 4g. Merge goals / pulse metrics
    merged['_goals'] = _merge_goals(all_extracted, workbook_names)

    # 5. Merge aliases
    for extracted in all_extracted:
        aliases = extracted.get('aliases', {})
        if isinstance(aliases, dict):
            merged['aliases'].update(aliases)

    # 6. Collect all worksheets and dashboards (kept per-workbook for thin reports)
    for extracted in all_extracted:
        merged['worksheets'].extend(extracted.get('worksheets', []))
        merged['dashboards'].extend(extracted.get('dashboards', []))

    return merged


def _preflight_calculation_identities(all_extracted: List[dict],
                                      workbook_names: List[str],
                                      assessment: MergeAssessment) -> dict:
    """Protect original calculation IDs before the two merge inventories collapse.

    A table scope is evidence only when the merge really retains that alias,
    not when the widest-table heuristic merely guesses a destination.
    """
    from powerbi_import.errors import PartialGenerationError
    from powerbi_import.tableau_identity import calculation_identity
    from powerbi_import.tmdl_generator import _calculation_alias_key

    isolated = {
        _normalize_table_key(name)
        for names in assessment.isolated_tables.values() for name in names
    }
    source_table_counts = [
        sum(len(ds.get('tables', [])) for ds in extracted.get('datasources', []))
        for extracted in all_extracted
    ]
    tables = []
    for wi, extracted in enumerate(all_extracted):
        for di, ds in enumerate(extracted.get('datasources', [])):
            fingerprints = build_table_fingerprints([ds])
            counts = {}
            for table in ds.get('tables', []):
                name = table.get('name', '')
                counts[name] = counts.get(name, 0) + 1
            for name, (fp, table, _) in fingerprints.items():
                tables.append({
                    'workbook': wi, 'datasource': di, 'ds': ds, 'table': table,
                    'name': name, 'fingerprint': fp.fingerprint(),
                    'unique': counts.get(name) == 1,
                })

    selected = [t for t in tables if _normalize_table_key(t['name']) not in isolated]
    if not selected and isolated:
        selected = tables
    retained = {}
    for table in selected:
        retained.setdefault(table['fingerprint'], table['name'])
    alias_fingerprints = {}
    for fingerprint, name in retained.items():
        alias_fingerprints.setdefault(name.casefold(), set()).add(fingerprint)
    selected_ids = {id(table) for table in selected}

    def scope_for(wi, di, calc):
        workbook_tables = [t for t in tables if t['workbook'] == wi]
        explicit = calc.get('table')
        if explicit:
            if not isinstance(explicit, str):
                return None, None
            candidates = [
                t for t in workbook_tables
                if t['name'] == explicit and (di is None or t['datasource'] == di)
            ]
        elif source_table_counts[wi] == 1 and len(workbook_tables) == 1:
            candidates = workbook_tables
            if di is not None and candidates[0]['datasource'] != di:
                return None, None
        else:
            return None, None
        if len(candidates) != 1:
            return None, None
        t = candidates[0]
        if (id(t) not in selected_ids or not t['unique']
                or retained.get(t['fingerprint']) != t['name']
                or len(alias_fingerprints.get(t['name'].casefold(), ())) != 1):
            return None, None
        ds_name = calc.get('datasource_name')
        if ds_name and ds_name != t['ds'].get('name'):
            return None, None
        return t['name'], t

    def declaration(calc):
        return {
            key: value for key, value in calc.items()
            if key not in ('datasource_name', 'table', '_merge_action',
                           '_source_workbook', '_source_workbooks')
        }

    def reject_premerged(calc, workbook):
        action = calc.get('_merge_action')
        if isinstance(action, str) and action.strip().casefold() == 'deduplicated':
            raise PartialGenerationError(
                "Cannot merge a previously deduplicated calculation: its "
                "complete original datasource-scoped IDs and target bindings "
                "are not available to this merge. Re-extract the original "
                "workbooks rather than discarding the deduplication marker.",
                item=calc.get('caption') or calc.get('name'),
                source=workbook,
                recovery_kind='calculation_identity_binding',
                required_evidence=[
                    'Complete original calculation identities and datasource namespaces',
                    'Verified source-ID-to-target bindings for each original calculation',
                ],
            )

    def source_identity(table):
        if table is None:
            return None
        ds, source = table['ds'], table['table']
        ds_name = ds.get('name')
        connection = source.get('connection_details')
        if not connection:
            connection = (ds.get('connection_map') or {}).get(source.get('connection'))
        connection = connection or ds.get('connection') or {}
        if not isinstance(connection, dict):
            return None
        details = connection.get('details') or {}
        if not isinstance(details, dict):
            return None
        identity = source.get('hyper_identity')
        hyper = (isinstance(identity, dict)
                 and all(identity.get(k) for k in ('workbook', 'archive_member', 'schema', 'table'))
                 and os.path.isabs(str(identity['workbook'])))
        location = (
            (details.get('server') or details.get('host'))
            and (details.get('database') or details.get('db'))
        )
        location = location or details.get('url') or details.get('dsn')
        location = location or any(
            os.path.isabs(str(details.get(key) or ''))
            for key in ('filepath', 'path', 'filename'))
        if (not isinstance(ds_name, str) or not ds_name.strip()
                or not (hyper or (connection.get('type') and location))):
            return None
        overrides = ds.get('m_query_overrides') or {}
        if not isinstance(overrides, dict):
            return None
        return (
            ds_name, source.get('source_table') or table['name'], connection, identity,
            source.get('custom_sql', source.get('query', '')),
            source.get('cols_physical_map'), source.get('m_query_override'),
            ds.get('m_query_override'), overrides.get(table['name']),
            ds.get('source_selection'), ds.get('materialized_extract'),
        )

    scopes, records = {}, []
    conflicts = {c.name for c in assessment.measure_conflicts}
    for wi, (wb_name, extracted) in enumerate(zip(workbook_names, all_extracted)):
        ds_records = []
        datasources = extracted.get('datasources', [])
        for di, ds in enumerate(datasources):
            for ci, calc in enumerate(ds.get('calculations', [])):
                reject_premerged(calc, wb_name)
                scope, table = scope_for(wi, di, calc)
                scopes[('datasource', wi, di, ci)] = scope
                entry = (wb_name, calc, scope, source_identity(table))
                ds_records.append(entry)
                records.append(entry)
        for ci, calc in enumerate(extracted.get('calculations', [])):
            reject_premerged(calc, wb_name)
            # The extractor ordinarily repeats each datasource calculation in
            # the workbook inventory. It is not a second source declaration.
            matches = [
                entry for entry in ds_records
                if declaration(entry[1]) == declaration(calc)
                and (not calc.get('datasource_name')
                     or calc.get('datasource_name') == entry[1].get('datasource_name'))
            ]
            if len(matches) == 1:
                matched_scope = matches[0][2]
                if calc.get('table') and calc.get('table') != matched_scope:
                    raise PartialGenerationError(
                        "Cannot merge a calculation whose workbook and "
                        "datasource copies disagree on the retained target table.",
                        item=calc.get('caption') or calc.get('name'),
                        source=wb_name,
                        recovery_kind='calculation_identity_binding',
                        required_evidence=[
                            'Consistent source calculation identity and target table',
                            'Unambiguous datasource-scoped source-to-target binding',
                        ],
                    )
                scopes[('workbook', wi, ci)] = matched_scope
                continue
            ds_indices = [
                di for di, ds in enumerate(datasources)
                if calc.get('datasource_name') and ds.get('name') == calc.get('datasource_name')
            ]
            di = ds_indices[0] if len(ds_indices) == 1 else (
                0 if not calc.get('datasource_name') and len(datasources) == 1 else None)
            scope, table = scope_for(wi, di, calc)
            scopes[('workbook', wi, ci)] = scope
            records.append((wb_name, calc, scope, source_identity(table)))

    groups = {}
    for wb_name, calc, scope, identity in records:
        if (not isinstance(calc.get('name', ''), str)
                or (calc.get('caption') is not None
                    and not isinstance(calc.get('caption'), str))):
            raise PartialGenerationError(
                "Cannot prove a calculation identity: its source ID or caption "
                "is not a string.",
                source=wb_name,
                recovery_kind='calculation_identity_binding',
                required_evidence=['Original datasource-scoped calculation ID and caption'],
            )
        name, caption = calculation_identity(calc)
        if not isinstance(caption, str) or not caption:
            continue
        # A conflicting measure is already given a distinct output caption.
        merge_caption = calc.get('caption') or calc.get('name', '')
        output = (f'{merge_caption} ({wb_name})'
                  if merge_caption in conflicts and calc.get('role', 'measure') == 'measure'
                  else caption)
        groups.setdefault(_calculation_alias_key(output), []).append(
            (wb_name, calc, scope, identity, name, output))

    for entries in groups.values():
        for index, first in enumerate(entries):
            for second in entries[index + 1:]:
                a_wb, a_calc, a_scope, a_origin, a_name, output = first
                b_wb, b_calc, b_scope, b_origin, b_name, _ = second
                if a_scope and b_scope and a_scope.casefold() != b_scope.casefold():
                    continue
                same_source = (
                    a_scope is not None and b_scope is not None
                    and a_origin is not None and a_origin == b_origin
                    and isinstance(a_name, str) and bool(a_name)
                    and a_name == b_name
                    and declaration(a_calc) == declaration(b_calc)
                )
                if same_source:
                    continue
                raise PartialGenerationError(
                    f"Cannot merge calculation caption {output!r}: distinct or "
                    "unproven source identities would share a column or measure, "
                    "or their target table scope is ambiguous. Preserve the "
                    "original datasource-scoped IDs and prove distinct target "
                    "tables, or use distinct, explicitly mapped output names.",
                    item=output,
                    source=f'{a_wb}, {b_wb}',
                    recovery_kind='calculation_identity_binding',
                    required_evidence=[
                        f'Original calculation IDs: {a_name!r}, {b_name!r}',
                        f'Proven retained target tables: {a_scope!r}, {b_scope!r}',
                        'Complete datasource-scoped identities and source-to-target bindings',
                    ],
                )
    return scopes


def _preflight_relationship_aliases(datasources: list) -> None:
    """Reject ambiguous or unmapped relationship endpoints.

    The merger keeps table aliases and relationship endpoints unchanged while
    collapsing all sources into one datasource. The manifest cannot remap
    duplicate aliases to their original tables.
    """
    from powerbi_import.errors import PartialGenerationError

    aliases: Dict[str, list] = {}
    retained_names: Dict[tuple, str] = {}
    unmapped_aliases: Dict[str, str] = {}
    for ds in datasources:
        connection_map = ds.get('connection_map') or {}
        for table in ds.get('tables') or []:
            if not isinstance(table, dict):
                continue
            name = table.get('name')
            if not isinstance(name, str) or not name.strip():
                continue
            connection = table.get('connection_details')
            if not connection and isinstance(connection_map, dict):
                connection = connection_map.get(table.get('connection', ''))
            connection = connection or ds.get('connection') or {}
            if not isinstance(connection, dict):
                connection = {}
            fingerprints = build_table_fingerprints([{
                'connection': connection, 'tables': [table],
            }])
            if name not in fingerprints:
                continue  # The merge does not retain this table.

            # Deduplication uses the datasource connection, not the resolved
            # table connection. Record spelling changes within one normalized
            # alias so isolated-table filtering treats both names alike.
            merge_fingerprints = build_table_fingerprints([{
                'connection': ds.get('connection') or {}, 'tables': [table],
            }])
            merge_key = (
                merge_fingerprints[name][0].fingerprint(),
                _normalize_table_key(name),
            )
            retained = retained_names.setdefault(merge_key, name)
            if name != retained:
                unmapped_aliases[name] = retained

            source_table = table.get('source_table') or name
            if not isinstance(source_table, str):
                source_table = name
            # Source identifiers can be case-sensitive even though Power BI
            # aliases and the legacy fingerprint are case-insensitive.
            source_name = _parse_table_name(source_table)
            details = connection.get('details') or {}
            if not isinstance(details, dict):
                details = {}
            # Additional explicit location fields distinguish sources which
            # the existing server/database fingerprint alone cannot separate.
            location = {
                key: str(details.get(key) or connection.get(key)).strip()
                for key in ('port', 'filename', 'directory', 'filepath', 'path',
                            'url', 'schema', 'catalog', 'project', 'project_id',
                            'dataset', 'warehouse', 'account', 'dsn')
                if details.get(key) or connection.get(key)
            }
            for key in ('filename', 'directory', 'filepath', 'path'):
                if key in location:
                    location[key] = os.path.normcase(os.path.normpath(location[key]))
            for key, synonyms in (
                    ('server', ('server', 'host')),
                    ('database', ('database', 'db'))):
                value = next(
                    (entry.get(field) for entry in (details, connection)
                     for field in synonyms if entry.get(field)), None)
                if value:
                    location[key] = (str(value).strip().casefold() if key == 'server'
                                     else str(value).strip())

            hyper_identity = table.get('hyper_identity')
            exact_hyper = (
                isinstance(hyper_identity, dict)
                and all(hyper_identity.get(key) for key in
                        ('workbook', 'archive_member', 'schema', 'table'))
                and os.path.isabs(str(hyper_identity['workbook']))
            )
            filename = details.get('filename') or connection.get('filename') or ''
            directory = details.get('directory') or connection.get('directory') or ''
            file_paths = [
                details.get(key) or connection.get(key) or ''
                for key in ('filepath', 'path')
            ]
            if filename:
                file_paths.append(os.path.join(str(directory), str(filename)))
            absolute_file = any(os.path.isabs(str(path)) for path in file_paths)
            known_connection = str(connection.get('type') or '').strip().casefold()
            source_proven = exact_hyper or (
                known_connection not in ('', 'unknown')
                and bool(
                    (location.get('server') and location.get('database'))
                    or (location.get('account') and location.get('database'))
                    or ((location.get('project') or location.get('project_id'))
                        and location.get('dataset'))
                    or location.get('url') or location.get('dsn') or absolute_file
                )
            )
            aliases.setdefault(name.strip().casefold(), []).append((
                fingerprints[name][0].fingerprint(), source_name,
                hyper_identity, location, source_proven,
            ))

    def distinct_sources(entries):
        if len(entries) > 1 and not all(entry[4] for entry in entries):
            return True
        baseline = entries[0][:3]
        locations = {}
        for entry in entries:
            if entry[:3] != baseline:
                return True
            for key, value in entry[3].items():
                if key in locations and locations[key] != value:
                    return True
                locations[key] = value
        return False

    ambiguous = {
        name for name, entries in aliases.items()
        if distinct_sources(entries)
    }
    if not ambiguous and not unmapped_aliases:
        return

    for ds in datasources:
        for rel in ds.get('relationships') or []:
            if not isinstance(rel, dict):
                continue
            if 'left' in rel or 'right' in rel:
                endpoints = [
                    side.get('table') if isinstance(side, dict) else None
                    for side in (rel.get('left'), rel.get('right'))
                ]
            else:
                endpoints = [
                    rel.get('from_table') or rel.get('fromTable'),
                    rel.get('to_table') or rel.get('toTable'),
                ]
            for name in endpoints:
                if not isinstance(name, str):
                    continue
                if name in unmapped_aliases:
                    raise PartialGenerationError(
                        f"Cannot merge relationship endpoint {name!r}: its table "
                        f"is deduplicated as {unmapped_aliases[name]!r} but the "
                        "relationship endpoint is not remapped. Map each endpoint "
                        "to its own retained table alias, or keep distinct aliases.",
                        item=name,
                        source=ds.get('name') or ds.get('caption'),
                        recovery_kind='unmapped_relationship_alias',
                        required_evidence=[
                            'Retained table alias after deduplication',
                            'Explicit mapping of both relationship endpoints',
                        ],
                    )
                if name.strip().casefold() in ambiguous:
                    raise PartialGenerationError(
                        f"Cannot merge relationship endpoint {name!r}: the same "
                        "table alias identifies distinct or not provably identical "
                        "source tables and the shared datasource and manifest do "
                        "not remap their relationships. Unify the source table "
                        "only when its identity is proven, or use distinct aliases "
                        "and remap both relationship endpoints before merging.",
                        item=name,
                        source=ds.get('name') or ds.get('caption'),
                        recovery_kind='ambiguous_relationship_alias',
                        required_evidence=[
                            'Distinct or proven-identical source table identities',
                            'Unambiguous table aliases and both relationship endpoints',
                        ],
                    )


def _preflight_merge_datasources(all_extracted: List[dict]) -> list:
    """Validate source relations before collapsing datasources and provenance.

    The shared datasource keeps legacy relationship edges, not the original
    datasource-scoped relation trees or their policies. A complex logical
    predicate therefore cannot be compiled safely after the merge.
    """
    from powerbi_import.errors import PartialGenerationError
    from powerbi_import.tmdl_relationships import (
        _physical_join_nodes,
        _physical_join_scopes,
        _resolved_single_equality,
        preflight_physical_joins,
    )

    datasources = [
        ds
        for extracted in all_extracted
        for ds in extracted.get('datasources', [])
    ]
    # Inspect the active source trees, including nested and object-graph joins,
    # before any source metadata is discarded. Historical native provenance on
    # a bound materialized extract is deliberately not replayed.
    nested_blocker = None
    try:
        preflight_physical_joins(datasources, provenance_retained=False)
    except PartialGenerationError as exc:
        # A nested join has protected cross-subtree pairs without direct
        # equalities. Only consider retaining its trees when that exact loss
        # of scope is the blocker; all predicate validation remains Semantic's.
        has_uncovered_scope = any(
            not scope['equalities']
            and f"relation_metadata.{scope['paths'][0]}" == exc.item
            for ds in datasources
            for scope in _physical_join_scopes(ds).values()
        )
        if exc.recovery_kind != 'physical_join_predicate' or not has_uncovered_scope:
            raise
        for ds in datasources:
            preflight_physical_joins(
                [ds], provenance_retained=True, allow_nested_retention=True)
        nested_blocker = exc

    for ds in datasources:
        metadata = ds.get('relation_metadata') or {}
        table_aliases = [
            table.get('name') for table in ds.get('tables') or []
            if isinstance(table, dict)
        ]
        for index, logical in enumerate(metadata.get('logical_relationships') or []):
            logical = logical if isinstance(logical, dict) else {}
            edge = _resolved_single_equality(logical.get('predicate'))
            flag = logical.get('requires_materialization')
            join_type = logical.get('join_type')
            if join_type is None or join_type == '':
                join_type = 'left'
            if isinstance(join_type, str):
                join_type = join_type.casefold()
            endpoints = [logical.get(side) or {} for side in ('left', 'right')]
            aliases = [
                endpoint.get('tables', []) if isinstance(endpoint, dict) else []
                for endpoint in endpoints
            ]
            # Only the extractor's declared equality fields can be represented
            # by the legacy edge. Unknown relationship/endpoint metadata may
            # contain cardinality, filtering or other datasource-scoped policy,
            # which the shared datasource cannot retain.
            extra_metadata = [
                str(key) for key in logical
                if key not in ('left', 'right', 'join_type', 'predicate',
                               'requires_materialization')
            ]
            for side, endpoint in zip(('left', 'right'), endpoints):
                if isinstance(endpoint, dict):
                    extra_metadata.extend(
                        f'{side}.{key}' for key in endpoint
                        if key not in ('object_id', 'tables')
                    )
            single_endpoints = all(
                isinstance(side, (list, tuple)) and len(side) == 1
                and isinstance(side[0], str) and side[0]
                for side in aliases
            )
            valid_edge = (
                edge is not None
                and (flag is None or flag is False)
                and join_type in ('left', 'right', 'inner', 'full')
                and not extra_metadata
                and single_endpoints
                and aliases[0][0] != aliases[1][0]
                and all(table_aliases.count(side[0]) == 1 for side in aliases)
                and {edge[0][0], edge[1][0]} == {aliases[0][0], aliases[1][0]}
            )

            # Do not synthesize an edge or use an inferred one in place of
            # source provenance that this merge cannot carry forward.
            legacy_edge = False
            if valid_edge:
                for rel in ds.get('relationships') or []:
                    if not isinstance(rel, dict) or rel.get('inferred') is True:
                        continue
                    sides = [rel.get(side) for side in ('left', 'right')]
                    rel_type = rel.get('type', 'left')
                    if (all(isinstance(side, dict) for side in sides)
                            and tuple((side.get('table'), side.get('column'))
                                      for side in sides) == edge
                            and isinstance(rel_type, str)
                            and rel_type.casefold() == join_type):
                        legacy_edge = True
                        break
            if valid_edge and legacy_edge:
                continue

            raise PartialGenerationError(
                "Cannot merge a logical relationship whose complete predicate, "
                "endpoints, or datasource-scoped policies cannot be preserved. "
                "The shared merge only retains a declared single-equality legacy "
                "edge; it cannot safely replace a composite, range, expression, "
                "ambiguous, or missing predicate with an inferred relationship. "
                "Materialize the full source semantics before merging, or migrate "
                "the datasource separately with its verified policies."
                + (f" Unpreserved logical metadata: {', '.join(sorted(extra_metadata))}."
                   if extra_metadata else ''),
                item=f'relation_metadata.logical_relationships[{index}]',
                source=ds.get('name') or ds.get('caption'),
                recovery_kind='logical_relationship_predicate',
                required_evidence=[
                    'Complete logical relationship predicate and endpoint lineage',
                    'Verified datasource-scoped cardinality, filter, null and comparison policies',
                    'A source-materialized relation or an exact declared single-equality edge',
                ],
            )

    _preflight_relationship_aliases(datasources)
    if nested_blocker is None:
        physical_node = next((
            (ds, path) for ds in datasources
            for path, _node in _physical_join_nodes(ds)
        ), None)
        if physical_node is not None:
            ds, path = physical_node
            nested_blocker = PartialGenerationError(
                'Physical join provenance must be retained to prevent '
                'additional relationships from being inferred.',
                item=f'relation_metadata.{path}',
                source=ds.get('name') or ds.get('caption'),
                recovery_kind='physical_join_predicate',
                required_evidence=[
                    'Complete nested physical join tree and its declared equality edges',
                ],
            )
    if nested_blocker is not None:
        return _retained_nested_physical_sources(datasources, nested_blocker)
    return []


def _retained_nested_physical_sources(datasources: list, blocker) -> list:
    """Select sources whose physical trees can be retained without remapping."""
    from powerbi_import.errors import PartialGenerationError
    from powerbi_import.tmdl_relationships import (
        _physical_join_nodes,
        _physical_join_scopes,
    )

    def refuse(reason, datasource):
        raise PartialGenerationError(
            f"Cannot retain nested physical join provenance in the shared "
            f"datasource: {reason}. Preserve the original source scope and "
            "bindings, or use a verified pre-joined source.",
            item=blocker.item,
            source=datasource.get('name') or datasource.get('caption'),
            recovery_kind='physical_join_predicate',
            required_evidence=[
                'Complete declared physical equalities and join types',
                'Unambiguous source and projected table/field bindings',
                'Preserved source-join scope through semantic generation',
            ],
        ) from blocker

    retained_sources = []
    protected_aliases = set()
    for ds in datasources:
        scopes = _physical_join_scopes(ds)
        if not scopes:
            continue
        metadata = ds.get('relation_metadata') or {}
        if (metadata.get('version') != 1
                or set(metadata) - {
                    'version', 'physical_relations', 'logical_tables',
                    'logical_relationships',
                }
                or metadata.get('logical_tables')
                or metadata.get('logical_relationships')):
            refuse('object-graph or unrecognized datasource-scoped metadata '
                   'cannot be safely combined', ds)

        nodes = list(_physical_join_nodes(ds))
        for path, node in nodes:
            if not path.startswith('physical_relations['):
                refuse('an object-graph physical join cannot be remapped', ds)
            children = node.get('children') or []
            if (len(children) != 2 or any(
                    not isinstance(child, dict)
                    or str(child.get('type', '')).casefold() not in ('join', 'table')
                    for child in children)):
                refuse('the join contains a union, custom SQL, or ambiguous subtree', ds)
            if str(node.get('join_type') or '').strip().casefold() not in (
                    'left', 'right', 'inner', 'full'):
                refuse('a source join type is missing or unsupported', ds)

        declared_edges = set()
        declared_aliases = set()
        for rel in ds.get('relationships') or []:
            if not isinstance(rel, dict):
                continue
            sides = [rel.get(side) for side in ('left', 'right')]
            if not all(isinstance(side, dict) for side in sides):
                continue
            tables = [side.get('table') for side in sides]
            if not all(isinstance(name, str) and name for name in tables):
                continue
            pair = frozenset(tables)
            if rel.get('inferred') is True:
                if pair in scopes:
                    refuse('an inferred relationship is inside the source join scope', ds)
                continue
            fields = [(side.get('table'), side.get('column')) for side in sides]
            if all(isinstance(column, str) and column for _, column in fields):
                declared_aliases.update(tables)
                if isinstance(rel.get('type'), str) and rel['type'].strip():
                    declared_edges.add(frozenset(fields))

        source_aliases = {alias for pair in scopes for alias in pair}
        protected_aliases.update(alias.casefold() for alias in source_aliases)
        if not source_aliases.issubset(declared_aliases):
            refuse('a source-scope alias has no declared relationship binding', ds)
        if any(edge not in declared_edges
               for scope in scopes.values() for edge in scope['equalities']):
            refuse('a complete physical equality or its join type has no '
                   'declared relationship', ds)

        retained_sources.append(ds)

    for ds in datasources:
        aliases = {
            table['name'].casefold()
            for table in ds.get('tables') or []
            if isinstance(table, dict) and isinstance(table.get('name'), str)
        }
        if not aliases.intersection(protected_aliases):
            continue
        overrides = ds.get('m_query_overrides') or {}
        if (ds.get('m_query_override')
                or not isinstance(overrides, dict)
                or any(isinstance(name, str) and name.casefold() in protected_aliases
                       for name in overrides)):
            refuse('an M-query override obscures a protected source table', ds)
    return retained_sources


def _validate_retained_physical_projection(merged_ds: dict, all_extracted: List[dict]) -> None:
    """Check all original edges and the actual projection before writing."""
    from powerbi_import.errors import PartialGenerationError
    from powerbi_import.tmdl_relationships import (
        _physical_join_nodes,
        _physical_join_scopes,
        _physical_join_sides,
        _single_physical_join_equality,
        preflight_physical_joins,
    )

    sources = [ds for wb in all_extracted for ds in wb.get('datasources', [])]
    original_edges = [rel for ds in sources for rel in ds.get('relationships') or []]
    scopes = _physical_join_scopes(merged_ds)
    for path, node in _physical_join_nodes(merged_ds):
        if str(node.get('join_type') or '').strip().casefold() not in ('left', 'right'):
            continue
        edge = _single_physical_join_equality(node)
        if edge is None:
            continue  # Semantic's preflight reports unsupported predicates.
        left, right = _physical_join_sides(node)
        for rel in original_edges:
            if not isinstance(rel, dict) or rel.get('inferred') is True:
                continue
            sides = [rel.get(side) for side in ('left', 'right')]
            if not all(isinstance(side, dict) for side in sides):
                continue
            fields = [(side.get('table'), side.get('column')) for side in sides]
            if (all(isinstance(value, str) for field in fields for value in field)
                    and frozenset(fields) == frozenset(edge)
                    and (fields[0][0] not in left or fields[1][0] not in right)):
                raise PartialGenerationError(
                    "Cannot retain a nested physical outer join when a "
                    "relationship reverses its declared source sides.",
                    item=f'relation_metadata.{path}',
                    source=merged_ds.get('name'),
                    recovery_kind='physical_join_predicate',
                    required_evidence=['Relationship endpoints in source join-side order'],
                )
    for rel in original_edges:
        if not isinstance(rel, dict) or rel.get('inferred') is not True:
            continue
        sides = [rel.get(side) or {} for side in ('left', 'right')]
        pair = frozenset(
            side.get('table') if isinstance(side, dict) else None for side in sides
        )
        if pair in scopes:
            raise PartialGenerationError(
                "Cannot retain a nested physical join when an inferred edge "
                "from another source falls inside its protected scope.",
                source=merged_ds.get('name'),
                recovery_kind='physical_join_predicate',
                required_evidence=['Declared source relationships without inferred edges'],
            )

    # Check every original edge before deduplication as well as the actual
    # selected tables and edges. A conflicting source must not disappear just
    # because the merge's relationship key ignores join policy.
    projection = dict(merged_ds, relationships=original_edges)
    preflight_physical_joins([projection], provenance_retained=True)
    preflight_physical_joins([merged_ds], provenance_retained=True)


def _merge_datasources(all_extracted: List[dict],
                       workbook_names: List[str],
                       assessment: MergeAssessment,
                       calculation_scopes: Optional[dict] = None) -> dict:
    """Merge all datasources into a single unified datasource.

    Tables are deduplicated by fingerprint. Columns are unioned.
    Relationships are deduplicated.
    """
    if calculation_scopes is None:
        calculation_scopes = _preflight_calculation_identities(
            all_extracted, workbook_names, assessment)
    retained_sources = _preflight_merge_datasources(all_extracted)

    merged_ds = {
        'name': 'SharedModel',
        'caption': 'Shared Semantic Model',
        'connection': {},
        'connection_map': {},
        'tables': [],
        'columns': [],
        'calculations': [],
        'relationships': [],
    }

    # Track which tables have been added (by fingerprint hash)
    added_tables: Dict[str, dict] = {}  # fp_hash → merged_table

    # Track relationships for deduplication
    rel_keys_seen = set()

    # Track datasource-level calculations for deduplication
    _ds_calc_seen = {}  # (caption, formula, proven target table) → retained calc

    # Build set of isolated table names (normalized) to skip
    _isolated_set = set()
    for _wb, tnames in assessment.isolated_tables.items():
        for t in tnames:
            _isolated_set.add(_normalize_table_key(t))

    for wi, (wb_name, extracted) in enumerate(zip(workbook_names, all_extracted)):
        for di, ds in enumerate(extracted.get('datasources', [])):
            # Adopt connection info from first datasource
            if not merged_ds['connection']:
                merged_ds['connection'] = copy.deepcopy(ds.get('connection', {}))

            # Merge connection_map entries
            for k, v in ds.get('connection_map', {}).items():
                if k not in merged_ds['connection_map']:
                    merged_ds['connection_map'][k] = copy.deepcopy(v)

            # Process tables — skip isolated tables
            fps = build_table_fingerprints([ds])
            for raw_name, (fp, table, _conn) in fps.items():
                # Skip tables classified as isolated (no links to merged model)
                if _normalize_table_key(raw_name) in _isolated_set:
                    continue

                fp_hash = fp.fingerprint()
                if fp_hash in added_tables:
                    # Merge columns into existing table
                    _merge_columns_into(added_tables[fp_hash], table)
                    # Lineage: track additional source workbook
                    added_tables[fp_hash].setdefault('_source_workbooks', []).append(wb_name)
                    added_tables[fp_hash]['_merge_action'] = 'deduplicated'
                else:
                    # Add new table
                    merged_table = copy.deepcopy(table)
                    merged_table['_source_workbooks'] = [wb_name]
                    merged_table['_merge_action'] = 'unique'
                    added_tables[fp_hash] = merged_table
                    merged_ds['tables'].append(merged_table)

            # Merge relationships (deduplicate)
            for rel in ds.get('relationships', []):
                key = _relationship_key(rel)
                if key not in rel_keys_seen:
                    rel_keys_seen.add(key)
                    rel_copy = copy.deepcopy(rel)
                    rel_copy['_source_workbooks'] = [wb_name]
                    rel_copy['_merge_action'] = 'unique'
                    merged_ds['relationships'].append(rel_copy)
                else:
                    # Find existing and update lineage
                    for existing_rel in merged_ds['relationships']:
                        if _relationship_key(existing_rel) == key:
                            existing_rel.setdefault('_source_workbooks', []).append(wb_name)
                            existing_rel['_merge_action'] = 'deduplicated'
                            break

            # Merge datasource-level calculations (dedup + namespace conflicts)
            conflict_names = {mc.name for mc in assessment.measure_conflicts}
            for ci, calc in enumerate(ds.get('calculations', [])):
                caption = calc.get('caption') or calc.get('name', '')
                formula = calc.get('formula', '').strip()
                role = calc.get('role', 'measure')
                scope = calculation_scopes.get(('datasource', wi, di, ci))

                if caption in conflict_names and role == 'measure':
                    # Namespace conflicting measure
                    namespaced = copy.deepcopy(calc)
                    new_caption = f"{caption} ({wb_name})"
                    namespaced['caption'] = new_caption
                    namespaced['_original_caption'] = caption
                    namespaced['_source_workbook'] = wb_name
                    namespaced['_source_workbooks'] = [wb_name]
                    namespaced['_merge_action'] = 'namespaced'
                    if scope and not namespaced.get('table'):
                        namespaced['table'] = scope
                    merged_ds['calculations'].append(namespaced)
                else:
                    # Equal captions in independently proven target tables
                    # are distinct model fields, even with equal formulas.
                    dup_key = (caption, formula, scope.casefold() if scope else None)
                    if dup_key not in _ds_calc_seen:
                        calc_copy = copy.deepcopy(calc)
                        calc_copy['_source_workbooks'] = [wb_name]
                        calc_copy['_merge_action'] = 'unique'
                        if scope and not calc_copy.get('table'):
                            calc_copy['table'] = scope
                        _ds_calc_seen[dup_key] = calc_copy
                        merged_ds['calculations'].append(calc_copy)
                    else:
                        existing_calc = _ds_calc_seen[dup_key]
                        existing_calc.setdefault('_source_workbooks', []).append(wb_name)
                        existing_calc['_merge_action'] = 'deduplicated'

    # Safety fallback: avoid emitting an empty shared semantic model.
    # If isolated-table filtering removed everything, re-include isolated
    # tables so the shared model remains openable and usable.
    if not merged_ds['tables'] and _isolated_set:
        logger.warning(
            "Shared merge produced 0 tables after isolated filtering; "
            "re-including isolated tables as fallback."
        )
        for wb_name, extracted in zip(workbook_names, all_extracted):
            for ds in extracted.get('datasources', []):
                fps = build_table_fingerprints([ds])
                for _raw_name, (fp, table, _conn) in fps.items():
                    fp_hash = fp.fingerprint()
                    if fp_hash in added_tables:
                        continue
                    merged_table = copy.deepcopy(table)
                    merged_table['_source_workbooks'] = [wb_name]
                    merged_table['_merge_action'] = 'fallback_included'
                    added_tables[fp_hash] = merged_table
                    merged_ds['tables'].append(merged_table)

    if retained_sources:
        from powerbi_import.tmdl_relationships import preserve_physical_join_provenance

        # Semantic owns tree preservation and source-to-projection checks.
        # Pass each source separately only after the cross-source alias check;
        # no table or field renaming is inferred.
        for ds in retained_sources:
            merged_ds = preserve_physical_join_provenance([ds], merged_ds)
        _validate_retained_physical_projection(merged_ds, all_extracted)

    return merged_ds


def _merge_columns_into(existing_table: dict, new_table: dict):
    """Merge columns from new_table into existing_table (union).

    Existing columns are kept. New columns not in existing are added.
    Type conflicts: wider type wins (string > real > integer).
    Tracks type history in ``_column_type_history`` for post-merge validation.
    """
    existing_cols = {c.get('name', '').lower(): c
                     for c in existing_table.get('columns', [])}

    # Ensure type history dict exists
    type_history = existing_table.setdefault('_column_type_history', {})

    # Seed type history from existing columns (first workbook)
    for col in existing_table.get('columns', []):
        cn = col.get('name', '').lower()
        if cn not in type_history:
            type_history[cn] = [col.get('datatype', 'string')]

    for col in new_table.get('columns', []):
        col_name = col.get('name', '').lower()
        new_type = col.get('datatype', 'string')
        if col_name in existing_cols:
            # Track type history for validation
            type_history.setdefault(col_name, []).append(new_type)
            # Resolve type conflicts — wider type wins
            existing_type = existing_cols[col_name].get('datatype', 'string')
            if _type_width(new_type) > _type_width(existing_type):
                existing_cols[col_name]['datatype'] = new_type
            # Merge metadata: unhide if unhidden in any workbook
            if not col.get('hidden', False):
                existing_cols[col_name]['hidden'] = False
            # Take first non-empty semantic_role
            if col.get('semantic_role') and not existing_cols[col_name].get('semantic_role'):
                existing_cols[col_name]['semantic_role'] = col['semantic_role']
            # Take first non-empty format
            if col.get('default_format') and not existing_cols[col_name].get('default_format'):
                existing_cols[col_name]['default_format'] = col['default_format']
        else:
            existing_table.setdefault('columns', []).append(copy.deepcopy(col))
            existing_cols[col_name] = existing_table['columns'][-1]


_TYPE_WIDTH = {
    'boolean': 1,
    'integer': 2,
    'real': 3,
    'currency': 3,
    'datetime': 4,
    'string': 5,
}


def _type_width(dtype: str) -> int:
    """Return a width score for type conflict resolution."""
    return _TYPE_WIDTH.get(dtype.lower(), 5)


def _workbook_main_table(extracted: dict) -> str:
    """The table a workbook's calculations belong to: its widest table."""
    best_name, best_width = '', -1
    for datasource in extracted.get('datasources', []):
        for table in datasource.get('tables', []):
            width = len(table.get('columns', []))
            if width > best_width:
                best_name, best_width = table.get('name', ''), width
    return best_name


def _attribute_calculations_to_source_tables(merged: dict,
                                             merged_datasource: dict,
                                             all_extracted: List[dict],
                                             workbook_names: List[str]) -> None:
    """Tag each merged calculation with the table it came from.

    Without this the generator routes every calculation to the widest table in
    the merged datasource, so a measure defined against one workbook's fact
    table is emitted on another's and the thin report referencing it breaks.
    """
    main_table_by_workbook = {
        name: _workbook_main_table(extracted)
        for name, extracted in zip(workbook_names, all_extracted)
    }
    available = {t.get('name') for t in merged_datasource.get('tables', [])}

    def _tag(calc):
        if calc.get('table'):
            return
        sources = calc.get('_source_workbooks') or []
        if not sources:
            return
        table = main_table_by_workbook.get(sources[0])
        # A deduplicated calculation belongs to every workbook that defined it;
        # leave it to default routing unless they all agree.
        if len(sources) > 1:
            tables = {main_table_by_workbook.get(s) for s in sources}
            if len(tables) != 1:
                return
        if table and table in available:
            calc['table'] = table

    for calc in merged.get('calculations', []):
        _tag(calc)
    for calc in merged_datasource.get('calculations', []):
        _tag(calc)


def _merge_calculations(all_extracted: List[dict],
                         workbook_names: List[str],
                         assessment: MergeAssessment,
                         calculation_scopes: Optional[dict] = None) -> list:
    """Merge calculations with conflict resolution.

    Proven same-target declarations may deduplicate; distinct proven target
    tables retain separate calculations. Assessed measure conflicts are
    namespaced with a workbook suffix.
    """
    if calculation_scopes is None:
        calculation_scopes = _preflight_calculation_identities(
            all_extracted, workbook_names, assessment)
    # Build conflict set for fast lookup
    conflict_names = {mc.name for mc in assessment.measure_conflicts}

    # Collect all calculations
    seen: Dict[tuple, dict] = {}
    result = []

    for wi, (wb_name, extracted) in enumerate(zip(workbook_names, all_extracted)):
        for ci, calc in enumerate(extracted.get('calculations', [])):
            caption = calc.get('caption') or calc.get('name', '')
            scope = calculation_scopes.get(('workbook', wi, ci))
            key = (caption, scope.casefold() if scope else None)

            if caption in conflict_names and calc.get('role', 'measure') == 'measure':
                # Namespace it
                namespaced = copy.deepcopy(calc)
                new_caption = f"{caption} ({wb_name})"
                namespaced['caption'] = new_caption
                namespaced['_original_caption'] = caption
                namespaced['_source_workbook'] = wb_name
                namespaced['_source_workbooks'] = [wb_name]
                namespaced['_merge_action'] = 'namespaced'
                if scope and not namespaced.get('table'):
                    namespaced['table'] = scope
                result.append(namespaced)
            elif key in seen:
                # Already seen — track source for lineage
                existing = seen[key]
                existing.setdefault('_source_workbooks', []).append(wb_name)
                existing['_merge_action'] = 'deduplicated'
            else:
                calc_copy = copy.deepcopy(calc)
                calc_copy['_source_workbooks'] = [wb_name]
                calc_copy['_merge_action'] = 'unique'
                if scope and not calc_copy.get('table'):
                    calc_copy['table'] = scope
                seen[key] = calc_copy
                result.append(calc_copy)

    return result


def _merge_parameters(all_extracted: List[dict],
                      workbook_names: List[str]) -> list:
    """Merge parameters. Same name+type+default → deduplicate. Conflicts → namespace."""
    param_map: Dict[str, List[Tuple[str, dict]]] = {}

    for wb_name, extracted in zip(workbook_names, all_extracted):
        for param in extracted.get('parameters', []):
            pname = param.get('name', param.get('caption', ''))
            if pname not in param_map:
                param_map[pname] = []
            param_map[pname].append((wb_name, param))

    result = []
    for pname, entries in param_map.items():
        if len(entries) == 1:
            p_copy = copy.deepcopy(entries[0][1])
            p_copy['_source_workbooks'] = [entries[0][0]]
            p_copy['_merge_action'] = 'unique'
            result.append(p_copy)
        else:
            # Check if all identical
            sigs = set()
            for wb, p in entries:
                sig = (p.get('datatype', ''), p.get('domain_type', ''),
                       str(p.get('current_value', '')))
                sigs.add(sig)
            if len(sigs) == 1:
                p_copy = copy.deepcopy(entries[0][1])
                p_copy['_source_workbooks'] = [wb for wb, _ in entries]
                p_copy['_merge_action'] = 'deduplicated'
                result.append(p_copy)
            else:
                # Namespace each
                for wb, p in entries:
                    namespaced = copy.deepcopy(p)
                    namespaced['name'] = f"{pname} ({wb})"
                    namespaced['caption'] = f"{pname} ({wb})"
                    namespaced['_source_workbook'] = wb
                    namespaced['_source_workbooks'] = [wb]
                    namespaced['_merge_action'] = 'namespaced'
                    result.append(namespaced)

    return result


def _merge_list_by_name(all_extracted: List[dict], key: str, workbook_names: Optional[List[str]] = None) -> list:
    """Merge a list-type object by deduplicating on 'name' field."""
    seen_names: Dict[str, dict] = {}  # name → item (for lineage tracking)
    result = []
    for idx, extracted in enumerate(all_extracted):
        wb_label = workbook_names[idx] if workbook_names and idx < len(workbook_names) else f'wb_{idx}'
        items = extracted.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            name = item.get('name', '')
            if name and name in seen_names:
                # Track additional source
                seen_names[name].setdefault('_source_workbooks', []).append(wb_label)
                seen_names[name]['_merge_action'] = 'deduplicated'
                continue
            item_copy = copy.deepcopy(item)
            item_copy['_source_workbooks'] = [wb_label]
            item_copy['_merge_action'] = 'unique'
            if name:
                seen_names[name] = item_copy
            result.append(item_copy)
    return result


# ═══════════════════════════════════════════════════════════════════
#  Sprint 54 — Artifact-level merge functions
# ═══════════════════════════════════════════════════════════════════

def _merge_hierarchies(all_extracted: List[dict],
                       workbook_names: List[str]) -> list:
    """Merge hierarchies with level-aware deduplication.

    Same name + same levels → deduplicate.
    Same name + different levels → keep the one with more levels.
    Different names → keep all.
    Cross-workbook hierarchies on the same table are unioned.
    """
    # hierarchy name → (levels_tuple, hierarchy_dict, source_wb)
    seen: Dict[str, Tuple[tuple, dict, str]] = {}
    result = []

    for wb_name, extracted in zip(workbook_names, all_extracted):
        items = extracted.get('hierarchies', [])
        if not isinstance(items, list):
            continue
        for item in items:
            name = item.get('name', '')
            if not name:
                result.append(copy.deepcopy(item))
                continue

            levels = item.get('levels', [])
            levels_key = tuple(
                lv.get('name', lv.get('column', '')) for lv in levels
            )

            if name in seen:
                existing_levels_key, _existing_item, _existing_wb = seen[name]
                if levels_key == existing_levels_key:
                    # Identical → skip duplicate, track source
                    for r in result:
                        if r.get('name', '') == name:
                            r.setdefault('_source_workbooks', []).append(wb_name)
                            r['_merge_action'] = 'deduplicated'
                            break
                    continue
                if len(levels_key) > len(existing_levels_key):
                    # New one has more levels → replace
                    for i, r in enumerate(result):
                        if r.get('name', '') == name:
                            replacement = copy.deepcopy(item)
                            replacement['_source_workbooks'] = [
                                _existing_wb, wb_name]
                            replacement['_merge_action'] = 'deduplicated'
                            result[i] = replacement
                            break
                    seen[name] = (levels_key, item, wb_name)
                    logger.debug(
                        "Hierarchy '%s': replaced (%d levels from %s) with (%d levels from %s)",
                        name, len(existing_levels_key), _existing_wb,
                        len(levels_key), wb_name
                    )
                # else: existing has more or equal levels → keep existing
            else:
                seen[name] = (levels_key, item, wb_name)
                item_copy = copy.deepcopy(item)
                item_copy['_source_workbooks'] = [wb_name]
                item_copy['_merge_action'] = 'unique'
                result.append(item_copy)

    return result


def _calc_group_signature(cg: dict) -> Tuple[Tuple[str, str], ...]:
    """Build a signature tuple for a calculation group's items."""
    items = cg.get('calculationItems', cg.get('calculation_items', []))
    return tuple(sorted(
        (it.get('name', ''), it.get('expression', ''))
        for it in items
    ))


def _merge_calculation_groups(all_extracted: List[dict],
                              workbook_names: List[str]) -> list:
    """Merge calculation groups across workbooks.

    Same name + same calculation items → deduplicate.
    Same name + different items → namespace as 'CalcGroup (Workbook)'.
    Different names → keep all.

    Calculation groups are stored in parameters with domain_type='list'
    where values match measure names. This function collects them from
    extracted parameters and deduplicates.
    """
    # Collect calc-group-like parameters: name → [(wb, param_dict)]
    cg_map: Dict[str, List[Tuple[str, dict]]] = {}

    for wb_name, extracted in zip(workbook_names, all_extracted):
        for param in extracted.get('parameters', []):
            caption = param.get('caption', param.get('name', ''))
            domain_type = param.get('domain_type', '')
            datatype = param.get('datatype', 'string')
            allowable_values = param.get('allowable_values', [])

            if datatype != 'string' or domain_type != 'list' or not allowable_values:
                continue

            # Mark this as calc-group candidate — store with values
            values = [v.get('value', '') for v in allowable_values
                      if v.get('type') != 'range']
            if len(values) < 2:
                continue

            cg_entry = {
                'caption': caption,
                'calculationItems': [
                    {'name': v, 'expression': 'CALCULATE(SELECTEDMEASURE())'}
                    for v in values
                ],
                'values': values,
                '_source_workbook': wb_name,
            }

            if caption not in cg_map:
                cg_map[caption] = []
            cg_map[caption].append((wb_name, cg_entry))

    result = []
    for name, entries in cg_map.items():
        if len(entries) == 1:
            entry = entries[0][1]
            entry['_source_workbooks'] = [entries[0][0]]
            entry['_merge_action'] = 'unique'
            result.append(entry)
            continue

        # Compare signatures
        sigs = {}
        for wb, cg in entries:
            sig = _calc_group_signature(cg)
            sigs[wb] = sig

        unique_sigs = set(sigs.values())
        if len(unique_sigs) == 1:
            # All identical → deduplicate
            entry = entries[0][1]
            entry['_source_workbooks'] = [wb for wb, _ in entries]
            entry['_merge_action'] = 'deduplicated'
            result.append(entry)
        else:
            # Conflict → namespace each
            for wb, cg in entries:
                namespaced = copy.deepcopy(cg)
                namespaced['caption'] = f"{name} ({wb})"
                namespaced['_source_workbook'] = wb
                namespaced['_source_workbooks'] = [wb]
                namespaced['_merge_action'] = 'namespaced'
                result.append(namespaced)
                logger.info(
                    "Calculation group '%s' namespaced → '%s' (conflict)",
                    name, namespaced['caption']
                )

    return result


def _merge_field_parameters(all_extracted: List[dict],
                            workbook_names: List[str]) -> list:
    """Merge field parameters across workbooks.

    Same name + same column references → deduplicate.
    Same name + different columns → union column references.
    Different names → keep all.
    """
    fp_map: Dict[str, List[Tuple[str, dict]]] = {}

    for wb_name, extracted in zip(workbook_names, all_extracted):
        for param in extracted.get('parameters', []):
            caption = param.get('caption', param.get('name', ''))
            domain_type = param.get('domain_type', '')
            datatype = param.get('datatype', 'string')
            allowable_values = param.get('allowable_values', [])

            if datatype != 'string' or domain_type != 'list' or not allowable_values:
                continue

            values = [v.get('value', '') for v in allowable_values
                      if v.get('type') != 'range']
            if len(values) < 2:
                continue

            fp_entry = {
                'caption': caption,
                'values': values,
                '_source_workbook': wb_name,
            }

            if caption not in fp_map:
                fp_map[caption] = []
            fp_map[caption].append((wb_name, fp_entry))

    result = []
    for name, entries in fp_map.items():
        if len(entries) == 1:
            entry = entries[0][1]
            entry['_source_workbooks'] = [entries[0][0]]
            entry['_merge_action'] = 'unique'
            result.append(entry)
            continue

        # Compare value sets
        all_value_sets = [set(fp['values']) for _wb, fp in entries]
        if all(vs == all_value_sets[0] for vs in all_value_sets):
            # All identical → deduplicate
            entry = entries[0][1]
            entry['_source_workbooks'] = [wb for wb, _ in entries]
            entry['_merge_action'] = 'deduplicated'
            result.append(entry)
        else:
            # Union all values (deduplicated, order-preserved)
            seen_vals = set()
            unioned_values = []
            for _wb, fp in entries:
                for v in fp['values']:
                    if v not in seen_vals:
                        seen_vals.add(v)
                        unioned_values.append(v)
            merged_fp = copy.deepcopy(entries[0][1])
            merged_fp['values'] = unioned_values
            merged_fp['_merged_from'] = [wb for wb, _ in entries]
            merged_fp['_source_workbooks'] = [wb for wb, _ in entries]
            merged_fp['_merge_action'] = 'unioned'
            result.append(merged_fp)
            logger.info(
                "Field parameter '%s': unioned %d values from %d workbooks",
                name, len(unioned_values), len(entries)
            )

    return result


def _merge_perspectives(all_extracted: List[dict],
                        workbook_names: List[str]) -> list:
    """Merge perspectives across workbooks.

    Same name → union table references.
    Different names → keep all.
    """
    persp_map: Dict[str, List[Tuple[str, set]]] = {}

    for wb_name, extracted in zip(workbook_names, all_extracted):
        perspectives = extracted.get('_perspectives', [])
        if not perspectives:
            continue
        for persp in perspectives:
            name = persp.get('name', '')
            tables = set(persp.get('tables', []))
            if name not in persp_map:
                persp_map[name] = []
            persp_map[name].append((wb_name, tables))

    result = []
    for name, entries in persp_map.items():
        # Union all table references
        all_tables = set()
        for _wb, tables in entries:
            all_tables.update(tables)
        result.append({
            'name': name,
            'tables': sorted(all_tables),
            '_source_workbooks': [wb for wb, _ in entries],
            '_merge_action': 'deduplicated' if len(entries) > 1 else 'unique',
        })

    return result


def _merge_cultures(all_extracted: List[dict],
                    workbook_names: List[str]) -> list:
    """Merge culture/locale settings across workbooks.

    Same locale → merge translation entries (first-seen wins per key).
    Different locales → keep all.
    """
    culture_map: Dict[str, Dict[str, str]] = {}  # locale → {object_name: translation}
    culture_sources: Dict[str, List[str]] = {}  # locale → [workbook_names]

    for wb_name, extracted in zip(workbook_names, all_extracted):
        culture = extracted.get('culture', '')
        languages = extracted.get('_languages', '')
        cultures_data = extracted.get('_cultures', [])

        # Collect cultures from explicit data
        for c in cultures_data:
            locale = c.get('locale', '')
            translations = c.get('translations', {})
            if locale:
                if locale not in culture_map:
                    culture_map[locale] = {}
                culture_sources.setdefault(locale, []).append(wb_name)
                for key, val in translations.items():
                    if key not in culture_map[locale]:
                        culture_map[locale][key] = val

        # Collect from culture field
        if culture and culture != 'en-US':
            if culture not in culture_map:
                culture_map[culture] = {}
            culture_sources.setdefault(culture, []).append(wb_name)

        # Collect from languages field
        if languages:
            for lang in languages.split(','):
                lang = lang.strip()
                if lang:
                    if lang not in culture_map:
                        culture_map[lang] = {}
                    culture_sources.setdefault(lang, []).append(wb_name)

    result = []
    for locale, translations in culture_map.items():
        result.append({
            'locale': locale,
            'translations': translations,
            '_source_workbooks': culture_sources.get(locale, []),
            '_merge_action': 'deduplicated' if len(culture_sources.get(locale, [])) > 1 else 'unique',
        })

    return result


def _merge_goals(all_extracted: List[dict],
                 workbook_names: List[str]) -> list:
    """Merge goals/scorecard metrics across workbooks.

    Same metric name + same measure → deduplicate.
    Same metric name + different measure → namespace.
    Different names → keep all.
    """
    goal_map: Dict[str, List[Tuple[str, dict]]] = {}

    for wb_name, extracted in zip(workbook_names, all_extracted):
        goals = extracted.get('_goals', [])
        if not goals:
            continue
        for goal in goals:
            name = goal.get('name', goal.get('metric_name', ''))
            if not name:
                continue
            if name not in goal_map:
                goal_map[name] = []
            goal_map[name].append((wb_name, goal))

    result = []
    for name, entries in goal_map.items():
        if len(entries) == 1:
            g_copy = copy.deepcopy(entries[0][1])
            g_copy['_source_workbooks'] = [entries[0][0]]
            g_copy['_merge_action'] = 'unique'
            result.append(g_copy)
            continue

        # Compare measure references
        measures = {}
        for wb, goal in entries:
            measure = goal.get('measure', goal.get('measure_name', ''))
            measures[wb] = measure

        unique_measures = set(measures.values())
        if len(unique_measures) == 1:
            # Identical → deduplicate
            g_copy = copy.deepcopy(entries[0][1])
            g_copy['_source_workbooks'] = [wb for wb, _ in entries]
            g_copy['_merge_action'] = 'deduplicated'
            result.append(g_copy)
        else:
            # Conflict → namespace
            for wb, goal in entries:
                namespaced = copy.deepcopy(goal)
                namespaced['name'] = f"{name} ({wb})"
                namespaced['_source_workbook'] = wb
                namespaced['_source_workbooks'] = [wb]
                namespaced['_merge_action'] = 'namespaced'
                result.append(namespaced)
                logger.info(
                    "Goal '%s' namespaced → '%s' (different measures)",
                    name, namespaced['name']
                )

    return result


# ═══════════════════════════════════════════════════════════════════
#  Field remapping — for thin reports referencing namespaced measures
# ═══════════════════════════════════════════════════════════════════

def build_field_mapping(assessment: MergeAssessment,
                        workbook_name: str) -> Dict[str, str]:
    """Build a field name mapping for a specific workbook's thin report.

    Maps original measure names to their namespaced versions (if conflicts exist).

    Args:
        assessment: The merge assessment.
        workbook_name: Which workbook's report we're generating.

    Returns:
        {original_name: namespaced_name} — only for conflicting measures.
    """
    mapping = {}
    for conflict in assessment.measure_conflicts:
        if workbook_name in conflict.variants:
            mapping[conflict.name] = f"{conflict.name} ({workbook_name})"
    return mapping


# ═══════════════════════════════════════════════════════════════════
#  Visual field validation
# ═══════════════════════════════════════════════════════════════════

def validate_thin_report_fields(converted_objects: dict,
                                 merged: dict,
                                 field_mapping: Optional[dict] = None) -> List[dict]:
    """Validate that all fields referenced by a thin report exist in the merged model.

    Checks worksheet columns, filters, mark encoding fields against the merged
    model's tables, columns, and measures.

    Args:
        converted_objects: The workbook's original extracted objects.
        merged: The merged converted_objects dict.
        field_mapping: Active field mapping (original → namespaced).

    Returns:
        List of validation issues: [{"field": str, "location": str, "issue": str}]
    """
    # Build set of available fields from merged model
    available = set()
    for ds in merged.get('datasources', []):
        for table in ds.get('tables', []):
            for col in table.get('columns', []):
                available.add(col.get('name', '').lower())
        for calc in ds.get('calculations', []):
            available.add(calc.get('caption', calc.get('name', '')).lower())

    for calc in merged.get('calculations', []):
        available.add(calc.get('caption', calc.get('name', '')).lower())

    for param in merged.get('parameters', []):
        available.add(param.get('name', param.get('caption', '')).lower())

    mapping = field_mapping or {}
    issues = []

    for ws in converted_objects.get('worksheets', []):
        ws_name = ws.get('name', 'unknown')

        # Check columns
        for col in ws.get('columns', []):
            field_name = col.get('name', '')
            mapped = mapping.get(field_name, field_name)
            if mapped.lower() not in available and field_name.lower() not in available:
                issues.append({
                    "field": field_name,
                    "location": f"worksheet '{ws_name}' columns",
                    "issue": "orphaned_field",
                })

        # Check filters
        for f in ws.get('filters', []):
            field_name = f.get('field', '')
            mapped = mapping.get(field_name, field_name)
            if field_name and mapped.lower() not in available and field_name.lower() not in available:
                issues.append({
                    "field": field_name,
                    "location": f"worksheet '{ws_name}' filters",
                    "issue": "orphaned_filter",
                })

        # Check mark encoding
        for channel, enc in ws.get('mark_encoding', {}).items():
            if isinstance(enc, dict):
                field_name = enc.get('field', '')
                mapped = mapping.get(field_name, field_name)
                if field_name and mapped.lower() not in available and field_name.lower() not in available:
                    issues.append({
                        "field": field_name,
                        "location": f"worksheet '{ws_name}' mark_encoding.{channel}",
                        "issue": "orphaned_encoding",
                    })

    return issues


# ═══════════════════════════════════════════════════════════════════
#  Column lineage tracking
# ═══════════════════════════════════════════════════════════════════

def build_column_lineage(all_extracted: List[dict],
                         workbook_names: List[str],
                         assessment: MergeAssessment) -> Dict[str, Dict[str, Any]]:
    """Build lineage metadata showing which workbooks contributed each table/column.

    Returns:
        {table_name: {"source_workbooks": [str], "columns": {col_name: [source_wbs]}}}
    """
    lineage: Dict[str, Dict] = {}

    for mc in assessment.merge_candidates:
        table_name = mc.table_name
        source_wbs = [s[0] for s in mc.sources]
        col_sources: Dict[str, list] = {}

        for wb_name, table, _ in mc.sources:
            for col in table.get('columns', []):
                cname = col.get('name', '')
                if cname not in col_sources:
                    col_sources[cname] = []
                if wb_name not in col_sources[cname]:
                    col_sources[cname].append(wb_name)

        lineage[table_name] = {
            "source_workbooks": source_wbs,
            "columns": col_sources,
        }

    # Also capture unique tables
    for wb, tables in assessment.unique_tables.items():
        for tname in tables:
            lineage[tname] = {
                "source_workbooks": [wb],
                "columns": {},
            }
            # Find the table's columns
            for extracted in all_extracted:
                for ds in extracted.get('datasources', []):
                    for t in ds.get('tables', []):
                        if t.get('name', '') == tname:
                            for col in t.get('columns', []):
                                lineage[tname]["columns"][col.get('name', '')] = [wb]

    return lineage


def generate_lineage_annotations(lineage: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Generate TMDL annotation strings for column lineage.

    Returns:
        {table_name: annotation_text} — ready to embed in TMDL table files.
    """
    annotations = {}
    for table_name, info in lineage.items():
        sources = info.get("source_workbooks", [])
        lines = [f"Source workbooks: {', '.join(sources)}"]
        col_info = info.get("columns", {})
        multi_source = [c for c, wbs in col_info.items() if len(wbs) > 1]
        single_source = [c for c, wbs in col_info.items() if len(wbs) == 1]
        if multi_source:
            lines.append(f"Shared columns ({len(multi_source)}): {', '.join(sorted(multi_source)[:10])}")
        if single_source:
            lines.append(f"Unique columns ({len(single_source)}): {', '.join(sorted(single_source)[:10])}")
        annotations[table_name] = " | ".join(lines)
    return annotations


# ═══════════════════════════════════════════════════════════════════
#  Measure expression risk analyzer
# ═══════════════════════════════════════════════════════════════════

@dataclass
class MeasureRiskAssessment:
    """Risk analysis for a measure conflict."""
    measure_name: str
    risk_level: str   # "low", "medium", "high"
    reason: str
    variants: Dict[str, str]  # {wb_name: formula}
    aggregation_types: Dict[str, str]  # {wb_name: detected_agg}


# ═══════════════════════════════════════════════════════════════════
#  Sprint 51 — Merge extensions
# ═══════════════════════════════════════════════════════════════════

def _normalize_sql(sql: str) -> str:
    """Normalize SQL text for comparison (case-fold, collapse whitespace)."""
    if not sql:
        return ''
    normalized = re.sub(r'\s+', ' ', sql.strip().lower())
    # Remove trailing semicolons
    return normalized.rstrip(';').strip()


def _hash_sql(sql: str) -> str:
    """Hash normalized SQL text for fingerprint comparison."""
    return hashlib.sha256(_normalize_sql(sql).encode('utf-8')).hexdigest()[:16]


def build_custom_sql_fingerprints(
    datasources: list,
) -> Dict[str, Tuple[str, dict, dict]]:
    """Build fingerprints for custom SQL tables based on query text hash.

    Returns:
        {sql_hash: (sql_text, table_dict, connection_dict)}
    """
    result = {}
    for ds in datasources:
        conn = ds.get('connection', {})
        for table in ds.get('tables', []):
            sql = table.get('custom_sql', table.get('query', ''))
            if not sql:
                continue
            sql_hash = _hash_sql(sql)
            if sql_hash not in result:
                result[sql_hash] = (sql, table, conn)
    return result


def _normalize_table_name_fuzzy(name: str) -> str:
    """Normalize a table name for fuzzy matching.

    Strips schema prefix, removes brackets, folds case, removes
    common separators (underscore, hyphen, space).
    """
    # Strip schema prefix
    clean = name.strip().strip('[]')
    if '.' in clean:
        parts = clean.rsplit('.', 1)
        clean = parts[-1].strip('[]')
    # Case fold and remove separators
    clean = clean.lower().replace('_', '').replace('-', '').replace(' ', '')
    return clean


def fuzzy_table_match(name_a: str, name_b: str) -> float:
    """Compute fuzzy similarity between two table names.

    Uses normalized string comparison: exact match = 1.0, prefix/suffix
    overlap as secondary signal.

    Returns:
        Float 0.0–1.0 representing name similarity.
    """
    norm_a = _normalize_table_name_fuzzy(name_a)
    norm_b = _normalize_table_name_fuzzy(name_b)

    if not norm_a or not norm_b:
        return 0.0

    if norm_a == norm_b:
        return 1.0

    # Check containment
    if norm_a in norm_b or norm_b in norm_a:
        shorter = min(len(norm_a), len(norm_b))
        longer = max(len(norm_a), len(norm_b))
        return shorter / longer

    # Character-level Jaccard on bigrams
    def _bigrams(s):
        return set(s[i:i+2] for i in range(len(s) - 1)) if len(s) > 1 else {s}

    bg_a = _bigrams(norm_a)
    bg_b = _bigrams(norm_b)
    if not bg_a or not bg_b:
        return 0.0
    return len(bg_a & bg_b) / len(bg_a | bg_b)


def detect_rls_conflicts(
    all_extracted: List[dict],
    workbook_names: List[str],
) -> List[dict]:
    """Detect overlapping RLS roles across workbooks.

    When merging models, detects roles that target the same table but
    have different filter expressions.

    Returns:
        List of conflict dicts: [{table, role_name, variants: {wb: expression}}]
    """
    # role_key (table, role_name) → {wb: expression}
    role_map: Dict[Tuple[str, str], Dict[str, str]] = {}

    for wb_name, extracted in zip(workbook_names, all_extracted):
        for uf in extracted.get('user_filters', []):
            table = uf.get('table', uf.get('datasource', 'default')).lower()
            role_name = uf.get('name', uf.get('field', 'unknown'))
            expression = uf.get('formula', uf.get('values', ''))
            if isinstance(expression, list):
                expression = str(sorted(expression))
            key = (table, role_name)
            if key not in role_map:
                role_map[key] = {}
            role_map[key][wb_name] = str(expression)

    conflicts = []
    for (table, role_name), variants in role_map.items():
        if len(variants) <= 1:
            continue
        unique_expressions = set(variants.values())
        if len(unique_expressions) > 1:
            conflicts.append({
                'table': table,
                'role_name': role_name,
                'variants': variants,
            })

    return conflicts


def suggest_cross_workbook_relationships(
    merged: dict,
) -> List[dict]:
    """Suggest potential relationships between tables in a merged model.

    Scans all table column names for matching patterns (e.g., customer_id
    in different tables) and suggests relationships.

    Returns:
        List of suggestion dicts: [{from_table, from_column, to_table, to_column, confidence}]
    """
    from powerbi_import.tmdl_relationships import _physical_join_scopes

    tables = []
    for ds in merged.get('datasources', []):
        for table in ds.get('tables', []):
            tname = table.get('name', '')
            cols = {c.get('name', '').lower(): c for c in table.get('columns', [])}
            tables.append((tname, cols))

    # Build existing relationship set
    existing_rels = set()
    for ds in merged.get('datasources', []):
        # A nested physical scope can contain pairs without a direct source
        # equality. They are protected, not opportunities for a guessed edge.
        for pair in _physical_join_scopes(ds):
            names = [name.lower() for name in pair if isinstance(name, str)]
            if len(names) == 2:
                existing_rels.add((names[0], names[1]))
                existing_rels.add((names[1], names[0]))
        for rel in ds.get('relationships', []):
            if 'left' in rel:
                existing_rels.add((
                    rel['left'].get('table', '').lower(),
                    rel['right'].get('table', '').lower(),
                ))
                existing_rels.add((
                    rel['right'].get('table', '').lower(),
                    rel['left'].get('table', '').lower(),
                ))
            else:
                existing_rels.add((
                    rel.get('from_table', '').lower(),
                    rel.get('to_table', '').lower(),
                ))
                existing_rels.add((
                    rel.get('to_table', '').lower(),
                    rel.get('from_table', '').lower(),
                ))

    suggestions = []
    key_suffixes = ('_id', '_key', '_code', 'id', 'key', 'code')

    for i, (t1_name, t1_cols) in enumerate(tables):
        for j, (t2_name, t2_cols) in enumerate(tables):
            if i >= j:
                continue
            if (t1_name.lower(), t2_name.lower()) in existing_rels:
                continue

            for col_name in t1_cols:
                if not any(col_name.endswith(s) for s in key_suffixes):
                    continue
                if col_name in t2_cols:
                    suggestions.append({
                        'from_table': t1_name,
                        'from_column': col_name,
                        'to_table': t2_name,
                        'to_column': col_name,
                        'confidence': 'high' if col_name.endswith('_id') else 'medium',
                    })

    return suggestions


def merge_preview(
    all_extracted: List[dict],
    workbook_names: List[str],
) -> dict:
    """Run a dry-run merge assessment and return detailed preview.

    Does not write any files — just reports what would happen. Unsupported
    source predicates raise the same clear blocker as a real merge, rather
    than reporting relationship suggestions for a lossy projection.

    Returns:
        Dict with keys: assessment, suggestions, rls_conflicts, actions
    """
    assessment = assess_merge(all_extracted, workbook_names)

    # Detect RLS conflicts
    rls_conflicts = detect_rls_conflicts(all_extracted, workbook_names)

    # Build merged model in memory for relationship suggestions
    merged = merge_semantic_models(all_extracted, assessment, "PreviewModel")
    suggestions = suggest_cross_workbook_relationships(merged)

    # Build action log
    actions = []
    for mc in assessment.merge_candidates:
        actions.append({
            'action': 'merge_table',
            'table': mc.table_name,
            'sources': [s[0] for s in mc.sources],
            'overlap': round(mc.column_overlap, 2),
        })

    for conflict in assessment.measure_conflicts:
        for wb, formula in conflict.variants.items():
            actions.append({
                'action': 'namespace_measure',
                'measure': conflict.name,
                'workbook': wb,
                'new_name': f"{conflict.name} ({wb})",
            })

    for wb, tables in assessment.isolated_tables.items():
        for t in tables:
            actions.append({
                'action': 'skip_isolated_table',
                'table': t,
                'workbook': wb,
            })

    return {
        'assessment': assessment.to_dict(),
        'rls_conflicts': rls_conflicts,
        'relationship_suggestions': suggestions,
        'actions': actions,
        'total_actions': len(actions),
    }


def analyze_measure_risk(conflicts: List[MeasureConflict]) -> List[MeasureRiskAssessment]:
    """Analyze semantic risk of measure conflicts by parsing DAX patterns.

    A conflict where both formulas use the same aggregation on the same column
    is LOW risk (likely formatting/alias difference). A conflict using different
    aggregation functions (SUM vs COUNT) on different columns is HIGH risk.

    Returns:
        List of MeasureRiskAssessment for each conflict.
    """
    results = []
    for mc in conflicts:
        agg_types = {}
        columns_ref = {}
        for wb, formula in mc.variants.items():
            agg = _detect_aggregation(formula)
            agg_types[wb] = agg
            columns_ref[wb] = _extract_column_refs(formula)

        unique_aggs = set(agg_types.values()) - {'unknown'}
        unique_cols = set()
        for cols in columns_ref.values():
            unique_cols.update(cols)

        if len(unique_aggs) <= 1 and len(unique_cols) <= 1:
            risk = "low"
            reason = "Same aggregation pattern, likely formatting difference"
        elif len(unique_aggs) <= 1:
            risk = "medium"
            reason = f"Same aggregation ({', '.join(unique_aggs or {'?'})}), different columns"
        else:
            risk = "high"
            reason = f"Different aggregations: {', '.join(unique_aggs)}"

        results.append(MeasureRiskAssessment(
            measure_name=mc.name,
            risk_level=risk,
            reason=reason,
            variants=mc.variants,
            aggregation_types=agg_types,
        ))

    return results


_AGG_PATTERN = re.compile(
    r'\b(SUM|SUMX|COUNT|COUNTA|COUNTX|COUNTROWS|DISTINCTCOUNT|'
    r'AVERAGE|AVERAGEX|MIN|MINX|MAX|MAXX|'
    r'CALCULATE|CALCULATETABLE|MEDIAN|PERCENTILE)\s*\(',
    re.IGNORECASE,
)

_COL_REF_PATTERN = re.compile(r"\[([^\]]+)\]")


def _detect_aggregation(formula: str) -> str:
    """Detect the primary aggregation function in a DAX formula."""
    m = _AGG_PATTERN.search(formula)
    return m.group(1).upper() if m else 'unknown'


def _extract_column_refs(formula: str) -> List[str]:
    """Extract column references [ColumnName] from a DAX formula."""
    return _COL_REF_PATTERN.findall(formula)


# ═══════════════════════════════════════════════════════════════════
#  RLS role consolidation
# ═══════════════════════════════════════════════════════════════════

@dataclass
class RLSConsolidation:
    """Analysis of RLS role consolidation across workbooks."""
    role_name: str
    source_workbooks: List[str]
    tables_affected: List[str]
    filter_expressions: Dict[str, str]  # {wb_name: dax_filter}
    action: str  # "keep", "merge", "namespace"
    merged_expression: Optional[str] = None


def consolidate_rls_roles(all_extracted: List[dict],
                          workbook_names: List[str],
                          strategy: str = "or") -> List[RLSConsolidation]:
    """Analyze and consolidate RLS roles across workbooks.

    Args:
        all_extracted: List of extracted data dicts.
        workbook_names: Corresponding workbook names.
        strategy: Predicate merge strategy - ``"and"`` (both must hold),
            ``"or"`` (either suffices), or ``"namespace"`` (keep separate).

    Rules:
    - Same role name + same filter expression → deduplicate (keep one)
    - Same role name + different table → keep both (different scope)
    - Same role name + same table + different filters → merge with strategy
    - Different role names → keep all

    Returns:
        List of RLSConsolidation recommendations.
    """
    # Collect all roles: role_name → [(wb_name, table, filter_expr)]
    role_map: Dict[str, list] = {}

    for wb_name, extracted in zip(workbook_names, all_extracted):
        for uf in extracted.get('user_filters', []):
            role_name = uf.get('name', uf.get('field', 'default'))
            table = uf.get('table', '')
            filter_expr = uf.get('filter_expression', uf.get('formula', ''))
            if role_name not in role_map:
                role_map[role_name] = []
            role_map[role_name].append((wb_name, table, filter_expr))

    results = []
    for role_name, entries in role_map.items():
        source_wbs = list(set(e[0] for e in entries))
        tables = list(set(e[1] for e in entries if e[1]))
        filters = {e[0]: e[2] for e in entries if e[2]}

        if len(source_wbs) == 1:
            results.append(RLSConsolidation(
                role_name=role_name,
                source_workbooks=source_wbs,
                tables_affected=tables,
                filter_expressions=filters,
                action="keep",
            ))
        else:
            unique_filters = set(filters.values())
            if len(unique_filters) <= 1:
                results.append(RLSConsolidation(
                    role_name=role_name,
                    source_workbooks=source_wbs,
                    tables_affected=tables,
                    filter_expressions=filters,
                    action="merge",
                    merged_expression=next(iter(unique_filters), None),
                ))
            elif strategy == "namespace":
                # Keep separate roles with workbook prefix
                for wb_name, expr in filters.items():
                    results.append(RLSConsolidation(
                        role_name=f"{role_name} ({wb_name})",
                        source_workbooks=[wb_name],
                        tables_affected=tables,
                        filter_expressions={wb_name: expr},
                        action="namespace",
                        merged_expression=expr,
                    ))
            else:
                # Merge with AND or OR
                joiner = " && " if strategy == "and" else " || "
                combined = joiner.join(
                    f"({expr})" for expr in unique_filters if expr
                )
                results.append(RLSConsolidation(
                    role_name=role_name,
                    source_workbooks=source_wbs,
                    tables_affected=tables,
                    filter_expressions=filters,
                    action="merge",
                    merged_expression=combined,
                ))

    return results


def merge_rls_roles(all_extracted: List[dict],
                    workbook_names: List[str]) -> List[dict]:
    """Merge RLS roles across workbooks with deduplication.

    Returns:
        Merged list of user_filter dicts ready for TMDL generation.
    """
    consolidations = consolidate_rls_roles(all_extracted, workbook_names)
    merged_roles = []
    seen_names = set()

    for cons in consolidations:
        if cons.role_name in seen_names:
            continue
        seen_names.add(cons.role_name)

        if cons.action == "keep":
            # Find original and keep it
            for wb_name, extracted in zip(workbook_names, all_extracted):
                for uf in extracted.get('user_filters', []):
                    rn = uf.get('name', uf.get('field', ''))
                    if rn == cons.role_name:
                        merged_roles.append(copy.deepcopy(uf))
                        break
                else:
                    continue
                break
        elif cons.action == "merge" and cons.merged_expression:
            # Create merged role
            role = {
                'name': cons.role_name,
                'table': cons.tables_affected[0] if cons.tables_affected else '',
                'filter_expression': cons.merged_expression,
                '_source_workbooks': cons.source_workbooks,
                '_consolidation': 'merged',
            }
            merged_roles.append(role)

    return merged_roles


def validate_rls_propagation(merged: dict) -> List[dict]:
    """Validate RLS roles have propagation paths to fact tables.

    For each RLS role's ``tablePermission`` target, verify the table
    exists in the model AND has a relationship path (direct or indirect)
    to at least one other table.

    Returns:
        List of ``{role, table, status, reason}`` dicts.
    """
    tables = {t.get('name', '') for t in merged.get('tables', []) if t.get('name')}
    # Also search inside datasources (merge output nests tables there)
    for ds in merged.get('datasources', []):
        for t in ds.get('tables', []):
            if t.get('name'):
                tables.add(t['name'])
    rels = merged.get('relationships', [])
    for ds in merged.get('datasources', []):
        rels = rels + ds.get('relationships', [])
    # Build adjacency set for connected tables
    connected = set()
    for r in rels:
        ft = r.get('fromTable', '')
        tt = r.get('toTable', '')
        if ft:
            connected.add(ft)
        if tt:
            connected.add(tt)

    results = []
    for uf in merged.get('user_filters', []):
        role_name = uf.get('name', uf.get('field', 'unknown'))
        tbl = uf.get('table', '')
        if not tbl:
            results.append({
                'role': role_name, 'table': '', 'status': 'warning',
                'reason': 'No table specified for tablePermission',
            })
            continue
        if tbl not in tables:
            results.append({
                'role': role_name, 'table': tbl, 'status': 'error',
                'reason': f'Table "{tbl}" not found in model',
            })
        elif tbl not in connected:
            results.append({
                'role': role_name, 'table': tbl, 'status': 'warning',
                'reason': f'Table "{tbl}" is isolated (no relationships)',
            })
        else:
            results.append({
                'role': role_name, 'table': tbl, 'status': 'ok',
                'reason': '',
            })
    return results


def validate_rls_principals(merged: dict) -> List[dict]:
    """Detect conflicting principal format requirements across merged RLS.

    Parses ``USERPRINCIPALNAME()`` patterns to detect format conflicts
    (e.g., ``user@domain.com`` vs ``DOMAIN\\user``).

    Returns:
        List of ``{role, format_detected, expression}`` dicts.
    """
    results = []
    for uf in merged.get('user_filters', []):
        role_name = uf.get('name', uf.get('field', 'unknown'))
        expr = uf.get('filter_expression', uf.get('formula', ''))
        if not expr:
            continue
        fmt = 'unknown'
        if 'USERPRINCIPALNAME()' in expr.upper():
            fmt = 'UPN (user@domain.com)'
        elif 'USERNAME()' in expr.upper():
            fmt = 'USERNAME (DOMAIN\\user)'
        results.append({
            'role': role_name,
            'format_detected': fmt,
            'expression': expr[:100],
        })
    return results


def detect_isolated_tables(merged: dict) -> List[dict]:
    """Detect tables that have no relationships (isolated).

    Returns:
        List of ``{table, reason}`` dicts for tables with no
        relationship connections.
    """
    tables = [t.get('name', '') for t in merged.get('tables', []) if t.get('name')]
    # Also search inside datasources (merge output nests tables there)
    for ds in merged.get('datasources', []):
        for t in ds.get('tables', []):
            if t.get('name'):
                tables.append(t['name'])
    rels = merged.get('relationships', [])
    for ds in merged.get('datasources', []):
        rels = rels + ds.get('relationships', [])
    connected = set()
    for r in rels:
        ft = r.get('fromTable', '')
        tt = r.get('toTable', '')
        if ft:
            connected.add(ft)
        if tt:
            connected.add(tt)

    isolated = []
    for tname in tables:
        if tname not in connected:
            isolated.append({
                'table': tname,
                'reason': f'Table "{tname}" has no relationships — may need manual wiring',
            })
            logger.warning("Isolated table detected: %s", tname)
    return isolated


# ═══════════════════════════════════════════════════════════════════
#  Cross-report navigation
# ═══════════════════════════════════════════════════════════════════

def build_cross_report_navigation(workbook_names: List[str],
                                   model_name: str) -> List[dict]:
    """Generate navigation button definitions for cross-report linking.

    Creates action button configs that allow users to navigate between
    sibling thin reports referencing the same shared semantic model.

    Args:
        workbook_names: Names of all thin reports.
        model_name: Name of the shared semantic model.

    Returns:
        List of navigation button definitions (one per report, containing
        links to all other reports).
    """
    nav_configs = []
    for current_wb in workbook_names:
        buttons = []
        for target_wb in workbook_names:
            if target_wb == current_wb:
                continue
            buttons.append({
                "label": target_wb,
                "target_report": f"{target_wb}.Report",
                "tooltip": f"Navigate to {target_wb}",
                "type": "navigation",
            })
        nav_configs.append({
            "report_name": current_wb,
            "model_name": model_name,
            "navigation_buttons": buttons,
        })
    return nav_configs


# ═══════════════════════════════════════════════════════════════════
#  TMDL reverse-engineering — load existing model from .tmdl files
# ═══════════════════════════════════════════════════════════════════

_UNQUOTE_RE = re.compile(r"^'((?:''|[^'])*)'$")


def _unquote_name(name: str) -> str:
    """Strip TMDL single-quote escaping: 'My Table' → My Table."""
    name = name.strip()
    m = _UNQUOTE_RE.match(name)
    if m:
        return m.group(1).replace("''", "'")
    return name


def _parse_tmdl_table(text: str) -> dict:
    """Parse a single ``table`` TMDL block into a dict.

    Returns a table dict compatible with the merged ``converted_objects``
    format used by ``tmdl_generator.generate_tmdl()``.
    """
    table = {"name": "", "columns": [], "measures": [], "partitions": [],
             "hierarchies": [], "annotations": []}

    lines = text.splitlines()
    if not lines:
        return table

    # First line: table 'Name' or table Name
    header = lines[0].strip()
    if header.startswith("table "):
        table["name"] = _unquote_name(header[6:].strip())

    i = 1
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ── measure ──
        if stripped.startswith("measure "):
            measure, i = _parse_tmdl_measure(lines, i)
            table["measures"].append(measure)
            continue

        # ── column ──
        if stripped.startswith("column "):
            column, i = _parse_tmdl_column(lines, i)
            table["columns"].append(column)
            continue

        # ── hierarchy ──
        if stripped.startswith("hierarchy "):
            hierarchy, i = _parse_tmdl_hierarchy(lines, i)
            table["hierarchies"].append(hierarchy)
            continue

        # ── partition ──
        if stripped.startswith("partition "):
            partition, i = _parse_tmdl_partition(lines, i)
            table["partitions"].append(partition)
            continue

        # ── annotation ──
        if stripped.startswith("annotation "):
            table["annotations"].append(stripped)

        i += 1

    return table


def _parse_tmdl_measure(lines: list, start: int) -> tuple:
    """Parse a measure block starting at *start*. Returns (measure_dict, next_index)."""
    header = lines[start].strip()
    # measure 'Name' = expression  OR  measure 'Name' = ```
    rest = header[len("measure "):].strip()

    # Find name vs expression split
    eq_pos = rest.find(" = ")
    if eq_pos >= 0:
        name = _unquote_name(rest[:eq_pos])
        expr_start = rest[eq_pos + 3:]
    else:
        name = _unquote_name(rest)
        expr_start = ""

    measure = {"name": name, "expression": "", "formatString": "",
               "displayFolder": "", "isHidden": False, "description": ""}

    # Handle multi-line expression (```)
    if expr_start.strip() == "```":
        expr_lines = []
        i = start + 1
        while i < len(lines):
            sl = lines[i].strip()
            if sl == "```":
                i += 1
                break
            expr_lines.append(sl)
            i += 1
        measure["expression"] = "\n".join(expr_lines)
    else:
        measure["expression"] = expr_start
        i = start + 1

    # Parse properties until next top-level item
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or _is_top_level(stripped):
            break
        if stripped.startswith("formatString:"):
            measure["formatString"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("displayFolder:"):
            measure["displayFolder"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("description:"):
            measure["description"] = stripped.split(":", 1)[1].strip()
        elif stripped == "isHidden":
            measure["isHidden"] = True
        i += 1

    return measure, i


def _parse_tmdl_column(lines: list, start: int) -> tuple:
    """Parse a column block. Returns (column_dict, next_index)."""
    from powerbi_import.tmdl_writers import _parse_source_column

    header = lines[start].strip()
    rest = header[len("column "):].strip()

    eq_pos = rest.find(" = ")
    is_calculated = eq_pos >= 0
    if is_calculated:
        name = _unquote_name(rest[:eq_pos])
        expr_start = rest[eq_pos + 3:]
    else:
        name = _unquote_name(rest)
        expr_start = ""

    column = {"name": name, "dataType": "string", "sourceColumn": name,
              "isCalculated": is_calculated, "isHidden": False, "isKey": False,
              "dataCategory": "", "description": "", "displayFolder": "",
              "sortByColumn": "", "formatString": "", "summarizeBy": "none"}

    if is_calculated:
        if expr_start.strip() == "```":
            expr_lines = []
            i = start + 1
            while i < len(lines):
                sl = lines[i].strip()
                if sl == "```":
                    i += 1
                    break
                expr_lines.append(sl)
                i += 1
            column["expression"] = "\n".join(expr_lines)
        else:
            column["expression"] = expr_start
            i = start + 1
    else:
        i = start + 1

    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or _is_top_level(stripped):
            break
        if stripped.startswith("dataType:"):
            column["dataType"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("sourceColumn:"):
            column["sourceColumn"] = _parse_source_column(stripped.split(":", 1)[1])
        elif stripped.startswith("formatString:"):
            column["formatString"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("summarizeBy:"):
            column["summarizeBy"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("dataCategory:"):
            column["dataCategory"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("displayFolder:"):
            column["displayFolder"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("description:"):
            column["description"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("sortByColumn:"):
            column["sortByColumn"] = _unquote_name(stripped.split(":", 1)[1].strip())
        elif stripped == "isHidden":
            column["isHidden"] = True
        elif stripped == "isKey":
            column["isKey"] = True
        i += 1

    return column, i


def _parse_tmdl_hierarchy(lines: list, start: int) -> tuple:
    """Parse a hierarchy block. Returns (hierarchy_dict, next_index)."""
    header = lines[start].strip()
    name = _unquote_name(header[len("hierarchy "):].strip())
    hierarchy = {"name": name, "levels": []}
    i = start + 1
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or _is_top_level(stripped):
            break
        if stripped.startswith("level "):
            level_name = _unquote_name(stripped[len("level "):].strip())
            level = {"name": level_name, "column": level_name}
            i += 1
            while i < len(lines):
                s2 = lines[i].strip()
                if not s2 or _is_top_level(s2) or s2.startswith("level "):
                    break
                if s2.startswith("column:"):
                    level["column"] = _unquote_name(s2.split(":", 1)[1].strip())
                i += 1
            hierarchy["levels"].append(level)
            continue
        i += 1
    return hierarchy, i


def _parse_tmdl_partition(lines: list, start: int) -> tuple:
    """Parse a partition block. Returns (partition_dict, next_index)."""
    header = lines[start].strip()
    name = _unquote_name(header[len("partition "):].strip())
    partition = {"name": name, "source": {"type": "m", "expression": ""}}
    i = start + 1
    expr_lines = []
    in_expression = False
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped and not in_expression:
            # Check if next non-blank is still partition content
            if i + 1 < len(lines) and _is_top_level(lines[i + 1].strip()):
                break
            if i + 1 >= len(lines):
                break
            i += 1
            continue
        if _is_top_level(stripped) and not in_expression:
            break
        if stripped.startswith("mode:"):
            partition.setdefault("mode", stripped.split(":", 1)[1].strip())
        elif stripped.startswith("source"):
            in_expression = False
        elif stripped.startswith("expression"):
            in_expression = True
            # Inline expression after =
            after_eq = stripped.split("=", 1)
            if len(after_eq) > 1 and after_eq[1].strip():
                if after_eq[1].strip() == "```":
                    i += 1
                    while i < len(lines):
                        sl = lines[i].strip()
                        if sl == "```":
                            i += 1
                            break
                        expr_lines.append(lines[i].rstrip())
                        i += 1
                    in_expression = False
                    continue
                else:
                    expr_lines.append(after_eq[1].strip())
        elif in_expression:
            if stripped == "```":
                in_expression = False
            else:
                expr_lines.append(lines[i].rstrip())
        i += 1
    if expr_lines:
        partition["source"]["expression"] = "\n".join(expr_lines)
    return partition, i


def _is_top_level(stripped: str) -> bool:
    """Check if a stripped line starts a new top-level TMDL element."""
    return (stripped.startswith("measure ") or
            stripped.startswith("column ") or
            stripped.startswith("hierarchy ") or
            stripped.startswith("partition ") or
            stripped.startswith("annotation ") or
            stripped.startswith("table ") or
            stripped.startswith("relationship ") or
            stripped.startswith("role "))


def _parse_tmdl_relationships(text: str) -> list:
    """Parse ``relationships.tmdl`` content into a list of relationship dicts."""
    relationships = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("relationship "):
            rel_id = stripped[len("relationship "):].strip()
            rel = {"name": rel_id, "fromTable": "", "fromColumn": "",
                   "toTable": "", "toColumn": "",
                   "fromCardinality": "many", "toCardinality": "one",
                   "crossFilteringBehavior": "oneDirection", "isActive": True}
            i += 1
            while i < len(lines):
                s = lines[i].strip()
                if not s:
                    i += 1
                    continue
                if s.startswith("relationship "):
                    break
                if s.startswith("fromColumn:"):
                    parts = s.split(":", 1)[1].strip()
                    dot = parts.rfind(".")
                    if dot >= 0:
                        rel["fromTable"] = _unquote_name(parts[:dot])
                        rel["fromColumn"] = _unquote_name(parts[dot + 1:])
                    else:
                        rel["fromColumn"] = _unquote_name(parts)
                elif s.startswith("toColumn:"):
                    parts = s.split(":", 1)[1].strip()
                    dot = parts.rfind(".")
                    if dot >= 0:
                        rel["toTable"] = _unquote_name(parts[:dot])
                        rel["toColumn"] = _unquote_name(parts[dot + 1:])
                    else:
                        rel["toColumn"] = _unquote_name(parts)
                elif s.startswith("fromCardinality:"):
                    rel["fromCardinality"] = s.split(":", 1)[1].strip()
                elif s.startswith("toCardinality:"):
                    rel["toCardinality"] = s.split(":", 1)[1].strip()
                elif s.startswith("crossFilteringBehavior:"):
                    rel["crossFilteringBehavior"] = s.split(":", 1)[1].strip()
                elif s.startswith("isActive:"):
                    rel["isActive"] = s.split(":", 1)[1].strip().lower() != "false"
                i += 1
            relationships.append(rel)
            continue
        i += 1
    return relationships


def _parse_tmdl_roles(text: str) -> list:
    """Parse ``roles.tmdl`` content into a list of role dicts."""
    roles = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("role "):
            role_name = _unquote_name(stripped[len("role "):].strip())
            role = {"name": role_name, "modelPermission": "read",
                    "tablePermissions": [], "_migration_note": ""}
            i += 1
            while i < len(lines):
                s = lines[i].strip()
                if not s:
                    i += 1
                    continue
                if s.startswith("role "):
                    break
                if s.startswith("modelPermission:"):
                    role["modelPermission"] = s.split(":", 1)[1].strip()
                elif s.startswith("annotation MigrationNote"):
                    val = s.split("=", 1)[1].strip().strip('"')
                    role["_migration_note"] = val
                elif s.startswith("tablePermission "):
                    tp_name = _unquote_name(s[len("tablePermission "):].strip())
                    tp = {"name": tp_name, "filterExpression": ""}
                    i += 1
                    while i < len(lines):
                        s2 = lines[i].strip()
                        if not s2:
                            i += 1
                            continue
                        if (s2.startswith("tablePermission ") or
                                s2.startswith("role ") or
                                s2.startswith("annotation ")):
                            break
                        if s2.startswith("filterExpression"):
                            eq = s2.split("=", 1)
                            if len(eq) > 1:
                                tp["filterExpression"] = eq[1].strip()
                        i += 1
                    role["tablePermissions"].append(tp)
                    continue
                i += 1
            roles.append(role)
            continue
        i += 1
    return roles


def load_existing_model(model_dir: str) -> dict:
    """Reverse-engineer an existing TMDL model directory into a
    ``converted_objects``-compatible dict.

    Reads ``.tmdl`` files from the SemanticModel definition directory
    (``tables/``, ``relationships.tmdl``, ``roles.tmdl``) and reconstructs
    the table/column/measure/relationship/parameter/RLS inventory.

    Args:
        model_dir: Path to the shared model output dir (contains
            ``<ModelName>.SemanticModel/definition/``), OR direct path to
            the ``definition/`` directory, OR path to the ``tables/``
            directory.

    Returns:
        A ``converted_objects``-compatible dict with ``datasources``,
        ``calculations``, ``parameters``, and ``user_filters``.
    """
    # Locate the definition directory
    def_dir = _find_definition_dir(model_dir)
    if not def_dir:
        logger.warning("Could not locate TMDL definition directory in %s", model_dir)
        return _empty_model()

    tables_dir = os.path.join(def_dir, "tables")
    tables = []
    if os.path.isdir(tables_dir):
        for fname in sorted(os.listdir(tables_dir)):
            if not fname.endswith(".tmdl"):
                continue
            fpath = os.path.join(tables_dir, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            tbl = _parse_tmdl_table(content)
            if tbl.get("name"):
                tables.append(tbl)

    # Relationships
    relationships = []
    rels_path = os.path.join(def_dir, "relationships.tmdl")
    if os.path.isfile(rels_path):
        with open(rels_path, "r", encoding="utf-8") as f:
            relationships = _parse_tmdl_relationships(f.read())

    # Roles (RLS)
    roles = []
    roles_path = os.path.join(def_dir, "roles.tmdl")
    if os.path.isfile(roles_path):
        with open(roles_path, "r", encoding="utf-8") as f:
            roles = _parse_tmdl_roles(f.read())

    # Reconstruct converted_objects format
    # Build a single datasource with all tables + relationships
    ds = {
        "name": "SharedModel",
        "caption": "Shared Semantic Model",
        "connection": {},
        "tables": [],
        "columns": [],
        "relationships": relationships,
    }

    calculations = []
    for tbl in tables:
        # Physical table entry
        ds_table = {
            "name": tbl["name"],
            "type": "table",
            "columns": [],
        }
        for col in tbl.get("columns", []):
            ds_col = {
                "name": col["name"],
                "dataType": col.get("dataType", "string"),
                "datatype": str(col.get("dataType", "string")).lower(),
                "table": tbl["name"],
            }
            ds_table["columns"].append(ds_col)

            if col.get("isCalculated") and col.get("expression"):
                calculations.append({
                    "name": f"[{col['name']}]",
                    "caption": col["name"],
                    "formula": col.get("expression", ""),
                    "table": tbl["name"],
                    "_classification": "calculated_column",
                    "role": "dimension",
                })

        ds["tables"].append(ds_table)

        # Measures → calculations
        for meas in tbl.get("measures", []):
            calculations.append({
                "name": f"[{meas['name']}]",
                "caption": meas["name"],
                "formula": meas.get("expression", ""),
                "table": tbl["name"],
                "_classification": "measure",
                "role": "measure",
            })

    # User filters from roles
    user_filters = []
    for role in roles:
        for tp in role.get("tablePermissions", []):
            user_filters.append({
                "name": role["name"],
                "table": tp.get("name", ""),
                "filter_expression": tp.get("filterExpression", ""),
            })

    return {
        "datasources": [ds] if ds["tables"] else [],
        "worksheets": [],
        "dashboards": [],
        "calculations": calculations,
        "parameters": [],
        "filters": [],
        "stories": [],
        "actions": [],
        "sets": [],
        "groups": [],
        "bins": [],
        "hierarchies": [],
        "sort_orders": [],
        "aliases": {},
        "custom_sql": [],
        "user_filters": user_filters,
    }


def _find_definition_dir(path: str) -> Optional[str]:
    """Locate the TMDL ``definition/`` directory from various starting points."""
    # Direct definition dir
    if os.path.basename(path) == "definition" and os.path.isdir(path):
        return path

    # Check for tables/ subdir (we're already in definition/)
    if os.path.isdir(os.path.join(path, "tables")):
        return path

    # Look for *.SemanticModel/definition/
    if os.path.isdir(path):
        for entry in os.listdir(path):
            if entry.endswith(".SemanticModel"):
                def_candidate = os.path.join(path, entry, "definition")
                if os.path.isdir(def_candidate):
                    return def_candidate

    # Try path/definition/
    def_candidate = os.path.join(path, "definition")
    if os.path.isdir(def_candidate):
        return def_candidate

    return None


# ═══════════════════════════════════════════════════════════════════
#  Incremental merge — add / remove workbook
# ═══════════════════════════════════════════════════════════════════

def add_to_model(model_dir: str,
                 new_extracted: dict,
                 new_workbook_name: str,
                 new_workbook_path: Optional[str] = None,
                 force: bool = False) -> dict:
    """Add a new workbook to an existing shared model.

    Loads the existing model from *model_dir* (via manifest + TMDL),
    performs an incremental merge with *new_extracted*, and returns
    the updated merged ``converted_objects`` plus an updated manifest.

    Args:
        model_dir: Path to the existing shared model output directory.
        new_extracted: ``converted_objects`` dict for the new workbook.
        new_workbook_name: Display name for the new workbook.
        new_workbook_path: Filesystem path to the new ``.twbx`` file.
        force: Merge even if score is low.

    Returns:
        Dict with ``merged``, ``manifest``, ``assessment``, ``validation``.
    """
    # Load existing manifest
    manifest = MergeManifest.load(model_dir)

    # Check for duplicate workbook
    if manifest.find_workbook(new_workbook_name) and not force:
        raise ValueError(
            f"Workbook '{new_workbook_name}' already exists in the model. "
            f"Use force=True to re-add (will replace)."
        )

    _require_incremental_physical_provenance(manifest)

    # Load existing model from TMDL
    existing = load_existing_model(model_dir)

    # Combine existing + new for merge
    all_extracted = [existing, new_extracted]
    workbook_names = ["_existing_model_", new_workbook_name]

    assessment = assess_merge(all_extracted, workbook_names)

    if assessment.recommendation == "separate" and not force:
        return {
            "merged": None,
            "manifest": manifest,
            "assessment": assessment,
            "validation": None,
            "status": "rejected",
            "reason": f"Low merge score ({assessment.merge_score}). Use force=True.",
        }

    merged = merge_semantic_models(all_extracted, assessment, manifest.model_name)

    # Validation
    validation = generate_merge_validation_report(merged)

    # Update manifest
    # Remove existing entry if re-adding
    manifest.workbooks = [
        wb for wb in manifest.workbooks
        if wb.get("name", "").lower() != new_workbook_name.lower()
    ]

    # Build new workbook entry
    new_fps = build_table_fingerprints(new_extracted.get("datasources", []))
    new_tables = list(new_fps.keys())
    existing_table_names = set()
    for wb in manifest.workbooks:
        existing_table_names.update(wb.get("tables", []))
    exclusive = [t for t in new_tables if t not in existing_table_names]

    new_measures = [
        c.get("caption", c.get("name", ""))
        for c in new_extracted.get("calculations", [])
        if c.get("_classification") == "measure" or c.get("role") == "measure"
    ]

    file_hash = _file_hash(new_workbook_path) if new_workbook_path and os.path.isfile(new_workbook_path) else ""

    manifest.workbooks.append({
        "name": new_workbook_name,
        "path": new_workbook_path or "",
        "hash": file_hash,
        "tables": new_tables,
        "measures": new_measures,
        "exclusive_tables": exclusive,
    })

    # Update fingerprints and counts
    for ds in merged.get("datasources", []):
        fps = build_table_fingerprints([ds])
        for tname, (fp, _, _) in fps.items():
            manifest.table_fingerprints[tname] = fp.fingerprint()

    merged_ds = merged.get("datasources", [{}])[0] if merged.get("datasources") else {}
    manifest.artifact_counts = {
        "tables": len(merged_ds.get("tables", [])),
        "measures": len([c for c in merged.get("calculations", [])
                        if c.get("_classification") == "measure" or c.get("role") == "measure"]),
        "relationships": len(merged_ds.get("relationships", [])),
        "rls_roles": len(merged.get("user_filters", [])),
        "parameters": len(merged.get("parameters", [])),
    }
    manifest.validation_score = validation.get("score", 0)
    manifest.merge_score = assessment.merge_score
    manifest.timestamp = datetime.now(timezone.utc).isoformat()
    manifest.physical_join_provenance_required = _has_retained_physical_join_scopes(merged)

    return {
        "merged": merged,
        "manifest": manifest,
        "assessment": assessment,
        "validation": validation,
        "status": "added",
    }


def remove_from_model(model_dir: str,
                      workbook_name: str) -> dict:
    """Remove a workbook's exclusive artifacts from the shared model.

    Tables shared with other workbooks are NOT removed. Only tables,
    measures, and thin reports exclusively owned by *workbook_name*
    are cleaned up.

    Args:
        model_dir: Path to the existing shared model output directory.
        workbook_name: Name of the workbook to remove.

    Returns:
        Dict with ``manifest``, ``removed_tables``, ``removed_measures``,
        ``status``.
    """
    manifest = MergeManifest.load(model_dir)
    wb_entry = manifest.find_workbook(workbook_name)

    if not wb_entry:
        return {
            "manifest": manifest,
            "removed_tables": [],
            "removed_measures": [],
            "status": "not_found",
            "reason": f"Workbook '{workbook_name}' not found in manifest.",
        }

    _require_incremental_physical_provenance(manifest)

    # Determine which tables are exclusive to this workbook
    other_tables = set()
    for wb in manifest.workbooks:
        if wb.get("name", "").lower() != workbook_name.lower():
            other_tables.update(wb.get("tables", []))

    wb_tables = set(wb_entry.get("tables", []))
    exclusive_tables = wb_tables - other_tables
    shared_tables = wb_tables & other_tables

    # Load existing model to rebuild without exclusive tables
    existing = load_existing_model(model_dir)

    # Remove exclusive tables from datasources
    removed_tables = []
    for ds in existing.get("datasources", []):
        original_tables = ds.get("tables", [])
        ds["tables"] = [
            t for t in original_tables
            if t.get("name") not in exclusive_tables
        ]
        removed_tables = [
            t.get("name") for t in original_tables
            if t.get("name") in exclusive_tables
        ]

        # Remove relationships involving removed tables
        ds["relationships"] = [
            r for r in ds.get("relationships", [])
            if r.get("fromTable") not in exclusive_tables
            and r.get("toTable") not in exclusive_tables
        ]

    # Remove measures from this workbook
    wb_measures = set(wb_entry.get("measures", []))
    removed_measures = []
    remaining_calcs = []
    for calc in existing.get("calculations", []):
        caption = calc.get("caption", calc.get("name", ""))
        if caption in wb_measures and calc.get("table") in exclusive_tables:
            removed_measures.append(caption)
        else:
            remaining_calcs.append(calc)
    existing["calculations"] = remaining_calcs

    # Update manifest
    manifest.workbooks = [
        wb for wb in manifest.workbooks
        if wb.get("name", "").lower() != workbook_name.lower()
    ]

    # Remove fingerprints for exclusive tables
    for tname in exclusive_tables:
        manifest.table_fingerprints.pop(tname, None)

    # Update artifact counts
    merged_ds = existing.get("datasources", [{}])[0] if existing.get("datasources") else {}
    manifest.artifact_counts = {
        "tables": len(merged_ds.get("tables", [])),
        "measures": len(remaining_calcs),
        "relationships": len(merged_ds.get("relationships", [])),
        "rls_roles": len(existing.get("user_filters", [])),
        "parameters": len(existing.get("parameters", [])),
    }
    manifest.timestamp = datetime.now(timezone.utc).isoformat()

    return {
        "merged": existing,
        "manifest": manifest,
        "removed_tables": removed_tables,
        "removed_measures": removed_measures,
        "shared_tables_kept": list(shared_tables),
        "status": "removed",
    }


def _empty_model() -> dict:
    """Return an empty converted_objects dict."""
    return {
        "datasources": [], "worksheets": [], "dashboards": [],
        "calculations": [], "parameters": [], "filters": [],
        "stories": [], "actions": [], "sets": [], "groups": [],
        "bins": [], "hierarchies": [], "sort_orders": [],
        "aliases": {}, "custom_sql": [], "user_filters": [],
    }
