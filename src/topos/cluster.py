"""Propose clusters of a structural topology, and report the edges that cross between them.

Several algorithms run, each with parameters fixed here, and every result is reported together
with where the results disagree. No parameter is tuned to a target: `PARAMETERS` is the whole
configuration, and it is recorded in the output.

- `layered` follows the layered method: tables are clustered by their foreign keys alone, then
  every other node is attached to the cluster it is most strongly joined to, and the edges that
  cross between clusters are counted.
- `louvain-r*` runs Louvain modularity on the whole weighted graph at three resolutions.
- `label-propagation` runs asynchronous label propagation on the whole weighted graph.

    python3 -m topos.cluster topology.json --topology-ref REF --out clustering.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

import networkx as nx
from networkx.algorithms.community import asyn_lpa_communities, louvain_communities, modularity

from topos import __version__
from topos.boundary import STATE, Classifier
from topos.canonical import sha256

PARAMETERS: dict[str, Any] = {
    # How strongly each edge kind joins its ends. Attachments of a trigger or policy to its table,
    # and a trigger to its function, are the tightest; a column's type is the loosest.
    "weights": {
        "foreign_key": 1.0,
        "reads": 1.0,
        "writes": 2.0,
        "calls": 1.0,
        "trigger_on": 3.0,
        "executes": 3.0,
        "policy_on": 3.0,
        "typed_by": 0.5,
    },
    "seed": 0,
    "louvain_resolutions": [0.5, 1.0, 2.0],
    "layered_table_resolution": 1.0,
}


def _graph(topology: dict[str, Any], kinds: set[str] | None = None) -> nx.Graph:
    """The undirected weighted graph of a topology; parallel edges add, self-loops are dropped."""
    weights = PARAMETERS["weights"]
    graph = nx.Graph()
    graph.add_nodes_from(sorted(node["id"] for node in topology["nodes"]))
    for edge in sorted(topology["edges"], key=lambda item: item["id"]):
        if edge["from"] == edge["to"] or (kinds is not None and edge["kind"] not in kinds):
            continue
        a, b = sorted((edge["from"], edge["to"]))
        weight = weights.get(edge["kind"], 1.0)
        if graph.has_edge(a, b):
            graph[a][b]["weight"] += weight
        else:
            graph.add_edge(a, b, weight=weight)
    return graph


def _on_integers(graph: nx.Graph) -> tuple[nx.Graph, list[str]]:
    """The graph relabelled 0..n-1 in sorted-name order, and the names by label.

    Community algorithms iterate over sets of nodes. Over strings that order changes with each
    process's hash seed, so they run over integers, whose order does not.
    """
    names = sorted(graph.nodes)
    index = {name: i for i, name in enumerate(names)}
    relabelled = nx.Graph()
    relabelled.add_nodes_from(range(len(names)))
    for a, b, data in sorted(
        graph.edges(data=True), key=lambda item: (index[item[0]], index[item[1]])
    ):
        relabelled.add_edge(*sorted((index[a], index[b])), weight=data["weight"])
    return relabelled, names


def _louvain(graph: nx.Graph, resolution: float) -> list[set[str]]:
    relabelled, names = _on_integers(graph)
    parts = louvain_communities(
        relabelled, weight="weight", resolution=resolution, seed=PARAMETERS["seed"]
    )
    return [{names[i] for i in part} for part in parts]


def _layered(topology: dict[str, Any], graph: nx.Graph) -> list[set[str]]:
    tables = sorted(node["id"] for node in topology["nodes"] if node["kind"] == "table")
    keys = _graph(topology, {"foreign_key"}).subgraph(tables).copy()
    table_parts = sorted(
        _louvain(keys, PARAMETERS["layered_table_resolution"]),
        key=lambda part: (-len(part), min(part)),
    )
    assignment: dict[str, int] = {}
    for index, part in enumerate(table_parts):
        for node in part:
            assignment[node] = index
    # Attach every other node to the cluster it is most strongly joined to. Each pass attaches
    # only through nodes placed by an earlier pass, so a node joins by its distance from the
    # tables and the result does not depend on the order nodes are visited. A tie goes to the
    # lowest cluster index (the larger cluster, then the smaller first member).
    pending = sorted(node for node in graph.nodes if node not in assignment)
    while pending:
        placed = {}
        for node in pending:
            strength: dict[int, float] = defaultdict(float)
            for neighbour, data in graph[node].items():
                if neighbour in assignment:
                    strength[assignment[neighbour]] += data["weight"]
            if strength:
                best = max(strength.values())
                placed[node] = min(c for c, value in strength.items() if value == best)
        if not placed:
            break
        assignment.update(placed)
        pending = [node for node in pending if node not in placed]
    # What joins nothing already clustered forms clusters of its own, by connected component.
    next_index = max(assignment.values(), default=-1) + 1
    for component in sorted(nx.connected_components(graph.subgraph(pending)), key=min):
        for node in component:
            assignment[node] = next_index
        next_index += 1
    parts: dict[int, set[str]] = defaultdict(set)
    for node, index in assignment.items():
        parts[index].add(node)
    return list(parts.values())


def _label_propagation(graph: nx.Graph) -> list[set[str]]:
    relabelled, names = _on_integers(graph)
    parts = asyn_lpa_communities(relabelled, weight="weight", seed=PARAMETERS["seed"])
    return [{names[i] for i in part} for part in parts]


def _adjusted_rand(a: dict[str, str], b: dict[str, str]) -> float:
    nodes = sorted(a)
    n = len(nodes)
    if n < 2:
        return 1.0
    pairs = Counter((a[node], b[node]) for node in nodes)
    sizes_a = Counter(a[node] for node in nodes)
    sizes_b = Counter(b[node] for node in nodes)

    def choose2(k: int) -> float:
        return k * (k - 1) / 2

    index = sum(choose2(k) for k in pairs.values())
    sum_a = sum(choose2(k) for k in sizes_a.values())
    sum_b = sum(choose2(k) for k in sizes_b.values())
    expected = sum_a * sum_b / choose2(n)
    maximum = (sum_a + sum_b) / 2
    if maximum == expected:
        return 1.0
    return round((index - expected) / (maximum - expected), 6)


def _describe(
    name: str,
    method: str,
    parameters: dict[str, Any],
    parts: list[set[str]],
    topology: dict[str, Any],
    graph: nx.Graph,
    classifier: Classifier,
) -> dict[str, Any]:
    kinds = {node["id"]: node["kind"] for node in topology["nodes"]}
    ordered = sorted(parts, key=lambda part: (-len(part), min(part)))
    cluster_of: dict[str, str] = {}
    for index, part in enumerate(ordered):
        for node in part:
            cluster_of[node] = f"{name}/c{index:03d}"
    internal: Counter[str] = Counter()
    crossing_count: Counter[str] = Counter()
    crossings = []
    for edge in topology["edges"]:
        a, b = cluster_of[edge["from"]], cluster_of[edge["to"]]
        if a == b:
            internal[a] += 1
            continue
        crossing_count[a] += 1
        crossing_count[b] += 1
        kind, basis = classifier.classify(edge, cluster_of.__getitem__)
        crossings.append(
            {"edge": edge["id"], "from_cluster": a, "to_cluster": b, "class": kind, "basis": basis}
        )
    clusters = []
    for index, part in enumerate(ordered):
        cluster_id = f"{name}/c{index:03d}"
        sub = graph.subgraph(part)
        anchor = max(sorted(part), key=lambda node: sub.degree(node, weight="weight"))
        clusters.append(
            {
                "id": cluster_id,
                "size": len(part),
                "anchor": anchor,
                "kinds": dict(sorted(Counter(kinds[node] for node in part).items())),
                "internal_edges": internal[cluster_id],
                "crossing_edges": crossing_count[cluster_id],
                "members": sorted(part),
            }
        )
    counts = Counter(item["class"] for item in crossings)
    return {
        "id": name,
        "method": method,
        "parameters": parameters,
        "modularity": round(modularity(graph, ordered, weight="weight"), 6),
        "clusters": clusters,
        "crossings": crossings,
        "crossing_counts": {
            "state-carrying": counts[STATE],
            "reference-only": counts["reference-only"],
        },
    }


def cluster_topology(topology: dict[str, Any], *, topology_ref: str = "") -> dict[str, Any]:
    """Run every algorithm over `topology`, as a clustering document (schema 0.2)."""
    graph = _graph(topology)
    classifier = Classifier(topology)
    weights = PARAMETERS["weights"]
    seed = PARAMETERS["seed"]
    runs = [
        (
            "layered",
            "layered",
            {
                "tables_by": "foreign_key",
                "table_method": "louvain",
                "resolution": PARAMETERS["layered_table_resolution"],
                "seed": seed,
                "attach_by": "strongest weighted join",
                "weights": weights,
            },
            _layered(topology, graph),
        )
    ]
    for resolution in PARAMETERS["louvain_resolutions"]:
        runs.append(
            (
                f"louvain-r{resolution}",
                "louvain",
                {"resolution": resolution, "seed": seed, "weights": weights},
                _louvain(graph, resolution),
            )
        )
    runs.append(
        (
            "label-propagation",
            "asynchronous label propagation",
            {"seed": seed, "weights": weights},
            _label_propagation(graph),
        )
    )
    algorithms = [
        _describe(name, method, parameters, parts, topology, graph, classifier)
        for name, method, parameters, parts in runs
    ]
    assignments = {
        run["id"]: {
            node: cluster["id"] for cluster in run["clusters"] for node in cluster["members"]
        }
        for run in algorithms
    }
    pairwise = [
        {"a": a, "b": b, "adjusted_rand": _adjusted_rand(assignments[a], assignments[b])}
        for a, b in combinations([run["id"] for run in algorithms], 2)
    ]
    best_match = []
    for run in algorithms:
        for other in algorithms:
            if other is run:
                continue
            for cluster in run["clusters"]:
                members = set(cluster["members"])
                scores = sorted(
                    (-len(members & set(o["members"])) / len(members | set(o["members"])), o["id"])
                    for o in other["clusters"]
                )
                score, match = -scores[0][0], scores[0][1]
                best_match.append(
                    {
                        "cluster": cluster["id"],
                        "other": other["id"],
                        "best": match,
                        "jaccard": round(score, 6),
                    }
                )
    return {
        "schema_version": "0.2",
        "target": dict(topology["target"]),
        "clusterer": {"name": "topos.cluster", "version": __version__, "parameters": PARAMETERS},
        "inputs": [
            {"role": "structural-topology", "ref": topology_ref, "sha256": sha256(topology)}
        ],
        "graph": {
            "nodes": graph.number_of_nodes(),
            "edges": len(topology["edges"]),
            "joined_pairs": graph.number_of_edges(),
        },
        "algorithms": algorithms,
        "disagreement": {"pairwise": pairwise, "best_match": best_match},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("topology", type=Path)
    parser.add_argument("--topology-ref", default="", help="where the topology is held")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    topology = json.loads(args.topology.read_text(encoding="utf-8"))
    document = cluster_topology(topology, topology_ref=args.topology_ref)
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
