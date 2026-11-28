"""Select the predicates a probe calls, and bind each argument, from an area and the schema.

A predicate is a member of the area of concern that is a function whose catalog return type is
boolean. Each is kept or excluded by these rules, applied in order; an excluded function records
the first rule it fails, and every reason is one of structure or tractability, never intent:

1. **Parseable language.** Only `sql` and `plpgsql` bodies can be read for argument derivation.
2. **Every argument can be supplied.** A parameter that identifies a table is bound to each fixture
   handle that supplies one of its tables. A text, enum or boolean parameter is bound to the
   literals the schema yields for it (`topos.probe.arguments`). A parameter with a default that
   can be supplied by neither is left to its default. Any other parameter excludes the function.
3. **Varies with the fixture.** At least one argument is bound to a handle. A function no handle
   reaches answers the same for every combination, so it relates no actor to any object.

A polymorphic id parameter is bound together with its partner: when the id takes a handle, the
partner takes the literal that names the handle's table. Volatility does not exclude: it is a
planner hint, not a guarantee about writes. Each predicate records it, and the probe rolls back
every call and checks a volatile one for writes.
"""

from __future__ import annotations

from typing import Any

from topos.probe.arguments import Function, Parameter, Schema, derive

VOLATILITY = {"i": "immutable", "s": "stable", "v": "volatile"}
TEXT_TYPES = ("text", "character varying", "character", "name")


def _written_table(schema: Schema, table: str, literals: dict[Any, set[str]]) -> str | None:
    for value in literals:
        if isinstance(value, str) and schema.table((value,)) == table:
            return value
    return None


def _bind(
    schema: Schema,
    function: Function,
    parameter: Parameter,
    handles: dict[str, str],
) -> tuple[dict[str, Any] | None, list[str], str | None]:
    """A parameter's binding, its bases, and a reason when it cannot be supplied."""
    if parameter.tables:
        supplied = [h for h, table in handles.items() if table in parameter.tables]
        bases = [
            f"{table}: {basis}"
            for table in sorted(parameter.tables)
            for basis in sorted(parameter.tables[table])
        ]
        unexercised = sorted(t for t in parameter.tables if t not in handles.values())
        if unexercised:
            bases.append(f"identifies tables no handle supplies: {', '.join(unexercised)}")
        if supplied:
            bind: dict[str, Any] = {"handles": supplied}
            partners = sorted(parameter.partners)
            if partners:
                partner = function.parameters[partners[0]]
                values = {h: _written_table(schema, handles[h], partner.literals) for h in supplied}
                if all(values.values()):
                    bind["partner"] = partner.name
                    bind["partner_values"] = values
            return bind, bases, None
    if parameter.literals and (parameter.kind == "e" or parameter.type in (*TEXT_TYPES, "boolean")):
        values = sorted(parameter.literals, key=lambda v: (str(type(v)), str(v)))
        bases = [
            f"{value!r}: {basis}" for value in values for basis in sorted(parameter.literals[value])
        ]
        return {"values": values}, bases, None
    if parameter.has_default:
        return {"default": True}, ["no supplied value; the parameter's default is used"], None
    if parameter.tables:
        tables = ", ".join(sorted(parameter.tables))
        return None, [], f"parameter {parameter.name} identifies {tables}, which no handle supplies"
    if parameter.type in (*TEXT_TYPES, "boolean") or parameter.kind == "e":
        return (
            None,
            [],
            f"parameter {parameter.name} ({parameter.type}): the schema yields no values",
        )
    return (
        None,
        [],
        f"parameter {parameter.name} ({parameter.type}): no table or literal is derivable for it",
    )


def select(
    area: dict[str, Any], snapshot: dict[str, Any], handles: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the selected predicates and the exclusions, for `area` over `snapshot`.

    `handles` maps each fixture handle to the table whose row it supplies, such as
    `{"actor": "public.profiles"}`.
    """
    schema = Schema(snapshot)
    run = derive(schema)
    members = set(area["members"])
    predicates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for function_id in sorted(members):
        function = schema.functions.get(function_id)
        if function is None or function.returns != "boolean":
            continue

        def exclude(basis: str, reason: str, element: str = function_id) -> None:
            excluded.append({"element": element, "basis": basis, "reason": reason})

        if function.language not in ("sql", "plpgsql"):
            exclude("structure", f"its body is {function.language}, which cannot be parsed")
            continue
        parameters: list[dict[str, Any]] = []
        refusal = None
        for parameter in run(function):
            bind, bases, refusal = _bind(schema, function, parameter, handles)
            if bind is None:
                break
            parameters.append(
                {"name": parameter.name, "type": parameter.type, "bind": bind, "basis": bases}
            )
        if refusal is not None:
            exclude("structure", f"an argument cannot be supplied: {refusal}")
            continue
        if not any("handles" in p["bind"] for p in parameters):
            exclude(
                "structure",
                "no argument is bound to a fixture handle, so its answer cannot vary with the axes",
            )
            continue
        partners = {p["bind"]["partner"] for p in parameters if "partner" in p["bind"]}
        for parameter in parameters:
            if parameter["name"] in partners:
                parameter["bind"] = {"partner_of": True}
                parameter["basis"].append("bound with its polymorphic id parameter")
        predicate: dict[str, Any] = {
            "id": function_id,
            "function": {"schema": function.schema, "name": function.name},
            "volatility": VOLATILITY[function.volatility],
            "parameters": parameters,
        }
        if function.parse_errors:
            predicate["parse_errors"] = function.parse_errors
        predicates.append(predicate)
    return predicates, excluded
