"""Data blending graph builder — Sprint 180 (v39.0.0).

Tableau data blending links a *primary* datasource to one or more *secondary*
datasources on shared link fields. Unlike a relationship inside a single data
model, a blend aggregates the secondary to the granularity of the primary at
query time, with the cross-filter flowing only from secondary → primary.

This module turns the flat ``data_blending`` link list produced by
``extract_data_blending`` (each entry a dict with ``datasource``,
``secondary_datasource``, ``column``, ``link_expression``, ``link_key``) into a
structured **blend graph**:

    [
        {
            "primary": "Orders",
            "secondaries": [
                {
                    "datasource": "Returns",
                    "link_fields": [{"primary": "Order ID", "secondary": "Order ID"}],
                    "direction": "secondary",          # cross-filter source
                    "cardinality": "manyToOne",        # primary(many) -> secondary(one)
                    "cross_filter": "oneDirection",     # secondary -> primary
                },
                ...
            ],
            "grade": "GREEN",
        },
        ...
    ]

From that graph it derives:
  * Power Query M merge queries (delegates to ``generate_blend_merge_query``)
  * TMDL-compatible relationship dicts (secondary → primary, single-direction)
  * cross-datasource DAX hints (RELATED for manyToOne, LOOKUPVALUE for
    manyToMany)
  * a per-blend assessment grade (GREEN/YELLOW/RED)

Stdlib-only.
"""

from __future__ import annotations

# Secondary "datasources" that are not real blends — Tableau exposes the
# parameter container as a pseudo-datasource that shows up in
# <datasource-dependencies>. These must never become blend edges.
VIRTUAL_SECONDARIES = frozenset({"parameters", "parameter", ""})

# When the secondary has at least this fraction of the primary's column count we
# treat the blend as a peer (many-to-many) instead of a lookup (many-to-one).
# Mirrors the relationship heuristic used elsewhere in the project.
_MANY_TO_MANY_RATIO = 0.7


def _ds_column_counts(datasources):
    """Return {datasource_display_name: column_count} for cardinality inference."""
    counts = {}
    for ds in datasources or []:
        name = ds.get("caption") or ds.get("name") or ""
        if not name:
            continue
        cols = ds.get("columns") or ds.get("fields") or []
        # Some datasources carry their fields under ``tables[*].columns``.
        if not cols:
            total = 0
            for tbl in ds.get("tables", []) or []:
                total += len(tbl.get("columns", []) or [])
            count = total
        else:
            count = len(cols)
        counts[name] = count
    return counts


def _infer_cardinality(primary, secondary, col_counts):
    """Infer blend cardinality from relative column counts.

    Tableau blends aggregate the secondary to the primary, so the natural
    default is many-to-one (many primary rows reference one secondary row).
    When the secondary is comparably wide we fall back to many-to-many.
    """
    p = col_counts.get(primary)
    s = col_counts.get(secondary)
    if p and s and p > 0:
        if s >= p * _MANY_TO_MANY_RATIO:
            return "manyToMany"
    return "manyToOne"


def _link_field(entry):
    """Extract a (primary_field, secondary_field) pair from a blend link entry.

    Tableau blends link on matching field names unless an explicit link key /
    expression overrides the secondary side.
    """
    col = (entry.get("column") or "").replace("[", "").replace("]", "").strip()
    key = (entry.get("link_key") or "").replace("[", "").replace("]", "").strip()
    expr = (entry.get("link_expression") or "").strip()
    primary_field = col or key
    secondary_field = key or expr or col
    return primary_field, secondary_field


def build_blend_graph(data_blending, datasources=None, skip_virtual=True):
    """Build a structured blend graph from the flat blend link list.

    Args:
        data_blending: list of dicts from ``extract_data_blending``.
        datasources: optional list of datasource dicts (for cardinality).
        skip_virtual: drop ``Parameters`` and other pseudo-datasources.

    Returns:
        list[dict]: one entry per primary datasource, each with ``primary``,
        ``secondaries`` (list) and ``grade``.
    """
    col_counts = _ds_column_counts(datasources)

    # primary -> {secondary_name -> secondary_dict}
    graphs = {}
    for entry in data_blending or []:
        primary = (entry.get("datasource") or "").strip()
        secondary = (entry.get("secondary_datasource") or "").strip()
        if not primary or not secondary:
            # Link-field markers without an explicit secondary aren't blend edges.
            continue
        if skip_virtual and secondary.lower() in VIRTUAL_SECONDARIES:
            continue
        if secondary == primary:
            continue

        g = graphs.setdefault(primary, {})
        sec = g.setdefault(
            secondary,
            {
                "datasource": secondary,
                "link_fields": [],
                "direction": "secondary",
            },
        )
        pf, sf = _link_field(entry)
        if pf:
            lf = {"primary": pf, "secondary": sf or pf}
            if lf not in sec["link_fields"]:
                sec["link_fields"].append(lf)

    # Detect circular blends: A is secondary of B and B is secondary of A.
    edges = set()
    for primary, secs in graphs.items():
        for secondary in secs:
            edges.add((primary, secondary))
    circular = {
        primary
        for (primary, secondary) in edges
        if (secondary, primary) in edges
    }

    result = []
    for primary in sorted(graphs):
        secondaries = []
        for secondary in sorted(graphs[primary]):
            sec = graphs[primary][secondary]
            sec["cardinality"] = _infer_cardinality(primary, secondary, col_counts)
            sec["cross_filter"] = "oneDirection"
            secondaries.append(sec)
        result.append(
            {
                "primary": primary,
                "secondaries": secondaries,
                "grade": _grade_blend(secondaries, is_circular=primary in circular),
            }
        )
    return result


