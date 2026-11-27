"""Small synthetic structural topologies, built in code for the clustering tests."""

from __future__ import annotations

from typing import Any

PROVENANCE = {"kind": "extracted", "source": "synthetic", "extractor": "tests"}


def node(node_id: str, **extra: Any) -> dict[str, Any]:
    kind, name = node_id.split(":", 1)
    return {
        "id": node_id,
        "kind": kind,
        "schema": "s",
        "name": name,
        "provenance": PROVENANCE,
        **extra,
    }


def edge(kind: str, source: str, target: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"{kind}:{source}->{target}",
        "kind": kind,
        "from": source,
        "to": target,
        "provenance": PROVENANCE,
        **extra,
    }


def topology(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "0.2",
        "target": {"name": "synthetic", "revision": "r1"},
        "extractor": {"name": "tests", "version": "0"},
        "nodes": nodes,
        "edges": edges,
        "excluded": [],
        "holes": [],
    }


def two_neighbourhoods() -> dict[str, Any]:
    """Two groups of tables, each with its own functions, joined by one read across."""
    nodes, edges = [], []
    for group in ("a", "b"):
        tables = [f"table:{group}{i}" for i in range(4)]
        nodes += [node(table) for table in tables]
        edges += [
            edge("foreign_key", tables[i], tables[j]) for i in range(4) for j in range(i + 1, 4)
        ]
        for i, table in enumerate(tables):
            function = f"function:{group}_write{i}()"
            nodes.append(node(function, volatility="volatile"))
            edges.append(edge("writes", function, table, operations=["update"]))
            edges.append(edge("reads", function, tables[(i + 1) % 4]))
    nodes.append(node("function:a_peek()", volatility="stable"))
    edges.append(edge("reads", "function:a_peek()", "table:a0"))
    edges.append(edge("reads", "function:a_peek()", "table:b0"))
    return topology(nodes, edges)
