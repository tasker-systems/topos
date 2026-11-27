"""Check every example against the schemas, and check that the examples are consistent.

Schema validation needs the `jsonschema` package. The consistency checks use only the standard
library: every id a model, intent, findings, topology or area document names must resolve, every
witness path must be a walk the model allows, every content hash must match the document it names,
and an area's boundary must list exactly the topology edges that cross it.

    python3 -I tools/check_examples.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = {
    "model.json": "system-model.schema.json",
    "intent.json": "intent.schema.json",
    "findings.json": "findings.schema.json",
    "topology.json": "structural-topology.schema.json",
    "area.json": "area-of-concern.schema.json",
    "clustering.json": "clustering.schema.json",
}


def canonical_sha256(document: object) -> str:
    """The hash every topos `sha256` field records: sorted keys, `,` and `:`, UTF-8."""
    text = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def validate_schemas(example: Path, errors: list[str]) -> None:
    try:
        import jsonschema
    except ImportError:
        errors.append("jsonschema is not installed; schema validation was not run")
        return
    for name, schema_name in SCHEMAS.items():
        document = example / name
        if not document.exists():
            continue
        schema = load(ROOT / "schema" / schema_name)
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(schema)
        for error in validator.iter_errors(load(document)):
            location = "/".join(str(part) for part in error.absolute_path) or "(root)"
            errors.append(f"{document.relative_to(ROOT)}: {location}: {error.message}")


def check_model(model: dict, where: str, errors: list[str]) -> None:
    roles = {role["id"] for role in model["roles"]}
    attributes = {attribute["id"] for attribute in model["attributes"]}
    capabilities = {capability["id"] for capability in model.get("capabilities", [])}
    states = {
        entity["id"]: {state["id"] for state in entity["states"]} for entity in model["entities"]
    }
    for entity in model["entities"]:
        for initial in entity["initial"]:
            if initial not in states[entity["id"]]:
                errors.append(f"{where}: entity {entity['id']}: unknown initial state {initial}")
    for transition in model["transitions"]:
        label = f"{where}: transition {transition['id']}"
        entity_states = states.get(transition["entity"])
        if entity_states is None:
            errors.append(f"{label}: unknown entity {transition['entity']}")
            continue
        for end in ("from", "to"):
            if transition[end] not in entity_states:
                errors.append(f"{label}: unknown {end} state {transition[end]}")
        errors.extend(
            f"{label}: unknown role {role}"
            for role in transition["actor_roles"]
            if role not in roles
        )
        named = transition.get("writes", []) + transition.get("guard", {}).get("reads", [])
        errors.extend(
            f"{label}: unknown attribute {attribute}"
            for attribute in named
            if attribute not in attributes
        )
    actors = {actor["id"] for actor in model.get("actor_classes", [])}
    objects = {obj["id"] for obj in model.get("object_classes", [])}
    for fact in model.get("access", []):
        label = f"{where}: access fact {fact['id']}"
        if fact["actor_class"] not in actors:
            errors.append(f"{label}: unknown actor class {fact['actor_class']}")
        if fact["object_class"] not in objects:
            errors.append(f"{label}: unknown object class {fact['object_class']}")
    for obj in model.get("object_classes", []):
        if "entity" in obj and obj["entity"] not in states:
            errors.append(f"{where}: object class {obj['id']}: unknown entity {obj['entity']}")
    for grant in model.get("grants", []):
        if grant["role"] not in roles:
            errors.append(f"{where}: grant {grant['id']}: unknown role {grant['role']}")
        if grant["capability"] not in capabilities:
            errors.append(f"{where}: grant {grant['id']}: unknown capability {grant['capability']}")


def check_intent(intent: dict, model: dict, where: str, errors: list[str]) -> None:
    roles = {role["id"] for role in model["roles"]}
    transitions = {transition["id"] for transition in model["transitions"]}
    personas = {persona["id"] for persona in intent["personas"]}
    for persona in intent["personas"]:
        errors.extend(
            f"{where}: persona {persona['id']}: unknown role {role}"
            for role in persona["roles"]
            if role not in roles
        )
    for rule in intent["rules"]:
        label = f"{where}: rule {rule['id']}"
        applies = rule["applies_to"]
        errors.extend(
            f"{label}: unknown persona {persona}"
            for persona in applies.get("personas", [])
            if persona not in personas
        )
        errors.extend(
            f"{label}: unknown role {role}"
            for role in applies.get("roles", [])
            if role not in roles
        )
        errors.extend(
            f"{label}: unknown transition {transition}"
            for transition in rule.get("concerns", {}).get("transitions", [])
            if transition not in transitions
        )
        objects = {obj["id"] for obj in model.get("object_classes", [])}
        errors.extend(
            f"{label}: unknown object class {obj}"
            for obj in rule.get("concerns", {}).get("access", {}).get("object_classes", [])
            if obj not in objects
        )


def check_findings(
    findings: dict, intent: dict, model: dict, where: str, errors: list[str]
) -> None:
    by_id = {transition["id"]: transition for transition in model["transitions"]}
    initial = {entity["id"]: set(entity["initial"]) for entity in model["entities"]}
    personas = {persona["id"] for persona in intent["personas"]}
    rules = {rule["id"] for rule in intent["rules"]} | {job["id"] for job in intent.get("jobs", [])}
    for finding in findings["findings"]:
        label = f"{where}: finding {finding['id']}"
        if finding["persona"] not in personas:
            errors.append(f"{label}: unknown persona {finding['persona']}")
        if finding["rule"] is not None and finding["rule"] not in rules:
            errors.append(f"{label}: unknown rule {finding['rule']}")
        if "access_fact" in finding["witness"]:
            facts = {fact["id"]: fact for fact in model.get("access", [])}
            fact = facts.get(finding["witness"]["access_fact"])
            if fact is None:
                errors.append(f"{label}: unknown access fact {finding['witness']['access_fact']}")
            elif "actor_class" in finding and finding["actor_class"] != fact["actor_class"]:
                errors.append(f"{label}: actor class differs from its witness fact's")
            continue
        current: dict[str, str] = {}
        for step in finding["witness"]["path"]:
            transition = by_id.get(step)
            if transition is None:
                errors.append(f"{label}: unknown transition {step} on the witness path")
                break
            entity = transition["entity"]
            here = current.get(entity)
            allowed = {here} if here is not None else initial[entity]
            if transition["from"] not in allowed:
                errors.append(f"{label}: {step} cannot fire from {sorted(allowed)}")
                break
            current[entity] = transition["to"]


def check_topology(topology: dict, where: str, errors: list[str]) -> None:
    nodes = {node["id"] for node in topology["nodes"]}
    if len(nodes) != len(topology["nodes"]):
        errors.append(f"{where}: topology: duplicate node ids")
    edge_ids = [edge["id"] for edge in topology["edges"]]
    if len(set(edge_ids)) != len(edge_ids):
        errors.append(f"{where}: topology: duplicate edge ids")
    for edge in topology["edges"]:
        for end in ("from", "to"):
            if edge[end] not in nodes:
                errors.append(f"{where}: topology edge {edge['id']}: unknown {end} {edge[end]}")
    for hole in topology["holes"]:
        if hole["element"] not in nodes:
            errors.append(f"{where}: topology hole: unknown element {hole['element']}")


def check_clustering(clustering: dict, topology: dict, where: str, errors: list[str]) -> None:
    nodes = sorted(node["id"] for node in topology["nodes"])
    for run in clustering["algorithms"]:
        label = f"{where}: clustering {run['id']}"
        cluster_of = {}
        for cluster in run["clusters"]:
            for member in cluster["members"]:
                if member in cluster_of:
                    errors.append(f"{label}: {member} is in two clusters")
                cluster_of[member] = cluster["id"]
        if sorted(cluster_of) != nodes:
            errors.append(f"{label}: the clusters are not a partition of the topology's nodes")
            continue
        crossing = {
            edge["id"]
            for edge in topology["edges"]
            if cluster_of[edge["from"]] != cluster_of[edge["to"]]
        }
        if {item["edge"] for item in run["crossings"]} != crossing:
            errors.append(f"{label}: crossings are not exactly the edges between clusters")


def check_area(area: dict, documents: dict[str, dict], where: str, errors: list[str]) -> None:
    by_hash = {canonical_sha256(document): role for role, document in documents.items()}
    for source in area["inputs"]:
        if by_hash.get(source["sha256"]) != source["role"]:
            errors.append(f"{where}: area input {source['role']}: no such document has that hash")
    topology = documents["structural-topology"]
    nodes = {node["id"] for node in topology["nodes"]}
    members = set(area["members"])
    errors.extend(
        f"{where}: area member {member} is not a topology node"
        for member in sorted(members - nodes)
    )
    derivation = area["derivation"]
    if derivation["method"] == "clustering":
        runs = {run["id"]: run for run in documents.get("clustering", {}).get("algorithms", [])}
        run = runs.get(derivation["algorithm"], {"clusters": []})
        by_id = {cluster["id"]: set(cluster["members"]) for cluster in run["clusters"]}
        expected = set().union(*(by_id.get(cluster, set()) for cluster in derivation["clusters"]))
        for edit in area["hand_edits"]:
            (expected.add if edit["op"] == "add" else expected.discard)(edit["element"])
        if expected != members:
            errors.append(f"{where}: area members are not its clusters with its hand edits")
    crossing = {
        edge["id"]: edge
        for edge in topology["edges"]
        if (edge["from"] in members) != (edge["to"] in members)
    }
    listed = {entry["edge"]: entry for entry in area["boundary"]}
    errors.extend(
        f"{where}: area boundary omits crossing edge {edge}"
        for edge in sorted(set(crossing) - set(listed))
    )
    errors.extend(
        f"{where}: area boundary lists {edge}, which does not cross it"
        for edge in sorted(set(listed) - set(crossing))
    )
    for edge_id, entry in listed.items():
        edge = crossing.get(edge_id)
        if edge is None:
            continue
        inside = edge["from"] if edge["from"] in members else edge["to"]
        outside = edge["to"] if inside == edge["from"] else edge["from"]
        if (entry["inside"], entry["outside"]) != (inside, outside):
            errors.append(f"{where}: area boundary {edge_id}: inside and outside are wrong")


def check_crossings(findings: dict, area: dict, where: str, errors: list[str]) -> None:
    examined = findings["examined"]
    if examined.get("area_of_concern") != area["name"]:
        return
    boundary = {entry["edge"] for entry in area["boundary"]}
    errors.extend(
        f"{where}: findings name an unexamined crossing {edge} that is not on the boundary"
        for edge in examined["unexamined_crossings"]
        if edge not in boundary
    )


def main() -> int:
    errors: list[str] = []
    examples = sorted(path for path in (ROOT / "examples").iterdir() if path.is_dir())
    for example in examples:
        validate_schemas(example, errors)
        where = str(example.relative_to(ROOT))
        model = load(example / "model.json")
        check_model(model, where, errors)
        if (example / "intent.json").exists():
            intent = load(example / "intent.json")
            check_intent(intent, model, where, errors)
            if (example / "findings.json").exists():
                check_findings(load(example / "findings.json"), intent, model, where, errors)
        documents: dict[str, dict] = {}
        if (example / "topology.json").exists():
            documents["structural-topology"] = load(example / "topology.json")
            check_topology(documents["structural-topology"], where, errors)
        if (example / "clustering.json").exists():
            documents["clustering"] = load(example / "clustering.json")
            check_clustering(
                documents["clustering"], documents["structural-topology"], where, errors
            )
        if (example / "area.json").exists():
            area = load(example / "area.json")
            if "structural-topology" not in documents:
                errors.append(f"{where}: an area needs the topology it was derived from")
            else:
                check_area(area, documents, where, errors)
            named = model.get("area_of_concern")
            if named and named["sha256"] != canonical_sha256(area):
                errors.append(f"{where}: the model's area-of-concern hash does not match area.json")
            if (example / "findings.json").exists():
                findings = load(example / "findings.json")
                check_crossings(findings, area, where, errors)
                if findings.get("model", {}).get("sha256") not in (None, canonical_sha256(model)):
                    errors.append(f"{where}: the findings' model hash does not match model.json")
    for error in errors:
        print(error, file=sys.stderr)
    print(f"checked {len(examples)} example(s): {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
