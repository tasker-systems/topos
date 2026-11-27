"""Parse SQL and PL/pgSQL function bodies with `pglast`, PostgreSQL's own parser.

The catalog does not record what a PL/pgSQL body references, so each body is parsed and every
relation it names, every function it calls, and every table it writes is collected. Names are
returned as written; resolving them against the schema is the builder's job. A dynamic statement
(`EXECUTE`) cannot be resolved statically, and is reported so the builder records it as a hole.

This is a reading of references, not of semantics: which branch runs, and with which rows, is
left to probing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pglast import parser

WRITE_STATEMENTS = {
    "InsertStmt": "insert",
    "UpdateStmt": "update",
    "DeleteStmt": "delete",
    "MergeStmt": "merge",
}


@dataclass
class BodyReferences:
    """What one body names. Each name is a tuple of its written parts, such as `("s", "t")`."""

    reads: set[tuple[str, ...]] = field(default_factory=set)
    writes: dict[tuple[str, ...], set[str]] = field(default_factory=dict)
    calls: dict[tuple[str, ...], set[int]] = field(default_factory=dict)
    local_names: set[str] = field(default_factory=set)
    dynamic_sql: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: BodyReferences) -> None:
        self.reads |= other.reads
        for name, operations in other.writes.items():
            self.writes.setdefault(name, set()).update(operations)
        for name, arities in other.calls.items():
            self.calls.setdefault(name, set()).update(arities)
        self.local_names |= other.local_names
        self.dynamic_sql += other.dynamic_sql
        self.errors.extend(other.errors)


def _name(parts: list[Any]) -> tuple[str, ...]:
    return tuple(part["String"]["sval"] for part in parts if "String" in part)


def _range_var(node: dict[str, Any]) -> tuple[str, ...]:
    parts = [node.get("catalogname"), node.get("schemaname"), node["relname"]]
    return tuple(part for part in parts if part)


def _walk_sql(node: Any, refs: BodyReferences) -> None:
    """Collect references from a parse tree in pglast's JSON form."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(current)
            continue
        if not isinstance(current, dict):
            continue
        for key, value in current.items():
            if key == "RangeVar":
                refs.reads.add(_range_var(value))
            elif key == "FuncCall":
                refs.calls.setdefault(_name(value["funcname"]), set()).add(
                    len(value.get("args", []))
                )
            elif key in WRITE_STATEMENTS and "relation" in value:
                target = _range_var(value["relation"])
                refs.writes.setdefault(target, set()).add(WRITE_STATEMENTS[key])
                rest = {k: v for k, v in value.items() if k != "relation"}
                stack.append(rest)
                continue
            elif key == "TruncateStmt":
                for relation in value.get("relations", []):
                    target = _range_var(relation["RangeVar"])
                    refs.writes.setdefault(target, set()).add("truncate")
                continue
            elif key == "CommonTableExpr":
                refs.local_names.add(value["ctename"])
            elif key == "CreateStmt":
                refs.local_names.add(value["relation"]["relname"])
                continue
            stack.append(value)


def references_in_sql(sql: str) -> BodyReferences:
    """References made by one or more SQL statements."""
    refs = BodyReferences()
    try:
        tree = json.loads(parser.parse_sql_json(sql))
    except parser.ParseError as error:
        refs.errors.append(str(error))
        return refs
    _walk_sql(tree, refs)
    return refs


def _expression_sql(query: str, mode: int) -> list[str]:
    """Candidate SQL texts for a PL/pgSQL expression, most likely first."""
    if mode == 0:
        return [query]
    candidates = [f"SELECT {query}"]
    if ":=" in query:
        candidates.append(f"SELECT {query.split(':=', 1)[1]}")
    elif "=" in query:
        candidates.append(f"SELECT {query.split('=', 1)[1]}")
    return candidates


def _walk_plpgsql(node: Any, refs: BodyReferences) -> None:
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(current)
            continue
        if not isinstance(current, dict):
            continue
        for key, value in current.items():
            if key == "PLpgSQL_stmt_dynexecute" or key == "PLpgSQL_stmt_dynfors":
                refs.dynamic_sql += 1
            if key == "PLpgSQL_expr":
                query, mode = value["query"], value.get("parseMode", 0)
                last_error = None
                for candidate in _expression_sql(query, mode):
                    found = references_in_sql(candidate)
                    if not found.errors:
                        refs.merge(found)
                        last_error = None
                        break
                    last_error = found.errors[0]
                if last_error is not None:
                    refs.errors.append(f"{last_error}: {query}")
                continue
            stack.append(value)


def references_in_plpgsql(definition: str) -> BodyReferences:
    """References made by a PL/pgSQL function, given its full `CREATE FUNCTION` statement."""
    refs = BodyReferences()
    try:
        tree = json.loads(parser.parse_plpgsql_json(definition))
    except parser.ParseError as error:
        refs.errors.append(str(error))
        return refs
    _walk_plpgsql(tree, refs)
    return refs
