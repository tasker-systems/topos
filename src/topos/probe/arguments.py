"""Derive, from the schema alone, the values a probe may pass to each parameter of a function.

A probe calls a target's predicate functions over every combination inside declared bounds. The
arguments it passes must come from the schema, never from a hand-picked case, and each value
records where it came from. For one function parameter this module finds:

- **tables:** the tables whose rows the parameter identifies. A parameter compared with a table's
  single-column primary key identifies that table; compared with a single-column foreign key, the
  table referenced. A parameter compared with the id column of a polymorphic reference (see
  `_Reader.polymorphic`) identifies each table its partner may name. A parameter passed on to
  another function takes on what that function's parameter identifies, recursively.
- **literals:** the values the parameter is compared with. These are the labels of its enum type;
  the literals a body compares it with directly (`=`, `IN`, `= ANY(ARRAY[...])`, `CASE p WHEN`);
  the literals of a check constraint on a column the body compares it with; and, recursively, what
  a callee's parameter is compared with. A boolean parameter takes both truth values.

The reading is of comparisons, not semantics: which branch a call takes is left to the probe.
Parsing uses `pglast`, PostgreSQL's own parser, for SQL and PL/pgSQL bodies alike.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pglast import parser

from topos.extract.bodies import _expression_sql

SIGNATURE_QUERIES: dict[str, str] = {
    "functions": """
        SELECT n.nspname AS schema, p.proname AS name,
               coalesce(p.proargnames[1:p.pronargs], ARRAY[]::text[]) AS argnames,
               ARRAY(SELECT format_type(x, NULL) FROM unnest(p.proargtypes) WITH ORDINALITY
                     AS a(x, i) ORDER BY i) AS args,
               ARRAY(SELECT t.typtype::text FROM unnest(p.proargtypes) WITH ORDINALITY
                     AS a(x, i) JOIN pg_type t ON t.oid = a.x ORDER BY i) AS argkinds,
               ARRAY(SELECT coalesce((SELECT string_agg(e.enumlabel, E'\\x1f'
                                                  ORDER BY e.enumsortorder)
                                      FROM pg_enum e WHERE e.enumtypid = a.x), '')
                     FROM unnest(p.proargtypes) WITH ORDINALITY AS a(x, i) ORDER BY i)
                 AS argenums,
               p.pronargdefaults AS nargdefaults,
               format_type(p.prorettype, NULL) AS returns, l.lanname AS language,
               p.provolatile::text AS volatility, p.proretset AS returns_set,
               p.prosrc AS src,
               CASE WHEN l.lanname IN ('sql', 'plpgsql') THEN pg_get_functiondef(p.oid) END
                 AS definition
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_language l ON l.oid = p.prolang
        WHERE p.prokind = 'f'
          AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
          AND n.nspname NOT LIKE 'pg_temp_%'
        ORDER BY 1, 2, p.oid""",
    "columns": """
        SELECT n.nspname AS schema, c.relname AS "table", a.attname AS name,
               format_type(a.atttypid, a.atttypmod) AS type
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'v', 'm')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
        ORDER BY 1, 2, a.attnum""",
    "constraints": """
        SELECT n.nspname AS schema, c.relname AS "table", con.contype::text AS kind,
               ARRAY(SELECT a.attname::text FROM unnest(con.conkey) WITH ORDINALITY AS k(n, i)
                     JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n
                     ORDER BY i) AS columns,
               rn.nspname AS ref_schema, rc.relname AS ref_table,
               pg_get_constraintdef(con.oid) AS definition
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_class rc ON rc.oid = con.confrelid
        LEFT JOIN pg_namespace rn ON rn.oid = rc.relnamespace
        WHERE con.contype IN ('p', 'f', 'c')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
        ORDER BY 1, 2, con.conname""",
    "enums": """
        SELECT n.nspname AS schema, t.typname AS name,
               ARRAY(SELECT e.enumlabel::text FROM pg_enum e WHERE e.enumtypid = t.oid
                     ORDER BY e.enumsortorder) AS labels
        FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE t.typtype = 'e'
          AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
        ORDER BY 1, 2""",
}


def read_signatures(dsn: str) -> dict[str, Any]:
    """Read what argument derivation needs from the database at `dsn`, read-only."""
    import psycopg
    from psycopg.rows import dict_row

    snapshot: dict[str, Any] = {}
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.read_only = True
        with connection.cursor() as cursor:
            for name, query in SIGNATURE_QUERIES.items():
                cursor.execute(query)
                snapshot[name] = [dict(row) for row in cursor.fetchall()]
        connection.rollback()
    for function in snapshot["functions"]:
        function["argenums"] = [
            label.split("\x1f") if label else [] for label in function["argenums"]
        ]
    return snapshot


def function_id(schema: str, name: str, args: list[str]) -> str:
    """The topology's id for a function: `function:schema.name(type,...)`."""
    return f"function:{schema}.{name}({','.join(args)})"


