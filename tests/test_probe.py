"""The probe: argument derivation, predicate selection, the engine, and the PostgreSQL target.

Everything here runs against the synthetic schema in `fixtures/probe.sql`.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from probe_builder import DECLARATION, build

from topos.canonical import sha256
from topos.probe.abstraction import compose
from topos.probe.arguments import Schema, derive, read_signatures
from topos.probe.engine import Fixture, Outcome, Unconstructible, calls, combinations, observe

ROOT = Path(__file__).resolve().parent.parent
SQL = (ROOT / "tests" / "fixtures" / "probe.sql").read_text(encoding="utf-8")
FUNCTIONS = [
    "function:public.can_edit_note(uuid,uuid)",
    "function:public.user_shared(uuid,text,text,uuid)",
    "function:public.holds_role(uuid,member_role)",
    "function:public.strict_check(uuid)",
    "function:public.unknown_for(uuid)",
    "function:public.noted(uuid)",
    "function:public.payload_ok(jsonb)",
    "function:public.titled(uuid,text)",
    "function:public.is_level(member_role)",
    "function:public.note_count(uuid)",
]
AREA = {"name": "synthetic-area", "target": {"name": "synthetic"}, "members": FUNCTIONS}


def _validate(document: dict[str, Any], schema_name: str) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "schema" / schema_name).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(document)


@pytest.fixture
def synthetic(database: Any) -> str:
    return database(SQL)


@pytest.fixture
def abstraction(synthetic: str) -> dict[str, Any]:
    document = compose(
        DECLARATION, AREA, read_signatures(synthetic), area_ref="test", applied_by="test"
    )
    _validate(document, "abstraction.schema.json")
    return document


def test_arguments_come_from_keys_checks_enums_and_bodies(synthetic: str) -> None:
    schema = Schema(read_signatures(synthetic))
    run = derive(schema)
    edit = {p.name: p for p in run(schema.functions[FUNCTIONS[0]])}
    assert set(edit["p_note"].tables) == {"public.notes"}
    assert set(edit["p_user"].tables) == {"public.users"}
    shared = {p.name: p for p in run(schema.functions[FUNCTIONS[1]])}
    assert set(shared["p_action"].literals) == {"read", "edit"}
    assert set(shared["p_subject_table"].literals) == {"notes", "groups"}
    assert set(shared["p_subject_id"].tables) == {"public.notes", "public.groups"}
    assert shared["p_subject_id"].partners == {2}
    role = {p.name: p for p in run(schema.functions[FUNCTIONS[2]])}
    assert list(role["p_role"].literals) == ["lead", "member", "viewer"]
    titled = {p.name: p for p in run(schema.functions[FUNCTIONS[7]])}
    assert not titled["p_title"].literals


def test_selection_states_a_structural_reason_for_every_exclusion(
    abstraction: dict[str, Any],
) -> None:
    selected = {p["id"] for p in abstraction["predicates"]}
    assert selected == set(FUNCTIONS[:6])
    excluded = {e["element"]: e for e in abstraction["excluded"]}
    assert set(excluded) == set(FUNCTIONS[6:9])
    assert all(e["basis"] == "structure" for e in excluded.values())
    assert "jsonb" in excluded[FUNCTIONS[6]]["reason"]
    assert "yields no values" in excluded[FUNCTIONS[7]]["reason"]
    assert "cannot vary" in excluded[FUNCTIONS[8]]["reason"]
    noted = next(p for p in abstraction["predicates"] if p["id"] == FUNCTIONS[5])
    assert noted["volatility"] == "volatile"
    roles = next(a for a in abstraction["axes"] if a["id"] == "role")
    assert roles["values"] == ["lead", "member", "viewer", None]
    assert "supplied by hand: no membership" in roles["source"]


def test_polymorphic_ids_are_bound_with_their_partner(abstraction: dict[str, Any]) -> None:
    shared = next(p for p in abstraction["predicates"] if p["id"] == FUNCTIONS[1])
    written = sorted(call.written() for call in calls(shared))
    assert written == [
        "public.user_shared(<actor>, 'edit', 'groups', <group>)",
        "public.user_shared(<actor>, 'edit', 'notes', <note>)",
        "public.user_shared(<actor>, 'read', 'groups', <group>)",
        "public.user_shared(<actor>, 'read', 'notes', <note>)",
    ]


def test_a_probe_records_every_combination_and_every_answer(
    synthetic: str, abstraction: dict[str, Any]
) -> None:
    import psycopg

    from topos.probe.postgres import PostgresTarget

    with psycopg.connect(synthetic, autocommit=True) as connection:
        model = observe(
            abstraction, PostgresTarget(connection, build), revision="r1", builder="probe_builder"
        )
        again = observe(
            abstraction, PostgresTarget(connection, build), revision="r1", builder="probe_builder"
        )
        left = connection.execute("SELECT count(*) FROM audit").fetchone()[0]
    _validate(model, "system-model.schema.json")
    assert sha256(model) == sha256(again), "a probe is reproducible"
    assert left == 0, "a probe leaves its database as it found it"

    # 4 roles x 2 x 2 flags x 2 subjects; the check constraint refuses edit without read.
    built = [c for c in model["not_constructed"] if "action" not in c]
    assert len(built) == 4 * 1 * 2
    assert all("23514" in c["reason"] for c in built)
    constructed = 4 * 3 * 2
    per_combination = 1 + 4 + 3 + 1 + 1 + 1
    assert len(model["access"]) + len(model["indeterminate"]) == constructed * per_combination

    errors = [i for i in model["indeterminate"] if i["outcome"] == "error"]
    nulls = [i for i in model["indeterminate"] if i["outcome"] == "null"]
    assert len(errors) == constructed and all("P0001" in e["detail"] for e in errors)
    assert len(nulls) == constructed
    assert not any(f["action"] in ("strict_check", "unknown_for") for f in model["access"])

    noted = [f for f in model["access"] if f["action"] == "noted"]
    assert len(noted) == constructed and all(f["wrote"] and f["allowed"] for f in noted)
    assert all("wrote" not in f for f in model["access"] if f["action"] != "noted")

    def allowed(action: str, **axes: Any) -> set[bool]:
        classes = {c["id"]: c["axes"] for c in model["actor_classes"]}
        return {
            f["allowed"]
            for f in model["access"]
            if f["action"] == action
            and all(classes[f["actor_class"]][k] == v for k, v in axes.items())
        }

    assert allowed("can_edit_note", role="viewer") == {False}
    assert allowed("can_edit_note", role="member") == {True}
    assert allowed("can_edit_note", role=None) == {False}


class FakeTarget:
    """A target with no database: refuses one combination, and supplies no `note` for another."""

    def __init__(self) -> None:
        self.built: list[dict[str, Any]] = []

    @contextmanager
    def combination(self, combination: dict[str, Any]) -> Any:
        self.built.append(combination)
        if combination["role"] == "viewer" and combination["subject"] == "groups":
            yield Unconstructible("refused")
        elif combination["role"] is None:
            yield Fixture({"actor": "a", "group": "g"})
        else:
            yield Fixture({"actor": "a", "group": "g", "note": "n"})

    def call(self, call: Any, fixture: Fixture) -> Outcome:
        if call.predicate["function"]["name"] == "strict_check":
            return Outcome("error", "boom")
        return Outcome("true" if fixture.handles["actor"] == "a" else "false")


def test_the_engine_enumerates_the_full_product_and_drops_nothing() -> None:
    abstraction = {
        "name": "fake",
        "owner": "tests",
        "statement": "fake",
        "target": {"name": "fake"},
        "area_of_concern": {"name": "fake", "sha256": "0" * 64},
        "axes": [
            {"id": "role", "side": "actor", "values": ["lead", "viewer", None], "source": "x"},
            {"id": "subject", "side": "object", "values": ["notes", "groups"], "source": "x"},
        ],
        "handles": [],
        "actor_handle": "actor",
        "excluded": [],
        "predicates": [
            {
                "id": "f1",
                "function": {"schema": "public", "name": "can_edit_note"},
                "volatility": "stable",
                "parameters": [
                    {"name": "p_user", "type": "uuid", "bind": {"handles": ["actor"]}, "basis": []},
                    {"name": "p_note", "type": "uuid", "bind": {"handles": ["note"]}, "basis": []},
                ],
            },
            {
                "id": "f2",
                "function": {"schema": "public", "name": "strict_check"},
                "volatility": "stable",
                "parameters": [
                    {"name": "p_user", "type": "uuid", "bind": {"handles": ["actor"]}, "basis": []}
                ],
            },
        ],
    }
    target = FakeTarget()
    model = observe(abstraction, target, revision="r", builder="fake")
    assert target.built == list(combinations(abstraction["axes"]))
    assert len(target.built) == 6
    refused = [c for c in model["not_constructed"] if "action" not in c]
    unbound = [c for c in model["not_constructed"] if "action" in c]
    assert len(refused) == 1 and refused[0]["reason"] == "refused"
    assert len(unbound) == 2 and all("supplies no note" in c["reason"] for c in unbound)
    assert len(model["access"]) == 3
    assert len(model["indeterminate"]) == 5
    assert {i["outcome"] for i in model["indeterminate"]} == {"error"}
