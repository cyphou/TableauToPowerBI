"""Entirely synthetic logical-pair fixtures and a deliberately small DAX oracle.

The oracle executes the *emitted* expression, not the Tableau formula or a copy
of the compiler. Unsupported syntax/functions raise instead of silently passing.
It is not a general DAX engine (in particular, it models no relationships or
general context transition). It does model the table lineage and filter-context
operations used by the compiler's explicit table scopes.
"""

import re
import xml.etree.ElementTree as ET


def relation_xml():
    ds = ET.Element("datasource", name="Synthetic")
    graph = ET.SubElement(ds, "object-graph")
    objects = ET.SubElement(graph, "objects")
    for alias in ("Current", "Prior"):
        obj = ET.SubElement(objects, "object", id=alias, caption=alias + " display")
        ET.SubElement(obj, "relation", type="table", name=alias,
                      connection="synthetic_connection", table="[Events]")
    rels = ET.SubElement(graph, "relationships")
    rel = ET.SubElement(rels, "relationship")
    ET.SubElement(rel, "first-end-point", **{"object-id": "Current"})
    ET.SubElement(rel, "second-end-point", **{"object-id": "Prior"})
    conjunction = ET.SubElement(rel, "expression", op="AND")
    for operator, left, right in (
        ("=", "C_Key", "P_Key"), ("=", "C_Group", "P_Group"),
        (">", "C_Time", "P_Time"),
    ):
        comparison = ET.SubElement(conjunction, "expression", op=operator)
        ET.SubElement(comparison, "expression", op=f"[Current].[{left}]")
        ET.SubElement(comparison, "expression", op=f"[Prior].[{right}]")
    return ds


def synthetic_datasource():
    from tableau_export.datasource_extractor import extract_relation_metadata

    specs = {
        "Current": [("C_Key", "Entity", "string"), ("C_Group", "Bucket", "string"),
                    ("C_Time", "Tick", "integer"), ("C_RowID", "RowID", "integer"),
                    ("C_Amount", "Amount", "integer")],
        "Prior": [("P_Key", "Entity", "string"), ("P_Group", "Bucket", "string"),
                  ("P_Time", "Tick", "integer"), ("P_State", "State", "string"),
                  ("P_RowID", "RowID", "integer")],
    }
    tables, columns, provenance = [], [], {}
    for alias, fields in specs.items():
        cols = [dict(name=name, caption=caption, datatype=dtype,
                     role="measure" if dtype == "integer" else "dimension")
                for name, caption, dtype in fields]
        tables.append(dict(name=alias, columns=cols, source_table="[Events]",
                           connection="synthetic_connection"))
        columns.extend(cols)
        provenance.update({name: alias for name, _, _ in fields})
    calculations = [
        dict(name="[anchor]", caption="Latest qualifying tick", datatype="integer",
             formula='{ FIXED [C_Key], [C_Group] : MAX('
                     'IF CONTAINS([P_State], "ready") THEN [P_Time] END) }'),
        dict(name="[row_score]", caption="Pair score", datatype="integer",
             formula='IF [P_Time] = [anchor] AND [P_State] = "ready" '
                     'THEN 1 ELSE 0 END'),
        dict(name="[total_score]", caption="Pair total", datatype="integer",
             formula="SUM([row_score])"),
        dict(name="[ordinary]", caption="Ordinary amount", datatype="integer",
             formula="SUM([C_Amount])"),
    ]
    return dict(name="Synthetic", tables=tables, columns=columns,
                col_local_name_map=provenance, calculations=calculations,
                connection=dict(type="SQL Server", server="synthetic_server",
                                database="synthetic_database"),
                relationships=[], relation_metadata=extract_relation_metadata(relation_xml()))


def safe_policy():
    return dict(
        contains_case_sensitive=True, comparison_case_sensitive=True,
        case_encoding_verified=True, unmatched_left="null_extend",
        row_scopes=dict(left="'Current'", right="'Prior'",
                        fixed_left="ALL('Current')", fixed_right="ALL('Prior')"),
    )


def compiler_arguments():
    ds = synthetic_datasource()
    bindings, types, refs = {}, {}, {}
    for table in ds["tables"]:
        for col in table["columns"]:
            key = table["name"], col["name"]
            bindings[key] = table["name"], col["caption"]
            types[key] = col["datatype"]
            refs[col["name"]] = key
    policy = safe_policy()
    policy.pop("case_encoding_verified")
    return dict(root_calculation="total_score", calculations=ds["calculations"],
                relation_metadata=ds["relation_metadata"], field_bindings=bindings,
                field_types=types, reference_bindings=refs, **policy)


