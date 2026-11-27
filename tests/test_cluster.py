"""Clustering a structural topology."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
from synthetic import scattered, two_neighbourhoods

from topos.cluster import PARAMETERS, cluster_topology

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schema" / "clustering.schema.json").read_text())
EXAMPLE = ROOT / "examples" / "lending-library"


def _partitions(document: dict) -> dict[str, set[frozenset[str]]]:
    return {
        run["id"]: {frozenset(c["members"]) for c in run["clusters"]}
        for run in document["algorithms"]
    }


def test_every_algorithm_runs_and_is_a_partition() -> None:
    topology = two_neighbourhoods()
    document = cluster_topology(topology)
    jsonschema.validate(document, SCHEMA)
    nodes = sorted(node["id"] for node in topology["nodes"])
    ids = [run["id"] for run in document["algorithms"]]
    assert ids == ["layered", "louvain-r0.5", "louvain-r1.0", "louvain-r2.0", "label-propagation"]
    for run in document["algorithms"]:
        members = sorted(m for cluster in run["clusters"] for m in cluster["members"])
        assert members == nodes, run["id"]


def test_the_two_neighbourhoods_are_found() -> None:
    document = cluster_topology(two_neighbourhoods())
    for algorithm in ("layered", "louvain-r1.0"):
        parts = _partitions(document)[algorithm]
        a_side = next(part for part in parts if "table:a0" in part)
        assert {f"table:a{i}" for i in range(4)} <= a_side
        assert not any(member.startswith("table:b") for member in a_side)


def test_crossings_are_exactly_the_edges_between_clusters() -> None:
    topology = two_neighbourhoods()
    document = cluster_topology(topology)
    for run in document["algorithms"]:
        cluster_of = {m: c["id"] for c in run["clusters"] for m in c["members"]}
        expected = {
            e["id"] for e in topology["edges"] if cluster_of[e["from"]] != cluster_of[e["to"]]
        }
        assert {item["edge"] for item in run["crossings"]} == expected
        counts = run["crossing_counts"]
        assert counts["state-carrying"] + counts["reference-only"] == len(expected)


def test_clustering_is_reproducible_and_records_its_parameters() -> None:
    first = cluster_topology(two_neighbourhoods())
    second = cluster_topology(two_neighbourhoods())
    assert first == second
    assert first["clusterer"]["parameters"] == PARAMETERS


def test_disagreement_is_reported_for_every_pair() -> None:
    document = cluster_topology(two_neighbourhoods())
    n = len(document["algorithms"])
    assert len(document["disagreement"]["pairwise"]) == n * (n - 1) // 2
    clusters = sum(len(run["clusters"]) for run in document["algorithms"])
    assert len(document["disagreement"]["best_match"]) == clusters * (n - 1)


def test_the_example_clustering_is_what_the_clusterer_produces() -> None:
    topology = json.loads((EXAMPLE / "topology.json").read_text())
    committed = json.loads((EXAMPLE / "clustering.json").read_text())
    ref = committed["inputs"][0]["ref"]
    assert cluster_topology(topology, topology_ref=ref) == committed


def test_the_result_does_not_depend_on_the_process_hash_seed(tmp_path: Path) -> None:
    import subprocess
    import sys

    source = tmp_path / "topology.json"
    source.write_text(json.dumps(scattered()))
    outputs = []
    for seed in ("1", "2", "3"):
        out = tmp_path / f"clustering-{seed}.json"
        env = {"PYTHONHASHSEED": seed, "PYTHONPATH": str(ROOT / "src")}
        subprocess.run(
            [sys.executable, "-m", "topos.cluster", str(source), "--out", str(out)],
            check=True,
            env=env,
        )
        outputs.append(out.read_text())
    assert outputs[0] == outputs[1] == outputs[2]


def test_layered_records_every_touch_and_each_outside_touch_is_a_crossing() -> None:
    topology = two_neighbourhoods()
    layered = cluster_topology(topology)["algorithms"][0]
    attachments = {item["node"]: item for item in layered["attachments"]}
    tables = {node["id"] for node in topology["nodes"] if node["kind"] == "table"}
    assert set(attachments) == {node["id"] for node in topology["nodes"]} - tables
    peek = attachments["function:a_peek()"]
    assert len(peek["touches"]) == 2
    assert next(iter(peek["touches"])) == peek["cluster"]
    edges = {edge["id"]: edge for edge in topology["edges"]}
    for item in layered["attachments"]:
        outside = set(item["touches"]) - {item["cluster"]}
        crossed = set()
        for crossing in layered["crossings"]:
            edge = edges[crossing["edge"]]
            if item["node"] in (edge["from"], edge["to"]):
                crossed |= {crossing["from_cluster"], crossing["to_cluster"]}
        assert outside <= crossed, item["node"]
    assert "attachments" not in cluster_topology(topology)["algorithms"][1]
