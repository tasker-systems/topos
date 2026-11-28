"""Probe a target over the full cartesian product of declared axes, and emit what it allows.

The engine reads an abstraction document (`schema/abstraction.schema.json`). For every combination
of the axes' values, it asks the target to build a fixture, then calls every selected predicate
with every argument binding the abstraction declares, and records each answer:

- `true` or `false` becomes an access fact with `probed` provenance;
- an error or a NULL is behaviour too, but not an answer: it becomes an `indeterminate` entry with
  its outcome and the database's own detail, and is never read as allowed or denied;
- a combination the builder cannot construct, or a call whose argument the fixture does not
  supply, is a gap in coverage: it becomes a `not_constructed` entry with the reason.

Nothing is dropped. A call to a function that may write is checked for writes, and any it made is
recorded (`wrote`); every call is rolled back, so no call sees another's effects. The engine
knows nothing about the target: building a fixture and making a call are the target's job, behind
the `Target` protocol. It never reads an intent.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from topos import __version__
from topos.canonical import sha256

PROBE = f"topos.probe {__version__}"


@dataclass(frozen=True)
class Unconstructible:
    """A combination the builder cannot construct, and why."""

    reason: str


@dataclass(frozen=True)
class Fixture:
    """A constructed combination: each handle's value, such as a row id."""

    handles: dict[str, Any]


@dataclass(frozen=True)
class Outcome:
    """A call's answer: `true`, `false`, `null` or `error`, with detail for the last two.

    `wrote` is whether the call changed any row, when that was checked, and None otherwise.
    """

    kind: str
    detail: str = ""
    wrote: bool | None = None


@dataclass(frozen=True)
class Argument:
    """One argument of a call: a handle's value, a literal, or the parameter's default."""

    parameter: str
    type: str
    handle: str | None = None
    value: Any = None
    default: bool = False

    def written(self) -> str:
        if self.default:
            return f"{self.parameter} => DEFAULT"
        if self.handle is not None:
            return f"<{self.handle}>"
        return _literal(self.value)


@dataclass(frozen=True)
class Call:
    """One call of a predicate, with its arguments bound."""

    predicate: dict[str, Any]
    arguments: tuple[Argument, ...]

    @property
    def action(self) -> str:
        literals = [a.value for a in self.arguments if a.handle is None and not a.default]
        return _id(".".join([self.predicate["function"]["name"], *map(str, literals)]))

    @property
    def handles(self) -> tuple[str, ...]:
        return tuple(a.handle for a in self.arguments if a.handle is not None)

    def written(self) -> str:
        function = self.predicate["function"]
        inner = ", ".join(a.written() for a in self.arguments if not a.default)
        return f"{function['schema']}.{function['name']}({inner})"


class Target(Protocol):
    """What the engine needs from a target. Every combination is built and torn down in turn."""

    def combination(
        self, combination: dict[str, Any]
    ) -> AbstractContextManager[Fixture | Unconstructible]:
        """Build a fixture for `combination`, and undo it on exit."""

    def call(self, call: Call, fixture: Fixture) -> Outcome:
        """Make one call against a built fixture, and undo whatever it wrote."""


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "NULL"
    if isinstance(value, int | float):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _id(text: str) -> str:
    """Make an id that matches the schemas' id pattern, `^[a-z][a-z0-9_.-]*$`."""
    out = "".join(c if c.isalnum() or c in "_.-" else "-" for c in text.lower())
    out = out.lstrip("_.-") or "x"
    return out if out[0].isalpha() else f"x{out}"


