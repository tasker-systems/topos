"""Derive an area of concern from a clustering, and verify one against what it was derived from.

An area is a union of clusters from one algorithm's result, plus any hand edits, each with its
reason. Its boundary is every topology edge with exactly one end inside, classed by
`topos.boundary`. `verify` recomputes all of this from the frozen inputs, so a reader can check an
area without trusting the run that produced it.

    python3 -m topos.area --topology T --clustering C --algorithm layered \
        --containing 'function:*.f(*' --rule 'the clusters that hold f' --name N --owner O
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path
from typing import Any

from topos.boundary import Classifier
from topos.canonical import sha256


def clusters_containing(
    clustering: dict[str, Any], algorithm: str, patterns: list[str]
) -> list[str]:
    """The ids of the clusters in one algorithm's result holding a node matching any pattern."""
    run = _algorithm(clustering, algorithm)
    return [
        cluster["id"]
        for cluster in run["clusters"]
        if any(
            fnmatch.fnmatchcase(member, pattern)
            for member in cluster["members"]
            for pattern in patterns
        )
    ]


def _algorithm(clustering: dict[str, Any], algorithm: str) -> dict[str, Any]:
    for run in clustering["algorithms"]:
        if run["id"] == algorithm:
            return run
    message = f"no algorithm {algorithm!r} in the clustering"
    raise ValueError(message)


def _members(
    clustering: dict[str, Any],
    algorithm: str,
    clusters: list[str],
    hand_edits: list[dict[str, str]],
) -> set[str]:
    run = _algorithm(clustering, algorithm)
    by_id = {cluster["id"]: cluster for cluster in run["clusters"]}
    members: set[str] = set()
    for cluster in clusters:
        if cluster not in by_id:
            message = f"no cluster {cluster!r} in {algorithm}"
            raise ValueError(message)
        members |= set(by_id[cluster]["members"])
    for edit in hand_edits:
        if edit["op"] == "add":
            members.add(edit["element"])
        else:
            members.discard(edit["element"])
    return members


def boundary(topology: dict[str, Any], members: set[str]) -> list[dict[str, str]]:
    """Every edge with exactly one end in `members`, classed."""
    classifier = Classifier(topology)
    result = []
    for edge in topology["edges"]:
        inside_from, inside_to = edge["from"] in members, edge["to"] in members
        if inside_from == inside_to:
            continue
        kind, basis = classifier.classify(edge, lambda node: node in members)
        inside, outside = (edge["from"], edge["to"]) if inside_from else (edge["to"], edge["from"])
        result.append(
            {
                "edge": edge["id"],
                "inside": inside,
                "outside": outside,
                "class": kind,
                "basis": basis,
            }
        )
    return sorted(result, key=lambda item: item["edge"])


def derive(
    topology: dict[str, Any],
    clustering: dict[str, Any],
    *,
    algorithm: str,
    clusters: list[str],
    rule: str,
    name: str,
    owner: str,
    hand_edits: list[dict[str, str]] | None = None,
    exclusions: list[dict[str, str]] | None = None,
    refs: dict[str, str] | None = None,
) -> dict[str, Any]:
    """An area-of-concern document (schema 0.2) drawn from `clustering`."""
    edits = list(hand_edits or [])
    for edit in edits:
        if not edit.get("reason") or not edit.get("by"):
            message = f"hand edit of {edit.get('element')!r} needs who made it and why"
            raise ValueError(message)
    members = _members(clustering, algorithm, clusters, edits)
    refs = refs or {}
    return {
        "schema_version": "0.2",
        "name": name,
        "target": {"name": topology["target"]["name"], "revision": topology["target"]["revision"]},
        "owner": owner,
        "inputs": [
            {
                "role": "structural-topology",
                "ref": refs.get("structural-topology", ""),
                "sha256": sha256(topology),
            },
            {"role": "clustering", "ref": refs.get("clustering", ""), "sha256": sha256(clustering)},
        ],
        "derivation": {
            "method": "clustering",
            "algorithm": algorithm,
            "clusters": list(clusters),
            "rule": rule,
        },
        "members": sorted(members),
        "hand_edits": edits,
        "exclusions": list(exclusions or []),
        "boundary": boundary(topology, members),
    }


def verify(
    area: dict[str, Any], topology: dict[str, Any], clustering: dict[str, Any] | None = None
) -> list[str]:
    """Every way `area` departs from what its inputs give. Empty when it is exactly derived."""
    problems = []
    hashes = {item["role"]: item["sha256"] for item in area["inputs"]}
    if hashes.get("structural-topology") != sha256(topology):
        problems.append("the structural-topology input hash does not match")
    nodes = {node["id"] for node in topology["nodes"]}
    members = set(area["members"])
    problems.extend(f"member {member} is not a topology node" for member in sorted(members - nodes))
    derivation = area["derivation"]
    if derivation["method"] == "clustering":
        if clustering is None:
            problems.append("the area was derived from a clustering that was not supplied")
        else:
            if hashes.get("clustering") != sha256(clustering):
                problems.append("the clustering input hash does not match")
            try:
                expected = _members(
                    clustering, derivation["algorithm"], derivation["clusters"], area["hand_edits"]
                )
            except ValueError as error:
                problems.append(str(error))
            else:
                if expected != members:
                    extra = sorted(members - expected)
                    missing = sorted(expected - members)
                    problems.append(
                        f"members are not the named clusters with their hand edits: "
                        f"{len(extra)} unexplained, {len(missing)} missing"
                    )
    problems.extend(
        f"hand edit of {edit['element']} carries no reason"
        for edit in area["hand_edits"]
        if not edit.get("reason", "").strip()
    )
    if sorted(area["boundary"], key=lambda item: item["edge"]) != boundary(topology, members):
        problems.append("the boundary is not exactly the classed edges that cross it")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--clustering", type=Path, required=True)
    parser.add_argument("--algorithm", required=True)
    parser.add_argument("--cluster", action="append", default=[], help="a cluster id to include")
    parser.add_argument(
        "--containing",
        action="append",
        default=[],
        help="include every cluster holding a node matching this pattern",
    )
    parser.add_argument("--rule", required=True, help="the selection rule, in words")
    parser.add_argument("--name", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--hand-edits", type=Path, help="a JSON list of hand edits")
    parser.add_argument("--topology-ref", default="")
    parser.add_argument("--clustering-ref", default="")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    topology = json.loads(args.topology.read_text(encoding="utf-8"))
    clustering = json.loads(args.clustering.read_text(encoding="utf-8"))
    clusters = list(args.cluster)
    for cluster in clusters_containing(clustering, args.algorithm, args.containing):
        if cluster not in clusters:
            clusters.append(cluster)
    edits = json.loads(args.hand_edits.read_text(encoding="utf-8")) if args.hand_edits else []
    document = derive(
        topology,
        clustering,
        algorithm=args.algorithm,
        clusters=clusters,
        rule=args.rule,
        name=args.name,
        owner=args.owner,
        hand_edits=edits,
        refs={"structural-topology": args.topology_ref, "clustering": args.clustering_ref},
    )
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