def _grade_blend(secondaries, is_circular=False):
    """Grade a single primary's blend complexity.

    GREEN  — exactly one secondary (simple two-source blend).
    YELLOW — two or more secondaries (fan-out blend, validate join keys).
    RED    — circular blend (bidirectional dependency, manual review required).
    """
    if is_circular:
        return "RED"
    n = len(secondaries)
    if n <= 1:
        return "GREEN"
    return "YELLOW"


def blend_graph_to_relationships(graph):
    """Convert a blend graph into TMDL-compatible relationship dicts.

    Each blend link field becomes a single-direction relationship flowing from
    the secondary (the "one"/lookup side) to the primary, matching Tableau's
    blend cross-filter direction.

    Returns:
        list[dict]: relationship dicts with fromTable/fromColumn/toTable/
        toColumn/cardinality/crossFilteringBehavior.
    """
    rels = []
    seen = set()
    for blend in graph or []:
        primary = blend.get("primary", "")
        for sec in blend.get("secondaries", []):
            secondary = sec.get("datasource", "")
            cardinality = sec.get("cardinality", "manyToOne")
            for lf in sec.get("link_fields", []):
                pf = lf.get("primary", "")
                sf = lf.get("secondary", pf)
                if not pf:
                    continue
                # Relationship: primary[pf] (many) -> secondary[sf] (one)
                rel = {
                    "fromTable": primary,
                    "fromColumn": pf,
                    "toTable": secondary,
                    "toColumn": sf,
                    "cardinality": cardinality,
                    "crossFilteringBehavior": "oneDirection",
                    "isBlend": True,
                }
                sig = (primary, pf, secondary, sf)
                if sig in seen:
                    continue
                seen.add(sig)
                rels.append(rel)
    return rels


def blend_graph_to_merge_queries(graph):
    """Generate Power Query M merge queries for every secondary in the graph.

    Handles multiple secondaries per primary by chaining merges: the output of
    the previous merge feeds the next.

    Returns:
        dict: {primary_name: m_query_string}
    """
    from tableau_export.m_query_builder import generate_blend_merge_query

    queries = {}
    for blend in graph or []:
        primary = blend.get("primary", "")
        secondaries = blend.get("secondaries", [])
        if not secondaries:
            continue
        # First merge starts from the primary query name; subsequent merges
        # chain off a synthetic step name so multiple secondaries compose.
        current = primary
        m = ""
        for idx, sec in enumerate(secondaries):
            secondary = sec.get("datasource", "")
            cardinality = sec.get("cardinality", "manyToOne")
            join_kind = "left" if cardinality == "manyToOne" else "full"
            m = generate_blend_merge_query(
                current, secondary, sec.get("link_fields", []), join_kind=join_kind
            )
            current = f"{primary}_Blend{idx + 1}"
        queries[primary] = m
    return queries


def blend_graph_dax_hint(cardinality):
    """Return the DAX cross-datasource accessor for a blend cardinality.

    manyToOne → RELATED (single related row); manyToMany → LOOKUPVALUE.
    """
    return "RELATED" if cardinality == "manyToOne" else "LOOKUPVALUE"


def assess_blend_graph(graph):
    """Summarise a blend graph for the migration assessment.

    Returns:
        dict: counts and an overall grade (worst of all primaries).
    """
    primaries = len(graph or [])
    secondaries = sum(len(b.get("secondaries", [])) for b in graph or [])
    link_fields = sum(
        len(sec.get("link_fields", []))
        for b in graph or []
        for sec in b.get("secondaries", [])
    )
    missing_keys = sum(
        1
        for b in graph or []
        for sec in b.get("secondaries", [])
        if not sec.get("link_fields")
    )

    grades = {b.get("grade", "GREEN") for b in graph or []}
    if "RED" in grades:
        overall = "RED"
    elif "YELLOW" in grades:
        overall = "YELLOW"
    else:
        overall = "GREEN"

    return {
        "primary_count": primaries,
        "secondary_count": secondaries,
        "link_field_count": link_fields,
        "missing_link_key_count": missing_keys,
        "grade": overall if primaries else "GREEN",
    }