@dataclass
class Parameter:
    """What the schema says one parameter may be. Each found value maps to its bases."""

    position: int
    name: str
    type: str
    kind: str
    has_default: bool
    tables: dict[str, set[str]] = field(default_factory=dict)
    literals: dict[Any, set[str]] = field(default_factory=dict)
    partners: set[int] = field(default_factory=set)

    def add_table(self, table: str, basis: str) -> None:
        self.tables.setdefault(table, set()).add(basis)

    def add_literal(self, value: Any, basis: str) -> None:
        self.literals.setdefault(value, set()).add(basis)


@dataclass
class Function:
    schema: str
    name: str
    parameters: list[Parameter]
    returns: str
    language: str
    volatility: str
    returns_set: bool
    sql: list[str]
    parse_errors: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return function_id(self.schema, self.name, [p.type for p in self.parameters])


class Schema:
    """The function signatures, columns and constraints argument derivation reads."""

    def __init__(self, snapshot: dict[str, Any], search_path: tuple[str, ...] = ("public",)):
        self.search_path = search_path
        self.columns: dict[str, set[str]] = {}
        for column in snapshot["columns"]:
            self.columns.setdefault(f"{column['schema']}.{column['table']}", set()).add(
                column["name"]
            )
        self.primary: dict[tuple[str, str], str] = {}
        self.foreign: dict[tuple[str, str], str] = {}
        self.checks: dict[tuple[str, str], list[str]] = {}
        for constraint in snapshot["constraints"]:
            table = f"{constraint['schema']}.{constraint['table']}"
            columns = constraint["columns"]
            if len(columns) != 1:
                continue
            key = (table, columns[0])
            if constraint["kind"] == "p":
                self.primary[key] = table
            elif constraint["kind"] == "f":
                self.foreign[key] = f"{constraint['ref_schema']}.{constraint['ref_table']}"
            elif constraint["kind"] == "c":
                self.checks.setdefault(key, []).append(constraint["definition"])
        self.functions: dict[str, Function] = {}
        self.by_name: dict[tuple[str, int], list[Function]] = {}
        for row in snapshot["functions"]:
            function = _function(row)
            self.functions[function.id] = function
            for arity in range(len(row["args"]) - row["nargdefaults"], len(row["args"]) + 1):
                for qualified in (function.name, f"{function.schema}.{function.name}"):
                    self.by_name.setdefault((qualified, arity), []).append(function)

    def table(self, written: tuple[str, ...]) -> str | None:
        """Resolve a table name as written against the search path."""
        if len(written) >= 2:
            name = f"{written[-2]}.{written[-1]}"
            return name if name in self.columns else None
        for schema in self.search_path:
            name = f"{schema}.{written[-1]}"
            if name in self.columns:
                return name
        return None

    def callee(self, written: tuple[str, ...], arity: int) -> Function | None:
        candidates = self.by_name.get((".".join(written[-2:]), arity), [])
        if len(written) == 1:
            in_path = [f for f in candidates if f.schema in self.search_path]
            candidates = in_path or candidates
        return candidates[0] if len(candidates) == 1 else None


def _function(row: dict[str, Any]) -> Function:
    count = len(row["args"])
    names = list(row["argnames"]) + [""] * (count - len(row["argnames"]))
    parameters = [
        Parameter(
            position=i,
            name=names[i] or f"${i + 1}",
            type=row["args"][i],
            kind=row["argkinds"][i],
            has_default=i >= count - row["nargdefaults"],
        )
        for i in range(count)
    ]
    for parameter, labels in zip(parameters, row["argenums"], strict=True):
        for label in labels:
            parameter.add_literal(label, f"enum {parameter.type}")
        if parameter.type == "boolean":
            parameter.add_literal(True, "type boolean")
            parameter.add_literal(False, "type boolean")
    function = Function(
        schema=row["schema"],
        name=row["name"],
        parameters=parameters,
        returns=row["returns"],
        language=row["language"],
        volatility=row["volatility"],
        returns_set=row["returns_set"],
        sql=[],
    )
    if row["language"] == "sql" and row["src"]:
        function.sql = [row["src"]]
    elif row["language"] == "plpgsql" and row["definition"]:
        function.sql, function.parse_errors = _plpgsql_sql(row["definition"])
    return function