def bag_rows():
    # Duplicate RowID AND identical full rows are intentional, as are timestamp
    # ties with different statuses. Neither keys nor time imply uniqueness.
    current = [
        dict(Entity="A", Bucket="x", Tick=30, RowID=7, Amount=5),
        dict(Entity="A", Bucket="x", Tick=30, RowID=7, Amount=5),
        dict(Entity="A", Bucket="x", Tick=20, RowID=7, Amount=11),
    ]
    prior = [
        dict(Entity="A", Bucket="x", Tick=10, State="ready", RowID=7),
        dict(Entity="A", Bucket="x", Tick=20, State="ready", RowID=7),
        dict(Entity="A", Bucket="x", Tick=20, State="ready", RowID=7),
        dict(Entity="A", Bucket="x", Tick=20, State="waiting", RowID=7),
        dict(Entity="A", Bucket="x", Tick=30, State="ready", RowID=7),
        dict(Entity="A", Bucket="other", Tick=29, State="ready", RowID=7),
        dict(Entity="other", Bucket="x", Tick=29, State="ready", RowID=7),
        dict(Entity="A", Bucket="x", Tick=None, State="ready", RowID=7),
    ]
    return {"Current": current, "Prior": prior}


class EmittedDax:
    """Parse/evaluate the compiler's explicit iterators with bag semantics."""

    _token = re.compile(
        r"""\s*('(?:[^']|'')*'|\[(?:[^\]]|\]\])*\]|"(?:[^"]|"")*"|"""
        r"\d+(?:\.\d+)?|[A-Za-z_]\w*|==|<>|>=|<=|&&|\|\||[(),{}=<>+])")
    _precedence = {"||": 1, "&&": 2, "==": 3, "=": 3, "<>": 3,
                   ">": 3, "<": 3, ">=": 3, "<=": 3, "+": 4}

    def __init__(self, expression):
        self.tokens = []
        pos = 0
        while expression[pos:].strip():
            match = self._token.match(expression, pos)
            if not match:
                raise AssertionError("Unrecognized DAX: " + expression[pos:pos + 60])
            self.tokens.append(match[1])
            pos = match.end()
        self.tokens.append("<EOF>")
        self.pos = 0
        self.tree = self.parse()
        self.need("<EOF>")

    def need(self, token):
        actual = self.tokens[self.pos]
        if actual != token:
            raise AssertionError(f"Expected {token}, got {actual}")
        self.pos += 1

    def parse(self, minimum=0):
        token = self.tokens[self.pos]
        self.pos += 1
        if token == "{":
            values = []
            if self.tokens[self.pos] != "}":
                values.append(self.parse())
                while self.tokens[self.pos] == ",":
                    self.pos += 1
                    values.append(self.parse())
            self.need("}")
            node = ("set", values)
        elif token == "VAR":
            assignments = []
            while True:
                name = self.tokens[self.pos]
                self.pos += 1
                self.need("=")
                assignments.append((name, self.parse()))
                if self.tokens[self.pos] != "VAR":
                    break
                self.pos += 1
            self.need("RETURN")
            node = ("let", assignments, self.parse())
        elif token == "(":
            node = self.parse()
            self.need(")")
        elif token.startswith("'"):
            name = token[1:-1].replace("''", "'")
            if self.tokens[self.pos].startswith("["):
                column = self.tokens[self.pos][1:-1].replace("]]", "]")
                self.pos += 1
                node = ("column", name, column)
            else:
                node = ("table", name)
        elif token.startswith('"'):
            node = ("value", token[1:-1].replace('""', '"'))
        elif token[0].isdigit():
            node = ("value", float(token))
        elif self.tokens[self.pos] == "(":
            self.pos += 1
            args = []
            if self.tokens[self.pos] != ")":
                args.append(self.parse())
                while self.tokens[self.pos] == ",":
                    self.pos += 1
                    args.append(self.parse())
            self.need(")")
            node = ("call", token.upper(), args)
        else:
            node = ("variable", token)
        while self._precedence.get(self.tokens[self.pos], -1) >= minimum:
            op = self.tokens[self.pos]
            self.pos += 1
            node = ("op", op, node, self.parse(self._precedence[op] + 1))
        return node

    def evaluate(self, tables, visible=None):
        # Keep table lineage in every table value as (table name, row). A table
        # scope therefore retains its identity when it is filtered/iterated,
        # while filter context is carried separately from the row context.
        visible = tables if visible is None else visible

        def run(node, variables, row, context):
            kind = node[0]
            if kind == "value":
                return node[1]
            if kind == "table":
                return [(node[1], value) for value in context[node[1]]]
            if kind == "column":
                return row[node[1]][node[2]]
            if kind == "variable":
                return variables[node[1]]
            if kind == "set":
                return [run(value, variables, row, context) for value in node[1]]
            if kind == "let":
                local = dict(variables)
                for name, value in node[1]:
                    local[name] = run(value, local, row, context)
                return run(node[2], local, row, context)
            if kind == "op":
                op = node[1]
                a, b = (run(n, variables, row, context) for n in node[2:])
                if op == "+":
                    # DAX addition preserves BLANK only when BOTH operands
                    # are BLANK; a single BLANK acts as numeric zero.
                    if a is None and b is None:
                        return None
                    return (0 if a is None else a) + (0 if b is None else b)
                if op in ("==", "="):
                    if isinstance(a, bool) or isinstance(b, bool):
                        return type(a) is type(b) and a == b
                    if isinstance(a, str) and isinstance(b, str):
                        return a.casefold() == b.casefold()
                    return a == b
                if op == "<>":
                    return a != b
                if op == "&&":
                    return bool(a) and bool(b)
                if op == "||":
                    return bool(a) or bool(b)
                return {">": lambda: a > b, "<": lambda: a < b,
                        ">=": lambda: a >= b, "<=": lambda: a <= b}[op]()
            name, args = node[1:]
            ev = lambda n: run(n, variables, row, context)
            if name == "IF":
                return ev(args[1]) if ev(args[0]) else ev(args[2])
            if name == "ALL":
                assert args[0][0] == "table", "Oracle supports ALL(table) only"
                table = args[0][1]
                return [(table, value) for value in tables[table]]
            if name == "SUM":
                assert args[0][0] == "column", "Oracle supports SUM(column) only"
                _, table, column = args[0]
                values = [value[column] for value in context[table]
                          if value[column] is not None]
                return sum(values) if values else None
            if name == "CALCULATETABLE":
                # Implement precisely the modifier/filter forms emitted by the
                # logical compiler. Modifiers reset a table's current filter
                # context; KEEPFILTERS(TREATAS(...)) intersects that context
                # without expanding or deduplicating its row bag.
                scoped = dict(context)
                pending = []
                for modifier in args[1:]:
                    if modifier[0] != "call":
                        raise AssertionError("Unsupported CALCULATETABLE filter")
                    filter_name, filter_args = modifier[1:]
                    if filter_name == "REMOVEFILTERS":
                        if (len(filter_args) != 1
                                or filter_args[0][0] != "table"):
                            raise AssertionError(
                                "Oracle supports REMOVEFILTERS(table) only")
                        table = filter_args[0][1]
                        scoped[table] = tables[table]
                    elif filter_name == "KEEPFILTERS":
                        treatas = filter_args[0]
                        if (len(filter_args) != 1 or treatas[0] != "call"
                                or treatas[1] != "TREATAS"):
                            raise AssertionError(
                                "Oracle supports KEEPFILTERS(TREATAS(...)) only")
                        values_node, column_node = treatas[2]
                        if (column_node[0] != "column"
                                or len(treatas[2]) != 2):
                            raise AssertionError(
                                "Oracle supports TREATAS(values, column) only")
                        allowed = run(values_node, variables, row, context)
                        table, column = column_node[1:]
                        pending.append((table, column, allowed))
                    else:
                        raise AssertionError(
                            "Unsupported CALCULATETABLE modifier: " + filter_name)
                for table, column, allowed in pending:
                    def coarse_match(value):
                        # TREATAS is only a coarse storage-engine key filter
                        # here. String collation may match case variants; the
                        # compiler's later EXACT predicate remains authoritative.
                        return any(
                            (isinstance(value, str) and isinstance(key, str)
                             and value.casefold() == key.casefold())
                            or (not isinstance(value, str)
                                and not isinstance(key, str) and value == key)
                            for key in allowed)
                    scoped[table] = [
                        value for value in scoped[table]
                        if coarse_match(value.get(column))
                    ]
                return run(args[0], variables, row, scoped)
            if name in ("FILTER", "SUMX", "MAXX"):
                values = []
                for table, value in ev(args[0]):
                    result = run(args[1], variables, dict(row, **{table: value}),
                                 context)
                    if name == "FILTER":
                        if result:
                            values.append((table, value))
                    elif result is not None:
                        values.append(result)
                if name == "FILTER":
                    return values
                return (sum(values) if name == "SUMX" else max(values)) if values else None
            values = [ev(arg) for arg in args]
            if name == "BLANK":
                return None
            if name in ("TRUE", "FALSE"):
                return name == "TRUE"
            if name == "ISBLANK":
                return values[0] is None
            if name == "ISEMPTY":
                return not values[0]
            if name == "EXACT":
                return values[0] == values[1]
            if name == "NOT":
                return not values[0]
            if name == "FIND":
                index = values[1].find(values[0], int(values[2]) - 1)
                return index + 1 if index >= 0 else values[3]
            raise AssertionError("Unsupported oracle function: " + name)

        return run(self.tree, {}, {}, visible)
