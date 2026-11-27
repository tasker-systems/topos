"""Deriving an area of concern from a clustering, and verifying one."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from synthetic import two_neighbourhoods

from topos.area import clusters_containing, derive, verify
from topos.cluster import cluster_topology

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schema" / "area-of-concern.schema.json").read_text())
EXAMPLE = ROOT / "examples" / "lending-library"


def _derived(**kwargs: object) -> tuple[dict, dict, dict]:
    topology = two_neighbourhoods()
    clustering = cluster_topology(topology)
    clusters = clusters_containing(clustering, "layered", ["table:a0"])
    area = derive(
        topology,
        clustering,
        algorithm="layered",
        clusters=clusters,
        rule="the cluster holding table a0",
        name="a",
        owner="tester",
        **kwargs,
    )
    return area, topology, clustering


def test_a_derived_area_verifies() -> None:
    area, topology, clustering = _derived()
    jsonschema.validate(area, SCHEMA)
    assert verify(area, topology, clustering) == []
    assert any(item["edge"].endswith("->table:b0") for item in area["boundary"])


def test_members_that_are_not_the_clusters_are_refused() -> None:
    area, topology, clustering = _derived()
    area["members"].append("table:b1")
    assert any("members are not" in problem for problem in verify(area, topology, clustering))


def test_a_hand_edit_is_recorded_and_needs_a_reason() -> None:
    edit = {"op": "add", "element": "table:b0", "by": "tester", "reason": "read across"}
    area, topology, clustering = _derived(hand_edits=[edit])
    assert "table:b0" in area["members"]
    assert verify(area, topology, clustering) == []
    with pytest.raises(ValueError, match="needs who made it and why"):
        _derived(hand_edits=[{"op": "add", "element": "table:b0", "by": "tester", "reason": ""}])


def test_a_changed_input_is_detected() -> None:
    area, topology, clustering = _derived()
    topology["nodes"][0]["name"] = "renamed"
    assert any("topology input hash" in problem for problem in verify(area, topology, clustering))


def test_the_example_area_verifies_against_its_inputs() -> None:
    area = json.loads((EXAMPLE / "area.json").read_text())
    topology = json.loads((EXAMPLE / "topology.json").read_text())
    clustering = json.loads((EXAMPLE / "clustering.json").read_text())
    assert verify(area, topology, clustering) == []


def test_the_guard_rule_selects_by_signature_not_name() -> None:
    from synthetic import edge, node, topology

    from topos.area import GUARD_RULE, clusters_with_guards

    nodes = [
        node("table:x0"),
        node("table:x1"),
        node("table:y0"),
        node("table:y1"),
        node("function:check_it(uuid)", returns="text", volatility="stable"),
        node("function:p(uuid)", returns="boolean", volatility="stable"),
    ]
    edges = [
        edge("foreign_key", "table:x0", "table:x1"),
        edge("foreign_key", "table:y0", "table:y1"),
        edge("reads", "function:check_it(uuid)", "table:x0"),
        edge("reads", "function:p(uuid)", "table:y0"),
    ]
    synthetic = topology(nodes, edges)
    clustering = cluster_topology(synthetic)
    chosen = clusters_with_guards(synthetic, clustering, "layered")
    run = clustering["algorithms"][0]
    members = set().union(*(set(c["members"]) for c in run["clusters"] if c["id"] in chosen))
    assert "function:p(uuid)" in members
    assert "function:check_it(uuid)" not in members
    area = derive(
        synthetic,
        clustering,
        algorithm="layered",
        clusters=chosen,
        rule=GUARD_RULE,
        name="guards",
        owner="tester",
        applied_by="topos test",
    )
    jsonschema.validate(area, SCHEMA)
    assert verify(area, synthetic, clustering) == []
