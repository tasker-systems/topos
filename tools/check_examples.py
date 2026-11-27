"""Check every example against the draft schemas, and check that the examples are consistent.

Schema validation needs the `jsonschema` package. The consistency checks use only the standard
library: every id a model, intent or findings document names must resolve, and every witness path
must be a walk the model allows.

    python3 -I tools/check_examples.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = {
    "model.json": "system-model.schema.json",
    "intent.json": "intent.schema.json",
    "findings.json": "findings.schema.json",
}


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
        if finding["rule"] not in rules:
            errors.append(f"{label}: unknown rule {finding['rule']}")
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
    for error in errors:
        print(error, file=sys.stderr)
    print(f"checked {len(examples)} example(s): {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
