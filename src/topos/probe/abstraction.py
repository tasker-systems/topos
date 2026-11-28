"""Compose a probe abstraction from a declaration, an area of concern and a database's schema.

    python3 -m topos.probe.abstraction --dsn postgresql://... --declaration declaration.json \
        --area area.json --area-ref 'artifact ...' --applied-by 'topos@SHA' --out abstraction.json

The declaration names the abstraction and its owner, the axes and where each takes its values, and
the fixture handles. An axis takes its values from the schema, or by hand with a stated reason:

- `{"enum": "schema.type"}`: the labels of an enum type, in declared order;
- `{"check": "schema.table.column"}`: the literals of that column's single-column check
  constraints, in written order;
- `{"type": "boolean"}`: `false` and `true`;
- `{"values": [...], "reason": "..."}`: values supplied by hand, recorded with the reason.

`"add": [...]` with `"add_reason"` appends values to a schema-derived axis, such as `null` for
"none". The predicates and exclusions are then derived by `topos.probe.select`, so nothing in the
result is chosen after the schema is read, and nothing is drawn from intent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from topos.canonical import sha256
from topos.probe.arguments import check_literals, read_signatures
from topos.probe.select import select

RULES = [
    "a predicate is an area member that is a function whose catalog return type is boolean",
    "a predicate whose body is not sql or plpgsql is excluded: it cannot be parsed",
    "every parameter is bound to the fixture handles supplying a table it identifies, or to the "
    "schema's literals for it, or left to its default; otherwise the predicate is excluded",
    "a predicate with no argument bound to a handle is excluded: its answer cannot vary with "
    "the axes",
    "volatility does not exclude; every call is rolled back, and a volatile call is checked "
    "for writes",
]


def axis_values(spec: dict[str, Any], snapshot: dict[str, Any]) -> tuple[list[Any], str]:
    """Resolve one axis's `from` into values and a statement of their source."""
    source = spec["from"]
    if "enum" in source:
        schema, name = source["enum"].split(".", 1)
        for enum in snapshot["enums"]:
            if enum["schema"] == schema and enum["name"] == name:
                values, text = list(enum["labels"]), f"labels of enum {source['enum']}"
                break
        else:
            raise SystemExit(f"no enum {source['enum']}")
    elif "check" in source:
        schema, table, column = source["check"].split(".", 2)
        definitions = [
            c["definition"]
            for c in snapshot["constraints"]
            if c["schema"] == schema
            and c["table"] == table
            and c["kind"] == "c"
            and c["columns"] == [column]
        ]
        if not definitions:
            raise SystemExit(f"no single-column check constraint on {source['check']}")
        values = []
        for definition in definitions:
            values.extend(v for v in check_literals(definition) if v not in values)
        text = f"check constraint literals on {source['check']}"
    elif source.get("type") == "boolean":
        values, text = [False, True], "type boolean"
    elif "values" in source:
        if not source.get("reason"):
            raise SystemExit(f"axis {spec['id']}: hand-supplied values need a reason")
        values, text = list(source["values"]), f"supplied by hand: {source['reason']}"
    else:
        raise SystemExit(f"axis {spec['id']}: unknown source {source}")
    if spec.get("add"):
        if not spec.get("add_reason"):
            raise SystemExit(f"axis {spec['id']}: added values need a reason")
        values = values + [v for v in spec["add"] if v not in values]
        text += f"; plus {json.dumps(spec['add'])} supplied by hand: {spec['add_reason']}"
    return values, text


def compose(
    declaration: dict[str, Any],
    area: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    area_ref: str,
    applied_by: str,
) -> dict[str, Any]:
    """The abstraction document for `declaration` over `area`, read against `snapshot`."""
    axes = []
    for spec in declaration["axes"]:
        values, source = axis_values(spec, snapshot)
        axis = {"id": spec["id"], "side": spec["side"], "values": values, "source": source}
        if spec.get("description"):
            axis["description"] = spec["description"]
        axes.append(axis)
    handles = {h["id"]: h["table"] for h in declaration["handles"]}
    predicates, excluded = select(area, snapshot, handles)
    area_sha = sha256(area)
    return {
        "schema_version": "0.2",
        "name": declaration["name"],
        "owner": declaration["owner"],
        "statement": declaration["statement"],
        "target": {"name": area["target"]["name"]},
        "area_of_concern": {"name": area["name"], "sha256": area_sha},
        "inputs": [
            {"role": "area-of-concern", "ref": area_ref, "sha256": area_sha},
            {"role": "declaration", "sha256": sha256(declaration)},
        ],
        "derivation": {"applied_by": applied_by, "rules": RULES},
        "axes": axes,
        "handles": declaration["handles"],
        "actor_handle": declaration["actor_handle"],
        "predicates": predicates,
        "excluded": [*declaration.get("excluded", []), *excluded],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--declaration", required=True, type=Path)
    parser.add_argument("--area", required=True, type=Path)
    parser.add_argument("--area-ref", required=True, help="how the area is cited, e.g. its id")
    parser.add_argument("--applied-by", required=True, help="the topos revision that ran this")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    document = compose(
        json.loads(args.declaration.read_text(encoding="utf-8")),
        json.loads(args.area.read_text(encoding="utf-8")),
        read_signatures(args.dsn),
        area_ref=args.area_ref,
        applied_by=args.applied_by,
    )
    text = json.dumps(document, indent=1, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
