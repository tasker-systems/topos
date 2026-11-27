"""Extraction from a real PostgreSQL database holding a synthetic schema."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import jsonschema

from topos.canonical import sha256
from topos.extract.catalog import read_catalog
from topos.extract.topology import Exclusion, build_topology

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schema" / "structural-topology.schema.json").read_text())
EXAMPLE = ROOT / "examples" / "lending-library"


def _extract(dsn: str, **kwargs: object) -> dict:
    document = build_topology(read_catalog(dsn), target_name="synthetic", revision="r1", **kwargs)
    jsonschema.validate(document, SCHEMA)
    return document


def _without_source(document: dict) -> dict:
    copy = json.loads(json.dumps(document))
    copy["extractor"].pop("source")
    return copy


def test_the_example_topology_is_what_the_extractor_reads(database: Callable[[str], str]) -> None:
    dsn = database((EXAMPLE / "schema.sql").read_text())
    document = build_topology(
        read_catalog(dsn), target_name="lending-library", revision="synthetic-1"
    )
    committed = json.loads((EXAMPLE / "topology.json").read_text())
    assert _without_source(document) == _without_source(committed)


def test_extraction_is_reproducible(database: Callable[[str], str]) -> None:
    sql = (ROOT / "tests" / "fixtures" / "features.sql").read_text()
    first, second = _extract(database(sql)), _extract(database(sql))
    assert sha256(_without_source(first)) == sha256(_without_source(second))


def test_every_construct_is_read(database: Callable[[str], str]) -> None:
    document = _extract(database((ROOT / "tests" / "fixtures" / "features.sql").read_text()))
    nodes = {node["id"]: node for node in document["nodes"]}
    edges = {edge["id"]: edge for edge in document["edges"]}
    holes = {(hole["element"], hole["what"]) for hole in document["holes"]}

    accounts = nodes["table:app.accounts"]
    columns = {column["name"]: column for column in accounts["columns"]}
    assert columns["kind"]["values"] == ["person", "robot"]
    assert columns["kind"]["values_from"] == "check"
    assert columns["tier"]["values"] == ["free", "paid"]
    assert columns["status"]["values_from"] == "enum"
    assert accounts["primary_key"] == ["id"]
    assert nodes["type:app.positive"]["type_kind"] == "domain"
    assert "typed_by:table:app.accounts->type:app.status" in edges

    # Partitions are represented by their parent; a materialized view is its own kind.
    assert "table:app.events_2026" not in nodes
    assert nodes["materialized_view:app.totals"]["kind"] == "materialized_view"

    # Two keys between the same tables stay two edges.
    keys = [e for e in edges if e.startswith("foreign_key:table:app.notes->table:app.accounts#")]
    assert len(keys) == 2

    # Overloads are distinct, and a call resolves by its number of arguments.
    summary = "function:app.summary(uuid)"
    assert f"calls:{summary}->function:app.describe(uuid)" in edges
    assert f"calls:{summary}->function:app.describe(uuid,boolean)" not in edges
    assert nodes[summary]["security_definer"] is True

    # Extension objects are excluded and stated, and calls to them are not holes.
    assert not any(node.startswith("function:public.gen_random_bytes") for node in nodes)
    assert any(item["selector"] == "extension:pgcrypto" for item in document["excluded"])
    assert (summary, "unresolved-reference") not in holes

    # A CTE and a temp table are local names, not unresolved references.
    assert "reads:function:app.recent(uuid)->table:app.events" in edges
    assert ("function:app.recent(uuid)", "unresolved-reference") not in holes
    assert ("function:app.scratch()", "unresolved-reference") not in holes
    assert edges["writes:function:app.scratch()->table:app.notes"]["operations"] == ["delete"]

    # What cannot be resolved is a hole, never dropped.
    assert ("function:app.purge(text)", "dynamic-sql") in holes
    assert ("function:app.broken()", "unresolved-reference") in holes

    # A function's own search_path resolves its unqualified names.
    assert "reads:function:public.uses_path()->table:app.accounts" in edges
    assert "reads:function:app.count_accounts()->table:app.accounts" in edges

    assert edges["writes:function:app.upsert(uuid)->table:app.accounts"]["operations"] == ["merge"]
    settle = "procedure:app.settle()"
    assert edges[f"writes:{settle}->table:app.accounts"]["operations"] == ["update"]
    assert edges[f"writes:{settle}->table:app.events"]["operations"] == ["truncate"]

    trigger = nodes["trigger:app.notes.notes_stamp"]
    assert (trigger["timing"], trigger["events"], trigger["level"]) == (
        "before",
        ["insert", "delete"],
        "statement",
    )
    assert "executes:trigger:app.notes.notes_stamp->function:app.stamp()" in edges
    assert "writes:function:app.stamp()->table:app.events" in edges

    # Views, policies, defaults and checks are read from pg_depend.
    assert "reads:view:app.mine->table:app.accounts" in edges
    assert "calls:view:app.mine->function:app.whoami()" in edges
    assert "calls:table:app.accounts->function:app.whoami()" in edges
    assert edges["calls:table:app.accounts->function:app.whoami()"]["via"] == "default"
    assert edges["calls:table:app.notes->function:app.is_short(text)"]["via"] == "check"
    policy = "policy:app.notes.notes_owner"
    assert nodes[policy]["events"] == ["update"]
    assert f"policy_on:{policy}->table:app.notes" in edges
    assert f"reads:{policy}->table:app.accounts" in edges
    assert f"calls:{policy}->function:app.whoami()" in edges
    assert nodes["table:app.notes"]["row_security"] is True

    for element in [*document["nodes"], *document["edges"]]:
        assert element["provenance"]["kind"] == "extracted"


def test_an_exclusion_removes_its_nodes_and_states_why(database: Callable[[str], str]) -> None:
    dsn = database((ROOT / "tests" / "fixtures" / "features.sql").read_text())
    rule = Exclusion("function:app.describe(*", "environment", "a stand-in for this test")
    document = _extract(dsn, exclusions=[rule])
    assert not any(node["id"].startswith("function:app.describe(") for node in document["nodes"])
    assert not any("app.describe(" in edge["id"] for edge in document["edges"])
    assert {"selector": rule.selector, "basis": "environment", "reason": rule.reason} in document[
        "excluded"
    ]