def _plpgsql_sql(definition: str) -> tuple[list[str], list[str]]:
    """The SQL texts of a PL/pgSQL body's expressions, each made parseable on its own."""
    try:
        tree = json.loads(parser.parse_plpgsql_json(definition))
    except parser.ParseError as error:
        return [], [str(error)]
    texts: list[str] = []
    errors: list[str] = []
    stack: list[Any] = [tree]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, dict):
            for key, value in current.items():
                if key == "PLpgSQL_expr":
                    for candidate in _expression_sql(value["query"], value.get("parseMode", 0)):
                        try:
                            parser.parse_sql_json(candidate)
                        except parser.ParseError:
                            continue
                        texts.append(candidate)
                        break
                    else:
                        errors.append(value["query"])
                else:
                    stack.append(value)
    return texts, errors


def _strings(parts: list[Any]) -> tuple[str, ...]:
    return tuple(p["String"]["sval"] for p in parts if "String" in p)


def _const(node: Any) -> Any:
    """The value of a literal, through any casts, or None if `node` is not one."""
    while isinstance(node, dict) and "TypeCast" in node:
        node = node["TypeCast"]["arg"]
    if not isinstance(node, dict) or "A_Const" not in node:
        return None
    const = node["A_Const"]
    if "sval" in const:
        return const["sval"]["sval"]
    if "ival" in const:
        return const["ival"].get("ival", 0)
    if "boolval" in const:
        return const["boolval"].get("boolval", False)
    return None


def _unwrap(node: Any) -> Any:
    while isinstance(node, dict) and "TypeCast" in node:
        node = node["TypeCast"]["arg"]
    return node


def check_literals(definition: str) -> list[Any]:
    """The literals a single-column check constraint's expression names, in order."""
    try:
        tree = json.loads(parser.parse_sql_json(f"SELECT 1 WHERE {definition[len('CHECK ') :]}"))
    except parser.ParseError:
        return []
    found: list[Any] = []
    stack: list[Any] = [tree]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(reversed(current))
        elif isinstance(current, dict):
            if "A_Const" in current:
                value = _const(current)
                if isinstance(value, str) and value not in found:
                    found.append(value)
                continue
            stack.extend(reversed(list(current.values())))
    return found


@dataclass(frozen=True)
class _Column:
    table: str
    name: str
    row: tuple[int, str]


