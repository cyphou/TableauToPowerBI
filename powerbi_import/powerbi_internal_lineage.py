"""Build a deterministic, static lineage graph from generated Power BI artifacts.

The scanner is read-only. Its JSON is canonical; the adjacent standalone HTML
is rendered exclusively from that serialized graph. No runtime execution is
performed or implied.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from powerbi_import.html_template import (
    badge,
    data_table,
    esc,
    html_close,
    html_open,
    section_close,
    section_open,
)


_GRAPH_VERSION = "1.0"
_DECLARATION_RE = re.compile(r"^\t(column|measure|partition)\s+(.+?)\s*$")
_DAX_REF_RE = re.compile(
    r"(?:(?:'((?:[^']|'')+)'|([A-Za-z_][\w ]*))\s*)?"
    r"\[((?:[^\]]|\]\])*)\]"
)
_VISUAL_TYPES = ("Column", "Measure", "Aggregation")


def _stable_id(kind: str, *parts: str) -> str:
    payload = json.dumps([kind, *parts], ensure_ascii=False, separators=(",", ":"))
    return f"{kind}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _split_assignment(text: str) -> tuple[str, str | None]:
    quoted = False
    i = 0
    while i < len(text):
        if text[i] == "'":
            if quoted and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            quoted = not quoted
        elif text[i] == "=" and not quoted:
            return text[:i].strip(), text[i + 1 :].strip()
        i += 1
    return text.strip(), None


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _evidence(path: str, line: int | None = None, role: str | None = None,
              index: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"path": path}
    if line is not None:
        result["line"] = line
    if role is not None:
        result["role"] = role
    if index is not None:
        result["index"] = index
    return result


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Unable to read artifact {path}: {exc}") from exc


def _read_json(path: Path) -> Any:
    try:
        return json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON artifact {path} at line {exc.lineno}, column {exc.colno}"
        ) from exc


def _mask_expression_comments(text: str, language: str) -> str:
    """Mask comments in place, retaining offsets and line breaks.

    M strings (including quoted identifiers) must remain available to the
    existing output-column recognizers. DAX strings are instead masked because
    they cannot contain field references. Unterminated literals/comments are
    masked through EOF rather than interpreted as executable references.
    """
    chars = list(text)
    size = len(text)

    def mask(start: int, end: int) -> None:
        for pos in range(start, end):
            if text[pos] not in "\r\n":
                chars[pos] = " "

    i = 0
    while i < size:
        ch = text[i]
        if ch == '"' or (language == "dax" and ch == "'"):
            # Both languages escape their quote character by doubling it.
            quote = ch
            start = i
            i += 1
            closed = False
            while i < size:
                if text[i] == quote:
                    if i + 1 < size and text[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed or (language == "dax" and quote == '"'):
                mask(start, i)
            continue
        if language == "dax" and ch == "[":
            # Comment markers in bracketed field names are not comments.
            start = i
            i += 1
            closed = False
            while i < size:
                if text[i] == "]":
                    if i + 1 < size and text[i + 1] == "]":
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                mask(start, i)
            continue
        if text.startswith("//", i) or (
            language == "dax" and text.startswith("--", i)
        ):
            start = i
            while i < size and text[i] not in "\r\n":
                i += 1
            mask(start, i)
            continue
        if text.startswith("/*", i):
            start = i
            i += 2
            depth = 1
            while i < size and depth:
                if language == "m" and text.startswith("/*", i):
                    depth += 1
                    i += 2
                elif text.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            mask(start, i)
            continue
        i += 1
    return "".join(chars)


def _table_artifact_files(root: Path) -> list[Path]:
    return sorted(
        (p for p in root.rglob("*.tmdl")
         if p.parent.name == "tables"
         and p.parent.parent.name == "definition"
         and p.parent.parent.parent.name.endswith(".SemanticModel")),
        key=lambda p: _relative(p, root),
    )


def _parse_tmdl_table(path: Path, root: Path) -> dict[str, Any]:
    text = _read_text(path)
    lines = text.splitlines()
    headers = [
        (number, match.group(1).strip())
        for number, line in enumerate(lines, 1)
        if (match := re.match(r"^table\s+(.+?)\s*$", line))
    ]
    if len(headers) != 1:
        raise ValueError(f"Expected exactly one TMDL table declaration in {path}")
    table_line, table_name = headers[0]
    table_name = _unquote(table_name)
    path_rel = _relative(path, root)
    table = {
        "name": table_name,
        "path": path_rel,
        "line": table_line,
        "columns": [],
        "measures": [],
        "partitions": [],
    }

    declarations: list[tuple[int, str, str, str | None]] = []
    for number, line in enumerate(lines, 1):
        match = _DECLARATION_RE.match(line)
        if not match:
            continue
        name, expression = _split_assignment(match.group(2))
        declarations.append((number, match.group(1), _unquote(name), expression))

    for index, (number, kind, name, expression) in enumerate(declarations):
        end = declarations[index + 1][0] - 1 if index + 1 < len(declarations) else len(lines)
        block = lines[number - 1 : end]
        expression_line = number
        if kind in {"column", "measure"}:
            expression_parts = [expression] if expression else []
            for offset, continuation in enumerate(block[1:], 1):
                if not continuation.strip():
                    continue
                if re.match(r"^\t\t(?:annotation\s|[\w ]+\s*:)", continuation):
                    break
                if not expression_parts:
                    expression_line = number + offset
                expression_parts.append(continuation.strip())
            expression = "\n".join(expression_parts) if expression_parts else None
        item: dict[str, Any] = {
            "name": name,
            "path": path_rel,
            "line": number,
            "expression_line": expression_line,
        }
        if kind == "column":
            item["expression"] = expression
            item["source_column"] = None
            for block_line_no, block_line in enumerate(block[1:], number + 1):
                source_match = re.match(r"^\t\t sourceColumn\s*:\s*(.*?)\s*$", block_line)
                if not source_match:
                    source_match = re.match(r"^\t\tsourceColumn\s*:\s*(.*?)\s*$", block_line)
                if source_match:
                    item["source_column"] = _unquote(source_match.group(1))
                    item["source_line"] = block_line_no
                    break
            table["columns"].append(item)
        elif kind == "measure":
            if not expression:
                raise ValueError(f"TMDL measure has no inline expression in {path}:{number}")
            item["expression"] = expression
            table["measures"].append(item)
        else:
            if not expression:
                raise ValueError(f"TMDL partition has no kind in {path}:{number}")
            item["partition_kind"] = (expression or "").strip().lower() or "unspecified"
            item["m_tokens"] = []
            if item["partition_kind"] == "m":
                source_start: int | None = None
                initial: str | None = None
                for block_offset, block_line in enumerate(block):
                    source_match = re.match(r"^\t\tsource\s*=\s*(.*)$", block_line)
                    if source_match:
                        source_start = block_offset
                        initial = source_match.group(1)
                        break
                if source_start is not None:
                    m_lines: list[str] = []
                    if initial:
                        m_lines.append(initial)
                    m_lines.extend(block[source_start + 1 :])
                    # Keep M in memory only. Do not copy expressions into graph data.
                    m_text = _mask_expression_comments("\n".join(m_lines), "m")
                    m_lines = m_text.split("\n")
                    m_first_line = number + source_start + (0 if initial else 1)
                    for offset, m_line in enumerate(m_lines):
                        for ident in re.finditer(r"\[([^\]]+)\]", m_line):
                            item["m_tokens"].append(
                                (ident.group(1), m_first_line + offset)
                            )
                        # Names in these M constructs identify output columns;
                        # arbitrary strings (including connection settings) do not.
                        for output_list in re.finditer(
                            r"#table\s*\(\s*\{([^}]*)\}", m_line, re.IGNORECASE
                        ):
                            for name_match in re.finditer(
                                r'"((?:[^"]|"")*)"', output_list.group(1)
                            ):
                                item["m_tokens"].append(
                                    (name_match.group(1).replace('""', '"'),
                                     m_first_line + offset)
                                )
                        for selected in re.finditer(
                            r'Table\.SelectColumns\([^,\n]+,\s*\{([^}]*)\}',
                            m_line,
                            re.IGNORECASE,
                        ):
                            for name_match in re.finditer(
                                r'"((?:[^"]|"")*)"', selected.group(1)
                            ):
                                item["m_tokens"].append(
                                    (name_match.group(1).replace('""', '"'),
                                     m_first_line + offset)
                                )
                        for renamed in re.finditer(
                            r'\{\s*"((?:[^"]|"")*)"\s*,\s*"((?:[^"]|"")*)"\s*\}',
                            m_line,
                        ):
                            item["m_tokens"].append(
                                (renamed.group(2).replace('""', '"'),
                                 m_first_line + offset)
                            )
                    for output_name in re.finditer(
                        r'Table\.AddColumn\([^,\n]*,\s*"((?:[^"]|"")*)"',
                        m_text,
                        re.IGNORECASE,
                    ):
                        # This fallback handles straightforward generated
                        # AddColumn calls; its source line is located below.
                        output = output_name.group(1).replace('""', '"')
                        line_no = m_first_line + m_text[:output_name.start()].count("\n")
                        item["m_tokens"].append((output, line_no))
                else:
                    raise ValueError(
                        f"M partition has no source expression in {path}:{number}"
                    )
            table["partitions"].append(item)

    if not table["partitions"]:
        raise ValueError(f"TMDL table has no partition declaration in {path}")
    return table


def _node(nodes: dict[str, dict[str, Any]], kind: str, label: str,
          identity: Iterable[str], evidence: dict[str, Any]) -> str:
    identity_tuple = tuple(identity)
    node_id = _stable_id("node", kind, *identity_tuple)
    existing = nodes.get(node_id)
    if existing is None:
        nodes[node_id] = {
            "id": node_id,
            "type": kind,
            "label": label,
            "evidence": [evidence],
        }
    elif evidence not in existing["evidence"]:
        existing["evidence"].append(evidence)
    return node_id


def _edge(edges: dict[str, dict[str, Any]], edge_type: str, source: str,
          target: str, status: str, evidence: dict[str, Any]) -> None:
    identity = _stable_id("edge", edge_type, source, target, status,
                          json.dumps(evidence, sort_keys=True, ensure_ascii=False))
    edges[identity] = {
        "id": identity,
        "type": edge_type,
        "source": source,
        "target": target,
        "status": status,
        "evidence": evidence,
    }


def _case_matches(items: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [item for item in items if item["name"].casefold() == name.casefold()]


def _resolve_field(tables: list[dict[str, Any]], table_name: str | None,
                   field_name: str, kind: str | None) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for table in tables:
        if table_name and table["name"].casefold() != table_name.casefold():
            continue
        field_sets = []
        if kind in (None, "Column", "Aggregation"):
            field_sets.append(table["columns"])
        if kind in (None, "Measure", "Aggregation"):
            field_sets.append(table["measures"])
        for fields in field_sets:
            candidates.extend(item for item in fields
                              if item["name"].casefold() == field_name.casefold())
    return candidates


def _mask_dax_strings(expression: str) -> str:
    return _mask_expression_comments(expression, "dax")


def _qualified_reference(value: str) -> tuple[str | None, str]:
    value = value.strip()
    quoted = re.match(r"^'((?:[^']|'')*)'\s*\.\s*(.+)$", value)
    if quoted:
        return _unquote("'" + quoted.group(1) + "'"), _unquote(quoted.group(2))
    if "." in value:
        table_name, field_name = value.rsplit(".", 1)
        return _unquote(table_name), _unquote(field_name)
    return None, _unquote(value)


def _parse_relationships(root: Path) -> list[dict[str, Any]]:
    files = sorted(
        (p for p in root.rglob("relationships.tmdl")
         if p.parent.name == "definition"
         and p.parent.parent.name.endswith(".SemanticModel")),
        key=lambda p: _relative(p, root),
    )
    relationships: list[dict[str, Any]] = []
    for path in files:
        lines = _read_text(path).splitlines()
        current: dict[str, Any] | None = None
        for number, line in enumerate(lines, 1):
            header = re.match(r"^relationship\s+(.+?)\s*$", line)
            if header:
                if current is not None:
                    relationships.append(current)
                current = {
                    "name": _unquote(header.group(1)),
                    "path": _relative(path, root),
                    "line": number,
                    "from": None,
                    "to": None,
                }
                continue
            if current is None:
                continue
            for prop, key in (("fromColumn", "from"), ("toColumn", "to")):
                match = re.match(rf"^\t{prop}\s*:\s*(.*?)\s*$", line)
                if match:
                    current[key] = (match.group(1), number)
        if current is not None:
            relationships.append(current)
    return relationships


def _field_node_or_reference(
    nodes: dict[str, dict[str, Any]], tables: list[dict[str, Any]],
    table_name: str | None,
    field_name: str, field_kind: str | None, evidence: dict[str, Any],
    reference_scope: str,
) -> tuple[str, str]:
    matches = _resolve_field(tables, table_name, field_name, field_kind)
    if len(matches) == 1:
        found = matches[0]
        return found["id"], "exact"
    status = "ambiguous" if matches else "unresolved"
    description = (
        f"{table_name}.{field_name}" if table_name else field_name
    )
    ref_id = _node(
        nodes, "unresolved_reference" if not matches else "ambiguous_reference",
        description,
        (reference_scope, (table_name or "").casefold(), field_name.casefold(),
         field_kind or ""),
        evidence,
    )
    return ref_id, status


def build_powerbi_lineage_graph(project_dir: str | Path) -> dict[str, Any]:
    """Scan generated PBIP artifacts without executing or modifying them.

    Raises ``FileNotFoundError`` for a missing project directory and
    ``ValueError`` for absent required artifacts, unreadable files, or corrupt
    TMDL/PBIR input. Formulas and M source are inspected in memory only.
    """
    root = Path(project_dir)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Power BI project directory does not exist: {root}")
    root = root.resolve()
    table_files = _table_artifact_files(root)
    if not table_files:
        raise ValueError("No generated *.SemanticModel/definition/tables/*.tmdl files found")
    tables = [_parse_tmdl_table(path, root) for path in table_files]

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    for table in tables:
        table_id = _node(
            nodes, "table", table["name"],
            (table["name"].casefold(), table["path"]),
            _evidence(table["path"], table["line"], role="table"),
        )
        table["id"] = table_id
        for field in table["columns"] + table["measures"]:
            field["table_name"] = table["name"]
            is_measure = field in table["measures"]
            field_type = "measure" if is_measure else "column"
            field["kind"] = field_type
            node_id = _node(
                nodes, field_type, f"{table['name']}.{field['name']}",
                (table["name"].casefold(), field["name"].casefold(), field_type,
                 field["path"], str(field["line"])),
                _evidence(field["path"], field["line"], role=field_type),
            )
            field["id"] = node_id
            _edge(
                edges, "contains", table_id, node_id, "exact",
                _evidence(field["path"], field["line"], role=field_type),
            )

        for partition in table["partitions"]:
            partition_id = _node(
                nodes, "partition", f"{table['name']}.{partition['name']}",
                (table["name"].casefold(), partition["name"].casefold(),
                 partition["path"], str(partition["line"])),
                _evidence(partition["path"], partition["line"], role="partition"),
            )
            partition["id"] = partition_id
            _edge(
                edges, "partition_of", partition_id, table_id, "exact",
                _evidence(partition["path"], partition["line"], role="partition"),
            )

            if partition["partition_kind"] != "m":
                continue
            for column in table["columns"]:
                source_name = column.get("source_column") or column["name"]
                tokens = partition["m_tokens"]
                hits = [(value, line) for value, line in tokens
                        if value.casefold() == source_name.casefold()]
                unique_lines = sorted({line for _, line in hits})
                if unique_lines:
                    for line_no in unique_lines:
                        _edge(
                            edges, "m_origin", partition_id,
                            column["id"], "exact",
                            _evidence(partition["path"], line_no, role="m_column_name"),
                        )
                else:
                    ev = _evidence(partition["path"], partition["line"],
                                   role="m_column_origin")
                    ref_id = _node(
                        nodes, "unresolved_reference",
                        f"{table['name']}.{column['name']} M origin not identified",
                        (partition_id, table["name"].casefold(),
                         column["name"].casefold()),
                        ev,
                    )
                    _edge(edges, "m_origin", partition_id, ref_id,
                          "unresolved", ev)

    # Link statically identifiable DAX references from calculations.
    for table in tables:
        for owner in table["measures"] + [
            c for c in table["columns"] if c.get("expression")
        ]:
            source_node = owner["id"]
            expression = owner.get("expression") or ""
            refs = _DAX_REF_RE.finditer(_mask_dax_strings(expression))
            for ref_index, match in enumerate(refs):
                qualifier = match.group(1) or match.group(2)
                field_name = match.group(3).replace("]]", "]").strip()
                if not field_name:
                    continue
                if qualifier and qualifier.casefold() in {
                    "var", "return", "true", "false",
                }:
                    continue
                target, status = _field_node_or_reference(
                    nodes, tables,
                    qualifier, field_name, None,
                    _evidence(owner["path"], owner["expression_line"],
                              role="dax_reference",
                              index=ref_index),
                    f"dax:{table['name']}.{owner['name']}",
                )
                _edge(
                    edges, "depends_on", source_node, target, status,
                    _evidence(owner["path"], owner["expression_line"],
                              role="dax_reference",
                              index=ref_index),
                )

    # Relationships are separate TMDL artifacts and stay explicit when an
    # endpoint is missing or not unique.
    relationship_files = [
        p for p in root.rglob("relationships.tmdl")
        if p.parent.name == "definition"
        and p.parent.parent.name.endswith(".SemanticModel")
    ]
    relationships = _parse_relationships(root)
    if relationship_files:
        for relationship in relationships:
            relationship_id = _node(
                nodes, "relationship", relationship["name"],
                (relationship["path"], str(relationship["line"]),
                 relationship["name"]),
                _evidence(relationship["path"], relationship["line"],
                          role="relationship"),
            )
            for endpoint in ("from", "to"):
                raw, line_no = relationship.get(endpoint) or (None, None)
                if raw is None:
                    ref_text = f"{relationship['name']}:{endpoint}"
                    ref_id = _node(
                        nodes, "unresolved_reference", ref_text,
                        (relationship["path"], str(relationship["line"]), endpoint),
                        _evidence(relationship["path"], relationship["line"],
                                  role=f"{endpoint}_endpoint"),
                    )
                    _edge(
                        edges, f"{endpoint}_endpoint", relationship_id, ref_id,
                        "unresolved", _evidence(relationship["path"],
                                                 relationship["line"],
                                                 role=f"{endpoint}_endpoint"),
                    )
                    continue
                table_name, field_name = _qualified_reference(raw)
                ev = _evidence(relationship["path"], line_no,
                               role=f"{endpoint}_endpoint")
                target, status = _field_node_or_reference(
                    nodes, tables,
                    table_name, field_name, "Column", ev,
                    f"relationship:{relationship['name']}:{endpoint}",
                )
                _edge(edges, f"{endpoint}_endpoint", relationship_id, target,
                      status, ev)

    # PBIR pages and visual queryState projections.
    def is_page_json(path: Path) -> bool:
        parts = path.relative_to(root).parts
        return path.name == "page.json" and any(
            part.endswith(".Report")
            and parts[index + 1 : index + 3] == ("definition", "pages")
            for index, part in enumerate(parts[:-2])
        )

    page_jsons = sorted(
        (p for p in root.rglob("page.json") if is_page_json(p)),
        key=lambda p: _relative(p, root),
    )
    metadata_files = sorted(
        (p for p in root.rglob("pages.json")
         if p.parent.name == "pages" and p.parent.parent.name == "definition"
         and p.parent.parent.parent.name.endswith(".Report")),
        key=lambda p: _relative(p, root),
    )
    display_names: dict[tuple[str, str], str] = {}
    report_files = sorted(
        (p for p in root.rglob("report.json")
         if p.parent.name == "definition"
         and p.parent.parent.name.endswith(".Report")),
        key=lambda p: _relative(p, root),
    )
    for report_path in report_files:
        report_data = _read_json(report_path)
        if not isinstance(report_data, dict):
            raise ValueError(f"Expected JSON object in {report_path}")
    if not page_jsons and not metadata_files:
        # A generated report with no pages may omit the pages directory
        # altogether. A bare semantic model is not evidence of such a report.
        if not report_files:
            raise ValueError("No generated report definition, pages or pages metadata found")
    for metadata_path in metadata_files:
        metadata = _read_json(metadata_path)
        if not isinstance(metadata, dict):
            raise ValueError(f"Expected JSON object in {metadata_path}")
        page_order = metadata.get("pageOrder")
        if not isinstance(page_order, list) or any(
            not isinstance(name, str) or not name for name in page_order
        ) or len(page_order) != len(set(page_order)):
            raise ValueError(f"Expected a unique string pageOrder array in {metadata_path}")
        emitted_pages = {
            path.parent.name for path in page_jsons
            if path.parent.parent == metadata_path.parent
        }
        if set(page_order) != emitted_pages:
            raise ValueError(f"Page metadata does not match emitted pages in {metadata_path}")
        report_key = metadata_path.parent.parent.parent.name.casefold()
        for page_name in page_order:
            display_names[(report_key, str(page_name).casefold())] = str(page_name)

    page_ids: dict[tuple[str, str], str] = {}
    for page_path in page_jsons:
        page_data = _read_json(page_path)
        if not isinstance(page_data, dict) or not isinstance(page_data.get("name"), str):
            raise ValueError(f"Page JSON is missing a string name: {page_path}")
        page_name = page_data["name"]
        report_dir = next(
            (part for part in page_path.parts if part.endswith(".Report")), ""
        )
        report_key = report_dir.casefold()
        label = page_data.get("displayName") or display_names.get(
            (report_key, page_name.casefold()), page_name
        )
        page_id = _node(
            nodes, "page", str(label),
            (report_key, page_name.casefold()),
            _evidence(_relative(page_path, root), role="page"),
        )
        page_ids[(report_key, page_name.casefold())] = page_id
        visuals_dir = page_path.parent / "visuals"
        if not visuals_dir.exists():
            continue
        visual_paths = sorted(visuals_dir.glob("*/visual.json"),
                              key=lambda p: _relative(p, root))
        for visual_path in visual_paths:
            visual_data = _read_json(visual_path)
            if not isinstance(visual_data, dict):
                raise ValueError(f"Expected JSON object in {visual_path}")
            visual = visual_data.get("visual")
            visual = visual if isinstance(visual, dict) else {}
            visual_name = str(visual_data.get("name") or visual_path.parent.name)
            visual_type = str(visual.get("visualType") or "unknown")
            visual_id = _node(
                nodes, "visual", f"{visual_name} ({visual_type})",
                (report_key, page_name.casefold(), visual_name.casefold(),
                 _relative(visual_path, root)),
                _evidence(_relative(visual_path, root), role="visual"),
            )
            visual_ev = _evidence(_relative(visual_path, root), role="visual_page")
            _edge(edges, "on_page", visual_id, page_id, "exact", visual_ev)
            query = visual.get("query") if isinstance(visual.get("query"), dict) else {}
            query_state = query.get("queryState")
            if not isinstance(query_state, dict):
                continue
            for role in sorted(query_state):
                role_state = query_state[role]
                projections = role_state.get("projections") if isinstance(role_state, dict) else None
                if not isinstance(projections, list):
                    continue
                for projection_index, projection in enumerate(projections):
                    if not isinstance(projection, dict):
                        continue
                    field = projection.get("field")
                    if not isinstance(field, dict):
                        continue
                    field_kind = next((name for name in _VISUAL_TYPES if name in field), None)
                    if field_kind is None or not isinstance(field[field_kind], dict):
                        continue
                    field_data = field[field_kind]
                    expression_data = field_data.get("Expression")
                    source_ref = (
                        expression_data.get("SourceRef")
                        if isinstance(expression_data, dict) else None
                    )
                    entity = source_ref.get("Entity") if isinstance(source_ref, dict) else None
                    prop = field_data.get("Property")
                    if field_kind == "Aggregation" and not prop:
                        column = (
                            expression_data.get("Column")
                            if isinstance(expression_data, dict) else None
                        )
                        if isinstance(column, dict):
                            prop = column.get("Property")
                            inner_expr = column.get("Expression")
                            inner_ref = inner_expr.get("SourceRef") if isinstance(inner_expr, dict) else None
                            if isinstance(inner_ref, dict):
                                entity = inner_ref.get("Entity", entity)
                    ev = _evidence(
                        _relative(visual_path, root), role=str(role),
                        index=projection_index,
                    )
                    if not isinstance(prop, str) or not prop:
                        prop = projection.get("queryRef")
                        if isinstance(prop, str) and "." in prop and not entity:
                            entity, prop = prop.rsplit(".", 1)
                        else:
                            prop = None
                    if not prop:
                        ref_id = _node(
                            nodes, "unresolved_reference", "Projection field missing",
                            (visual_id, str(role), str(projection_index)),
                            ev,
                        )
                        _edge(edges, "projects", visual_id, ref_id, "unresolved", ev)
                        continue
                    target, status = _field_node_or_reference(
                        nodes, tables,
                        entity if isinstance(entity, str) else None, prop,
                        field_kind, ev,
                        f"visual:{visual_name}:{role}:{projection_index}",
                    )
                    _edge(edges, "projects", visual_id, target, status, ev)

    node_list = sorted(nodes.values(), key=lambda item: item["id"])
    for item in node_list:
        item["evidence"].sort(
            key=lambda ev: (ev["path"], ev.get("line", 0), ev.get("role", ""),
                            ev.get("index", -1))
        )
    edge_list = sorted(edges.values(), key=lambda item: item["id"])
    return {
        "schema_version": _GRAPH_VERSION,
        "runtime_status": "not_run",
        "runtime_note": (
            "Static artifact inspection only; Desktop rendering, semantic execution, "
            "refresh, and deployment were not run."
        ),
        "nodes": node_list,
        "edges": edge_list,
    }


def _canonical_json(graph: dict[str, Any]) -> str:
    return json.dumps(graph, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def render_powerbi_lineage_html(canonical_json: str) -> str:
    """Render a standalone, interactive HTML view from canonical graph JSON."""
    graph = json.loads(canonical_json)
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) \
            or not isinstance(graph.get("edges"), list):
        raise ValueError("Canonical lineage JSON must contain node and edge arrays")
    embedded = json.dumps(graph, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    embedded = (embedded.replace("&", "\\u0026").replace("<", "\\u003c")
                .replace(">", "\\u003e").replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029"))
    node_rows = [
        [
            esc(node["type"]),
            esc(node["label"]),
            esc(node["id"]),
            esc(" | ".join(
                f"{e['path']}"
                + (f":{e['line']}" if "line" in e else "")
                + (f" · {e['role']}" if "role" in e else "")
                + (f" · index {e['index']}" if "index" in e else "")
                for e in node.get("evidence", [])
            )),
        ]
        for node in graph["nodes"]
    ]
    nodes_html = data_table(
        ["Type", "Node", "Stable ID", "Evidence path / location"],
        node_rows, table_id="lineage-nodes", detail=True,
    )
    by_id = {node["id"]: node for node in graph["nodes"]}
    edge_rows = [
        [
            esc(edge["type"]),
            esc(by_id.get(edge["source"], {}).get("label", edge["source"])),
            esc(by_id.get(edge["target"], {}).get("label", edge["target"])),
            esc(edge["status"]),
            esc(
                edge["evidence"]["path"]
                + (f":{edge['evidence']['line']}" if "line" in edge["evidence"] else "")
                + (f" · {edge['evidence']['role']}" if "role" in edge["evidence"] else "")
                + (f" · index {edge['evidence']['index']}"
                   if "index" in edge["evidence"] else "")
            ),
        ]
        for edge in graph["edges"]
    ]
    edges_html = data_table(
        ["Relationship", "Source", "Target", "Status", "Evidence path / location"],
        edge_rows, table_id="lineage-edges", detail=True,
    )
    payload = esc(json.dumps(graph, ensure_ascii=False, sort_keys=True, indent=2))
    node_types = sorted({node["type"] for node in graph["nodes"]})
    type_options = "".join(
        f'<option value="{esc(node_type)}">{esc(node_type)}</option>'
        for node_type in node_types
    )
    parts = [
        html_open(
            "Power BI internal lineage",
            subtitle="Static generated-artifact graph; no runtime validation performed",
            timestamp="Static scan",
            version=_GRAPH_VERSION,
        ),
        f'<p>{badge("not_run")} {esc(graph.get("runtime_note", ""))}</p>',
        '<p><label for="lineage-search">Search </label>'
        '<input id="lineage-search" type="search" placeholder="Search nodes and edges"> '
        '<label for="lineage-type">Node type </label>'
        f'<select id="lineage-type"><option value="">All</option>{type_options}</select> '
        '<label for="lineage-status">Edge status </label>'
        '<select id="lineage-status"><option value="">All</option>'
        '<option>exact</option><option>unresolved</option><option>ambiguous</option>'
        '</select> <span id="lineage-counts"></span></p>',
        section_open("lineage-node-section", "Nodes"),
        nodes_html,
        section_close(),
        section_open("lineage-edge-section", "Edges"),
        edges_html,
        section_close(),
        '<details><summary>Canonical graph JSON</summary><pre>'
        + payload + '</pre></details>',
        '<script id="lineage-data" type="application/json">'
        + embedded + '</script>',
        """<script>
