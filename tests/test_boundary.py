"""The crossing-edge classes."""

from __future__ import annotations

from synthetic import edge, node, topology

from topos.boundary import REFERENCE, STATE, Classifier

NODES = [
    node("table:t"),
    node("table:orphan"),
    node("view:v"),
    node("function:w()", volatility="volatile"),
    node("function:r()", volatility="stable"),
    node("function:pure()", volatility="immutable"),
    node("trigger:t.tr"),
    node("policy:t.p"),
    node("type:e"),
]
EDGES = [
    edge("writes", "function:w()", "table:t"),
    edge("reads", "function:r()", "table:t"),
    edge("reads", "function:r()", "table:orphan"),
    edge("reads", "function:r()", "view:v"),
    edge("calls", "function:r()", "function:pure()"),
    edge("calls", "function:r()", "function:w()"),
    edge("trigger_on", "trigger:t.tr", "table:t"),
    edge("executes", "trigger:t.tr", "function:w()"),
    edge("policy_on", "policy:t.p", "table:t"),
    edge("foreign_key", "table:orphan", "table:t"),
    edge("typed_by", "table:t", "type:e"),
]
CLASSIFIER = Classifier(topology(NODES, EDGES))


def _class(kind: str, source: str, target: str, inside: set[str]) -> str:
    return CLASSIFIER.classify(edge(kind, source, target), lambda n: n in inside)[0]


def test_a_read_carries_state_when_the_table_is_written_on_its_own_side() -> None:
    assert _class("reads", "function:r()", "table:t", {"table:t", "function:w()"}) == STATE


def test_a_read_is_reference_only_when_every_writer_is_on_the_readers_side() -> None:
    assert _class("reads", "function:r()", "table:t", {"table:t"}) == REFERENCE


def test_a_table_with_no_extracted_writer_is_assumed_written_elsewhere() -> None:
    assert _class("reads", "function:r()", "table:orphan", {"table:orphan"}) == STATE


def test_views_writes_triggers_and_policies_carry_state() -> None:
    assert _class("reads", "function:r()", "view:v", {"view:v"}) == STATE
    assert _class("writes", "function:w()", "table:t", {"table:t"}) == STATE
    assert _class("trigger_on", "trigger:t.tr", "table:t", {"table:t"}) == STATE
    assert _class("executes", "trigger:t.tr", "function:w()", {"function:w()"}) == STATE
    assert _class("policy_on", "policy:t.p", "table:t", {"table:t"}) == STATE


def test_a_call_carries_state_unless_the_callee_is_immutable() -> None:
    assert _class("calls", "function:r()", "function:w()", {"function:w()"}) == STATE
    assert _class("calls", "function:r()", "function:pure()", {"function:pure()"}) == REFERENCE


def test_keys_and_types_are_reference_only() -> None:
    assert _class("foreign_key", "table:orphan", "table:t", {"table:t"}) == REFERENCE
    assert _class("typed_by", "table:t", "type:e", {"type:e"}) == REFERENCE