class _Reader:
    """Walks one function's bodies and records what each parameter is compared with."""

    def __init__(self, schema: Schema, function: Function, derive: Any):
        self.schema = schema
        self.function = function
        self.derive = derive
        self.by_name = {p.name: p for p in function.parameters}
        self.scope: list[dict[str, str]] = []
        self.rows: dict[tuple[int, str], list[tuple[tuple[str, str], Parameter | str, int]]] = {}
        self.conjunction = 0

    def parameter(self, node: Any, scope: list[dict[str, str]]) -> Parameter | None:
        node = _unwrap(node)
        if not isinstance(node, dict):
            return None
        if "ParamRef" in node:
            number = node["ParamRef"].get("number", 0)
            if 1 <= number <= len(self.function.parameters):
                return self.function.parameters[number - 1]
            return None
        if "ColumnRef" in node:
            fields = _strings(node["ColumnRef"]["fields"])
            # A bare name is a parameter unless a column in scope has it (the column wins).
            if (
                len(fields) == 1
                and fields[0] in self.by_name
                and self._column(fields, scope) is None
            ):
                return self.by_name[fields[0]]
            if len(fields) == 2 and fields[0] == self.function.name and fields[1] in self.by_name:
                return self.by_name[fields[1]]
        return None

    def _column(self, fields: tuple[str, ...], scope: list[dict[str, str]]) -> _Column | None:
        if len(fields) >= 2:
            qualifier, column = fields[-2], fields[-1]
            for frame in reversed(scope):
                if qualifier in frame:
                    table = frame[qualifier]
                    if column not in self.schema.columns[table]:
                        return None
                    return _Column(table, column, (id(frame), qualifier))
            return None
        column = fields[0]
        for frame in reversed(scope):
            owners = [(a, t) for a, t in frame.items() if column in self.schema.columns[t]]
            if len(owners) == 1:
                alias, table = owners[0]
                return _Column(table, column, (id(frame), alias))
            if owners:
                return None
        return None

    def column(self, node: Any, scope: list[dict[str, str]]) -> _Column | None:
        node = _unwrap(node)
        if isinstance(node, dict) and "ColumnRef" in node:
            return self._column(_strings(node["ColumnRef"]["fields"]), scope)
        return None

    def compared(self, parameter: Parameter, other: Any, scope: list[dict[str, str]]) -> None:
        value = _const(other)
        if value is not None:
            parameter.add_literal(value, f"compared in {self.function.id}")
            return
        column = self.column(other, scope)
        if column is None:
            return
        table, name = column.table, column.name
        self.rows.setdefault(column.row, []).append(((table, name), parameter, self.conjunction))
        where = f"{table}.{name}"
        if (table, name) in self.schema.primary:
            parameter.add_table(table, f"compared with primary key {where} in {self.function.id}")
        if (table, name) in self.schema.foreign:
            parameter.add_table(
                self.schema.foreign[(table, name)],
                f"compared with foreign key {where} in {self.function.id}",
            )
        for check in self.schema.checks.get((table, name), []):
            for literal in check_literals(check):
                parameter.add_literal(literal, f"check constraint on {where}")

    def passed(
        self, parameter: Parameter, callee: Function, position: int, args: list[Any]
    ) -> None:
        if callee.id == self.function.id:
            return
        target = self.derive(callee)[position]
        via = f"passed to {callee.id} as {target.name}"
        # A polymorphic partner given a literal table name narrows the tables to that one.
        named = {
            self.schema.table((value,))
            for partner in target.partners
            if partner < len(args) and isinstance(value := _const(args[partner]), str)
        }
        for table, bases in target.tables.items():
            if named and table not in named:
                continue
            for basis in sorted(bases):
                parameter.add_table(table, f"{via}; {basis}")
        for partner in target.partners:
            if partner < len(args):
                passed_partner = self.parameter(args[partner], self.scope)
                if passed_partner is not None:
                    parameter.partners.add(passed_partner.position)
        if parameter.kind != "e" and parameter.type != "boolean":
            for literal, bases in target.literals.items():
                for basis in sorted(bases):
                    parameter.add_literal(literal, f"{via}; {basis}")

    def read(self, sql: str) -> None:
        try:
            tree = json.loads(parser.parse_sql_json(sql))
        except parser.ParseError as error:
            self.function.parse_errors.append(str(error))
            return
        self.walk(tree, [])

    def polymorphic(self) -> None:
        """Find polymorphic references: a row naming a table in one column and its row in another.

        Two columns of one row pair when they share a stem, such as `subject_table` and
        `subject_id`, and the first has a check constraint whose literals all name tables. A
        parameter compared with the id column then identifies the table its partner names: each
        table a partner parameter may name, or the one a partner literal names in the same boolean
        term. The id parameter records its partner, so a probe binds the two consistently.
        """
        for compared in self.rows.values():
            for id_column, id_side, id_conjunction in compared:
                if not isinstance(id_side, Parameter):
                    continue
                for name_column, name_side, name_conjunction in compared:
                    if not self._pairs(name_column, id_column):
                        continue
                    if not isinstance(name_side, Parameter) and name_conjunction != id_conjunction:
                        continue
                    if isinstance(name_side, Parameter):
                        if name_side is id_side:
                            continue
                        written = [v for v in name_side.literals if isinstance(v, str)]
                        id_side.partners.add(name_side.position)
                    else:
                        written = [name_side]
                    for value in written:
                        table = self.schema.table((value,))
                        if table is None:
                            continue
                        id_side.add_table(
                            table,
                            f"polymorphic reference {name_column[0]}.{name_column[1]} names the "
                            f"table of {id_column[1]} ({value}) in {self.function.id}",
                        )

    def _pairs(self, name_column: tuple[str, str], id_column: tuple[str, str]) -> bool:
        (table, name), (_, other) = name_column, id_column
        if name == other or "_" not in name or "_" not in other:
            return False
        if name.rsplit("_", 1)[0] != other.rsplit("_", 1)[0]:
            return False
        checks = self.schema.checks.get((table, name), [])
        literals = [v for check in checks for v in check_literals(check)]
        return bool(literals) and all(self.schema.table((v,)) is not None for v in literals)

    def frame(self, statement: dict[str, Any]) -> dict[str, str]:
        frame: dict[str, str] = {}
        items: list[Any] = []
        for key in ("fromClause", "usingClause"):
            items.extend(statement.get(key, []))
        if "relation" in statement:
            items.append({"RangeVar": statement["relation"]})
        while items:
            item = items.pop()
            if not isinstance(item, dict):
                continue
            if "RangeVar" in item:
                var = item["RangeVar"]
                written = tuple(p for p in (var.get("schemaname"), var["relname"]) if p)
                table = self.schema.table(written)
                if table is not None:
                    frame[var.get("alias", {}).get("aliasname", var["relname"])] = table
            elif "JoinExpr" in item:
                items.extend([item["JoinExpr"]["larg"], item["JoinExpr"]["rarg"]])
        return frame

    def walk(self, node: Any, scope: list[dict[str, str]]) -> None:
        if isinstance(node, list):
            for item in node:
                self.walk(item, scope)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in ("SelectStmt", "UpdateStmt", "DeleteStmt", "InsertStmt"):
                inner = [*scope, self.frame(value)]
                self.walk(list(value.values()), inner)
                continue
            if key == "BoolExpr":
                outer, self.conjunction = self.conjunction, id(value)
                self.walk(value, scope)
                self.conjunction = outer
                continue
            if key == "A_Expr":
                self.a_expr(value, scope)
            elif key == "CaseExpr" and value.get("arg") is not None:
                parameter = self.parameter(value["arg"], scope)
                if parameter is not None:
                    for when in value.get("args", []):
                        self.compared(parameter, when["CaseWhen"]["expr"], scope)
            elif key == "FuncCall":
                self.scope = scope
                args = value.get("args", [])
                callee = self.schema.callee(_strings(value["funcname"]), len(args))
                if callee is not None:
                    for position, arg in enumerate(args):
                        parameter = self.parameter(arg, scope)
                        if parameter is not None:
                            self.passed(parameter, callee, position, args)
            self.walk(value, scope)

    def a_expr(self, expr: dict[str, Any], scope: list[dict[str, str]]) -> None:
        kind = expr.get("kind")
        name = _strings(expr.get("name", []))
        left, right = expr.get("lexpr"), expr.get("rexpr")
        if kind in ("AEXPR_OP", "AEXPR_NOT_DISTINCT", "AEXPR_DISTINCT") and name in (
            ("=",),
            ("<>",),
        ):
            for one, other in ((left, right), (right, left)):
                parameter = self.parameter(one, scope)
                if parameter is not None:
                    self.compared(parameter, other, scope)
                value = _const(other)
                column = self.column(one, scope)
                if isinstance(value, str) and column is not None:
                    key = (column.table, column.name)
                    self.rows.setdefault(column.row, []).append((key, value, self.conjunction))
        elif kind == "AEXPR_IN":
            parameter = self.parameter(left, scope)
            items = right.get("List", {}).get("items", []) if isinstance(right, dict) else []
            if parameter is not None:
                for item in items:
                    self.compared(parameter, item, scope)
            else:
                for item in items:
                    parameter = self.parameter(item, scope)
                    if parameter is not None:
                        self.compared(parameter, left, scope)
        elif kind in ("AEXPR_OP_ANY", "AEXPR_OP_ALL") and name == ("=",):
            parameter = self.parameter(left, scope)
            array = _unwrap(right)
            if parameter is not None and isinstance(array, dict) and "A_ArrayExpr" in array:
                for element in array["A_ArrayExpr"].get("elements", []):
                    self.compared(parameter, element, scope)


def derive(schema: Schema) -> Any:
    """A memoized function from a `Function` to its parameters, with tables and literals found."""
    done: dict[str, list[Parameter]] = {}
    active: set[str] = set()

    def run(function: Function) -> list[Parameter]:
        if function.id in done:
            return done[function.id]
        if function.id in active:
            return function.parameters
        active.add(function.id)
        reader = _Reader(schema, function, run)
        for sql in function.sql:
            reader.read(sql)
        reader.polymorphic()
        active.discard(function.id)
        done[function.id] = function.parameters
        return function.parameters

    return run
