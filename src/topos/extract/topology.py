"""Build a structural-topology document (schema 0.2) from a catalog snapshot.

Every node and edge is `extracted`: it comes from the catalog, or from a function body parsed by
`pglast`. Nothing here reads an intent, and nothing is excluded because it is not meant to happen.
Each exclusion states its basis, and each reference the extractor could not resolve is recorded as
a hole rather than dropped.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from pglast import parser

from topos import __version__
from topos.extract.bodies import BodyReferences, references_in_plpgsql, references_in_sql

EXTRACTOR = "topos.extract.postgres"
RELKINDS = {"r": "table", "p": "table", "v": "view", "m": "materialized_view"}
VOLATILITY = {"i": "immutable", "s": "stable", "v": "volatile"}
POLICY_COMMANDS = {"r": "select", "a": "insert", "w": "update", "d": "delete", "*": "all"}
CATALOG_SCHEMAS = {"pg_catalog", "information_schema"}


@dataclass(frozen=True)
class Exclusion:
    """A node-id pattern to leave out, with its basis and reason."""

    selector: str
    basis: str
    reason: str


def _provenance(source: str) -> dict[str, str]:
    return {"kind": "extracted", "source": source, "extractor": f"{EXTRACTOR} {__version__}"}


def _search_path(setting: str) -> list[str]:
    schemas = []
    for part in setting.split(","):
        name = part.strip().strip('"')
        if name and name != "$user":
            schemas.append(name)
    return schemas


def _function_search_path(config: list[str], default: list[str]) -> list[str]:
    for entry in config:
        key, _, value = entry.partition("=")
        if key.strip() == "search_path":
            return _search_path(value)
    return default


def _check_values(definition: str) -> tuple[str, list[str]] | None:
    """The column and value set of a check constraint of the form `col IN (...)`, if it is one."""
    expression = definition.removeprefix("CHECK ").removesuffix(" NOT VALID")
    try:
        tree = json.loads(parser.parse_sql_json(f"SELECT 1 WHERE {expression}"))
    except parser.ParseError:
        return None
    node = tree["stmts"][0]["stmt"]["SelectStmt"]["whereClause"]
    if "A_Expr" not in node:
        return None
    expr = node["A_Expr"]
    if expr.get("kind") not in ("AEXPR_OP_ANY", "AEXPR_IN"):
        return None
    left = expr.get("lexpr", {}).get("ColumnRef")
    if left is None or len(left["fields"]) != 1 or "String" not in left["fields"][0]:
        return None
    right = expr.get("rexpr")
    if isinstance(right, dict) and "A_ArrayExpr" in right:
        items = right["A_ArrayExpr"].get("elements", [])
    elif isinstance(right, dict) and "List" in right:
        items = right["List"].get("items", [])
    elif isinstance(right, list):
        items = right
    else:
        return None
    values = []
    for item in items:
        while "TypeCast" in item:
            item = item["TypeCast"]["arg"]
        constant = item.get("A_Const", {})
        if "sval" not in constant:
            return None
        values.append(constant["sval"]["sval"])
    return left["fields"][0]["String"]["sval"], values


def _trigger_shape(tgtype: int) -> tuple[str, list[str], str]:
    level = "row" if tgtype & 1 else "statement"
    if tgtype & 64:
        timing = "instead_of"
    elif tgtype & 2:
        timing = "before"
    else:
        timing = "after"
    events = [
        name
        for bit, name in ((4, "insert"), (8, "delete"), (16, "update"), (32, "truncate"))
        if tgtype & bit
    ]
    return timing, events, level


class _Builder:
    def __init__(self, snapshot: dict[str, Any], exclusions: list[Exclusion]) -> None:
        self.snapshot = snapshot
        self.exclusions = exclusions
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.holes: list[dict[str, Any]] = []
        self.rel_ids: dict[int, str] = {}
        self.fn_ids: dict[int, str] = {}
        self.type_ids: dict[int, str] = {}
        self.rel_by_name: dict[tuple[str, str], str] = {}
        self.fn_by_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.catalog_functions = {row["name"] for row in snapshot["catalog_function_names"]}
        self.extension_names = {
            (row["schema"], row["name"]) for row in snapshot["extension_object_names"]
        }
        self.extension_bare = {name for _, name in self.extension_names}
        self.default_path = _search_path(snapshot["search_path"])

    def excluded(self, node_id: str) -> bool:
        return any(fnmatch.fnmatchcase(node_id, rule.selector) for rule in self.exclusions)

    def add_node(self, node: dict[str, Any]) -> bool:
        if self.excluded(node["id"]):
            return False
        self.nodes[node["id"]] = node
        return True

    def add_edge(self, kind: str, source: str, target: str, provenance: str, **extra: Any) -> None:
        if source not in self.nodes or target not in self.nodes:
            return
        edge = {"kind": kind, "from": source, "to": target, "provenance": _provenance(provenance)}
        edge.update({key: value for key, value in extra.items() if value})
        self.edges.append(edge)

    def relations(self) -> None:
        columns: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for column in self.snapshot["columns"]:
            columns[column["rel"]].append(column)
        types = {row["oid"]: row for row in self.snapshot["types"]}
        for row in self.snapshot["types"]:
            kind = {"e": "enum", "d": "domain"}[row["typtype"]]
            node_id = f"type:{row['schema']}.{row['name']}"
            node = {
                "id": node_id,
                "kind": "type",
                "schema": row["schema"],
                "name": row["name"],
                "type_kind": kind,
                "provenance": _provenance(f"pg_type:{row['schema']}.{row['name']}"),
            }
            if kind == "enum":
                node["values"] = list(row["labels"])
            if self.add_node(node):
                self.type_ids[row["oid"]] = node_id
        checks: dict[int, dict[str, tuple[str, list[str]]]] = defaultdict(dict)
        for con in self.snapshot["constraints"]:
            if con["kind"] == "c":
                found = _check_values(con["definition"])
                if found is not None:
                    checks[con["rel"]][found[0]] = (con["name"], found[1])
        for row in self.snapshot["relations"]:
            kind = RELKINDS[row["relkind"]]
            node_id = f"{kind}:{row['schema']}.{row['name']}"
            node: dict[str, Any] = {
                "id": node_id,
                "kind": kind,
                "schema": row["schema"],
                "name": row["name"],
            }
            cols = []
            for column in columns.get(row["oid"], []):
                entry: dict[str, Any] = {
                    "name": column["name"],
                    "type": column["type"],
                    "nullable": column["nullable"],
                }
                if column["default"] is not None:
                    entry["default"] = column["default"]
                enum = types.get(column["type_oid"])
                if enum is not None and enum["typtype"] == "e":
                    entry["values"] = list(enum["labels"])
                    entry["values_from"] = "enum"
                elif column["name"] in checks[row["oid"]]:
                    entry["values"] = checks[row["oid"]][column["name"]][1]
                    entry["values_from"] = "check"
                cols.append(entry)
            node["columns"] = cols
            if kind == "table":
                node["row_security"] = row["row_security"]
            node["provenance"] = _provenance(f"pg_class:{row['schema']}.{row['name']}")
            if self.add_node(node):
                self.rel_ids[row["oid"]] = node_id
                self.rel_by_name[(row["schema"], row["name"])] = node_id
        names = {col["rel"]: {} for col in self.snapshot["columns"]}
        for column in self.snapshot["columns"]:
            names[column["rel"]][column["num"]] = column["name"]
        for con in self.snapshot["constraints"]:
            source = self.rel_ids.get(con["rel"])
            if source is None:
                continue
            cols = [names.get(con["rel"], {}).get(num, str(num)) for num in con["cols"] or []]
            if con["kind"] == "p":
                self.nodes[source]["primary_key"] = cols
            elif con["kind"] == "f":
                target = self.rel_ids.get(con["ref_rel"])
                if target is not None:
                    self.add_edge(
                        "foreign_key",
                        source,
                        target,
                        f"pg_constraint:{con['name']}",
                        via="key",
                        columns=cols,
                        name=con["name"],
                    )
        for column in self.snapshot["columns"]:
            source = self.rel_ids.get(column["rel"])
            target = self.type_ids.get(column["type_oid"])
            if source and target:
                self.add_edge(
                    "typed_by",
                    source,
                    target,
                    f"pg_attribute:{column['name']}",
                    via="column",
                    columns=[column["name"]],
                )

    def functions(self) -> None:
        for row in self.snapshot["functions"]:
            kind = "procedure" if row["kind"] == "p" else "function"
            signature = f"{row['schema']}.{row['name']}({','.join(row['args'])})"
            node_id = f"{kind}:{signature}"
            node = {
                "id": node_id,
                "kind": kind,
                "schema": row["schema"],
                "name": row["name"],
                "arguments": list(row["args"]),
                "returns": row["returns"],
                "language": row["language"],
                "volatility": VOLATILITY[row["volatility"]],
                "security_definer": row["security_definer"],
                "body_sha256": hashlib.sha256((row["src"] or "").encode()).hexdigest(),
                "provenance": _provenance(f"pg_proc:{signature}"),
            }
            if self.add_node(node):
                self.fn_ids[row["oid"]] = node_id
                self.fn_by_name[(row["schema"], row["name"])].append(row)
        for row in self.snapshot["functions"]:
            node_id = self.fn_ids.get(row["oid"])
            if node_id is None:
                continue
            refs = self._parse_body(row, node_id)
            if refs is not None:
                path = _function_search_path(row["config"], self.default_path)
                self._link_body(node_id, refs, path)

    def _parse_body(self, row: dict[str, Any], node_id: str) -> BodyReferences | None:
        language = row["language"]
        if language == "plpgsql":
            refs = references_in_plpgsql(row["definition"])
        elif language == "sql":
            refs = references_in_sql(row["definition"] if row["atomic"] else row["src"])
        else:
            self.holes.append(
                {
                    "element": node_id,
                    "what": "unsupported-language",
                    "detail": f"language {language} is not parsed",
                }
            )
            return None
        for error in sorted(set(refs.errors)):
            self.holes.append({"element": node_id, "what": "unparsed-body", "detail": error})
        if refs.dynamic_sql:
            self.holes.append(
                {
                    "element": node_id,
                    "what": "dynamic-sql",
                    "detail": f"{refs.dynamic_sql} dynamic statement(s)",
                }
            )
        return refs

    def _resolve_relation(self, name: tuple[str, ...], path: list[str]) -> str | bool | None:
        """A node id, `None` if it should be ignored, or `False` if it did not resolve."""
        if len(name) >= 2:
            schema, relname = name[-2], name[-1]
            if schema in CATALOG_SCHEMAS or (schema, relname) in self.extension_names:
                return None
            return self.rel_by_name.get((schema, relname), False)
        relname = name[0]
        for schema in path:
            if (schema, relname) in self.rel_by_name:
                return self.rel_by_name[(schema, relname)]
        if relname.startswith("pg_") or relname in self.extension_bare:
            return None
        return False

    def _resolve_function(
        self, name: tuple[str, ...], arities: set[int], path: list[str]
    ) -> list[str] | None:
        if len(name) >= 2:
            schemas = [name[-2]]
            if schemas[0] in CATALOG_SCHEMAS or (schemas[0], name[-1]) in self.extension_names:
                return []
        else:
            schemas = path
        fname = name[-1]
        for schema in schemas:
            candidates = self.fn_by_name.get((schema, fname))
            if not candidates:
                continue
            fitting = [
                row
                for row in candidates
                if any(
                    row["nargs"] - row["nargdefaults"] <= arity <= row["nargs"] for arity in arities
                )
            ]
            chosen = fitting or candidates
            return sorted(self.fn_ids[row["oid"]] for row in chosen)
        if len(name) == 1 and (fname in self.catalog_functions or fname in self.extension_bare):
            return []
        return None

    def _link_body(self, node_id: str, refs: BodyReferences, path: list[str]) -> None:
        source = f"{self.nodes[node_id]['provenance']['source']} body"
        unresolved = set()
        for name in sorted(refs.reads):
            if len(name) == 1 and name[0] in refs.local_names:
                continue
            target = self._resolve_relation(name, path)
            if target is False:
                unresolved.add(".".join(name))
            elif target:
                self.add_edge("reads", node_id, target, source, via="body")
        for name, operations in sorted(refs.writes.items()):
            if len(name) == 1 and name[0] in refs.local_names:
                continue
            target = self._resolve_relation(name, path)
            if target is False:
                unresolved.add(".".join(name))
            elif target:
                self.add_edge(
                    "writes", node_id, target, source, via="body", operations=sorted(operations)
                )
        for name, arities in sorted(refs.calls.items()):
            targets = self._resolve_function(name, arities, path)
            if targets is None:
                unresolved.add(".".join(name) + "()")
                continue
            for target in targets:
                self.add_edge("calls", node_id, target, source, via="body")
        for name in sorted(unresolved):
            self.holes.append({"element": node_id, "what": "unresolved-reference", "detail": name})

    def triggers(self) -> None:
        for row in self.snapshot["triggers"]:
            table = self.rel_ids.get(row["rel"])
            function = self.fn_ids.get(row["func"])
            if table is None:
                continue
            relation = table.split(":", 1)[1]
            node_id = f"trigger:{relation}.{row['name']}"
            timing, events, level = _trigger_shape(row["tgtype"])
            schema = self.nodes[table]["schema"]
            node = {
                "id": node_id,
                "kind": "trigger",
                "schema": schema,
                "name": row["name"],
                "timing": timing,
                "events": events,
                "level": level,
                "provenance": _provenance(f"pg_trigger:{relation}.{row['name']}"),
            }
            if not self.add_node(node):
                continue
            self.add_edge("trigger_on", node_id, table, f"pg_trigger:{row['name']}", via="trigger")
            if function is not None:
                self.add_edge(
                    "executes", node_id, function, f"pg_trigger:{row['name']}", via="trigger"
                )

    def policies(self) -> dict[int, str]:
        ids = {}
        for row in self.snapshot["policies"]:
            table = self.rel_ids.get(row["rel"])
            if table is None:
                continue
            relation = table.split(":", 1)[1]
            node_id = f"policy:{relation}.{row['name']}"
            node = {
                "id": node_id,
                "kind": "policy",
                "schema": self.nodes[table]["schema"],
                "name": row["name"],
                "events": [POLICY_COMMANDS[row["cmd"]]],
                "permissive": row["permissive"],
                "roles": list(row["roles"]),
                "provenance": _provenance(f"pg_policy:{relation}.{row['name']}"),
            }
            if self.add_node(node):
                ids[row["oid"]] = node_id
                self.add_edge("policy_on", node_id, table, f"pg_policy:{row['name']}", via="policy")
        return ids

    def depends(self, policy_ids: dict[int, str]) -> None:
        for row in self.snapshot["depends"]:
            via = row["via"]
            owner = (
                policy_ids.get(row["owner"]) if via == "policy" else self.rel_ids.get(row["owner"])
            )
            if owner is None:
                continue
            source = f"pg_depend:{via}:{owner}"
            if row["ref_class"] == "pg_class":
                target = self.rel_ids.get(row["ref"])
                if target and via in ("view", "policy"):
                    self.add_edge("reads", owner, target, source, via=via)
            elif row["ref_class"] == "pg_proc":
                target = self.fn_ids.get(row["ref"])
                if target:
                    self.add_edge("calls", owner, target, source, via=via)

    def finish_edges(self) -> list[dict[str, Any]]:
        merged: dict[tuple[str, ...], dict[str, Any]] = {}
        for edge in self.edges:
            key = (
                edge["kind"],
                edge["from"],
                edge["to"],
                edge.get("via", ""),
                edge.get("name", ""),
            )
            if key in merged:
                current = merged[key]
                for field in ("columns", "operations"):
                    if field in edge:
                        current[field] = sorted(set(current.get(field, [])) | set(edge[field]))
                continue
            merged[key] = dict(edge)
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in merged.values():
            groups[f"{edge['kind']}:{edge['from']}->{edge['to']}"].append(edge)
        result = []
        for base, members in groups.items():
            for edge in members:
                suffix = ""
                if len(members) > 1:
                    suffix = "#" + (edge.get("name") or edge.get("via", ""))
                edge.pop("name", None)
                if "columns" in edge and edge["kind"] != "foreign_key":
                    edge["columns"] = sorted(edge["columns"])
                result.append({"id": base + suffix, **edge})
        return sorted(result, key=lambda edge: edge["id"])


def build_topology(
    snapshot: dict[str, Any],
    *,
    target_name: str,
    revision: str,
    exclusions: list[Exclusion] | None = None,
) -> dict[str, Any]:
    """The structural topology of the snapshot's schema, as a schema-0.2 document."""
    rules = list(exclusions or [])
    builder = _Builder(snapshot, rules)
    builder.relations()
    builder.functions()
    builder.triggers()
    builder.depends(builder.policies())
    edges = builder.finish_edges()
    excluded = [
        {
            "selector": "schema:pg_catalog, schema:information_schema, schema:pg_toast",
            "basis": "scope",
            "reason": "the system catalog is the database's, not the target's; references to its "
            "objects are not recorded as edges",
        },
        {
            "selector": "sequences",
            "basis": "scope",
            "reason": "schema 0.2 does not model sequences; a default that reads one is not an edge",
        },
        {
            "selector": "partitions",
            "basis": "structure",
            "reason": "a partition is represented by its parent table",
        },
    ]
    for extension in snapshot["extensions"]:
        if extension["members"]:
            excluded.append(
                {
                    "selector": f"extension:{extension['name']}",
                    "basis": "scope",
                    "reason": f"the {extension['name']} {extension['version']} extension's "
                    f"{extension['members']} objects belong to the extension, not the "
                    "target; references to them are not recorded as edges",
                }
            )
    excluded.extend(
        {"selector": rule.selector, "basis": rule.basis, "reason": rule.reason} for rule in rules
    )
    holes = sorted(
        builder.holes, key=lambda hole: (hole["element"], hole["what"], hole.get("detail", ""))
    )
    return {
        "schema_version": "0.2",
        "target": {"name": target_name, "revision": revision, "kind": "software"},
        "extractor": {
            "name": EXTRACTOR,
            "version": __version__,
            "parameters": {"exclusions": [rule.selector for rule in rules]},
            "source": {
                "server_version": snapshot["server_version"],
                "search_path": snapshot["search_path"],
            },
        },
        "nodes": sorted(builder.nodes.values(), key=lambda node: node["id"]),
        "edges": edges,
        "excluded": excluded,
        "holes": holes,
    }
