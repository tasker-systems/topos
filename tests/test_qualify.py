"""The qualifier, on a synthetic model and intent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from topos.qualify import NO_MATCH, qualify

ROOT = Path(__file__).resolve().parent.parent
PROBED = {"kind": "probed", "probe": "test", "call": "f()", "fixture": "x"}
DECLARED = {"kind": "extracted", "source": "abstraction test"}


def _fact(fid: str, actor: str, action: str, allowed: bool) -> dict[str, Any]:
    return {
        "id": fid,
        "actor_class": actor,
        "action": action,
        "object_class": "object.note",
        "allowed": allowed,
        "provenance": PROBED,
    }


MODEL: dict[str, Any] = {
    "schema_version": "0.2",
    "target": {"name": "synthetic", "revision": "r1"},
    "abstraction": {"owner": "tests", "statement": "roles against a note"},
    "roles": [],
    "entities": [],
    "attributes": [],
    "transitions": [],
    "actor_classes": [
        {"id": "actor.lead", "axes": {"role": "lead"}, "provenance": DECLARED},
        {"id": "actor.viewer", "axes": {"role": "viewer"}, "provenance": DECLARED},
    ],
    "object_classes": [{"id": "object.note", "provenance": DECLARED}],
    "access": [
        _fact("a1", "actor.lead", "edit", True),
        _fact("a2", "actor.viewer", "edit", True),
        _fact("a3", "actor.lead", "read", True),
        _fact("a4", "actor.lead", "delete", False),
        _fact("a5", "actor.viewer", "share", True),
    ],
    "indeterminate": [
        {
            "id": "i1",
            "actor_class": "actor.viewer",
            "action": "archive",
            "object_class": "object.note",
            "outcome": "error",
            "detail": "P0001: refused",
            "provenance": PROBED,
        }
    ],
    "not_constructed": [
        {
            "id": "n1",
            "actor_class": "actor.viewer",
            "object_class": "object.note",
            "reason": "23514: check",
            "provenance": PROBED,
        }
    ],
}


def _rule(rid: str, polarity: str, roles: list[str], actions: list[str]) -> dict[str, Any]:
    return {
        "id": rid,
        "statement": rid,
        "polarity": polarity,
        "applies_to": {"personas": ["someone"]},
        "concerns": {"access": {"actor_axes": {"role": roles}, "actions": actions}},
        "source": "docs/x.md:1",
    }


INTENT: dict[str, Any] = {
    "schema_version": "0.2",
    "target": {"name": "synthetic"},
    "owner": "tests",
    "personas": [{"id": "someone", "roles": []}],
    "rules": [
        _rule("leads-edit", "may", ["lead"], ["edit"]),
        _rule("viewers-never-edit", "never", ["viewer"], ["edit"]),
        _rule("leads-delete", "may", ["lead"], ["delete"]),
        _rule("viewers-share", "may", ["viewer"], ["share"]),
        _rule("nobody-shares", "never", ["viewer", "lead"], ["share"]),
        _rule("viewers-archive", "may", ["viewer"], ["archive"]),
        _rule("leads-publish", "may", ["lead"], ["publish"]),
    ],
}


def _validate(report: dict[str, Any]) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "schema" / "findings.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(report)


def _by_fact(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for finding in report["findings"]:
        key = finding["witness"].get("access_fact", finding["rule"])
        out.setdefault(key, []).append(finding)
    return out


def test_every_fact_lands_in_exactly_one_category() -> None:
    report = qualify(MODEL, INTENT, intent_ref="test-intent")
    _validate(report)
    found = _by_fact(report)
    assert [f["kind"] for f in found["a1"]] == ["intended"]
    assert [f["kind"] for f in found["a2"]] == ["reachable-not-intended"]
    assert found["a2"][0]["rule"] == "viewers-never-edit"
    assert [f["kind"] for f in found["a3"]] == ["unqualified"]
    assert found["a3"][0]["rule"] is None
    assert [f["kind"] for f in found["a4"]] == ["intended-not-reachable"]
    categories = {fid: {f["kind"] == "intended" for f in fs} for fid, fs in found.items()}
    assert all(len(kinds) == 1 for kinds in categories.values())
    assert {
        f["witness"]["access_fact"] for f in report["findings"] if "access_fact" in f["witness"]
    } == {
        "a1",
        "a2",
        "a3",
        "a4",
        "a5",
    }


def test_a_self_contradicting_intent_is_visible() -> None:
    found = _by_fact(qualify(MODEL, INTENT))["a5"]
    assert [(f["kind"], f["rule"]) for f in found] == [("reachable-not-intended", "nobody-shares")]
    assert found[0]["conflicting_rules"] == ["viewers-share", "nobody-shares"]


def test_a_rule_no_fact_matches_is_intended_not_reachable() -> None:
    found = _by_fact(qualify(MODEL, INTENT))
    publish = found["leads-publish"]
    assert publish[0]["kind"] == "intended-not-reachable"
    assert publish[0]["witness"] == {"path": [], "blocked_at": NO_MATCH}
    archive = found["viewers-archive"][0]
    assert "1 indeterminate entries match it" in archive["witness"]["blocked_at"]


def test_indeterminate_and_not_constructed_are_counted_never_qualified() -> None:
    report = qualify(MODEL, INTENT)
    assert report["indeterminate"] == [
        {"action": "archive", "outcome": "error", "count": 1, "details": {"P0001: refused": 1}}
    ]
    assert not any(f.get("action") == "archive" for f in report["findings"])
    assert "not constructed: (combination): 23514: check (1)" in report["examined"]["not_examined"]
    assert report["summary"]["by_kind"] == {
        "intended": 1,
        "intended-not-reachable": 3,
        "reachable-not-intended": 2,
        "unqualified": 1,
    }
    assert report["summary"]["by_action"]["edit"] == {"intended": 1, "reachable-not-intended": 1}


def test_with_no_intent_everything_is_unqualified() -> None:
    report = qualify(MODEL, None)
    _validate(report)
    assert report["intent"] is None
    assert {f["kind"] for f in report["findings"]} == {"unqualified"}
    assert len(report["findings"]) == len(MODEL["access"])


def test_a_named_area_must_be_supplied() -> None:
    model = {**MODEL, "area_of_concern": {"name": "a", "sha256": "0" * 64}}
    with pytest.raises(ValueError, match="area of concern"):
        qualify(model, INTENT)
