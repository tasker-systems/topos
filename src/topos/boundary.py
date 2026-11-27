"""Classify an edge that crosses between two sides of a structural topology.

A crossing edge is **state-carrying** when state written on one side is read by a function or
guard on the other, and **reference-only** when it names the other side without carrying its
state across. The rule reads only the topology: edge kinds, which side writes each relation, and
each function's declared volatility. It never reads intent.

| Edge | Class |
|---|---|
| `writes` | state-carrying: a write into the other side |
| `reads` of a table | state-carrying if the table is written on its own side, or has no extracted writer (it is then written outside the extracted schema); reference-only if every extracted writer is on the reader's side |
| `reads` of a view | state-carrying: a view exposes the state of what it reads |
| `calls` | state-carrying unless the callee is immutable, whose answer depends only on its arguments |
| `trigger_on`, `executes` | state-carrying: a trigger fires on a write and passes the row on |
| `policy_on` | state-carrying: a policy guards the rows of its table |
| `foreign_key`, `typed_by` | reference-only: a key or a type names the other side without reading its state |
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

STATE = "state-carrying"
REFERENCE = "reference-only"


class Classifier:
    """Classifies crossing edges of one topology, for any assignment of nodes to sides."""

    def __init__(self, topology: dict[str, Any]) -> None:
        self.nodes = {node["id"]: node for node in topology["nodes"]}
        self.writers: dict[str, set[str]] = defaultdict(set)
        for edge in topology["edges"]:
            if edge["kind"] == "writes":
                self.writers[edge["to"]].add(edge["from"])

    def classify(self, edge: dict[str, Any], side: Callable[[str], object]) -> tuple[str, str]:
        """The class of `edge` and its basis, where `side(node)` names the node's side."""
        kind = edge["kind"]
        if kind == "writes":
            return STATE, "a write into a table on the other side"
        if kind in ("foreign_key", "typed_by"):
            return (
                REFERENCE,
                f"a {kind.replace('_', ' ')} names the other side without reading its state",
            )
        if kind in ("trigger_on", "executes"):
            return STATE, "a trigger fires on a write and passes the row to its function"
        if kind == "policy_on":
            return STATE, "a policy guards the rows of its table"
        if kind == "calls":
            callee = self.nodes.get(edge["to"], {})
            if callee.get("volatility") == "immutable":
                return (
                    REFERENCE,
                    "the callee is immutable: its answer depends only on its arguments",
                )
            volatility = callee.get("volatility", "unknown")
            return STATE, f"the callee is {volatility}: its answer can depend on state it reads"
        if kind == "reads":
            target = self.nodes.get(edge["to"], {})
            if target.get("kind") in ("view", "materialized_view"):
                return STATE, "a view exposes the state of the relations it reads"
            writers = self.writers.get(edge["to"], set())
            if not writers:
                return (
                    STATE,
                    "the table has no extracted writer, so it is written outside the schema",
                )
            target_side = side(edge["to"])
            if any(side(writer) == target_side for writer in writers):
                return STATE, "the table is written on its own side and read on the other"
            return REFERENCE, "every extracted writer of the table is on the reader's side"
        return STATE, f"an edge of kind {kind} is not classified, so it is assumed to carry state"