def combinations(axes: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Every combination of the axes' values, in declared order. No case is hand-picked."""
    ids = [axis["id"] for axis in axes]
    for values in itertools.product(*(axis["values"] for axis in axes)):
        yield dict(zip(ids, values, strict=True))


def calls(predicate: dict[str, Any]) -> list[Call]:
    """Every call the abstraction declares for one predicate, in a stable order."""
    by_name = {p["name"]: p for p in predicate["parameters"]}
    partner_of = {
        p["bind"]["partner"]: p["name"] for p in predicate["parameters"] if "partner" in p["bind"]
    }
    choices: list[list[tuple[str, Any]]] = []
    free: list[str] = []
    for parameter in predicate["parameters"]:
        bind = parameter["bind"]
        if parameter["name"] in partner_of or bind.get("default"):
            continue
        free.append(parameter["name"])
        if "handles" in bind:
            choices.append([("handle", h) for h in bind["handles"]])
        else:
            choices.append([("value", v) for v in bind["values"]])
    out: list[Call] = []
    for picked in itertools.product(*choices):
        chosen = dict(zip(free, picked, strict=True))
        arguments: list[Argument] = []
        for parameter in predicate["parameters"]:
            name, type_, bind = parameter["name"], parameter["type"], parameter["bind"]
            if bind.get("default"):
                arguments.append(Argument(name, type_, default=True))
            elif name in partner_of:
                handle = chosen[partner_of[name]][1]
                value = by_name[partner_of[name]]["bind"]["partner_values"][handle]
                arguments.append(Argument(name, type_, value=value))
            elif chosen[name][0] == "handle":
                arguments.append(Argument(name, type_, handle=chosen[name][1]))
            else:
                arguments.append(Argument(name, type_, value=chosen[name][1]))
        out.append(Call(predicate, tuple(arguments)))
    return out


def _class_id(prefix: str, values: dict[str, Any]) -> str:
    parts = [f"{k}-{'none' if v is None else v}" for k, v in values.items()]
    return _id(".".join([prefix, *parts]))


def observe(
    abstraction: dict[str, Any], target: Target, *, revision: str, builder: str
) -> dict[str, Any]:
    """Probe `target` at `revision` under `abstraction`, and return a schema-0.2 system model.

    `builder` names the fixture builder and its content hash, so the model records what built
    every fixture.
    """
    axes = abstraction["axes"]
    actor_axes = [a["id"] for a in axes if a["side"] == "actor"]
    object_axes = [a["id"] for a in axes if a["side"] == "object"]
    actor_handle = abstraction["actor_handle"]
    planned = [(p, calls(p)) for p in abstraction["predicates"]]
    abstraction_sha = sha256(abstraction)

    actor_classes: dict[str, dict[str, Any]] = {}
    object_classes: dict[str, dict[str, Any]] = {}
    access: list[dict[str, Any]] = []
    indeterminate: list[dict[str, Any]] = []
    not_constructed: list[dict[str, Any]] = []
    declared = {"kind": "extracted", "source": f"abstraction {abstraction['name']}"}

    def actor_class(combination: dict[str, Any]) -> str:
        values = {k: combination[k] for k in actor_axes}
        key = _class_id("actor", values)
        actor_classes.setdefault(key, {"id": key, "axes": values, "provenance": declared})
        return key

    def object_class(combination: dict[str, Any], handles: tuple[str, ...]) -> str:
        values: dict[str, Any] = {k: combination[k] for k in object_axes}
        bound = [h for h in handles if h != actor_handle]
        values["bound"] = "+".join(bound) if bound else None
        key = _class_id("object", values)
        object_classes.setdefault(key, {"id": key, "axes": values, "provenance": declared})
        return key

    for combination in combinations(axes):
        actor = actor_class(combination)
        with target.combination(combination) as built:
            if isinstance(built, Unconstructible):
                not_constructed.append(
                    {
                        "id": f"n{len(not_constructed) + 1:07d}",
                        "actor_class": actor,
                        "object_class": object_class(combination, ()),
                        "reason": built.reason,
                        "provenance": {
                            "kind": "probed",
                            "probe": PROBE,
                            "call": f"build combination {sha256(combination)}",
                            "fixture": builder,
                        },
                    }
                )
                continue
            fixture_ref = sha256({"combination": combination, "handles": built.handles})
            for _, planned_calls in planned:
                for call in planned_calls:
                    provenance = {
                        "kind": "probed",
                        "probe": PROBE,
                        "call": call.written(),
                        "fixture": fixture_ref,
                    }
                    obj = object_class(combination, call.handles)
                    missing = [h for h in call.handles if h not in built.handles]
                    if missing:
                        not_constructed.append(
                            {
                                "id": f"n{len(not_constructed) + 1:07d}",
                                "actor_class": actor,
                                "action": call.action,
                                "object_class": obj,
                                "reason": f"the fixture supplies no {', '.join(missing)}",
                                "provenance": provenance,
                            }
                        )
                        continue
                    outcome = target.call(call, built)
                    entry: dict[str, Any] = {
                        "actor_class": actor,
                        "action": call.action,
                        "object_class": obj,
                    }
                    if outcome.kind in ("true", "false"):
                        entry = {"id": f"a{len(access) + 1:07d}", **entry}
                        entry["allowed"] = outcome.kind == "true"
                        collection = access
                    else:
                        entry = {"id": f"i{len(indeterminate) + 1:07d}", **entry}
                        entry["outcome"] = outcome.kind
                        entry["detail"] = outcome.detail
                        collection = indeterminate
                    if outcome.wrote is not None:
                        entry["wrote"] = outcome.wrote
                    entry["provenance"] = provenance
                    collection.append(entry)

    return {
        "schema_version": "0.2",
        "target": {**abstraction["target"], "revision": revision},
        "abstraction": {
            "owner": abstraction["owner"],
            "statement": abstraction["statement"],
            "excluded": [
                f"{e['element']}: {e['basis']}: {e['reason']}" for e in abstraction["excluded"]
            ],
            "document": {"name": abstraction["name"], "sha256": abstraction_sha},
        },
        "area_of_concern": abstraction["area_of_concern"],
        "probe": {"probe": PROBE, "builder": builder},
        "roles": [],
        "entities": [],
        "attributes": [],
        "transitions": [],
        "actor_classes": list(actor_classes.values()),
        "object_classes": list(object_classes.values()),
        "access": access,
        "indeterminate": indeterminate,
        "not_constructed": not_constructed,
    }