(()=>{"use strict";
const g=JSON.parse(document.getElementById("lineage-data").textContent);
const q=document.getElementById("lineage-search"),nt=document.getElementById("lineage-type"),
es=document.getElementById("lineage-status");
const byId=new Map(g.nodes.map(n=>[n.id,n]));
function filter(tableId,predicate){const body=document.querySelector("#"+tableId+" tbody");
let count=0;for(const row of body.rows){const show=predicate(row);row.hidden=!show;if(show)count++}return count}
function render(){const query=q.value.toLowerCase(),nodeType=nt.value,edgeStatus=es.value;
const nodeCount=filter("lineage-nodes",r=>(!nodeType||r.cells[0].textContent===nodeType)&&
(!query||r.textContent.toLowerCase().includes(query)));
const edgeCount=filter("lineage-edges",r=>(!edgeStatus||r.cells[3].textContent===edgeStatus)&&
(!query||r.textContent.toLowerCase().includes(query))&&
(!nodeType||[...byId.values()].some(n=>n.type===nodeType&&
(n.label===r.cells[1].textContent||n.label===r.cells[2].textContent))));
document.getElementById("lineage-counts").textContent=`${nodeCount} nodes · ${edgeCount} edges · runtime not_run`}
q.addEventListener("input",render);nt.addEventListener("change",render);
es.addEventListener("change",render);render()})();
</script>""",
        html_close(version=_GRAPH_VERSION, timestamp="Static scan"),
    ]
    return "\n".join(parts)


def write_powerbi_lineage_artifacts(project_dir: str | Path) -> tuple[Path, Path]:
    """Write adjacent canonical JSON and standalone HTML in an existing PBIP folder."""
    root = Path(project_dir)
    graph = build_powerbi_lineage_graph(root)
    json_text = _canonical_json(graph)
    html_text = render_powerbi_lineage_html(json_text)
    json_path = root / "powerbi_lineage.json"
    html_path = root / "powerbi_lineage.html"
    try:
        json_path.write_text(json_text, encoding="utf-8", newline="\n")
        html_path.write_text(html_text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise ValueError(f"Unable to write Power BI lineage artifacts in {root}: {exc}") from exc
    return json_path, html_path
